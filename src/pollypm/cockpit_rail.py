"""Rail rendering + routing for the cockpit left pane (#404).

This module owns every piece of the cockpit *rail* contract:

* :class:`CockpitItem` — the dataclass every row is built from.
* :class:`CockpitRouter` — the orchestrator that loads state, persists
  the selected entry, manages the tmux layout, and routes a selection
  to the right pane's live session or static view.
* The pure helpers that walk the plugin-host rail registry
  (``_selected_project_key``, ``_hidden_rail_items``,
  ``_collapsed_rail_sections``, ``_visibility_passes``,
  ``_rows_for_registration``).
* :class:`PollyCockpitRail` — the text-only renderer used by ``pm rail``
  when the user wants the pre-Textual surface.

Splitting the rail out of ``cockpit.py`` keeps that module focused on
the dashboard + detail-pane renderers; importers that still reach into
``pollypm.cockpit`` for the router / item classes land in the
back-compat shim defined there.
"""

from __future__ import annotations

import json
import os
import select
import shlex
import shutil
import sys
import termios
import time
import tty
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from pollypm.atomic_io import atomic_write_json
from pollypm.cockpit_rail_routes import (
    LiveSessionRoute,
    ProjectRoute,
    StaticViewRoute,
)
from pollypm.cockpit_content import (
    CockpitContentContext,
    ErrorPane,
    FallbackPane,
    LiveAgentPane,
    TextualCommandPane,
    resolve_cockpit_content,
)
from pollypm.cockpit_window_manager import (
    CockpitWindowManager,
    CockpitWindowSpec,
    CockpitWindowState,
)
from pollypm.cockpit_project_state import (
    ProjectRailState,
    ProjectStateRollup,
    actionable_alert_task_ids,
    rollup_project_state,
)
from pollypm.config import load_config
from pollypm.heartbeats.snapshots import read_recent_heartbeat_snapshot
from pollypm.providers import get_provider
from pollypm.runtimes import get_runtime
# Lazy: ``PollyPMService`` pulls supervisor → sqlalchemy on import. The
# rail module is loaded by every cockpit pane (project dashboard, inbox,
# tasks); deferring this import shaves the supervisor cost off any pane
# that doesn't instantiate a ``CockpitRouter``.
from pollypm.session_services import create_tmux_client


# ── Color palette ────────────────────────────────────────────────────────────

_C = tuple[int, int, int]

PALETTE: dict[str, _C] = {
    "bg":              (15, 19, 23),
    "wordmark_hi":     (91, 138, 255),
    "wordmark_lo":     (55, 95, 195),
    "slogan":          (92, 106, 119),
    "slogan_fade":     (42, 52, 64),
    "section_rule":    (30, 39, 48),
    "section_label":   (74, 85, 104),
    "item_normal":     (184, 196, 207),
    "item_muted":      (92, 106, 119),
    "sel_bg":          (26, 58, 92),
    "sel_text":        (238, 246, 255),
    "sel_accent":      (91, 138, 255),
    "active_bg":       (22, 42, 64),
    "live_bg":         (19, 42, 30),
    "live_text":       (126, 232, 164),
    "live_indicator":  (61, 220, 132),
    "alert_bg":        (53, 26, 29),
    "alert_text":      (242, 184, 188),
    "alert_indicator": (255, 95, 109),
    # #989 — Warn-tier badges. Today the supervisor raises both ``warn``
    # alerts (one click to recover, e.g. ``pane:permission_prompt``) and
    # ``error`` alerts (account repair / restart needed) but the rail
    # painted them the same red. Amber for warn keeps the user's eye on
    # the genuinely-broken rows without burying the soft alerts.
    "warn_bg":         (50, 40, 24),
    "warn_text":       (240, 207, 158),
    "warn_indicator":  (240, 192, 90),
    "inbox_has":       (240, 196, 90),
    "inbox_empty":     (74, 85, 104),
    "idle":            (74, 85, 104),
    "dead":            (255, 95, 109),
    "hint":            (52, 64, 77),
}

ARC_SPINNER = ("◜", "◝", "◞", "◟")

# Glyphs the activity-sparkline helper emits — the eight block heights
# from ``_spark_bar`` plus the U+00B7 dot we substitute for in-line
# zero buckets. Used by rail renderers to detect and remove a trailing
# sparkline from compact project rows.
_SPARK_GLYPHS = frozenset("·▁▂▃▄▅▆▇█ ")
_RAIL_TICKER_TEXT_WIDTH = 28


def _strip_trailing_spark(label: str) -> tuple[str, str]:
    """Return ``(head, spark)`` if ``label`` ends with a 10-char activity
    sparkline preceded by a space; otherwise ``(label, "")``.

    Project rows may carry a fixed-width activity spark
    (``_project_activity_sparkline``) at the tail of the label. Compact
    rail renderers use this helper to render the project name without
    the sparkline in the narrow navigation rail.
    """
    if len(label) < 11:
        return label, ""
    head, sep, tail = label.rpartition(" ")
    if not sep or len(tail) != 10:
        return label, ""
    if not all(ch in _SPARK_GLYPHS for ch in tail):
        return label, ""
    return head, tail


def _format_event_ticker(
    labels: list[str],
    *,
    width: int = _RAIL_TICKER_TEXT_WIDTH,
) -> str:
    """Format event labels without leaving a clipped trailing separator."""
    if not labels:
        return ""
    text = "events"
    for label in labels:
        candidate = f"{text} · {label}"
        if len(candidate) <= width:
            text = candidate
            continue
        if text != "events":
            break
        budget = width - len("events · ")
        if budget <= 1:
            return "events"
        text = f"events · {label[: budget - 1]}…"
        break
    return "" if text == "events" else text

ASCII_POLLY = (
    "█▀█ █▀█ █   █   █▄█",
    "█▀▀ █▄█ █▄▄ █▄▄  █ ",
)

POLLY_SLOGANS = [
    ("Plans first.", "Chaos later."),
    ("Inbox clear.", "Projects moving."),
    ("Small steps.", "Sharp turns."),
    ("Less thrash.", "More shipped."),
    ("Watch the drift.", "Trim the waste."),
    ("Keep it modular.", "Keep it moving."),
    ("Fewer heroics.", "More progress."),
    ("Big picture.", "Tight loops."),
    ("Plan clean.", "Land faster."),
    ("Break it down.", "Ship it right."),
    ("Stay useful.", "Stay honest."),
    ("No mystery.", "Just momentum."),
    ("Steady lanes.", "Clean handoffs."),
    ("Less panic.", "More process."),
    ("Trim the scope.", "Raise the bar."),
    ("One project.", "Many good turns."),
]

GUTTER = 2


@dataclass(slots=True)
class CockpitPresence:
    """Presence-aware glyph helper for the rail renderer."""

    tmux: object
    _cache_ttl_seconds: float = 2.0
    _cached_attached: bool | None = None
    _cached_at: float = 0.0
    _cached_session: str | None = None
    _heartbeat_states: dict[str, tuple[str | None, int]] = field(default_factory=dict)

    def _calm_mode(self) -> bool:
        value = os.environ.get("POLLY_CALM", "")
        return value not in {"", "0", "false", "False", "no", "NO"}

    def is_tmux_attached(self) -> bool:
        """Return True when animation should behave as if tmux is attached."""
        if self._calm_mode():
            return False
        session_name = self._current_session_name()
        if session_name is None:
            return True
        now = time.monotonic()
        if (
            self._cached_session == session_name
            and self._cached_attached is not None
            and now - self._cached_at < self._cache_ttl_seconds
        ):
            return self._cached_attached
        attached = self._probe_attached(session_name)
        self._cached_session = session_name
        self._cached_attached = attached
        self._cached_at = now
        return attached

    def _current_session_name(self) -> str | None:
        getter = getattr(self.tmux, "current_session_name", None)
        if not callable(getter):
            return None
        try:
            value = getter()
            return str(value) if value else None
        except Exception:  # noqa: BLE001
            return None

    def _probe_attached(self, session_name: str) -> bool:
        list_clients = getattr(self.tmux, "list_clients", None)
        if callable(list_clients):
            try:
                result = list_clients(session_name)
            except Exception:  # noqa: BLE001
                return True
            if isinstance(result, str):
                return bool(result.strip())
            try:
                return bool(list(result))
            except TypeError:
                return True
        run = getattr(self.tmux, "run", None)
        if not callable(run):
            return True
        try:
            result = run(
                "list-clients",
                "-t",
                session_name,
                "-F",
                "#{client_tty}",
                check=False,
            )
        except Exception:  # noqa: BLE001
            return True
        stdout = getattr(result, "stdout", "") or ""
        return bool(str(stdout).strip())

    def should_animate(self) -> bool:
        return self.is_tmux_attached()

    def working_frame(self, spinner_index: int) -> str:
        if not self.should_animate():
            return ARC_SPINNER[0]
        return ARC_SPINNER[spinner_index % len(ARC_SPINNER)]

    def heartbeat_frame(self, spinner_index: int) -> str:
        frames = ("♥", "♡")
        if not self.should_animate():
            return frames[0]
        return frames[spinner_index % len(frames)]

    def heartbeat_frame_for(
        self,
        session_name: str,
        heartbeat_at: str | None,
    ) -> str:
        frames = ("♥", "♡")
        if not self.should_animate():
            return frames[0]
        previous_at, frame_index = self._heartbeat_states.get(session_name, (None, 0))
        if heartbeat_at and heartbeat_at != previous_at:
            frame_index = (frame_index + 1) % len(frames)
        self._heartbeat_states[session_name] = (heartbeat_at, frame_index)
        return frames[frame_index]


# ── Rail data model + router ─────────────────────────────────────────────
#
# Moved out of pollypm.cockpit in #404 so the routing concern (which rail
# row is selected, where to mount the right pane, how to persist state)
# lives next to the renderer that consumes its output. The item rendering
# helpers below (PollyCockpitRail) used to import CockpitItem/CockpitRouter
# from pollypm.cockpit — now they sit side by side.


@dataclass(slots=True)
class CockpitItem:
    key: str
    label: str
    state: str
    selectable: bool = True
    session_name: str | None = None
    work_state: str | None = None
    heartbeat_at: str | None = None
    # #989 — Severity attached out-of-band so existing ``state``-based
    # rendering keeps working unchanged. ``warn`` and ``error`` drive
    # the rail's amber-vs-red badge differentiation; ``None`` means the
    # row carries no actionable alert (or alerts are all operational).
    alert_severity: str | None = None
    # ``alert_message`` carries the most recent actionable alert's full
    # message text so the rail can surface it in the alert-detail
    # modal without re-querying the supervisor on Enter.
    alert_message: str | None = None
    # ``alert_type`` is the canonical alert family (``recovery_limit``,
    # ``pane:permission_prompt``, …) — drives the recovery action map.
    alert_type: str | None = None
    # ``alert_id`` is the supervisor's row id, used by recovery
    # handlers that need to clear/lookup the specific alert.
    alert_id: int | None = None


def _stuck_alert_already_user_waiting(
    alert_type: str, user_waiting_task_ids: frozenset[str] | set[str],
) -> bool:
    """Return True for ``stuck_on_task:<id>`` alerts whose underlying
    task is already in a user-waiting state.

    The session sat idle because the user hasn't responded — the
    rail's ⚠ glyph then tells the user "this is broken" when the
    system is doing exactly what it should: waiting on them. The
    project rollup already encodes this state via the project status
    marker (cockpit_project_state, cycle 53); the per-session glyph
    that duplicates it is just noise.
    """
    prefix = "stuck_on_task:"
    if not alert_type or not alert_type.startswith(prefix):
        return False
    task_id = alert_type[len(prefix):].strip()
    return bool(task_id) and task_id in user_waiting_task_ids


def _selected_project_key(selected: object) -> str | None:
    """Extract the project key from the ``selected`` cockpit state."""
    if not isinstance(selected, str) or not selected.startswith("project:"):
        return None
    parts = selected.split(":", 2)
    if len(parts) < 2:
        return None
    return parts[1] or None


def _task_mount_parts(session_name: object) -> tuple[str, str] | None:
    """Parse ``task-<project>-<number>`` mounted session names."""
    if not isinstance(session_name, str) or not session_name.startswith("task-"):
        return None
    suffix = session_name[len("task-") :]
    project_key, sep, task_num = suffix.rpartition("-")
    if not sep or not project_key or not task_num:
        return None
    return project_key, task_num


def _hidden_rail_items(config: object) -> frozenset[str]:
    """Return user-configured hidden rail item keys (``section.label``)."""
    rail_cfg = getattr(config, "rail", None)
    hidden = getattr(rail_cfg, "hidden_items", None) if rail_cfg is not None else None
    if not hidden:
        return frozenset()
    return frozenset(str(item) for item in hidden)


def _collapsed_rail_sections(config: object) -> frozenset[str]:
    """Return user-configured collapsed section names."""
    rail_cfg = getattr(config, "rail", None)
    collapsed = (
        getattr(rail_cfg, "collapsed_sections", None) if rail_cfg is not None else None
    )
    if not collapsed:
        return frozenset()
    return frozenset(str(item) for item in collapsed)


def _visibility_passes(reg, ctx) -> bool:
    """Evaluate the registration's visibility predicate.

    * ``"always"`` — always visible.
    * ``"has_feature"`` — visible only if ``ctx.extras["features"]``
      (a set/frozenset of capability names) includes
      ``reg.feature_name`` (or ``reg.item_key`` as fallback).
    * ``Callable`` — invoked; exceptions treat as hidden-and-logged.
    """
    import logging

    visibility = reg.visibility
    if visibility == "always":
        return True
    if visibility == "has_feature":
        features = ctx.extras.get("features") or frozenset()
        if not isinstance(features, (set, frozenset)):
            try:
                features = frozenset(features)
            except TypeError:
                return False
        target = reg.feature_name or reg.item_key
        return target in features
    if callable(visibility):
        try:
            return bool(visibility(ctx))
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception(
                "Rail item %s visibility predicate raised — hiding item",
                reg.item_key,
            )
            return False
    return True


def _build_project_pm_primer(supervisor, project_key: str) -> str | None:
    """Return a project-context priming message for a per-project PM session.

    The cockpit attaches a project's PM Chat to the project's worker
    session (``project:<key>:session``). The launch-time profile prompt
    primes the agent with project context, but a long-running
    conversation can drift the agent off-identity (#958 — booktalk's PM
    answered "I'm Codex" instead of "I'm booktalk's PM"). This helper
    renders a short re-anchoring brief the cockpit injects on attach so
    the agent recovers project identity without the user having to
    manually re-prime.

    The primer covers the four signals the issue calls out: project
    name + root path, current task summary, plan status, active issue
    count. Failures in any individual lookup are swallowed so a missing
    state.db never blocks the mount.
    """
    project = supervisor.config.projects.get(project_key)
    if project is None:
        return None
    persona_raw = getattr(project, "persona_name", None)
    persona = (
        persona_raw.strip()
        if isinstance(persona_raw, str) and persona_raw.strip()
        else None
    )
    project_name = project.name or project_key
    project_path = str(project.path)

    queued = in_progress = review = done_recent = 0
    plan_status = "unknown"
    inbox_titles: list[str] = []
    inbox_total = 0
    try:
        from pollypm.work.inbox_view import inbox_tasks
        from pollypm.work.models import WorkStatus
        from pollypm.work.sqlite_service import SQLiteWorkService

        db_path = project.path / ".pollypm" / "state.db"
        if db_path.exists():
            with SQLiteWorkService(
                db_path=db_path, project_path=project.path,
            ) as svc:
                tasks = list(svc.list_tasks(project=project_key))
                for task in tasks:
                    status = task.work_status
                    if status == WorkStatus.QUEUED:
                        queued += 1
                    elif status == WorkStatus.IN_PROGRESS:
                        in_progress += 1
                    elif status == WorkStatus.REVIEW:
                        review += 1
                    elif status == WorkStatus.DONE:
                        done_recent += 1
                inbox_items = inbox_tasks(svc, project=project_key)
                inbox_total = len(inbox_items)
                for task in inbox_items[:3]:
                    title = (task.title or "").strip()
                    if title:
                        inbox_titles.append(f"{task.task_id}: {title}")
                # Plan status — look for a plan_review or plan_approved
                # marker in the task list. A project with at least one
                # done plan task counts as approved; otherwise the plan
                # is missing.
                has_plan_done = any(
                    "plan" in (t.labels or [])
                    and t.work_status == WorkStatus.DONE
                    for t in tasks
                )
                has_plan_open = any(
                    "plan_review" in (t.labels or [])
                    or "plan" in (t.labels or [])
                    for t in tasks
                )
                if has_plan_done:
                    plan_status = "approved"
                elif has_plan_open:
                    plan_status = "in review"
                else:
                    plan_status = "missing"
    except Exception:  # noqa: BLE001 — primer is best-effort
        pass

    # #1007: previous wording ("You are <persona>, the PM … Re-anchor
    # on this identity for the rest of this session." +
    # "Acknowledge briefly and stand by for the next instruction.")
    # parsed as a fake-system-authority directive to Claude's
    # injection defense — the model rejected it as an injection
    # attempt and refused to engage. New wording is conversational:
    # the user is mounting the PM chat, so frame it as a "hey, here's
    # the snapshot you'd want for this project" briefing rather than a
    # role-imposition.
    if persona is not None:
        identity_line = (
            f"Hey {persona} — the user just opened the PM chat for "
            f"project '{project_name}' (key: {project_key}). Quick "
            f"snapshot below so you have the context they're seeing."
        )
    else:
        identity_line = (
            f"Hey — the user just opened the PM chat for project "
            f"'{project_name}' (key: {project_key}). Quick snapshot "
            f"below so you have the context they're seeing."
        )

    lines = [
        identity_line,
        f"Project root: {project_path}",
        (
            f"Tasks: {queued} queued, {in_progress} in progress, "
            f"{review} in review, {done_recent} done"
        ),
        f"Plan: {plan_status}",
        f"Active inbox: {inbox_total} item(s)",
    ]
    if inbox_titles:
        lines.append("Recent inbox:")
        lines.extend(f"  - {title}" for title in inbox_titles)
    lines.append(
        "Glance at it and pick up wherever the user takes the conversation."
    )
    return "\n".join(lines)


def _build_operator_primer(supervisor) -> str | None:
    """Return a workspace-level primer for the Polly operator session (#961).

    The operator session ships with ``polly_prompt()`` baked into its
    launch-time profile, but that prompt is only injected on a *fresh*
    launch (see :meth:`Supervisor._send_initial_input_if_fresh`). When
    the cockpit attaches to a long-running operator pane — or when the
    pane was resumed without the marker — the agent has no identity
    context and answers as a generic Claude/Codex assistant. This
    helper renders a short re-anchoring brief the cockpit injects when
    the user mounts ``Polly · chat`` so Polly recovers her PollyPM
    operator identity without the user retyping it.

    Distinct from :func:`_build_project_pm_primer` (#958): that primer
    re-anchors a per-project PM on a single project. This one anchors
    the workspace-level operator on the whole workspace — it
    enumerates known projects, totals the inbox, and surfaces a short
    list of recent inbox subjects so Polly can answer "how's the
    workspace?" the moment she's mounted.
    """
    project_lines: list[str] = []
    inbox_total = 0
    inbox_titles: list[tuple[str, str]] = []
    project_count = 0
    try:
        from pollypm.work.inbox_view import inbox_tasks
        from pollypm.work.sqlite_service import SQLiteWorkService
    except Exception:  # noqa: BLE001
        inbox_tasks = None  # type: ignore[assignment]
        SQLiteWorkService = None  # type: ignore[assignment]

    projects = getattr(supervisor.config, "projects", {}) or {}
    for project_key, project in projects.items():
        project_count += 1
        project_name = getattr(project, "name", None) or project_key
        project_lines.append(f"  - {project_name} ({project_key})")
        if inbox_tasks is None or SQLiteWorkService is None:
            continue
        db_path = project.path / ".pollypm" / "state.db"
        if not db_path.exists():
            continue
        try:
            with SQLiteWorkService(
                db_path=db_path, project_path=project.path,
            ) as svc:
                items = list(inbox_tasks(svc, project=project_key))
                inbox_total += len(items)
                for task in items[:2]:
                    title = (task.title or "").strip()
                    if title:
                        inbox_titles.append((project_key, f"{task.task_id}: {title}"))
        except Exception:  # noqa: BLE001 — primer is best-effort
            continue

    # #1007: previous wording ("You are Polly, the PollyPM operator.
    # Re-anchor on this identity for the rest of this session: …" +
    # "acknowledge briefly and stand by for the next instruction.")
    # tripped Claude's prompt-injection defense — the model treated
    # the primer as a fake-authority directive and refused to engage.
    # New wording is conversational: the user just mounted Polly chat,
    # so frame the primer as a workspace briefing she'd want before
    # the user starts talking, rather than a role-imposition.
    lines = [
        "Hey Polly — the user just opened the operator chat. Quick "
        "workspace briefing so you have the context they're seeing "
        "before they say anything.",
        f"Workspace: {project_count} project(s) under management.",
    ]
    if project_lines:
        lines.append("Projects:")
        lines.extend(project_lines[:10])
    lines.append(f"Active inbox (workspace-wide): {inbox_total} item(s)")
    if inbox_titles:
        lines.append("Recent inbox:")
        for project_key, title in inbox_titles[:5]:
            lines.append(f"  - [{project_key}] {title}")
    lines.append(
        "Glance at the inbox/status (`pm inbox`, `pm status`) and "
        "pick up wherever the user takes the conversation."
    )
    return "\n".join(lines)


def _rows_for_registration(reg, ctx) -> list:
    """Produce the list of :class:`RailRow` a registration renders to.

    Default: one row using the (possibly dynamic) label, icon, and
    state. When ``rows_provider`` is set we defer to the plugin —
    handy for sections like ``projects`` where one registration fans
    out into N rows.
    """
    import logging
    from pollypm.plugin_api.v1 import RailRow

    logger = logging.getLogger(__name__)

    if reg.rows_provider is not None:
        try:
            rows = reg.rows_provider(ctx)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Rail item %s rows_provider raised — skipping", reg.item_key,
            )
            return []
        return [r for r in rows if isinstance(r, RailRow)]

    label = reg.label
    if reg.label_provider is not None:
        try:
            dynamic_label = reg.label_provider(ctx)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Rail item %s label_provider raised — falling back to static",
                reg.item_key,
            )
            dynamic_label = None
        if isinstance(dynamic_label, str) and dynamic_label:
            label = dynamic_label

    state = "idle"
    if reg.state_provider is not None:
        try:
            dynamic_state = reg.state_provider(ctx)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Rail item %s state_provider raised — falling back to idle",
                reg.item_key,
            )
            dynamic_state = None
        if isinstance(dynamic_state, str) and dynamic_state:
            state = dynamic_state

    # Badge appended to label if provider returns a non-null value. The
    # badge-rendering tick is cheap; provider exceptions fall back to no
    # badge per er03 acceptance.
    if reg.badge_provider is not None:
        try:
            badge = reg.badge_provider(ctx)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Rail item %s badge_provider raised — rendering without badge",
                reg.item_key,
            )
            badge = None
        if badge not in (None, 0, ""):
            # Only append a badge when the label_provider hasn't already
            # baked the count in (e.g. "Inbox (3)" from core_rail_items).
            if f"({badge})" not in label:
                label = f"{label} ({badge})"

    key = reg.selection_key
    return [RailRow(key=key, label=label, state=state, selectable=True)]


class CockpitRouter:
    _STATE_FILE = "cockpit_state.json"
    _COCKPIT_WINDOW = "PollyPM"
    _LEFT_PANE_WIDTH = 30  # default; actual value persisted in cockpit state.
    _STATE_WRITE_DEBOUNCE_SECONDS = 0.25

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        from pollypm.service_api import PollyPMService
        self.service = PollyPMService(config_path)
        self.tmux = create_tmux_client()
        self.presence = CockpitPresence(self.tmux)
        self._supervisor = None
        self._config_cache: object | None = None
        self._config_cache_mtime_ns: int | None = None
        self._state_cache: dict[str, object] | None = None
        self._state_cache_mtime_ns: int | None = None
        self._state_cache_path: Path | None = None
        self._state_dirty_since: float | None = None
        self._hidden_items_cache_key: int | None = None
        self._hidden_items_cache: frozenset[str] | None = None
        self._collapsed_sections_cache_key: int | None = None
        self._collapsed_sections_cache: frozenset[str] | None = None
        self._grouped_rail_cache_key: int | None = None
        self._grouped_rail_cache: dict[str, tuple[object, ...]] | None = None
        # Per-project activity cache keyed by project key.
        # value: (db_mtime, git_mtime, is_active, has_working_task)
        # Skips re-opening SQLite on every 0.8s cockpit tick when nothing changed.
        self._project_activity_cache: dict[str, tuple[float, float, bool, bool]] = {}

    def _presence(self) -> CockpitPresence:
        presence = getattr(self, "presence", None)
        if not isinstance(presence, CockpitPresence):
            presence = CockpitPresence(self.tmux)
            self.presence = presence
        elif presence.tmux is not self.tmux:
            presence.tmux = self.tmux
        return presence

    def _load_config(self):
        try:
            mtime_ns = self.config_path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if self._config_cache is not None and self._config_cache_mtime_ns == mtime_ns:
            return self._config_cache
        self._clear_rail_caches()
        config = load_config(self.config_path)
        self._config_cache = config
        self._config_cache_mtime_ns = mtime_ns
        return config

    def _clear_rail_caches(self) -> None:
        self._hidden_items_cache_key = None
        self._hidden_items_cache = None
        self._collapsed_sections_cache_key = None
        self._collapsed_sections_cache = None
        self._grouped_rail_cache_key = None
        self._grouped_rail_cache = None

    def _config_identity(self, config: object) -> int:
        return id(config)

    def _hidden_rail_items_cached(self, config: object) -> frozenset[str]:
        cache_key = self._config_identity(config)
        if self._hidden_items_cache_key == cache_key and self._hidden_items_cache is not None:
            return self._hidden_items_cache
        hidden = _hidden_rail_items(config)
        self._hidden_items_cache_key = cache_key
        self._hidden_items_cache = hidden
        return hidden

    def _collapsed_rail_sections_cached(self, config: object) -> frozenset[str]:
        cache_key = self._config_identity(config)
        if (
            self._collapsed_sections_cache_key == cache_key
            and self._collapsed_sections_cache is not None
        ):
            return self._collapsed_sections_cache
        collapsed = _collapsed_rail_sections(config)
        self._collapsed_sections_cache_key = cache_key
        self._collapsed_sections_cache = collapsed
        return collapsed

    def _grouped_rail_registrations(
        self,
        config: object,
        registry,
    ) -> dict[str, tuple[object, ...]]:
        cache_key = self._config_identity(config)
        if self._grouped_rail_cache_key == cache_key and self._grouped_rail_cache is not None:
            return self._grouped_rail_cache
        from pollypm.plugin_api.v1 import RAIL_SECTIONS

        hidden_keys = self._hidden_rail_items_cached(config)
        grouped: dict[str, list[object]] = {name: [] for name in RAIL_SECTIONS}
        for reg in registry.items():
            if reg.item_key in hidden_keys:
                continue
            grouped.setdefault(reg.section, []).append(reg)
        cache_value = {section: tuple(rows) for section, rows in grouped.items()}
        self._grouped_rail_cache_key = cache_key
        self._grouped_rail_cache = cache_value
        return cache_value

    def _load_supervisor(self, *, fresh: bool = False):
        # Reload config if the file changed (picks up new projects, sessions, etc.)
        if not fresh and self._supervisor is not None:
            try:
                config_mtime = self.config_path.stat().st_mtime
                if not hasattr(self, "_config_mtime") or config_mtime != self._config_mtime:
                    fresh = True
                    self._config_mtime = config_mtime
            except OSError:
                pass
        if fresh or self._supervisor is None:
            if self._supervisor is not None:
                self._supervisor.store.close()
            self._clear_rail_caches()
            self._config_cache = None
            self._config_cache_mtime_ns = None
            self._supervisor = self.service.load_supervisor()
            self._supervisor.ensure_layout()
            try:
                self._config_mtime = self.config_path.stat().st_mtime
            except OSError:
                pass
            # Bump epoch so the cockpit TUI refreshes on next tick
            try:
                from pollypm.state_epoch import bump
                bump()
            except Exception:  # noqa: BLE001
                pass
        return self._supervisor

    def _state_path(self) -> Path:
        config = self._load_config()
        config.project.base_dir.mkdir(parents=True, exist_ok=True)
        return config.project.base_dir / self._STATE_FILE

    def selected_key(self) -> str:
        self._validate_state()
        data = self._load_state()
        value = data.get("selected")
        return str(value) if isinstance(value, str) and value else "dashboard"

    def set_selected_key(self, key: str) -> None:
        data = self._load_state()
        if data.get("selected") == key:
            return
        self._validate_state()
        data = self._load_state()
        if data.get("selected") == key:
            return
        data["selected"] = key
        self._write_state(data)
        try:
            from pollypm.state_epoch import bump
            bump()
        except Exception:  # noqa: BLE001
            pass

    def _return_key_for_live_mount(self, previous_key: object) -> str | None:
        """Return the static cockpit route to restore after a live mount."""
        if not isinstance(previous_key, str) or not previous_key:
            return None
        if previous_key in {
            "dashboard",
            "inbox",
            "workers",
            "metrics",
            "settings",
            "activity",
        }:
            return previous_key
        if previous_key.startswith(("inbox:", "activity:")):
            return previous_key
        if previous_key.startswith("project:"):
            parts = previous_key.split(":")
            if len(parts) == 2:
                return f"{previous_key}:dashboard"
            if len(parts) >= 3 and parts[2] in {"dashboard", "settings", "issues"}:
                return previous_key
        return None

    def _record_live_mount_return_key(
        self,
        plan,
        previous_key: object,
    ) -> None:
        state = self._load_state()
        if isinstance(plan, LiveAgentPane):
            return_key = self._return_key_for_live_mount(previous_key)
            if return_key:
                state["mounted_return_key"] = return_key
            else:
                state.pop("mounted_return_key", None)
        else:
            state.pop("mounted_return_key", None)
        self._write_state(state)

    def should_show_palette_tip(self) -> bool:
        data = self._load_state()
        return not bool(data.get("palette_tip_seen"))

    def mark_palette_tip_seen(self) -> None:
        data = self._load_state()
        if data.get("palette_tip_seen") is True:
            return
        data["palette_tip_seen"] = True
        self._write_state(data)

    def _load_state(self) -> dict[str, object]:
        path = self._state_path()
        if self._state_cache is not None and self._state_cache_path == path:
            if self._state_dirty_since is not None:
                self._maybe_flush_state()
                return dict(self._state_cache)
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                self._state_cache = {}
                self._state_cache_mtime_ns = None
                self._state_dirty_since = None
                return {}
            if self._state_cache_mtime_ns == mtime_ns:
                return dict(self._state_cache)
        if not path.exists():
            self._state_cache = {}
            self._state_cache_path = path
            self._state_cache_mtime_ns = None
            self._state_dirty_since = None
            return {}
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            payload = {}
        state = payload if isinstance(payload, dict) else {}
        self._state_cache = dict(state)
        self._state_cache_path = path
        try:
            self._state_cache_mtime_ns = path.stat().st_mtime_ns
        except OSError:
            self._state_cache_mtime_ns = None
        self._state_dirty_since = None
        return dict(self._state_cache)

    def _write_state(self, data: dict[str, object]) -> None:
        path = self._state_path()
        if (
            self._state_cache is not None
            and self._state_cache_path == path
            and self._state_dirty_since is not None
        ):
            merged = dict(self._state_cache)
            merged.update(data)
            data = merged
        else:
            data = dict(data)
        atomic_write_json(path, data)
        self._state_cache = dict(data)
        self._state_cache_path = path
        try:
            self._state_cache_mtime_ns = path.stat().st_mtime_ns
        except OSError:
            self._state_cache_mtime_ns = None
        self._state_dirty_since = None

    _LAYOUT_MUTATION_TOKEN = "_layout_mutation_token"
    _LAYOUT_MUTATION_UNTIL = "_layout_mutating_until"
    _LAYOUT_MUTATION_TTL_SECONDS = 30.0

    def _begin_layout_mutation(self) -> str:
        token = f"{os.getpid()}:{time.monotonic_ns()}"
        self._active_layout_mutation_token = token
        state = self._load_state()
        state[self._LAYOUT_MUTATION_TOKEN] = token
        state[self._LAYOUT_MUTATION_UNTIL] = time.time() + self._LAYOUT_MUTATION_TTL_SECONDS
        self._write_state(state)
        return token

    def _end_layout_mutation(self, token: str) -> None:
        try:
            state = self._load_state()
            if state.get(self._LAYOUT_MUTATION_TOKEN) == token:
                state.pop(self._LAYOUT_MUTATION_TOKEN, None)
                state.pop(self._LAYOUT_MUTATION_UNTIL, None)
                self._write_state(state)
        finally:
            if getattr(self, "_active_layout_mutation_token", None) == token:
                self._active_layout_mutation_token = None

    def _layout_mutation_active_elsewhere(self) -> bool:
        state = self._load_state()
        token = state.get(self._LAYOUT_MUTATION_TOKEN)
        if not isinstance(token, str) or not token:
            return False
        if token == getattr(self, "_active_layout_mutation_token", None):
            return False
        until = state.get(self._LAYOUT_MUTATION_UNTIL)
        try:
            deadline = float(until)
        except (TypeError, ValueError):
            return False
        return deadline > time.time()

    def rail_width(self) -> int:
        """Return the persisted rail width, falling back to the default."""
        data = self._load_state()
        value = data.get("rail_width")
        if isinstance(value, int) and 20 <= value <= 120:
            return value
        return self._LEFT_PANE_WIDTH

    def set_rail_width(self, width: int) -> None:
        """Persist the rail width so subsequent launches and layout checks use it."""
        if not isinstance(width, int) or width < 20 or width > 120:
            return
        data = self._load_state()
        if data.get("rail_width") == width:
            return
        data["rail_width"] = width
        self._state_cache = dict(data)
        self._state_cache_path = self._state_path()
        self._state_dirty_since = time.monotonic()
        self._maybe_flush_state()

    def _maybe_flush_state(self) -> None:
        if (
            self._state_dirty_since is None
            or self._state_cache is None
            or self._state_cache_path is None
        ):
            return
        if time.monotonic() - self._state_dirty_since < self._STATE_WRITE_DEBOUNCE_SECONDS:
            return
        self._write_state(self._state_cache)

    def _validate_state(self, *, panes: list | None = None, target: str | None = None) -> list:
        """Clear stale entries from cockpit_state.json.

        Checks that right_pane_id points to a real pane and that
        mounted_session is actually alive. Prevents stale state from
        blocking heartbeat recovery or causing wrong session mounts.

        Returns the list of panes fetched (or the list passed in) so
        callers can avoid re-issuing ``list_panes`` — see #175.
        """
        if self._layout_mutation_active_elsewhere():
            return panes if panes is not None else []
        state = self._load_state()
        dirty = False
        if target is None:
            config = self._load_config()
            target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        if panes is None:
            panes = self._safe_list_panes(target)

        right_pane_id = state.get("right_pane_id")
        right_pane = None
        if isinstance(right_pane_id, str) and right_pane_id:
            right_pane = next((pane for pane in panes if pane.pane_id == right_pane_id), None)
            if right_pane is None or getattr(right_pane, "pane_dead", False):
                state.pop("right_pane_id", None)
                state.pop("mounted_session", None)
                state.pop("mounted_identity", None)
                dirty = True
                right_pane = None

        mounted = state.get("mounted_session")
        if isinstance(mounted, str) and mounted:
            release_lease = False
            try:
                supervisor = self._load_supervisor()
                if (
                    self._mounted_window_name(supervisor, mounted) is None
                    or not self._mounted_session_matches_pane(right_pane)
                ):
                    state.pop("mounted_session", None)
                    state.pop("mounted_identity", None)
                    dirty = True
                    release_lease = True
            except Exception:  # noqa: BLE001
                state.pop("mounted_session", None)
                state.pop("mounted_identity", None)
                dirty = True
                release_lease = True
            if release_lease:
                self._release_cockpit_lease(supervisor if "supervisor" in locals() else None, mounted)

        if dirty:
            self._write_state(state)
        return panes

    def _safe_list_panes(self, target: str) -> list:
        try:
            return self.tmux.list_panes(target)
        except Exception:  # noqa: BLE001
            return []

    def _mounted_session_matches_pane(self, pane) -> bool:
        if pane is None or getattr(pane, "pane_dead", False):
            return False
        if not self._is_live_provider_pane(pane):
            return False
        # A live provider pane is running — trust the state rather than
        # trying to match CWD.  CWD matching is unreliable because the
        # agent may cd elsewhere during its turn.  If state says this
        # session is mounted and the pane is alive, believe it.
        return True

    def _release_cockpit_lease(self, supervisor, session_name: str) -> None:
        if supervisor is None:
            try:
                supervisor = self._load_supervisor()
            except Exception:  # noqa: BLE001
                return
        try:
            supervisor.release_lease(session_name, expected_owner="cockpit")
        except Exception:  # noqa: BLE001
            pass

    def _ui_initialized_sessions(self) -> set[str]:
        data = self._load_state()
        value = data.get("ui_initialized_sessions")
        if not isinstance(value, list):
            return set()
        return {item for item in value if isinstance(item, str) and item}

    def _mark_ui_initialized(self, session_name: str) -> None:
        data = self._load_state()
        current = data.get("ui_initialized_sessions")
        items = [item for item in current if isinstance(item, str) and item] if isinstance(current, list) else []
        if session_name not in items:
            items.append(session_name)
        data["ui_initialized_sessions"] = items
        self._write_state(data)

    def pinned_projects(self) -> list[str]:
        """Return pinned project keys in most-recently-pinned-first order.

        Persisted as a JSON list so the insertion order survives restarts
        (#677 acceptance: "Multiple pins maintain their relative pin
        order (most recently pinned first)").
        """
        data = self._load_state()
        value = data.get("pinned_projects")
        if not isinstance(value, list):
            return []
        # Dedupe while preserving first-seen order.
        seen: set[str] = set()
        ordered: list[str] = []
        for item in value:
            if isinstance(item, str) and item and item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    def is_project_pinned(self, project_key: str) -> bool:
        return project_key in self.pinned_projects()

    def toggle_pinned_project(self, project_key: str) -> bool:
        data = self._load_state()
        current = self.pinned_projects()
        if project_key in current:
            current.remove(project_key)
            pinned = False
        else:
            # Most-recently-pinned first — prepend so the newest pin
            # sorts to the top.
            current.insert(0, project_key)
            pinned = True
        data["pinned_projects"] = current
        self._write_state(data)
        return pinned

    def build_items(self, *, spinner_index: int = 0) -> list[CockpitItem]:
        """Build rail rows by walking the plugin-host rail registry.

        Rows are gathered in section order (``top`` → ``projects`` →
        ``workflows`` → ``tools`` → ``system``), then within each
        section by ``(index, plugin_name)``. Items that declare a
        ``rows_provider`` expand into N rows; others collapse into a
        single :class:`CockpitItem` built from the static label plus
        optional ``label_provider`` / ``state_provider`` callables.

        Pre-er02 behaviour is preserved by the built-in
        ``core_rail_items`` plugin, which registers every rail entry
        that used to be hardcoded here.
        """
        from pollypm.plugin_api.v1 import RailContext, RAIL_SECTIONS

        supervisor = self._load_supervisor()
        config = supervisor.config
        launches, windows, alerts, _leases, _errors = supervisor.status()
        recent_events = []
        try:
            recent_events = list(supervisor.store.recent_events(limit=300))
        except Exception:  # noqa: BLE001
            recent_events = []

        cockpit_state = self._load_state()
        ctx = RailContext(
            selected_project=_selected_project_key(cockpit_state.get("selected")),
            cockpit_state=dict(cockpit_state),
            config=config,
            router=self,
            supervisor=supervisor,
            launches=list(launches),
            windows=list(windows),
            alerts=list(alerts),
            spinner_index=spinner_index,
            # ``extras`` retained as an escape hatch for cockpit-private
            # hints that haven't been promoted to typed fields (#800).
            # Mirrored from the typed fields so existing readers keep
            # working through the deprecation window.
            extras={
                "router": self,
                "supervisor": supervisor,
                "config": config,
                "launches": launches,
                "windows": windows,
                "alerts": alerts,
                "spinner_index": spinner_index,
            },
        )

        registry = self._rail_registry()

        # Hidden items + visibility predicates land in er03 / er04. The
        # renderer here runs them every tick — badge providers likewise
        # (a crash falls back to no-badge).
        collapsed_sections = self._collapsed_rail_sections_cached(config)
        grouped_registrations = self._grouped_rail_registrations(config, registry)
        grouped: dict[str, list[CockpitItem]] = {name: [] for name in RAIL_SECTIONS}
        project_session_map = self._project_session_map(launches)
        project_rollups = self._project_state_rollups(config, alerts)

        for section, registrations in grouped_registrations.items():
            for reg in registrations:
                if not _visibility_passes(reg, ctx):
                    continue
                rows = _rows_for_registration(reg, ctx)
                for row in rows:
                    item = CockpitItem(
                        key=row.key,
                        label=row.label,
                        state=row.state,
                        selectable=row.selectable,
                    )
                    self._attach_session_metadata(
                        item,
                        launches=launches,
                        supervisor=supervisor,
                        project_session_map=project_session_map,
                        alerts=list(alerts),
                    )
                    grouped.setdefault(section, []).append(item)

        items: list[CockpitItem] = []
        for section in RAIL_SECTIONS:
            rows = grouped.get(section) or []
            if not rows:
                continue
            active_rows = [row for row in rows if self._row_is_active(row)]
            # ``projects`` is the primary navigation surface — keep it
            # visible even when no project has an active session, so the
            # user can still click into a project from the rail. Only
            # transient/dynamic sections get auto-collapsed when idle.
            if section in collapsed_sections or (
                section not in {"top", "system", "projects"} and not active_rows
            ):
                # Collapsed sections render as a disabled header row so
                # the user can still see the section exists. Expansion
                # is a runtime concept tracked via set_selected_key.
                items.append(
                    CockpitItem(
                        key=f"_section:{section}",
                        label=f"{section.upper()} ({len(rows)})",
                        state="separator",
                        selectable=False,
                    )
                )
                continue
            items.extend(rows)

        items = self._inject_mounted_task_rows(items, cockpit_state=cockpit_state)
        return self._decorate_project_items(
            items,
            selected_project=_selected_project_key(cockpit_state.get("selected")),
            launches=launches,
            recent_events=recent_events,
            project_session_map=project_session_map,
            project_rollups=project_rollups,
        )

    def _attach_session_metadata(
        self,
        item: CockpitItem,
        *,
        launches,
        supervisor,
        project_session_map: dict[str, str],
        alerts: list[object] | None = None,
    ) -> None:
        session_name = self._session_name_for_item(item, project_session_map)
        if session_name is not None:
            item.session_name = session_name
            launch = next(
                (entry for entry in launches if entry.session.name == session_name),
                None,
            )
            if launch is not None:
                item.work_state = self._work_state_for_item_state(
                    item.state, launch.session.role,
                )
                try:
                    heartbeat = supervisor.store.latest_heartbeat(session_name)
                except Exception:  # noqa: BLE001
                    heartbeat = None
                item.heartbeat_at = getattr(heartbeat, "created_at", None)
        # #989 — Surface the top actionable alert's severity + message
        # on the item so the renderer can pick the right palette
        # (``warn`` amber vs ``error`` red) and the alert-detail modal
        # can read the message without re-querying the supervisor.
        # Project rows aggregate alerts across both ``worker_<key>``
        # and ``architect_<key>`` / ``plan_gate-<key>`` sessions, so
        # the lookup walks every alert keyed off the project rather
        # than restricting to the rail row's own ``session_name``.
        if alerts:
            self._attach_alert_metadata(item, alerts=alerts)

    def _session_name_for_item(
        self,
        item: CockpitItem,
        project_session_map: dict[str, str],
    ) -> str | None:
        if item.key == "polly":
            return "operator"
        if item.key == "russell":
            return "reviewer"
        if not item.key.startswith("project:") or item.key.count(":") != 1:
            return None
        project_key = item.key.split(":", 1)[1]
        return project_session_map.get(project_key)

    # ── #989 — alert metadata attachment ────────────────────────────────
    #
    # Decoupled from ``_attach_session_metadata`` because project rows
    # aggregate alerts across multiple sessions (``worker_<key>``,
    # ``architect_<key>``, ``plan_gate-<key>``) and the rail's project
    # rollup already handles the project case via
    # ``cockpit_project_state``. Top-level rows (Polly, Russell, etc.)
    # use their own session name directly.
    def _attach_alert_metadata(
        self,
        item: CockpitItem,
        *,
        alerts: list[object],
    ) -> None:
        candidate_sessions = self._alert_session_candidates(item)
        if not candidate_sessions:
            return
        actionable: list[object] = []
        for alert in alerts:
            session_name = getattr(alert, "session_name", "") or ""
            if session_name not in candidate_sessions:
                continue
            alert_type = getattr(alert, "alert_type", "") or ""
            if alert_type in self._SILENT_ALERT_TYPES:
                continue
            actionable.append(alert)
        if not actionable:
            return
        # Prefer ``error`` over ``warn`` so the badge palette reflects
        # the most severe open alert. Within a tier, take the first
        # match (the alerts list is already newest-first from
        # ``supervisor.status()``).
        actionable.sort(
            key=lambda a: 0 if str(getattr(a, "severity", "")).lower() == "error" else 1,
        )
        top = actionable[0]
        severity = str(getattr(top, "severity", "") or "").lower()
        if severity in {"error", "critical"}:
            item.alert_severity = "error"
        else:
            # Treat ``warn`` / ``warning`` / unknown as warn — the
            # supervisor uses both literal spellings (``warn`` /
            # ``warning``) for the same intent.
            item.alert_severity = "warn"
        item.alert_message = getattr(top, "message", None) or None
        item.alert_type = getattr(top, "alert_type", None) or None
        item.alert_id = getattr(top, "alert_id", None)

    def _alert_session_candidates(self, item: CockpitItem) -> frozenset[str]:
        """Return the supervisor session names whose alerts attach to ``item``."""
        if item.key == "polly":
            return frozenset({"operator"})
        if item.key == "russell":
            return frozenset({"reviewer"})
        if item.key.startswith("project:") and item.key.count(":") == 1:
            project_key = item.key.split(":", 1)[1]
            # Mirrors the project-rollup aggregation in
            # ``cockpit_project_state.rollup_project_state``: any session
            # owned by the project rolls up into the project row. Some
            # session names are sanitized to underscores (e.g.
            # ``worker_blackjack_trainer`` for project key
            # ``blackjack-trainer``), so include both the literal
            # project_key and its underscored alias.
            alias = project_key.replace("-", "_")
            return frozenset({
                f"worker_{project_key}",
                f"architect_{project_key}",
                f"plan_gate-{project_key}",
                f"reviewer_{project_key}",
                f"worker_{alias}",
                f"architect_{alias}",
                f"plan_gate-{alias}",
                f"reviewer_{alias}",
            })
        return frozenset()

    def _work_state_for_item_state(self, state: str, role: str) -> str | None:
        if state == "dead":
            return "exited"
        if state.startswith("!"):
            return "stuck"
        if state.endswith("working"):
            return "writing"
        if role == "reviewer":
            return "reviewing"
        if state.endswith("live") or state in {"idle", "ready"}:
            return "idle"
        return None

    def _rail_registry(self):
        """Return the plugin-host rail registry for the active root dir."""
        from pollypm.plugin_host import extension_host_for_root

        config = self._load_config()
        host = extension_host_for_root(str(config.project.root_dir.resolve()))
        # Initialize plugins so the rail registry is populated. Safe to
        # call repeatedly — it tracks which plugins have been init'd.
        try:
            host.initialize_plugins(config=config)
        except Exception:  # noqa: BLE001
            # Plugin init failures surface via degraded_plugins; don't
            # block the rail rendering.
            pass
        registry = host.rail_registry()
        # Worker roster top-rail entry. Registered here (not in
        # ``core_rail_items``) so the cockpit router + roster Textual
        # app land in one feature drop — the rail row, the route handler
        # (``route_selected("workers")``), and the panel renderer all
        # live alongside each other. The registration is gated on
        # ``core_rail_items`` being active so disabling the core plugin
        # still yields an empty rail (see
        # ``test_removing_core_rail_items_yields_empty_rail``).
        try:
            core_enabled = "core_rail_items" in host.plugins()
        except Exception:  # noqa: BLE001
            core_enabled = True
        if core_enabled:
            # Deferred imports: the worker roster + metrics registrations
            # live in ``pollypm.cockpit`` alongside the gather helpers
            # they call into. Importing them at module load time would
            # create a cycle (cockpit imports cockpit_rail to re-export
            # the router), so we resolve them per-tick instead.
            from pollypm.cockpit import (
                _register_metrics_rail_item,
                _register_worker_roster_rail_item,
            )
            _register_worker_roster_rail_item(registry, self)
            _register_metrics_rail_item(registry, self)
        return registry

    def _project_session_map(self, launches) -> dict[str, str]:
        from pollypm.models import CONTROL_ROLES

        project_session_map: dict[str, str] = {}
        for launch in launches:
            role = getattr(launch.session, "role", "")
            if role in CONTROL_ROLES:
                continue
            project = getattr(launch.session, "project", None)
            name = getattr(launch.session, "name", None)
            if project and name:
                project_session_map.setdefault(project, name)
        return project_session_map

    def _decorate_project_items(
        self,
        items: list[CockpitItem],
        *,
        selected_project: str | None,
        launches,
        recent_events: list,
        project_session_map: dict[str, str],
        project_rollups: dict[str, ProjectStateRollup] | None = None,
    ) -> list[CockpitItem]:
        from pollypm.cockpit_sections.base import _iso_to_dt, _spark_bar

        first_project = next((idx for idx, item in enumerate(items) if item.key.startswith("project:")), None)
        if first_project is None:
            return items
        last_project = max(
            (idx for idx, item in enumerate(items) if item.key.startswith("project:")),
            default=None,
        )
        if last_project is None:
            return items

        prefix = list(items[:first_project])
        region = list(items[first_project : last_project + 1])
        suffix = list(items[last_project + 1 :])
        project_event_spark = self._project_activity_sparkline(
            project_session_map,
            recent_events,
            _iso_to_dt=_iso_to_dt,
            _spark_bar=_spark_bar,
        )

        rollups = project_rollups or {}
        project_blocks: list[tuple[str, list[CockpitItem], bool, bool, ProjectStateRollup | None]] = []
        current_key: str | None = None
        current_block: list[CockpitItem] = []
        current_pinned = False
        current_selected = False

        def flush_block() -> None:
            nonlocal current_key, current_block, current_pinned, current_selected
            if current_key is not None and current_block:
                project_blocks.append((
                    current_key,
                    current_block,
                    current_pinned,
                    current_selected,
                    rollups.get(current_key),
                ))
            current_key = None
            current_block = []
            current_pinned = False
            current_selected = False

        for item in region:
            if item.key.startswith("project:"):
                parts = item.key.split(":")
                if len(parts) == 2:
                    flush_block()
                    current_key = parts[1]
                    current_selected = current_key == selected_project
                    current_pinned = self.is_project_pinned(current_key)
                    current_block = [
                        self._decorate_project_row(
                            item,
                            project_event_spark.get(current_key),
                            rollup=rollups.get(current_key),
                        )
                    ]
                    continue
                if current_key is not None and len(parts) > 2 and parts[1] == current_key:
                    current_block.append(item)
                    continue
            flush_block()
            prefix.append(item)
        flush_block()

        project_region: list[CockpitItem] = []
        if project_blocks:
            # Pinned projects sort first, ordered by most-recently-pinned
            # (pinned_projects() returns newest-first, see #677). The
            # selected project bubbles ahead of unpinned; unpinned
            # projects sort alphabetically.
            pin_order = self.pinned_projects()
            pin_rank = {key: idx for idx, key in enumerate(pin_order)}
            project_blocks.sort(
                key=lambda row: (
                    self._project_rollup_sort_rank(row[4]),
                    not row[2],  # pinned ahead of unpinned within severity
                    pin_rank.get(row[0], 0),  # pinned: by recency
                    not row[3],  # selected ahead of other unpinned
                    row[0].lower(),  # unpinned: alphabetical
                ),
            )
            for _key, block, _pinned, _selected, _rollup in project_blocks:
                project_region.extend(block)
        return [*prefix, *project_region, *suffix]

    def _project_rollup_sort_rank(self, rollup: ProjectStateRollup | None) -> int:
        if rollup is None:
            return 4
        return rollup.sort_rank

    def _decorate_project_row(
        self,
        item: CockpitItem,
        sparkline: str | None,
        *,
        rollup: ProjectStateRollup | None = None,
    ) -> CockpitItem:
        label = item.label
        if self.is_project_pinned(item.key.split(":", 1)[1]):
            label = f"📌 {label}"
        if sparkline:
            label = f"{label} {sparkline}"
        return replace(
            item,
            label=label,
            state=self._project_row_state(item.state, rollup),
        )

    def _project_row_state(
        self,
        fallback: str,
        rollup: ProjectStateRollup | None,
    ) -> str:
        if rollup is None:
            return fallback
        if rollup.state is ProjectRailState.RED:
            return "project-red"
        if rollup.state is ProjectRailState.YELLOW:
            return "project-yellow"
        if rollup.state is ProjectRailState.GREEN:
            return "project-green"
        if rollup.state is ProjectRailState.WORKING:
            return "project-working"
        return fallback

    def _project_state_rollups(
        self,
        config: object,
        alerts: list[object],
    ) -> dict[str, ProjectStateRollup]:
        projects = getattr(config, "projects", {}) or {}
        rollups: dict[str, ProjectStateRollup] = {}
        for project_key, project in projects.items():
            tasks, plan_blocked = self._project_tasks_for_rollup(
                str(project_key),
                project,
                config=config,
            )
            rollup = rollup_project_state(
                str(project_key),
                tasks,
                plan_blocked=plan_blocked,
                actionable_task_alert_ids=actionable_alert_task_ids(
                    alerts,
                    project_key=str(project_key),
                ),
            )
            rollups[str(project_key)] = rollup
        return rollups

    def _project_tasks_for_rollup(
        self,
        project_key: str,
        project: object,
        *,
        config: object,
    ) -> tuple[list[object], bool]:
        project_path = getattr(project, "path", None)
        if not isinstance(project_path, Path):
            return [], False
        db_path = project_path / ".pollypm" / "state.db"
        if not db_path.exists():
            return [], False
        from pollypm.plugins_builtin.project_planning.plan_presence import has_acceptable_plan
        from pollypm.work.project_aliases import project_storage_aliases
        from pollypm.work.sqlite_service import SQLiteWorkService

        work = SQLiteWorkService(db_path, project_path=project_path)
        try:
            # #1092 — match the dashboard's alias-aware lookup. The work
            # DB stores tasks under the display name (``booktalk``) while
            # the rollup receives the slugified config key, so a single
            # ``list_tasks(project=project_key)`` silently returned zero
            # tasks for projects whose key and display name diverge.
            # The dashboard correctly counts ``on_hold`` via the alias
            # union — without it here, the rail rollup falls through to
            # ``ProjectRailState.NONE`` and the held-task project shows
            # up in the rail with the idle ``♥·`` glyph (no marker)
            # instead of the ``◆`` "needs attention" indicator.
            aliases = project_storage_aliases(config, project_key)
            seen_ids: set[str] = set()
            tasks: list = []
            for alias in aliases:
                for task in work.list_tasks(project=alias):
                    tid = getattr(task, "task_id", None)
                    if tid and tid in seen_ids:
                        continue
                    if tid:
                        seen_ids.add(tid)
                    tasks.append(task)
            planner = getattr(config, "planner", None)
            plan_dir = str(getattr(planner, "plan_dir", "docs/plan") or "docs/plan")
            global_enforce = bool(getattr(planner, "enforce_plan", True))
            project_enforce = getattr(project, "enforce_plan", None)
            enforce_plan = (
                project_enforce if project_enforce is not None else global_enforce
            )
            if not enforce_plan:
                return tasks, False
            plan_blocked = bool(tasks) and not has_acceptable_plan(
                project_key,
                project_path,
                work,
                plan_dir=plan_dir,
            )
            return tasks, plan_blocked
        except Exception:  # noqa: BLE001
            return [], False
        finally:
            work.close()

    def _inject_mounted_task_rows(
        self,
        items: list[CockpitItem],
        *,
        cockpit_state: dict[str, object],
    ) -> list[CockpitItem]:
        mounted_task = _task_mount_parts(cockpit_state.get("mounted_session"))
        if mounted_task is None:
            return items
        project_key, task_num = mounted_task
        selected = cockpit_state.get("selected")
        if not isinstance(selected, str) or not selected.startswith(f"project:{project_key}"):
            return items
        task_key = f"project:{project_key}:task:{task_num}"
        if any(item.key == task_key for item in items):
            return items
        row = CockpitItem(
            key=task_key,
            label=f"  ⟳ Task #{task_num}",
            state="sub",
        )

        insert_at: int | None = None
        task_prefix = f"project:{project_key}:task:"
        for index, item in enumerate(items):
            if item.key.startswith(task_prefix):
                insert_at = index + 1
        if insert_at is None:
            for fallback_key in (
                f"project:{project_key}:issues",
                f"project:{project_key}:session",
                f"project:{project_key}:dashboard",
            ):
                insert_at = next(
                    (index + 1 for index, item in enumerate(items) if item.key == fallback_key),
                    None,
                )
                if insert_at is not None:
                    break
        if insert_at is None:
            insert_at = next(
                (
                    index
                    for index, item in enumerate(items)
                    if item.key == f"project:{project_key}:settings"
                ),
                None,
            )
        if insert_at is None:
            return items
        return [*items[:insert_at], row, *items[insert_at:]]

    def _project_activity_sparkline(
        self,
        project_session_map: dict[str, str],
        recent_events: list,
        *,
        _iso_to_dt,
        _spark_bar,
    ) -> dict[str, str]:
        from datetime import UTC, datetime

        if not recent_events:
            return {}
        now = datetime.now(UTC)
        buckets: dict[str, list[int]] = {key: [0] * 10 for key in project_session_map}
        session_to_project = {
            session_name: project_key
            for project_key, session_name in project_session_map.items()
        }
        for event in recent_events:
            project_key = session_to_project.get(getattr(event, "session_name", ""))
            if project_key is None:
                continue
            dt = _iso_to_dt(getattr(event, "created_at", None))
            if dt is None:
                continue
            age_minutes = int((now - dt).total_seconds() // 60)
            if age_minutes < 0 or age_minutes >= 60:
                continue
            bucket = 9 - (age_minutes // 6)
            buckets[project_key][bucket] += 1
        result: dict[str, str] = {}
        for project_key, values in buckets.items():
            if any(values):
                # ``_spark_bar`` uses U+0020 for the zero block. In the
                # rail, where the spark line sits inline next to a
                # project name, interleaved spaces between blocks read
                # as padding instead of "no activity in that bucket"
                # — e.g. ``PollyPM     █    █`` looked like a name
                # followed by two stray glyphs. Substitute the
                # all-zero fallback's dot glyph for in-line zeros so
                # the spark line stays visually continuous.
                result[project_key] = _spark_bar(values).replace(" ", "·")
            else:
                result[project_key] = "·" * len(values)
        return result

    def _row_is_active(self, item: CockpitItem) -> bool:
        state = item.state.strip().lower()
        if not state or state in {"idle", "separator", "sub"}:
            return False
        return any(
            marker in state
            for marker in ("working", "live", "watch", "ready", "alert", "active")
        ) or state.startswith("!")

    # Alert types that are informational / auto-managed — don't show red triangle
    _SILENT_ALERT_TYPES = frozenset({
        "suspected_loop",      # auto-clears when snapshot changes
        "stabilize_failed",    # stale after successful recovery
        "needs_followup",      # informational, handled by heartbeat
    })

    def _session_state(
        self,
        session_name: str,
        launches,
        windows,
        alerts,
        spinner_index: int,
        *,
        user_waiting_task_ids: frozenset[str] | set[str] = frozenset(),
    ) -> str:
        actionable = [
            a for a in alerts
            if a.session_name == session_name
            and a.alert_type not in self._SILENT_ALERT_TYPES
            and not _stuck_alert_already_user_waiting(
                a.alert_type, user_waiting_task_ids,
            )
        ]
        if actionable:
            # Include a short reason so the user knows what's wrong
            top = actionable[0]
            short_reason = top.alert_type.replace("_", " ")
            return f"! {short_reason}"
        launch = next((item for item in launches if item.session.name == session_name), None)
        if launch is None:
            return "idle"
        window_map = {window.name: window for window in windows}
        window = window_map.get(launch.window_name)
        # If the session is mounted in the cockpit, its storage window is gone.
        # Check the cockpit right pane instead.
        if window is None:
            state = self._load_state()
            if state.get("mounted_session") == session_name:
                window = self._mounted_window_proxy(launch, windows)
        if window is None:
            return "idle"
        if window.pane_dead:
            return "dead"
        if launch.session.role in ("worker", "operator-pm", "reviewer"):
            heartbeat = None
            try:
                supervisor = self._load_supervisor()
            except Exception:  # noqa: BLE001
                supervisor = None
            if supervisor is not None:
                try:
                    heartbeat = supervisor.store.latest_heartbeat(session_name)
                except Exception:  # noqa: BLE001
                    heartbeat = None
            working = self._is_pane_working(
                window,
                launch.session.provider,
                heartbeat=heartbeat,
                session_name=session_name,
            )
            if working:
                return f"{self._presence().working_frame(spinner_index)} working"
            if launch.session.role == "worker":
                return "\u25cf live"
            return "ready"
        if launch.session.role == "heartbeat-supervisor":
            return "watch"
        if launch.session.role == "triage":
            return "triage"
        return "live"

    def _mounted_window_proxy(self, launch, windows):
        """Return a window-like object for a session mounted in the cockpit pane."""
        cockpit_windows = [w for w in windows if w.name == self._COCKPIT_WINDOW]
        if not cockpit_windows:
            return None
        # The cockpit window has multiple panes; the right pane is the mounted session.
        try:
            supervisor = self._load_supervisor()
            target = f"{supervisor.config.project.tmux_session}:{self._COCKPIT_WINDOW}"
            panes = self.tmux.list_panes(target)
            if len(panes) < 2:
                return None
            right_pane = max(panes, key=self._pane_left)
            # Return the cockpit window but with the right pane's info
            cockpit_win = cockpit_windows[0]
            from dataclasses import replace as dc_replace
            return dc_replace(
                cockpit_win,
                pane_id=right_pane.pane_id,
                pane_current_command=right_pane.pane_current_command,
                pane_dead=right_pane.pane_dead,
            )
        except Exception:  # noqa: BLE001
            return None

    def _session_snapshot_is_stable(
        self, session_name: str, *, stable_frames: int = 3,
    ) -> bool:
        """Return True if the last ``stable_frames`` heartbeats all share
        the same snapshot_hash — a strong signal that the pane is not
        actually producing output, regardless of what the current pane
        text suggests. See #764.
        """
        try:
            supervisor = self._load_supervisor()
        except Exception:  # noqa: BLE001
            return False
        if supervisor is None:
            return False
        try:
            records = supervisor.store.recent_heartbeats(
                session_name, limit=stable_frames,
            )
        except Exception:  # noqa: BLE001
            return False
        if len(records) < stable_frames:
            return False
        hashes = {r.snapshot_hash for r in records if r.snapshot_hash}
        # All three stored with the same hash → unchanged pane.
        return len(hashes) == 1

    def _is_pane_working(
        self,
        window,
        provider,
        *,
        heartbeat=None,
        session_name: str | None = None,
    ) -> bool:
        """Check if a session pane has an active turn (agent is working, not idle at prompt).

        #764: even if the pane text says "esc to interrupt" (claude's
        turn-in-progress marker), treat the session as NOT working
        when the snapshot hash has been stable across the last three
        heartbeats. A genuinely working agent produces fresh output on
        every heartbeat interval; a stalled session stays pixel-for-
        pixel identical while still showing "esc to interrupt" from a
        turn that's been hung for minutes or hours.
        """
        if session_name and self._session_snapshot_is_stable(session_name):
            return False
        pane_text = read_recent_heartbeat_snapshot(heartbeat)
        if pane_text is None:
            try:
                pane_text = self.tmux.capture_pane(window.pane_id, lines=15)
            except Exception:  # noqa: BLE001
                return False
        try:
            tail_lines = [
                line.rstrip()
                for line in pane_text.splitlines()
                if line.strip()
            ][-6:]
        except Exception:  # noqa: BLE001
            return False
        if not tail_lines:
            return False
        tail = "\n".join(tail_lines)
        lowered = tail.lower()
        # Universal working indicator — both Claude and Codex show this during active turns
        if "esc to interrupt" in lowered:
            return True
        prompt_lines = tail_lines[-2:]
        # Universal idle indicators — if any are present, the session is idle regardless of provider
        idle_markers = (
            "bypass permissions", "new task?", "/clear to save", "shift+tab to cycle",  # Claude
            "press enter to confirm", "% left",  # Codex
        )
        # Provider-specific prompt detection
        provider_value = provider.value if hasattr(provider, "value") else str(provider)
        if any(marker in lowered for marker in idle_markers):
            if provider_value == "claude" and any("\u276f" in line for line in prompt_lines):
                return False
            if provider_value == "codex" and any("\u203a" in line for line in prompt_lines):
                return False
            return False
        if provider_value == "claude":
            return "\u276f" not in tail
        if provider_value == "codex":
            # Codex idle: › prompt
            if "\u203a" in tail:
                return False
            return bool(tail.strip())
        return False

    def _cleanup_duplicate_windows(self, storage_session: str) -> None:
        """Kill duplicate windows in the storage closet, keeping only the first of each name."""
        try:
            windows = self.tmux.list_windows(storage_session)
        except Exception:  # noqa: BLE001
            return
        seen: dict[str, int] = {}  # name -> first index
        for window in windows:
            if window.name in seen:
                try:
                    self.tmux.kill_window(f"{storage_session}:{window.index}")
                except Exception:  # noqa: BLE001
                    pass
            else:
                seen[window.name] = window.index

    def ensure_cockpit_layout(self) -> None:
        if self._layout_mutation_active_elsewhere():
            return
        config = self._load_config()
        target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        panes = self._safe_list_panes(target)
        self._validate_state(panes=panes, target=target)
        try:
            supervisor = self._load_supervisor()
            self._cleanup_duplicate_windows(supervisor.storage_closet_session_name())
        except Exception:  # noqa: BLE001
            pass
        state = self._load_state()
        right_pane_id = state.get("right_pane_id")
        right_pane_present = isinstance(right_pane_id, str) and any(pane.pane_id == right_pane_id for pane in panes)
        if len(panes) == 1 and right_pane_present and panes[0].pane_id == right_pane_id:
            # The rail (left) pane died, only the worker (right) pane survived.
            # Park the worker back to storage and clear stale state so the
            # split below creates a fresh right pane from the cockpit pane.
            supervisor = self._load_supervisor()
            mounted = state.get("mounted_session")
            if isinstance(mounted, str) and mounted:
                storage_session = supervisor.storage_closet_session_name()
                window_name = self._mounted_window_name(supervisor, mounted)
                if window_name is not None and self.tmux.has_session(storage_session):
                    try:
                        self.tmux.break_pane(panes[0].pane_id, storage_session, window_name)
                    except Exception:  # noqa: BLE001
                        pass
            state.pop("right_pane_id", None)
            state.pop("mounted_session", None)
            state.pop("mounted_identity", None)
            self._write_state(state)
            try:
                panes = self.tmux.list_panes(target)  # structural change (break_pane)
            except Exception:  # noqa: BLE001
                panes = []
            right_pane_id = None
            right_pane_present = False
        if len(panes) < 2:
            # Calculate right pane size so the rail starts at exactly rail_width
            # columns — avoids the visible flash of a 50/50 split followed by resize.
            window_width = panes[0].pane_width if panes else 200
            right_size = max(window_width - self.rail_width() - 1, 40)
            # #991 — context-aware repair split. When the user's selection
            # is project-scoped (e.g. they just clicked PM Chat or a
            # project sub-item), splitting with the hardcoded
            # ``pm cockpit-pane polly`` default leaves Polly's workspace
            # dashboard visible if any subsequent mount step bails — the
            # exact fallthrough surface in #991. Pick a default that
            # matches the user's intent so a partial repair shows the
            # project's pane, not Polly's.
            right_pane_id = self.tmux.split_window(
                target,
                self._default_repair_command(state),
                horizontal=True,
                detached=True,
                size=right_size,
            )
            state["right_pane_id"] = right_pane_id
            self._write_state(state)
            panes = self.tmux.list_panes(target)  # split added a pane
        elif len(panes) > 2:
            ordered = sorted(panes, key=self._pane_left)
            left_pane = ordered[0]
            preferred_right = None
            if isinstance(right_pane_id, str):
                preferred_right = next(
                    (
                        pane for pane in ordered
                        if pane.pane_id == right_pane_id
                        and not getattr(pane, "pane_dead", False)
                    ),
                    None,
                )
            if preferred_right is None or preferred_right.pane_id == left_pane.pane_id:
                preferred_right = ordered[-1]
            keep_ids = {left_pane.pane_id, preferred_right.pane_id}
            for pane in ordered:
                if pane.pane_id in keep_ids:
                    continue
                try:
                    self.tmux.kill_pane(pane.pane_id)
                except Exception:  # noqa: BLE001
                    pass
            panes = self.tmux.list_panes(target)  # kill_pane removed panes
        if len(panes) >= 2:
            # ``_normalize_layout`` may swap pane positions but never changes
            # pane IDs or count — so we can reason about the post-swap left/
            # right locally without another ``list-panes`` round-trip.
            self._normalize_layout(target, panes)
            left_pane, right_pane = self._post_normalize_lr(panes)
            state["right_pane_id"] = right_pane.pane_id
            self._write_state(state)
            self._try_resize_rail(left_pane.pane_id)

    def _post_normalize_lr(self, panes):
        """Return ``(left, right)`` for the panes after ``_normalize_layout``.

        ``_normalize_layout`` guarantees the ``uv`` (rail) pane ends up on
        the left. When neither pane is ``uv`` (edge case), fall back to the
        pre-swap ``pane_left`` ordering — the same answer the old code would
        have computed via a second ``list-panes`` + ``min/max(pane_left)``.
        """
        if len(panes) != 2:
            left = min(panes, key=self._pane_left)
            right = max(panes, key=self._pane_left)
            return left, right
        a, b = panes
        a_cmd = getattr(a, "pane_current_command", "")
        b_cmd = getattr(b, "pane_current_command", "")
        if a_cmd == "uv":
            return a, b
        if b_cmd == "uv":
            return b, a
        left = min(panes, key=self._pane_left)
        right = max(panes, key=self._pane_left)
        return left, right

    def _pane_left(self, pane) -> int:
        return int(getattr(pane, "pane_left", 0))

    def _try_resize_rail(self, pane_id: str) -> None:
        """Best-effort resize of the rail pane. Never raises."""
        try:
            self.tmux.resize_pane_width(pane_id, self.rail_width())
        except Exception:  # noqa: BLE001
            pass

    def _right_pane_size(self, window_target: str) -> int | None:
        """Calculate the exact right-pane size so the rail starts at rail_width columns."""
        try:
            panes = self.tmux.list_panes(window_target)
            if panes:
                window_width = max(p.pane_width for p in panes)
                return max(window_width - self.rail_width() - 1, 40)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _join_storage_pane_into_cockpit(
        self,
        *,
        source: str,
        source_pane_id: str | None,
        left_pane_id: str,
        previous_right_pane_id: str | None,
        window_target: str,
    ):
        """Join a storage pane without exposing a one-pane cockpit layout.

        The old sequence killed the static right pane first, then joined the
        live pane. A concurrent rail layout tick could observe that one-pane
        gap and split a fresh static dashboard, which then raced the live
        join. Join first, then remove stale panes while keeping the known
        source pane.
        """
        self.tmux.join_pane(source, left_pane_id, horizontal=True)
        if (
            previous_right_pane_id is not None
            and previous_right_pane_id != source_pane_id
        ):
            try:
                self.tmux.kill_pane(previous_right_pane_id)
            except Exception:  # noqa: BLE001
                pass
        panes = self.tmux.list_panes(window_target)
        if source_pane_id is not None:
            keep = {left_pane_id, source_pane_id}
            for pane in panes:
                pane_id = getattr(pane, "pane_id", None)
                if pane_id is None or pane_id in keep:
                    continue
                try:
                    self.tmux.kill_pane(pane_id)
                except Exception:  # noqa: BLE001
                    pass
            panes = self.tmux.list_panes(window_target)
            right_pane = next(
                (
                    pane for pane in panes
                    if getattr(pane, "pane_id", None) == source_pane_id
                ),
                None,
            )
            if right_pane is not None:
                return right_pane
        return max(panes, key=self._pane_left)

    def _normalize_layout(self, target: str, panes) -> None:
        if len(panes) != 2:
            return
        left_pane = min(panes, key=self._pane_left)
        right_pane = max(panes, key=self._pane_left)
        left_command = getattr(left_pane, "pane_current_command", "")
        right_command = getattr(right_pane, "pane_current_command", "")
        if left_command == "uv":
            return
        if right_command == "uv":
            self.tmux.swap_pane(right_pane.pane_id, left_pane.pane_id)

    def _right_pane_id(self, target: str) -> str | None:
        panes = self.tmux.list_panes(target)
        if len(panes) < 2:
            return None
        return max(panes, key=self._pane_left).pane_id

    def _left_pane_id(self, target: str) -> str | None:
        panes = self.tmux.list_panes(target)
        if not panes:
            return None
        return min(panes, key=self._pane_left).pane_id

    def _route_live_session(
        self,
        supervisor,
        window_target: str,
        route: LiveSessionRoute,
    ) -> None:
        from pollypm.dev_network_simulation import raise_if_network_dead

        raise_if_network_dead(
            self.config_path,
            surface=f"live session route to {route.session_name}",
        )
        launches = supervisor.plan_launches()
        storage_session = supervisor.storage_closet_session_name()
        launch = next((item for item in launches if item.session.name == route.session_name), None)
        if launch is not None:
            storage_windows = {window.name for window in self.tmux.list_windows(storage_session)}
            if launch.window_name not in storage_windows:
                try:
                    supervisor.launch_session(route.session_name)
                except Exception:  # noqa: BLE001
                    pass
        try:
            self._show_live_session(supervisor, route.session_name, window_target)
        except Exception:  # noqa: BLE001
            self._show_static_view(supervisor, window_target, route.fallback_kind)
            return
        # #961 — re-anchor operator identity on attach. ``polly_prompt()``
        # is baked into the launch-time profile but only injected on a
        # *fresh* launch; mounting an existing operator pane (or a pane
        # resumed without its bootstrap marker) leaves Polly answering as
        # a generic agent. A one-shot per-cockpit-process primer brings
        # her back to the PollyPM operator identity with workspace
        # context. Distinct from #958's per-project primer — #958 anchors
        # a project PM, this one anchors the workspace-level operator.
        if route.session_name == "operator":
            self._maybe_prime_operator_session(supervisor, window_target)
        # #987 — clicking a chat session in the rail should leave focus
        # on the right pane so the user can start typing immediately.
        # Static views (Inbox, Workers, Metrics, project dashboards) keep
        # rail focus; only live agent mounts auto-focus. The Ctrl-h
        # rail-recovery path from #985 still returns focus to the rail.
        self._auto_focus_right_after_live_mount(window_target)

    def _route_static_view(
        self,
        supervisor,
        window_target: str,
        route: StaticViewRoute,
    ) -> None:
        self._show_static_view(supervisor, window_target, route.kind, route.project_key)

    def _route_project_selection(
        self,
        supervisor,
        window_target: str,
        route: ProjectRoute,
    ) -> None:
        project_key = route.project_key
        sub_view = route.sub_view
        if sub_view is None or sub_view == "dashboard":
            self.set_selected_key(f"project:{project_key}:dashboard")
            self._show_static_view(supervisor, window_target, "project", project_key)
            return
        if sub_view in ("settings", "issues"):
            task_id = (
                f"{project_key}/{route.task_num}"
                if sub_view == "issues" and route.task_num
                else None
            )
            self._show_static_view(
                supervisor,
                window_target,
                sub_view,
                project_key,
                task_id=task_id,
            )
            return
        if sub_view == "task" and route.task_num:
            task_num = route.task_num
            window_name = f"task-{project_key}-{task_num}"
            storage = supervisor.storage_closet_session_name()
            try:
                storage_windows = self.tmux.list_windows(storage)
                target_win = next((w for w in storage_windows if w.name == window_name), None)
                if target_win is not None:
                    self._park_mounted_session(supervisor, window_target)
                    self._cleanup_extra_panes(window_target)
                    left_pane = self._left_pane_id(window_target)
                    right_pane_id = self._right_pane_id(window_target)
                    source_pane_id = getattr(target_win, "pane_id", None)
                    source = (
                        source_pane_id
                        if isinstance(source_pane_id, str) and source_pane_id
                        else f"{storage}:{target_win.index}.0"
                    )
                    right_p = self._join_storage_pane_into_cockpit(
                        source=source,
                        source_pane_id=source_pane_id,
                        left_pane_id=left_pane,
                        previous_right_pane_id=right_pane_id,
                        window_target=window_target,
                    )
                    panes = self.tmux.list_panes(window_target)
                    left_p = min(panes, key=self._pane_left)
                    self._try_resize_rail(left_p.pane_id)
                    self.tmux.set_pane_history_limit(right_p.pane_id, 200)
                    state = self._load_state()
                    state["mounted_session"] = window_name
                    state["right_pane_id"] = right_p.pane_id
                    self._write_state(state)
                    return
            except Exception:  # noqa: BLE001
                pass
            self.set_selected_key(f"project:{project_key}:dashboard")
            self._show_static_view(supervisor, window_target, "project", project_key)
            return
        if sub_view == "session":
            launches = supervisor.plan_launches()
            session_name = self._project_session_map(launches).get(project_key)
            if session_name is not None:
                if not self._session_available_for_mount(supervisor, session_name, window_target):
                    try:
                        supervisor.launch_session(session_name)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    self._show_live_session(supervisor, session_name, window_target)
                except Exception:  # noqa: BLE001
                    self.set_selected_key(f"project:{project_key}:dashboard")
                    self._show_static_view(supervisor, window_target, "project", project_key)
                else:
                    # #958 — re-anchor project identity on attach. Long
                    # conversations drift workers off-persona; a one-shot
                    # primer per session brings the agent back to the
                    # right project context without the user having to
                    # manually retype "you are the PM for X".
                    self._maybe_prime_project_pm_session(
                        supervisor, project_key, session_name, window_target,
                    )
            else:
                try:
                    self.create_worker_and_route(project_key)
                except Exception:  # noqa: BLE001
                    self.set_selected_key(f"project:{project_key}:dashboard")
                    self._show_static_view(supervisor, window_target, "project", project_key)
            return
        self.set_selected_key(f"project:{project_key}:dashboard")
        self._show_static_view(supervisor, window_target, "project", project_key)

    def _project_rollup_for_route(
        self,
        supervisor,
        project_key: str,
    ) -> ProjectStateRollup | None:
        try:
            alerts = supervisor.open_alerts()
        except Exception:  # noqa: BLE001
            alerts = []
        try:
            return self._project_state_rollups(supervisor.config, alerts).get(project_key)
        except Exception:  # noqa: BLE001
            return None

    def _content_context(self, supervisor) -> CockpitContentContext:
        """Return data-only facts for the pure right-pane content resolver."""
        try:
            launches = supervisor.plan_launches()
        except Exception:  # noqa: BLE001
            launches = []
        try:
            projects = supervisor.config.projects
        except Exception:  # noqa: BLE001
            projects = None
        return CockpitContentContext.from_projects(
            projects,
            project_sessions=self._project_session_map(launches),
        )

    def _route_content_plan(self, supervisor, window_target: str, plan) -> None:
        """Materialize a pure content plan using the legacy pane applicators.

        This is the first integration step for the modular cockpit
        architecture: route decisions now come from ``cockpit_content`` while
        the still-existing tmux applicators keep live behavior stable.
        """
        if isinstance(plan, FallbackPane):
            if (
                plan.reason == "missing_worker"
                and plan.fallback.project_key is not None
                and plan.route_key == f"project:{plan.fallback.project_key}:session"
            ):
                self.set_selected_key(plan.route_key)
                self.create_worker_and_route(plan.fallback.project_key)
                return
            self.set_selected_key(plan.selected_key)
            self._route_content_plan(supervisor, window_target, plan.fallback)
            return

        if isinstance(plan, TextualCommandPane):
            self.set_selected_key(plan.selected_key)
            if plan.task_id is None:
                self._show_static_view(
                    supervisor,
                    window_target,
                    plan.pane_kind,
                    plan.project_key,
                )
                return
            self._show_static_view(
                supervisor,
                window_target,
                plan.pane_kind,
                plan.project_key,
                task_id=plan.task_id,
            )
            return

        if isinstance(plan, LiveAgentPane):
            self.set_selected_key(plan.selected_key)
            self._route_live_agent_plan(supervisor, window_target, plan)
            return

        if isinstance(plan, ErrorPane):
            self.set_selected_key(plan.selected_key)
            self._show_message_view(supervisor, window_target, plan.title, plan.message)
            return

        raise RuntimeError(f"Unsupported cockpit content plan: {plan!r}")

    def _route_live_agent_plan(
        self,
        supervisor,
        window_target: str,
        plan: LiveAgentPane,
    ) -> None:
        fallback_kind = plan.fallback.pane_kind if plan.fallback is not None else "polly"
        if plan.session_name in {"operator", "reviewer"}:
            self._route_live_session(
                supervisor,
                window_target,
                LiveSessionRoute(
                    session_name=plan.session_name,
                    fallback_kind=fallback_kind,
                ),
            )
            return

        if not self._session_available_for_mount(
            supervisor,
            plan.session_name,
            window_target,
        ):
            try:
                supervisor.launch_session(plan.session_name)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._show_live_session(supervisor, plan.session_name, window_target)
        except Exception:  # noqa: BLE001
            if plan.fallback is not None:
                self._route_content_plan(supervisor, window_target, plan.fallback)
            return
        if plan.project_key:
            self._maybe_prime_project_pm_session(
                supervisor,
                plan.project_key,
                plan.session_name,
                window_target,
            )
        # #987 — see :meth:`_route_live_session` for the rationale.
        # Project PM Chat (Persona) also mounts a typing-primary live
        # agent CLI, so auto-focus the right pane after mount.
        self._auto_focus_right_after_live_mount(window_target)

    def _window_manager(self, supervisor) -> CockpitWindowManager:
        config = getattr(supervisor, "config", None) or self._load_config()
        tmux_session = config.project.tmux_session
        try:
            rail_command = supervisor.console_command()
        except Exception:  # noqa: BLE001
            rail_command = None
        # #991 — context-aware default. The window manager uses
        # ``default_content_command`` for repair paths
        # (``_repair_dead_panes``, ``_split_content_pane``). When the
        # user's selection is on a project, falling through to Polly's
        # workspace dashboard during a repair is the exact symptom of
        # #991. Match the repair surface to the user's intent.
        try:
            default_command = self._default_repair_command(self._load_state())
        except Exception:  # noqa: BLE001
            default_command = self._right_pane_command("polly")
        return CockpitWindowManager(
            CockpitWindowSpec(
                tmux_session=tmux_session,
                cockpit_window=self._COCKPIT_WINDOW,
                rail_width=self.rail_width(),
                default_content_command=default_command,
                rail_command=rail_command,
            ),
            self.tmux,
        )

    def _window_state_from_cockpit_state(
        self,
        supervisor,
        state: dict[str, object] | None = None,
    ) -> CockpitWindowState:
        state = state or self._load_state()
        right_pane_id = state.get("right_pane_id")
        mounted_session = state.get("mounted_session")
        mounted_window_name = None
        if isinstance(mounted_session, str) and mounted_session:
            mounted_window_name = self._mounted_window_name(supervisor, mounted_session)
        return CockpitWindowState(
            right_pane_id=right_pane_id if isinstance(right_pane_id, str) else None,
            mounted_session=mounted_session if isinstance(mounted_session, str) else None,
            mounted_window_name=mounted_window_name,
        )

    def _write_window_state(
        self,
        window_state: CockpitWindowState,
        *,
        base: dict[str, object] | None = None,
    ) -> None:
        state = dict(base or self._load_state())
        if window_state.right_pane_id:
            state["right_pane_id"] = window_state.right_pane_id
        else:
            state.pop("right_pane_id", None)
        if window_state.mounted_session:
            state["mounted_session"] = window_state.mounted_session
        else:
            state.pop("mounted_session", None)
            state.pop("mounted_identity", None)
        self._write_state(state)

    def route_selected(self, key: str) -> None:
        # #967 follow-up — persist the user's intent BEFORE any
        # potentially-failing layout/mount work. The previous order
        # (ensure_cockpit_layout → set_selected_key) meant that any
        # exception in ensure_cockpit_layout / _right_pane_id / the
        # supervisor load left ``state["selected"]`` pinned at the
        # PREVIOUS click's key (typically ``inbox`` because that's where
        # users sit). When the cockpit's layout-recovery tick fires at
        # ~30s (``_LAYOUT_CHECK_INTERVAL`` in cockpit_ui), it triggers a
        # ``_refresh_rows`` that reads ``state["selected"]`` back into
        # the in-memory ``self.selected_key`` — bouncing the cursor +
        # right pane to the stale prior selection. Writing the new key
        # to disk first keeps the rail aligned with the user's intent
        # even when downstream work raises.
        state_before_route = self._load_state()
        previous_key = state_before_route.get("selected")
        self.set_selected_key(key)
        token = self._begin_layout_mutation()
        try:
            supervisor = self._load_supervisor()
            window_target = f"{supervisor.config.project.tmux_session}:{self._COCKPIT_WINDOW}"
            self.ensure_cockpit_layout()
            right_pane = self._right_pane_id(window_target)
            if right_pane is None:
                raise RuntimeError("Cockpit right pane is not available.")

            plan = resolve_cockpit_content(key, self._content_context(supervisor))
            self._record_live_mount_return_key(plan, previous_key)
            self._route_content_plan(supervisor, window_target, plan)
        finally:
            # #995 — guarantee a two-pane cockpit on every rail click.
            # The route work may park, kill, and re-mount panes via
            # ``_show_live_session`` / ``_show_command_view``. If any
            # intermediate step bails (tmux failure, race with the
            # heartbeat, exception in a fallback path), the cockpit
            # window can be left with a single pane: tmux's auto-resize
            # then expands the rail to full window width and the rail
            # TUI renders doubled because it never received a resize
            # signal sized for the new column count. Subsequent rail
            # clicks process keystrokes but the layout never recovers
            # because every code path assumes the rail has a sibling
            # right pane already. Re-running ``ensure_cockpit_layout``
            # here splits a fresh content pane (using
            # ``_default_repair_command`` so the surface matches the
            # user's intent) before releasing the layout-mutation lock.
            try:
                self._heal_layout_after_route()
            except Exception:  # noqa: BLE001
                pass
            self._end_layout_mutation(token)

    def _heal_layout_after_route(self) -> None:
        """Repair a degraded cockpit layout left over from a failed route.

        Inputs: none (reads tmux + cockpit state directly).
        Outputs: ``None``.
        Side effects: when the cockpit window has fewer than two live
        panes, calls ``ensure_cockpit_layout`` to split a fresh content
        pane via ``_default_repair_command``. When two panes are
        present, no-op.
        Invariants: never raises (callers wrap in best-effort try). Only
        triggers when the layout is observably bad — never bounces a
        healthy mount.
        """
        try:
            config = self._load_config()
        except Exception:  # noqa: BLE001
            return
        target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        try:
            panes = self.tmux.list_panes(target)
        except Exception:  # noqa: BLE001
            return
        live_panes = [
            pane for pane in panes if not getattr(pane, "pane_dead", False)
        ]
        if len(live_panes) >= 2:
            return  # healthy — leave alone
        # Layout is degraded. ``ensure_cockpit_layout`` will detect
        # ``len(panes) < 2`` and split a fresh content pane via the
        # context-aware ``_default_repair_command`` from #991. The
        # caller still holds the layout-mutation token, so this
        # recursive call sees ``_layout_mutation_active_elsewhere() ==
        # False`` and proceeds.
        try:
            self.ensure_cockpit_layout()
        except Exception:  # noqa: BLE001
            pass

    def focus_right_pane(self) -> None:
        config = self._load_config()
        window_target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        self.ensure_cockpit_layout()
        right_pane = self._right_pane_id(window_target)
        if right_pane is not None:
            self.tmux.run(
                "display-message",
                "-t",
                window_target,
                "PollyPM: Ctrl-b Left returns to the rail.",
                check=False,
            )
            self.tmux.select_pane(right_pane)

    def _auto_focus_right_after_live_mount(self, window_target: str) -> None:
        """Move tmux focus to the right pane after a live agent mount (#987).

        Called only from the live-agent dispatch paths in
        :meth:`_route_live_session` and :meth:`_route_live_agent_plan` —
        static views (project dashboards, Inbox, Workers, Metrics) keep
        rail focus on click. The Ctrl-h rail-recovery affordance from
        #985 (:meth:`focus_rail_pane`) still returns focus to the rail
        when the user wants to navigate further.
        Side effects: tmux ``select-pane`` to the right pane and a
        one-line ``display-message`` reminder for the recovery path.
        Layout is assumed already validated by the caller — we don't
        re-run ``ensure_cockpit_layout`` to keep this cheap and avoid
        re-entering the layout-mutation guard ``route_selected`` holds.
        """
        try:
            right_pane = self._right_pane_id(window_target)
        except Exception:  # noqa: BLE001
            return
        if right_pane is None:
            return
        try:
            self.tmux.run(
                "display-message",
                "-t",
                window_target,
                "PollyPM: Ctrl-b Left returns to the rail.",
                check=False,
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            self.tmux.select_pane(right_pane)
        except Exception:  # noqa: BLE001
            pass

    def focus_rail_pane(self) -> None:
        """Hand tmux focus back to the cockpit rail pane (#985).

        Called from right-pane Textual apps that want to surrender focus
        without exiting (e.g. ``Ctrl-h`` / ``Escape`` on the inbox).
        Without this, once tmux focuses the right pane there's no
        keyboard-only way back to the rail short of restarting the
        cockpit — j/k and Tab all land in the right pane app.
        """
        config = self._load_config()
        window_target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        self.ensure_cockpit_layout()
        rail_pane = self._left_pane_id(window_target)
        if rail_pane is not None:
            self.tmux.select_pane(rail_pane)

    def send_key_to_right_pane(self, key: str) -> None:
        config = self._load_config()
        window_target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        self.ensure_cockpit_layout()
        right_pane = self._right_pane_id(window_target)
        if right_pane is not None:
            self.tmux.run("send-keys", "-t", right_pane, key, check=False)

    def active_live_right_pane_id(self) -> str | None:
        """Return the right pane id when tmux focus is in a mounted live agent.

        ``pm cockpit-send-key`` normally enters through the rail's Textual
        input bridge. After a live chat mount, though, the cockpit
        intentionally moves tmux focus to the right pane so typing should
        reach Codex/Claude instead of rail hotkeys.
        """
        state = self._load_state()
        mounted = state.get("mounted_session")
        right_pane_id = state.get("right_pane_id")
        if not isinstance(mounted, str) or not mounted:
            return None
        if not isinstance(right_pane_id, str) or not right_pane_id:
            return None
        try:
            config = self._load_config()
            window_target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
            panes = self.tmux.list_panes(window_target)
        except Exception:  # noqa: BLE001
            return None
        active = next(
            (pane for pane in panes if getattr(pane, "active", False)),
            None,
        )
        if active is None or getattr(active, "pane_dead", False):
            return None
        if getattr(active, "pane_id", None) != right_pane_id:
            return None
        return right_pane_id

    def reload_cockpit_shell(
        self,
        *,
        kind: str = "settings",
        project_key: str | None = None,
        selected_key: str | None = None,
    ) -> None:
        """Reload the cockpit shell without shutting down any sessions.

        Inputs: the right-pane ``kind`` to relaunch, optional
        ``project_key`` for project-scoped panes, and an optional
        ``selected_key`` to persist in cockpit state.
        Outputs: ``None``.
        Side effects: re-parks any mounted session, respawns the left
        rail pane into its shell bootstrap, re-injects the cockpit TUI
        loop, and respawns the right pane back into the requested static
        view.
        Invariant: agent sessions remain alive in the storage closet;
        this path never tears down the tmux session.
        """
        supervisor = self._load_supervisor()
        tmux_session = supervisor.config.project.tmux_session
        window_target = f"{tmux_session}:{self._COCKPIT_WINDOW}"
        self.ensure_cockpit_layout()
        resolved_selected = selected_key or self._selection_key_for_static_view(
            kind, project_key,
        )
        self.set_selected_key(resolved_selected)
        self._park_mounted_session(supervisor, window_target)
        self._cleanup_extra_panes(window_target)
        left_pane_id = self._left_pane_id(window_target)
        right_pane_id = self._right_pane_id(window_target)
        if left_pane_id is None or right_pane_id is None:
            raise RuntimeError("Cockpit panes are not available for reload.")
        state = self._load_state()
        state["selected"] = resolved_selected
        state["right_pane_id"] = right_pane_id
        state.pop("mounted_session", None)
        state.pop("mounted_identity", None)
        self._write_state(state)
        self.tmux.respawn_pane(left_pane_id, supervisor.console_command())
        supervisor.start_cockpit_tui(tmux_session)
        # Respawn the current right pane last because this may replace
        # the process that is currently executing the reload request.
        self.tmux.respawn_pane(
            right_pane_id, self._right_pane_command(kind, project_key),
        )

    def create_worker_and_route(
        self,
        project_key: str,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        supervisor = self._load_supervisor()
        launches = supervisor.plan_launches()
        session_name = self._project_session_map(launches).get(project_key)
        storage_session = supervisor.storage_closet_session_name()
        storage_windows = {w.name for w in self.tmux.list_windows(storage_session)}

        target: str | None = None
        launch = None
        should_stabilize_visible = False
        if session_name is not None:
            launch = next(
                item for item in launches
                if item.session.name == session_name
            )
            if launch.window_name not in storage_windows:
                _launch, target = supervisor.create_session_window(session_name, on_status=on_status)
                launch = _launch
                should_stabilize_visible = target is not None
        else:
            prompt = self.service.suggest_worker_prompt(project_key=project_key)
            self.service.create_and_launch_worker(
                project_key=project_key, prompt=prompt, on_status=on_status, skip_stabilize=True,
            )
            # Re-read launches to pick up the newly created session
            supervisor = self._load_supervisor(fresh=True)
            launches = supervisor.plan_launches()
            session_name = self._project_session_map(launches).get(project_key)
            if session_name is not None:
                launch = next(
                    item for item in launches
                    if item.session.name == session_name
                )
                target_window = next(
                    (
                        window for window in self.tmux.list_windows(storage_session)
                        if window.name == launch.window_name
                    ),
                    None,
                )
                if target_window is not None:
                    source_pane_id = getattr(target_window, "pane_id", None)
                    target = (
                        source_pane_id
                        if isinstance(source_pane_id, str) and source_pane_id
                        else f"{storage_session}:{target_window.index}.0"
                    )
                    should_stabilize_visible = True

        # #964 — route to the project's PM Chat session when a worker is
        # known to exist (either pre-existing or freshly spawned above).
        # The previous implementation routed to ``project:{key}`` which
        # resolves to the static project Dashboard, leaving every PM
        # Chat sub-item dead-ending on Dashboard. Only fall back to the
        # project Dashboard when worker creation truly produced no
        # session — that path keeps the cockpit usable while the user
        # diagnoses the launch failure rather than surfacing a blank
        # right pane.
        if session_name is not None:
            if should_stabilize_visible and launch is not None:
                self._mount_created_worker_and_stabilize(
                    supervisor,
                    project_key=project_key,
                    session_name=session_name,
                    launch=launch,
                    target=target,
                    on_status=on_status,
                )
                return
            self.route_selected(f"project:{project_key}:session")
        else:
            self.route_selected(f"project:{project_key}")

    def _mount_created_worker_and_stabilize(
        self,
        supervisor,
        *,
        project_key: str,
        session_name: str,
        launch,
        target: str | None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        token = self._begin_layout_mutation()
        try:
            tmux_session = supervisor.config.project.tmux_session
            window_target = f"{tmux_session}:{self._COCKPIT_WINDOW}"
            self.ensure_cockpit_layout()
            self.set_selected_key(f"project:{project_key}:session")
            self._show_live_session(supervisor, session_name, window_target)
            right_pane_id = self._right_pane_id(window_target)
            stabilize_target = (
                target
                if isinstance(target, str) and target.startswith("%")
                else right_pane_id
            )
            if stabilize_target is not None:
                supervisor.stabilize_launch(launch, stabilize_target, on_status=on_status)
            self._maybe_prime_project_pm_session(
                supervisor, project_key, session_name, window_target,
            )
        finally:
            self._end_layout_mutation(token)

    def _maybe_prime_operator_session(
        self,
        supervisor,
        window_target: str,
    ) -> None:
        """Send a workspace-level identity primer the first time the
        operator (Polly) session is mounted at a given pane (#961).

        The operator session's launch-time prompt only fires on a fresh
        launch; long-running panes or resumed sessions land in the
        cockpit with no identity context. The primer is sent once per
        cockpit right-pane id, tracked under
        ``cockpit_state["operator_primed_pane"]``. Idempotent across
        re-mounts of the same pane so clicking ``Polly · chat``
        repeatedly does not spam the agent. When the cockpit restarts
        or the operator session is killed and respawned the right
        pane id changes, so the primer re-fires automatically — that
        is the regression path #961 reported. Failures are best-effort.
        Distinct from :meth:`_maybe_prime_project_pm_session` (#958)
        which targets per-project PM sessions; the workspace operator
        primer never carries a ``project_key``.
        """
        try:
            right_pane_id = self._right_pane_id(window_target)
            state = self._load_state()
            if (
                right_pane_id is not None
                and state.get("operator_primed_pane") == right_pane_id
            ):
                return
            primer = _build_operator_primer(supervisor)
            if not primer:
                return
            target = right_pane_id if right_pane_id else window_target
            try:
                self.tmux.send_keys(target, primer, press_enter=True)
            except Exception:  # noqa: BLE001
                return
            if right_pane_id is not None:
                state["operator_primed_pane"] = right_pane_id
                self._write_state(state)
        except Exception:  # noqa: BLE001
            return

    def _maybe_prime_project_pm_session(
        self,
        supervisor,
        project_key: str,
        session_name: str,
        window_target: str,
    ) -> None:
        """Send a project-context primer the first time a per-project PM
        session is mounted in the cockpit (#958).

        The primer is sent once per ``(cockpit-process, session_name)``
        pair, tracked in ``cockpit_state["pm_primed_sessions"]``. Idempotent
        across re-mounts so clicking PM Chat twice does not spam the agent.
        Failures are best-effort — the mount has already succeeded; a
        primer that does not land does not roll back attachment.
        """
        try:
            state = self._load_state()
            primed_raw = state.get("pm_primed_sessions")
            if isinstance(primed_raw, list):
                primed = set(primed_raw)
            else:
                primed = set()
            if session_name in primed:
                return
            primer = _build_project_pm_primer(supervisor, project_key)
            if not primer:
                return
            right_pane_id = self._right_pane_id(window_target)
            target = right_pane_id if right_pane_id else window_target
            try:
                self.tmux.send_keys(target, primer, press_enter=True)
            except Exception:  # noqa: BLE001
                return
            primed.add(session_name)
            state["pm_primed_sessions"] = sorted(primed)
            self._write_state(state)
        except Exception:  # noqa: BLE001
            return

    def _show_live_session(self, supervisor, session_name: str, window_target: str) -> None:
        # Mount-time identity check (replaces the legacy bare-string
        # ``mounted_session == session_name`` early-return). The legacy
        # check believed cockpit_state.json without verifying it
        # against tmux, and the CWD fallback in
        # ``_mounted_session_name`` would write *guesses* back as
        # ground truth — that's how clicking "Polly" sometimes mounted
        # Russell. See :mod:`pollypm.cockpit_mount_identity` for the
        # full design.
        from pollypm.cockpit_mount_identity import (
            MountedIdentity,
            expected_identity_for_session_name,
            identity_matches,
            make_mounted_identity,
            verify_mount_against_tmux,
        )

        expected = expected_identity_for_session_name(
            session_name, supervisor.config,
        )
        launch = next(item for item in supervisor.plan_launches() if item.session.name == session_name)
        if expected is not None:
            state = self._load_state()
            persisted = MountedIdentity.from_state_dict(state.get("mounted_identity"))
            if persisted is not None and identity_matches(persisted, expected):
                try:
                    panes = self.tmux.list_panes(window_target)
                    storage_session = supervisor.storage_closet_session_name()
                    storage_windows = self.tmux.list_windows(storage_session)
                except Exception:  # noqa: BLE001
                    panes = []
                    storage_windows = []
                if verify_mount_against_tmux(
                    persisted,
                    panes=panes,
                    storage_windows=storage_windows,
                ):
                    return  # tmux confirms the persisted identity
                # Mismatch — fall through to tear-down + remount fresh.
        self._park_mounted_session(supervisor, window_target)
        self._cleanup_extra_panes(window_target)
        left_pane_id = self._left_pane_id(window_target)
        if left_pane_id is None:
            raise RuntimeError("Cockpit left pane is not available.")
        right_pane_id = self._right_pane_id(window_target)
        storage_session = supervisor.storage_closet_session_name()
        storage_windows = {window.name for window in self.tmux.list_windows(storage_session)}
        if launch.window_name not in storage_windows:
            # Control sessions (operator, reviewer) should be respawned
            # when missing — the user clicking on Polly expects to talk to
            # Polly, not see a placeholder.
            if launch.session.role in {"operator-pm", "reviewer"}:
                try:
                    supervisor.launch_session(session_name)
                    storage_windows = {w.name for w in self.tmux.list_windows(storage_session)}
                    if launch.window_name in storage_windows:
                        # Look up the freshly-spawned window's index so
                        # the persisted identity records the
                        # disambiguating index, not just the name.
                        live_windows = self.tmux.list_windows(storage_session)
                        target_window_for_identity = next(
                            (w for w in live_windows if w.name == launch.window_name),
                            None,
                        )
                        source_pane_id = (
                            getattr(target_window_for_identity, "pane_id", None)
                            if target_window_for_identity is not None
                            else None
                        )
                        source = (
                            source_pane_id
                            if isinstance(source_pane_id, str) and source_pane_id
                            else f"{storage_session}:{launch.window_name}.0"
                        )
                        # #934 follow-up — fifth-layer rail-mount guard.
                        # If the source pane already shows another role's
                        # canonical banner (e.g. a long-running rail
                        # daemon or stale cockpit process wrote heartbeat
                        # content into a pane the cockpit later mounts as
                        # ``operator``), refuse the join. Falling back to
                        # the static view keeps the cockpit usable while
                        # the operator manually clears the bad pane.
                        if not self._source_pane_role_matches_launch(
                            source, launch,
                        ):
                            raise RuntimeError(
                                "persona_swap_detected: source pane "
                                "shows another role's banner — refusing "
                                "to join into cockpit."
                            )
                        right_pane = self._join_storage_pane_into_cockpit(
                            source=source,
                            source_pane_id=source_pane_id,
                            left_pane_id=left_pane_id,
                            previous_right_pane_id=right_pane_id,
                            window_target=window_target,
                        )
                        panes = self.tmux.list_panes(window_target)
                        left_pane = min(panes, key=self._pane_left)
                        self._try_resize_rail(left_pane.pane_id)
                        self.tmux.set_pane_history_limit(right_pane.pane_id, 200)
                        state = self._load_state()
                        state["mounted_session"] = session_name
                        state["right_pane_id"] = right_pane.pane_id
                        if expected is not None:
                            mounted = make_mounted_identity(
                                expected,
                                right_pane_id=right_pane.pane_id,
                                window_index=getattr(
                                    target_window_for_identity, "index", None,
                                ),
                            )
                            state["mounted_identity"] = mounted.to_state_dict()
                        else:
                            state.pop("mounted_identity", None)
                        self._write_state(state)
                        return
                except Exception:  # noqa: BLE001
                    pass  # Fall through to static view if relaunch fails
            # Non-control sessions or failed relaunch — show static view
            fallback_kind = "polly" if session_name == "operator" else "project"
            fallback_target = launch.session.project if fallback_kind == "project" else None
            if right_pane_id is None:
                right_size = self._right_pane_size(window_target)
                right_pane_id = self.tmux.split_window(
                    left_pane_id,
                    self._right_pane_command(fallback_kind, fallback_target),
                    horizontal=True,
                    detached=True,
                    size=right_size,
                )
            else:
                self.tmux.respawn_pane(right_pane_id, self._right_pane_command(fallback_kind, fallback_target))
            state = self._load_state()
            state.pop("mounted_session", None)
            state.pop("mounted_identity", None)
            state["right_pane_id"] = self._right_pane_id(window_target)
            self._write_state(state)
            return
        # Use window index to avoid ambiguity with duplicate window names
        storage_windows = self.tmux.list_windows(storage_session)
        target_window = next(
            (w for w in storage_windows if w.name == launch.window_name),
            None,
        )
        if target_window is None:
            fallback_kind = "polly" if session_name == "operator" else "project"
            fallback_target = launch.session.project if fallback_kind == "project" else None
            self._show_static_view(supervisor, window_target, fallback_kind, fallback_target)
            return
        source_pane_id = getattr(target_window, "pane_id", None)
        source = (
            source_pane_id
            if isinstance(source_pane_id, str) and source_pane_id
            else f"{storage_session}:{target_window.index}.0"
        )
        # #934 follow-up — fifth-layer rail-mount guard. Even after the
        # supervisor's four crossing guards, a stale storage-closet pane
        # can carry another role's banner (e.g. an old rail-daemon
        # process running pre-#931 code wrote heartbeat content into a
        # pane the cockpit later mounts as ``operator``). Capture the
        # source pane and refuse the mount when its content is
        # unambiguously someone else's. Falls back to the static view so
        # the cockpit stays usable while the operator clears the bad
        # pane manually.
        if not self._source_pane_role_matches_launch(source, launch):
            fallback_kind = "polly" if session_name == "operator" else "project"
            fallback_target = launch.session.project if fallback_kind == "project" else None
            self._show_static_view(supervisor, window_target, fallback_kind, fallback_target)
            return
        right_pane = self._join_storage_pane_into_cockpit(
            source=source,
            source_pane_id=source_pane_id,
            left_pane_id=left_pane_id,
            previous_right_pane_id=right_pane_id,
            window_target=window_target,
        )
        panes = self.tmux.list_panes(window_target)
        left_pane = min(panes, key=self._pane_left)
        self._try_resize_rail(left_pane.pane_id)
        self.tmux.set_pane_history_limit(right_pane.pane_id, 200)
        state = self._load_state()
        # Persist both the legacy bare-string ``mounted_session`` (for
        # back-compat with code paths that still read it) and the new
        # typed ``mounted_identity`` record. The typed record is what
        # the next ``_show_live_session`` will validate against tmux
        # — see :mod:`pollypm.cockpit_mount_identity`.
        state["mounted_session"] = session_name
        state["right_pane_id"] = right_pane.pane_id
        if expected is not None:
            mounted = make_mounted_identity(
                expected,
                right_pane_id=right_pane.pane_id,
                window_index=getattr(target_window, "index", None),
            )
            state["mounted_identity"] = mounted.to_state_dict()
        else:
            state.pop("mounted_identity", None)
        self._write_state(state)
        # Auto-claim a cockpit lease so the heartbeat won't send
        # nudges while a human is viewing/typing in this session.
        try:
            supervisor.claim_lease(session_name, "cockpit", "mounted in cockpit")
        except Exception:  # noqa: BLE001
            pass  # Lease may conflict — best effort

    def _should_boot_visible(self, launch) -> bool:
        if launch.session.name in self._ui_initialized_sessions():
            return False
        return launch.session.provider.value in {"claude", "codex"}

    def _launch_visible_session(self, supervisor, launch, window_target: str, left_pane_id: str, right_pane_id: str | None):
        storage_session = supervisor.storage_closet_session_name()
        for window in self.tmux.list_windows(storage_session):
            if window.name == launch.window_name:
                self.tmux.kill_window(f"{storage_session}:{window.index}")
                break
        visible_launch = launch
        if launch.session.provider.value == "codex" and launch.initial_input:
            provider = get_provider(launch.session.provider, root_dir=supervisor.config.project.root_dir)
            runtime = get_runtime(launch.account.runtime, root_dir=supervisor.config.project.root_dir)
            visible_command = provider.build_launch_command(launch.session, launch.account)
            visible_launch = replace(
                launch,
                command=runtime.wrap_command(visible_command, launch.account, supervisor.config.project),
                initial_input=visible_command.initial_input,
                resume_marker=visible_command.resume_marker,
                fresh_launch_marker=visible_command.fresh_launch_marker,
            )
        if right_pane_id is not None:
            self.tmux.kill_pane(right_pane_id)
        right_size = self._right_pane_size(window_target)
        right_pane_id = self.tmux.split_window(
            left_pane_id,
            visible_launch.command,
            horizontal=True,
            detached=False,
            size=right_size,
        )
        self.tmux.set_pane_history_limit(right_pane_id, 200)
        self.tmux.pipe_pane(right_pane_id, visible_launch.log_path)
        supervisor.stabilize_launch(visible_launch, right_pane_id)
        return max(self.tmux.list_panes(window_target), key=self._pane_left)

    def _park_mounted_session(self, supervisor, window_target: str) -> None:
        state = self._load_state()
        mounted_session = self._mounted_session_name(supervisor, window_target)
        if not isinstance(mounted_session, str) or not mounted_session:
            return
        right_pane_id = self._right_pane_id(window_target)
        if right_pane_id is None:
            state.pop("mounted_session", None)
            state.pop("mounted_identity", None)
            self._write_state(state)
            self._release_cockpit_lease(supervisor, mounted_session)
            return
        right_pane = max(self.tmux.list_panes(window_target), key=self._pane_left)
        if not self._is_live_provider_pane(right_pane):
            state.pop("mounted_session", None)
            state.pop("mounted_identity", None)
            self._write_state(state)
            self._release_cockpit_lease(supervisor, mounted_session)
            return
        window_name = self._mounted_window_name(supervisor, mounted_session)
        if window_name is None:
            state.pop("mounted_session", None)
            state.pop("mounted_identity", None)
            self._write_state(state)
            self._release_cockpit_lease(supervisor, mounted_session)
            return
        storage_session = supervisor.storage_closet_session_name()
        before = {(window.index, window.name) for window in self.tmux.list_windows(storage_session)}
        self.tmux.break_pane(right_pane_id, storage_session, window_name)
        after = self.tmux.list_windows(storage_session)
        created = [window for window in after if (window.index, window.name) not in before]
        if created:
            self.tmux.rename_window(f"{storage_session}:{created[-1].index}", window_name)
        else:
            for window in after:
                if window.name == self._COCKPIT_WINDOW:
                    self.tmux.rename_window(f"{storage_session}:{window.index}", window_name)
                    break
        state.pop("mounted_session", None)
        state.pop("mounted_identity", None)
        self._write_state(state)
        self._release_cockpit_lease(supervisor, mounted_session)

    # Roles that should NEVER be auto-detected as mounted via CWD fallback.
    # These are background roles — if the user is looking at a pane, it's
    # not the heartbeat.  Guessing wrong here causes cascading mis-parks.
    _NEVER_MOUNT_ROLES = frozenset({"heartbeat-supervisor", "triage"})

    def _source_pane_role_matches_launch(
        self, source: str, launch
    ) -> bool:
        """Return True iff ``source`` pane's content is consistent with ``launch``'s role.

        Fifth-layer crossing guard for the cockpit join path (#931–#934).
        Even after the supervisor's four crossing guards on the kickoff
        send path, a stale storage-closet pane can be left over — for
        example, if a long-running rail-daemon process is running pre-#931
        code that happily wrote heartbeat content into a pane the cockpit
        later mounts as ``operator``. Before joining a pane into the
        cockpit window, capture its scrollback and refuse the join when
        the pane shows another role's canonical banner.

        The check is conservative on every failure (capture exception,
        empty pane, no banner present): returns ``True`` so a legitimate
        fresh pane (which has not yet rendered its banner) is allowed
        through. The guard only fires when an UNAMBIGUOUS banner for a
        DIFFERENT role appears — that's the verifiable persona-swap
        signal #931 already trusts.
        """
        expected_role = getattr(getattr(launch, "session", None), "role", None)
        if not expected_role:
            return True
        try:
            pane = self.tmux.capture_pane(source, lines=120)
        except Exception:  # noqa: BLE001
            return True
        if not pane or "CANONICAL ROLE:" not in pane:
            return True
        observed_roles: list[str] = []
        for line in pane.splitlines():
            stripped = line.strip()
            if stripped.startswith("CANONICAL ROLE:"):
                observed = stripped.split(":", 1)[1].strip()
                if observed:
                    observed_roles.append(observed)
        if not observed_roles:
            return True
        # Mismatch only when *every* observed banner names a different
        # role. If our role's banner is among them, the pane already
        # belongs to the right session (idempotent re-mount).
        if any(observed == expected_role for observed in observed_roles):
            return True
        details = (
            f"source={source!r} expected_role={expected_role!r} "
            f"observed_roles={observed_roles!r} "
            f"session_name={getattr(launch.session, 'name', None)!r} "
            f"window_name={getattr(launch, 'window_name', None)!r}"
        )
        try:
            import logging as _logging
            _logging.getLogger("pollypm.cockpit_rail").error(
                "persona_swap_detected (rail-mount): %s — refusing to "
                "join a pane whose content shows a different role's banner",
                details,
            )
        except Exception:  # noqa: BLE001
            pass
        # Best-effort event so the operator-visible diagnostic surfaces
        # without the user having to debug from the agent transcript.
        # Routes through the public ``record_persona_swap_diagnostic``
        # method (added in #940) — never touch ``supervisor._msg_store``
        # directly: that violates the Supervisor private-reach-through
        # boundary enforced by ``tests/test_import_boundary.py``.
        try:
            supervisor = self._load_supervisor()
            supervisor.record_persona_swap_diagnostic(
                scope=getattr(launch.session, "name", "operator"),
                message=(
                    f"rail-mount source-pane guard refused join: {details}"
                ),
            )
        except Exception:  # noqa: BLE001
            pass
        return False

    # When CWD is ambiguous (multiple sessions share the same cwd), prefer
    # the session the user is most likely interacting with.
    _MOUNT_PRIORITY = {"operator-pm": 0, "reviewer": 1, "worker": 2}

    def _mounted_window_name(self, supervisor, session_name: str) -> str | None:
        launch = next(
            (item for item in supervisor.plan_launches() if item.session.name == session_name),
            None,
        )
        if launch is not None:
            return launch.window_name
        if _task_mount_parts(session_name) is not None:
            return session_name
        return None

    def _mounted_session_name(self, supervisor, window_target: str) -> str | None:
        """Return the session_name currently mounted in the cockpit's right pane.

        Source of truth: the typed ``mounted_identity`` record in
        ``cockpit_state.json``, falling back to the legacy bare-string
        ``mounted_session`` for back-compat with state files written
        by older builds. **Never** guesses from CWD: the prior
        priority-based CWD fallback (operator > reviewer > worker)
        used to write its guess back as ground truth, which was the
        structural cause of the "click Polly, see Russell" persona-
        binding bug. If the state has nothing to say, return None and
        let the caller force a fresh mount.
        """
        state = self._load_state()
        identity_raw = state.get("mounted_identity")
        if isinstance(identity_raw, dict):
            session = identity_raw.get("session_name")
            if isinstance(session, str) and session:
                return session
        # Back-compat: pre-rewrite state files only have the bare string.
        mounted_session = state.get("mounted_session")
        if isinstance(mounted_session, str) and mounted_session:
            return mounted_session
        return None

    def _is_live_provider_pane(self, pane) -> bool:
        cmd = getattr(pane, "pane_current_command", "")
        # Claude Code may report the version string (e.g. "2.1.98") as the
        # current command instead of "claude" or "node".
        if cmd in {"node", "claude", "codex"}:
            return True
        # Treat any version-like string (digits and dots) as a live Claude pane.
        if cmd and all(c.isdigit() or c == "." for c in cmd):
            return True
        return False

    def _session_available_for_mount(self, supervisor, session_name: str, window_target: str) -> bool:
        """Return True only if the session is already running (mounted or in storage)."""
        mounted = self._mounted_session_name(supervisor, window_target)
        if mounted == session_name:
            return True
        launch = next((item for item in supervisor.plan_launches() if item.session.name == session_name), None)
        if launch is None:
            return False
        storage_session = supervisor.storage_closet_session_name()
        storage_windows = {window.name for window in self.tmux.list_windows(storage_session)}
        return launch.window_name in storage_windows

    def _show_static_view(
        self,
        supervisor,
        window_target: str,
        kind: str,
        project_key: str | None = None,
        task_id: str | None = None,
    ) -> None:
        self._show_command_view(
            supervisor,
            window_target,
            self._right_pane_command(kind, project_key, task_id=task_id),
        )

    def _show_message_view(
        self,
        supervisor,
        window_target: str,
        title: str,
        message: str,
    ) -> None:
        body = f"{title}\n\n{message}\n\nSelect another rail item to continue."
        script = f"printf '%s\\n' {shlex.quote(body)}; while IFS= read -r _; do :; done"
        self._show_command_view(
            supervisor,
            window_target,
            f"sh -lc {shlex.quote(script)}",
        )

    def _show_command_view(
        self,
        supervisor,
        window_target: str,
        command: str,
    ) -> None:
        self._park_mounted_session(supervisor, window_target)
        state = self._load_state()
        state.pop("mounted_return_key", None)
        result = self._window_manager(supervisor).show_static(
            command,
            self._window_state_from_cockpit_state(supervisor, state),
        )
        if not result.ok:
            raise RuntimeError(
                "Cockpit pane layout is invalid after static route: "
                + "; ".join(result.postcondition.errors)
            )
        self._write_window_state(result.state, base=state)

    def _cleanup_extra_panes(self, window_target: str) -> None:
        """Kill any extra panes beyond the expected 2 (rail + right)."""
        try:
            panes = self.tmux.list_panes(window_target)
        except Exception:  # noqa: BLE001
            return
        if len(panes) <= 2:
            return
        left_pane = min(panes, key=self._pane_left)
        # Keep the leftmost (rail) and rightmost (content) panes, kill the rest
        right_pane = max(panes, key=self._pane_left)
        for pane in panes:
            if pane.pane_id not in {left_pane.pane_id, right_pane.pane_id}:
                try:
                    self.tmux.kill_pane(pane.pane_id)
                except Exception:  # noqa: BLE001
                    pass

    def _selection_key_for_static_view(
        self, kind: str, project_key: str | None = None,
    ) -> str:
        if kind == "project" and project_key:
            return f"project:{project_key}:dashboard"
        if kind == "settings" and project_key:
            return f"project:{project_key}:settings"
        if kind == "issues" and project_key:
            return f"project:{project_key}:issues"
        if kind == "activity" and project_key:
            return f"activity:{project_key}"
        return kind

    def _default_repair_command(self, state: dict[str, object] | None) -> str:
        """Return the right-pane command to use when repairing a degraded
        cockpit layout (e.g. ``ensure_cockpit_layout`` finds <2 panes).

        Inputs: the cockpit state dict (so a fresh-load isn't required).
        Outputs: a shell command string the caller can hand to
        ``tmux split-window``.
        Side effects: none.

        Without this helper, the repair split hardcoded
        ``pm cockpit-pane polly``. When the user's intent is on a project
        (``selected = project:<key>:session`` or any project sub-item),
        a layout-recovery split followed by a failed mount left the Polly
        workspace dashboard visible — the exact fallthrough #991 reports
        for architect-only projects. Routing the repair split to a
        project-aware command (the project's static dashboard for
        project routes; Polly only when the selection itself is workspace
        scoped) keeps a partial repair on the user's intended surface.
        """
        selected: object | None = None
        if isinstance(state, dict):
            selected = state.get("selected")
        if isinstance(selected, str) and selected.startswith("project:"):
            parts = selected.split(":")
            if len(parts) >= 2 and parts[1]:
                project_key = parts[1]
                # ``settings``/``issues`` panes have their own kinds, but
                # the project static dashboard is a sane neutral surface
                # while the actual mount finishes — every project sub
                # route legitimately overlays the project dashboard.
                return self._right_pane_command("project", project_key)
        if isinstance(selected, str):
            if selected in {"dashboard", "inbox", "workers", "metrics", "activity", "settings"}:
                return self._right_pane_command(selected)
            if selected.startswith("inbox:"):
                return self._right_pane_command("inbox", selected.split(":", 1)[1])
            if selected.startswith("activity:"):
                return self._right_pane_command("activity", selected.split(":", 1)[1])
            if selected == "polly":
                return self._right_pane_command("polly")
        return self._right_pane_command("dashboard")

    def _right_pane_command(
        self,
        kind: str,
        project_key: str | None = None,
        *,
        task_id: str | None = None,
    ) -> str:
        root = str(self.config_path.parent.resolve())
        package_parent = Path(__file__).resolve().parents[1]
        python_path = str(package_parent)
        existing_python_path = os.environ.get("PYTHONPATH")
        if existing_python_path:
            python_path = os.pathsep.join([python_path, existing_python_path])
        args = [
            "env",
            "POLLYPM_HOLD_UNUSABLE_DATABASE_SCREEN=1",
            f"PYTHONPATH={python_path}",
            sys.executable,
            "-m",
            "pollypm",
            "cockpit-pane",
            kind,
        ]
        if project_key is not None:
            # #751 — inbox and activity both use --project to scope
            # their view. Other views (settings/issues/project) use
            # the positional target argument because they mount the
            # per-project screen directly, not a scoped filter.
            if kind in {"inbox", "activity"}:
                args.extend(["--project", project_key])
            else:
                args.append(project_key)
        if task_id is not None:
            args.extend(["--task", task_id])
        joined = shlex.join(args)
        # #986 — ``exec`` replaces the shell with the pane CLI process so
        # tmux's ``respawn-pane -k`` SIGKILL hits the Python child
        # directly. Without ``exec`` the shell parent is killed but its
        # Python child survives, reparenting to PID 1 across cockpit
        # kill+restart cycles. Each respawn would then leak one
        # cockpit-pane orphan that holds open file handles, races the
        # fresh cockpit's right-pane app, and persists across boots.
        inner = f"cd {shlex.quote(root)} && exec {joined}"
        return f"sh -lc {shlex.quote(inner)}"


def focus_cockpit_rail_pane(config_path: Path) -> bool:
    """Hand tmux focus from the right pane back to the cockpit rail (#985).

    Inputs: the active cockpit ``config_path`` (used to resolve the tmux
    session + cockpit window).
    Outputs: ``True`` when a select-pane command was issued, ``False``
    when the cockpit layout isn't there or any tmux call raised.
    Side effects: a single ``tmux select-pane`` against the rail pane id.
    Invariants: never raises — right-pane apps call this from key
    bindings and a missing tmux session must not crash the inbox.

    Without this helper, once the user's tmux client focuses the right
    pane (via mouse click or the rail's Tab forward) every j/k/Enter
    keystroke is consumed by whatever ``pm cockpit-pane <kind>`` app is
    running there. Escape inside the inbox calls ``self.exit()`` which
    tears down the inbox app but leaves tmux focus on the right pane,
    so the rail still can't see keys until the user issues a tmux
    prefix command. Right-pane apps bind a key to this helper so the
    user gets back to the rail with one keystroke.
    """
    try:
        router = CockpitRouter(config_path)
    except Exception:  # noqa: BLE001
        return False
    try:
        router.focus_rail_pane()
    except Exception:  # noqa: BLE001
        return False
    return True


# ── Render row ───────────────────────────────────────────────────────────────

@dataclass(slots=True)
class RenderRow:
    text: str
    fg: _C = field(default_factory=lambda: PALETTE["item_normal"])
    bg: _C = field(default_factory=lambda: PALETTE["bg"])
    bold: bool = False


def _sgr(row: RenderRow) -> str:
    fr, fg, fb = row.fg
    br, bg, bb = row.bg
    bold = "1;" if row.bold else ""
    return f"\x1b[{bold}38;2;{fr};{fg};{fb};48;2;{br};{bg};{bb}m"


# ── Rail renderer ────────────────────────────────────────────────────────────

class PollyCockpitRail:
    def __init__(self, config_path: Path) -> None:
        self.router = CockpitRouter(config_path)
        self.presence = CockpitPresence(self.router.tmux)
        self.selected_key = self.router.selected_key()
        self.spinner_index = 0
        self.slogan_started_at = time.time()
        self._ticker_started_at = time.monotonic()
        self._last_items: list[CockpitItem] = []
        self._slogan_phase = 0

    def run(self) -> None:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            self._write("\x1b[?25l")
            while True:
                self.router.ensure_cockpit_layout()
                # #751 — pick up selection changes made by external
                # routers so the rail highlight tracks them.
                self._sync_selection_from_router()
                items = self.router.build_items(spinner_index=self.spinner_index)
                self._last_items = items
                self._clamp_selection(items)
                self._render(items)
                if self.presence.should_animate():
                    self.spinner_index = (self.spinner_index + 1) % 4
                self._tick_slogan()
                ready, _, _ = select.select([sys.stdin], [], [], 1.0)
                if not ready:
                    continue
                key = os.read(fd, 32)
                if not key:
                    continue
                if not self._handle_key(key, items):
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            self._write("\x1b[0m\x1b[?25h")

    def _sync_selection_from_router(self) -> None:
        """Pick up selection changes made by external routers (#751).

        The persisted selection at ``~/.pollypm/.cockpit-state.json``
        is the canonical state — it's updated by every call to
        ``CockpitRouter.set_selected_key``, including calls from apps
        running in the right pane (e.g. PollyProjectDashboardApp's
        jump-to-inbox flow). Without this sync, the rail's in-memory
        ``selected_key`` only tracked keys pressed on the rail itself,
        so the user saw the old highlight persist after navigating
        from elsewhere.
        """
        try:
            external_key = self.router.selected_key()
        except Exception:  # noqa: BLE001
            return
        if external_key and external_key != self.selected_key:
            self.selected_key = external_key

    def _handle_key(self, key: bytes, items: list[CockpitItem]) -> bool:
        if key in {b"q", b"\x03"}:
            return False
        if key in {b"j", b"\x1b[B"}:
            if self.selected_key == "settings":
                self._send_key_to_settings_pane("j")
                return True
            self._move(1, items)
            return True
        if key in {b"k", b"\x1b[A"}:
            if self.selected_key == "settings":
                self._send_key_to_settings_pane("k")
                return True
            self._move(-1, items)
            return True
        if key in {b"g", b"\x1b[H"}:
            self._select_first(items)
            return True
        if key in {b"G", b"\x1b[F"}:
            self._select_last(items)
            return True
        if key in {b"\r", b"\n"}:
            self.router.route_selected(self.selected_key)
            return True
        if key in {b"n", b"N"} and self.selected_key.startswith("project:"):
            self.router.create_worker_and_route(self.selected_key.split(":", 1)[1])
            return True
        if key in {b"s", b"S"}:
            self.router.route_selected("settings")
            self.selected_key = "settings"
            return True
        if key in {b"i", b"I"}:
            self.router.route_selected("inbox")
            self.selected_key = "inbox"
            return True
        if key in {b"t", b"T"}:
            self.router.route_selected("activity")
            self.selected_key = "activity"
            return True
        if key == b"\t":
            self.router.send_key_to_right_pane("Tab")
            return True
        if key == b"A":
            if self.selected_key == "workers":
                self.router.send_key_to_right_pane("A")
            else:
                self.router.route_selected("workers")
                self.selected_key = "workers"
            return True
        if key in {b"p", b"P"} and self.selected_key.startswith("project:"):
            project_key = self.selected_key.split(":", 2)[1]
            self.router.toggle_pinned_project(project_key)
            return True
        if key in {b"\x0b"}:
            # Raw rail has no palette UI of its own; jump to settings so
            # the user's ctrl-k habit still lands in the palette-enabled pane.
            self.router.route_selected("settings")
            self.selected_key = "settings"
            return True
        return True

    def _send_key_to_settings_pane(self, key: str) -> None:
        delivered = None
        try:
            from pollypm.cockpit_input_bridge import send_key_to_first_live
            delivered = send_key_to_first_live(
                self.router.config_path, key, kind="settings", timeout=0.2,
            )
        except Exception:  # noqa: BLE001
            delivered = None
        if delivered is not None:
            return
        try:
            self.router.send_key_to_right_pane(key)
        except Exception:  # noqa: BLE001
            pass

    # ── Navigation ───────────────────────────────────────────────────────

    def _move(self, delta: int, items: list[CockpitItem]) -> None:
        keys = [item.key for item in items if item.selectable]
        if not keys:
            return
        try:
            index = keys.index(self.selected_key)
        except ValueError:
            self.selected_key = keys[0]
            self.router.set_selected_key(self.selected_key)
            return
        self.selected_key = keys[(index + delta) % len(keys)]
        self.router.set_selected_key(self.selected_key)

    def _select_first(self, items: list[CockpitItem]) -> None:
        for item in items:
            if item.selectable:
                self.selected_key = item.key
                self.router.set_selected_key(self.selected_key)
                return

    def _select_last(self, items: list[CockpitItem]) -> None:
        for item in reversed(items):
            if item.selectable:
                self.selected_key = item.key
                self.router.set_selected_key(self.selected_key)
                return

    def _clamp_selection(self, items: list[CockpitItem]) -> None:
        keys = {item.key for item in items if item.selectable}
        if self.selected_key not in keys and keys:
            self.selected_key = next(iter(keys))
            self.router.set_selected_key(self.selected_key)

    # ── Slogan rotation ─────────────────────────────────────────────────

    def _tick_slogan(self) -> None:
        elapsed = time.time() - self.slogan_started_at
        cycle = 60
        pos = elapsed % cycle
        if pos >= cycle - 1:
            self._slogan_phase = -1  # fade out
        elif pos < 1:
            self._slogan_phase = 1   # fade in
        else:
            self._slogan_phase = 0   # normal

    def _current_slogan(self) -> tuple[str, str]:
        index = int((time.time() - self.slogan_started_at) // 60) % len(POLLY_SLOGANS)
        return POLLY_SLOGANS[index]

    def _slogan_color(self) -> _C:
        if self._slogan_phase != 0:
            return PALETTE["slogan_fade"]
        return PALETTE["slogan"]

    # ── Rendering ────────────────────────────────────────────────────────

    def _render(self, items: list[CockpitItem]) -> None:
        size = shutil.get_terminal_size((30, 24))
        width = max(16, size.columns)
        height = max(8, size.lines)
        lines: list[RenderRow] = []
        pad = " " * GUTTER

        # ── Wordmark (centered)
        lines.append(RenderRow(""))
        wm0 = ASCII_POLLY[0]
        wm1 = ASCII_POLLY[1]
        lines.append(RenderRow(wm0.center(width)[:width], fg=PALETTE["wordmark_hi"], bold=True))
        lines.append(RenderRow(wm1.center(width)[:width], fg=PALETTE["wordmark_lo"]))
        lines.append(RenderRow(""))

        # ── Slogan (centered)
        slogan = self._current_slogan()
        sc = self._slogan_color()
        lines.append(RenderRow(slogan[0].center(width)[:width], fg=sc))
        lines.append(RenderRow(slogan[1].center(width)[:width], fg=sc))
        lines.append(RenderRow(""))

        # ── Section: navigation
        settings_item = next((item for item in items if item.key == "settings"), None)
        body_items = [item for item in items if item.key != "settings"]
        active_view = self.router.selected_key()

        first_project = True
        for item in body_items:
            if item.key.startswith("project:") and first_project:
                lines.append(RenderRow(""))
                lines.append(self._section_header("projects", width))
                first_project = False
            lines.append(self._item_row(item, width, active_view))

        # ── Spacer + settings at bottom
        if settings_item is not None:
            target_lines = len(lines) + 4  # section header + blank + item + hint
            while len(lines) < height - target_lines + len(lines):
                if len(lines) >= height - 4:
                    break
                lines.append(RenderRow(""))
            lines.append(RenderRow(""))
            lines.append(self._section_header("system", width))
            lines.append(self._item_row(settings_item, width, active_view))

        ticker_text = self._event_ticker_text()
        reserve = 3 if ticker_text else 2
        while len(lines) < height - reserve:
            lines.append(RenderRow(""))
        lines.append(RenderRow(""))
        if ticker_text:
            lines.append(RenderRow(ticker_text[:width], fg=PALETTE["hint"]))
        # Compact hint: full keymap is one ``?`` away (#790). Width
        # budget is the 30-col rail, so drop everything but the most
        # frequently-used actions.
        hint = f"{pad}j/k \u21b5open \u00b7 ? help \u00b7 q quit"
        lines.append(RenderRow(hint[:width], fg=PALETTE["hint"]))

        # ── Flush
        bg = PALETTE["bg"]
        clear_line = f"\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m" + " " * width + "\x1b[0m"
        self._write("\x1b[H")
        for i in range(height):
            if i < len(lines):
                row = lines[i]
                if isinstance(row, _RawRow):
                    self._write(_sgr(row) + row.text + "\x1b[0m\r\n")
                else:
                    self._write(_sgr(row) + row.text.ljust(width)[:width] + "\x1b[0m\r\n")
            else:
                self._write(clear_line + "\r\n")

    # Heartbeat ticks and token-ledger syncs flood the events stream
    # but carry no user-facing signal (#793, #876). Mirrors the
    # PollyCockpitApp suppression list so the headless rail behaves
    # the same as the Textual cockpit when the user runs ``pm rail``.
    _TICKER_SUPPRESSED_EVENT_TYPES = frozenset({
        "heartbeat",
        "token_ledger",
        "lease",
        "launch",
        "recovered",
        "recovery_prompt",
        "recovery_recommendation",
        "scheduler",
        "operator_lease",
    })

    _TICKER_EVENT_LABELS: dict[str, str] = {
        "alert": "alert",
        "task_assigned": "task assigned",
        "task_completed": "task done",
        "plan_approved": "plan ok",
        "plan_rejected": "plan rejected",
        "rework_requested": "rework",
        "review_requested": "review",
    }

    def _event_ticker_text(self) -> str:
        # #656 gate: use the router's CockpitPresence so tmux detach
        # actually pauses the ticker. ``sys.stdin.isatty()`` stays True
        # across detach and was burning CPU with no viewer.
        try:
            if not self.router._presence().is_tmux_attached():
                return ""
        except Exception:  # noqa: BLE001
            pass
        try:
            supervisor = self.router._load_supervisor()
            raw_events = list(supervisor.store.recent_events(limit=48))
        except Exception:  # noqa: BLE001
            return ""
        events = [
            e for e in raw_events
            if getattr(e, "event_type", "") not in self._TICKER_SUPPRESSED_EVENT_TYPES
        ]
        if not events:
            return ""
        # #667 acceptance: cycle a window of the 3 newest events.
        window_size = min(3, len(events))
        offset = int((time.monotonic() - self._ticker_started_at) // 10)
        cycled = [events[(offset + i) % len(events)] for i in range(window_size)]
        labels: list[str] = []
        for event in cycled:
            event_type = getattr(event, "event_type", "event")
            label = self._TICKER_EVENT_LABELS.get(
                event_type, event_type.replace("_", " "),
            )
            if label not in labels:
                labels.append(label)
        return _format_event_ticker(labels)

    def _section_header(self, label: str, width: int) -> RenderRow:
        pad = " " * GUTTER
        prefix = f"{pad}\u2500\u2500 {label.upper()} "
        remaining = width - len(prefix)
        rule = "\u2500" * max(0, remaining)
        return RenderRow((prefix + rule)[:width], fg=PALETTE["section_label"])

    def _item_row(self, item: CockpitItem, width: int, active_view: str) -> RenderRow:
        is_selected = item.key == self.selected_key
        is_active = item.key == active_view
        indicator, ind_color = self._indicator(item)

        # Build the text with gutter (2-char prefix for alignment)
        bar = "\u258c " if is_selected else "  "
        label = item.label
        if item.key.startswith("project:"):
            label = _strip_trailing_spark(label)[0]
        indicator_width = max(1, len(indicator))
        max_label = width - (5 + indicator_width)
        if len(label) > max_label and max_label > 3:
            label = label[: max_label - 1] + "\u2026"
        text = f" {bar}{indicator} {label}"
        text = text[:width]

        # Determine colors
        fg = PALETTE["item_normal"]
        bg = PALETTE["bg"]
        bold = False

        if item.state.startswith("!"):
            # #989 — Pick the amber palette for warn-tier alerts so
            # they read as "needs a click" rather than "account repair".
            if item.alert_severity == "warn":
                fg = PALETTE["warn_text"]
                bg = PALETTE["warn_bg"]
            else:
                fg = PALETTE["alert_text"]
                bg = PALETTE["alert_bg"]
        elif (item.state.endswith("live") or item.state.endswith("working") or item.state == "watch") and not is_selected:
            fg = PALETTE["live_text"]
            bg = PALETTE["live_bg"]

        if is_selected:
            fg = PALETTE["sel_text"]
            bg = PALETTE["sel_bg"]
            bold = True
        elif is_active and not is_selected:
            fg = PALETTE["sel_text"]
            bg = PALETTE["active_bg"]

        row = RenderRow(text, fg=fg, bg=bg, bold=bold)

        # Apply indicator color inline via ANSI if indicator is present
        if ind_color and indicator.strip():
            # Rebuild text with colored indicator
            bar_sgr = ""
            if is_selected:
                bar_sgr = f"\x1b[38;2;{PALETTE['sel_accent'][0]};{PALETTE['sel_accent'][1]};{PALETTE['sel_accent'][2]}m"
            label_part = f" {label}"
            ind_sgr = f"\x1b[38;2;{ind_color[0]};{ind_color[1]};{ind_color[2]}m"
            text_sgr = _sgr(row)
            row.text = f" {bar_sgr}{bar}\x1b[0m{text_sgr}{ind_sgr}{indicator} \x1b[0m{text_sgr}{label_part}"
            # Pad manually since we have inline escapes
            visible_len = 1 + 1 + len(indicator) + 1 + len(label)
            if visible_len < width:
                row.text += " " * (width - visible_len)
            row.text = row.text  # already formatted
            # Return a special row that writes raw (skip _sgr in render)
            return _RawRow(row.text, fg=fg, bg=bg, bold=bold)

        return row

    def _indicator(self, item: CockpitItem) -> tuple[str, _C | None]:
        if item.key.startswith("project:"):
            if item.state == "project-red":
                # #989 \u2014 The project rollup paints ``project-red`` for
                # both warn and error alerts; pick the amber indicator
                # when the open alerts are warn-tier so the project
                # badge matches the row palette.
                color = (
                    PALETTE["warn_indicator"]
                    if item.alert_severity == "warn"
                    else PALETTE["alert_indicator"]
                )
                return "\u25b2", color
            if item.state == "project-yellow":
                # #1092 \u2014 use \u25c6 to match the dashboard's "needs attention"
                # diamond. ``\u2022`` (U+2022) and the idle ``\u00b7`` (U+00B7) are
                # visually indistinguishable in many terminal fonts, so a
                # held-task project read as idle in the rail.
                return "\u25c6", PALETTE["inbox_has"]
            if item.state == "project-green":
                return "\u2022", PALETTE["live_indicator"]
            if item.state == "project-working":
                return "\u2022", PALETTE["live_indicator"]
        if item.session_name and item.work_state:
            pulse = self.presence.heartbeat_frame_for(
                item.session_name,
                item.heartbeat_at,
            )
            work_glyph, color = self._session_work_glyph(
                item.work_state, alert_severity=item.alert_severity,
            )
            return f"{pulse}{work_glyph}", color
        if item.state.startswith("!"):
            color = (
                PALETTE["warn_indicator"]
                if item.alert_severity == "warn"
                else PALETTE["alert_indicator"]
            )
            return "\u25b2", color
        if item.key.startswith("project:"):
            if item.state == "unread":
                return "\u2022", PALETTE["inbox_has"]
            if item.state.endswith("working"):
                return "\u2022", PALETTE["live_indicator"]
            if item.state == "dead":
                return "\u2715", PALETTE["dead"]
            return "\u25cb", PALETTE["idle"]
        # State-based indicators for global rows.
        if item.state.endswith("working"):
            char = self.presence.working_frame(self.spinner_index)
            return char, PALETTE["live_indicator"]
        if item.state.endswith("live"):
            return "\u25cf", PALETTE["live_indicator"]
        if item.state == "dead":
            return "\u2715", PALETTE["dead"]
        if item.state == "watch":
            return "\u25ce", PALETTE["live_indicator"]
        if item.state == "ready":
            return "\u25cf", PALETTE["sel_accent"]
        # Key-based fallbacks for items with no meaningful state
        if item.key == "inbox":
            has_items = "(" in item.label and not item.label.endswith("(0)")
            if has_items:
                return "\u25c6", PALETTE["inbox_has"]
            return "\u25c7", PALETTE["inbox_empty"]
        if item.key == "polly" and item.state == "idle":
            return "\u2022", PALETTE["item_muted"]
        if item.key == "settings":
            return "\u2699", PALETTE["item_muted"]
        if item.state == "idle":
            return "\u25cb", PALETTE["idle"]
        if item.state == "sub":
            return " ", None
        return "\u25cb", PALETTE["idle"]

    def _session_work_glyph(
        self,
        work_state: str,
        *,
        alert_severity: str | None = None,
    ) -> tuple[str, _C | None]:
        if work_state == "writing":
            if not self.presence.should_animate():
                return "…", PALETTE["live_indicator"]
            return self.presence.working_frame(self.spinner_index), PALETTE["live_indicator"]
        if work_state == "reviewing":
            return "✎", PALETTE["live_indicator"]
        if work_state == "stuck":
            # #989 — Warn-tier stuck rows (e.g. pane:permission_prompt)
            # render in amber so the user knows it's a one-keystroke
            # fix, not an account-repair situation.
            color = (
                PALETTE["warn_indicator"]
                if alert_severity == "warn"
                else PALETTE["alert_indicator"]
            )
            return "⚠", color
        if work_state == "exited":
            return "✕", PALETTE["dead"]
        return "·", PALETTE["idle"]

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()


class _RawRow(RenderRow):
    """A row whose .text already contains ANSI escapes -- render without wrapping in _sgr()."""
    pass


def run_cockpit_rail(config_path: Path) -> None:
    PollyCockpitRail(config_path).run()
