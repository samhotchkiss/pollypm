"""PollyPM Supervisor — session orchestration over tmux.

Status: **late-stage decomposition** (see issues #179, #186). The
Supervisor used to own every moving part of PollyPM's runtime; steps
0–8 of the split extracted the durable pieces into dedicated modules:

* ``pollypm.core.CoreRail`` — process-wide startup/shutdown rail.
* ``pollypm.launch_planner`` / ``default_launch_planner`` — plan_launches
  + effective_session + tmux_session_for_launch + launch_by_session.
* ``pollypm.recovery`` / ``DefaultRecoveryPolicy`` — health classification
  and intervention-ladder policy.
* ``pollypm.session_services`` — tmux mechanics, session lifecycle.
* ``pollypm.heartbeat`` / ``HeartbeatRail`` — sealed tick + job queue
  + worker pool.
* ``pollypm.core.console_window`` — cockpit console window manager (#186).

What's left on this class is what hasn't been teased apart yet:
tmux bootstrap + layout, session stabilization, recovery interventions,
and the send_input surface. Removing Supervisor outright (or renaming it
to ``LegacySupervisor``) was considered in #186 and deferred — the
remaining surface is big enough that a single rename would ripple
through every internal caller with zero architectural benefit. Instead,
Supervisor stays as the orchestrator of those remaining responsibilities
while future issues peel off one subsystem at a time.

External callers **must** route through ``pollypm.service_api`` — the
import-boundary test (``tests/test_import_boundary.py``) enforces that
Supervisor-direct imports are confined to the allow-list.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

from pollypm.agent_profiles import get_agent_profile
from pollypm.agent_profiles.base import AgentProfileContext
from pollypm.checkpoints import record_checkpoint, snapshot_hash, write_mechanical_checkpoint
from pollypm.config import PollyPMConfig
from pollypm.errors import _last_lines, format_probe_failure
from pollypm.heartbeats import get_heartbeat_backend
from pollypm.heartbeats.api import SupervisorHeartbeatAPI
from pollypm.knowledge_extract import EXTRACTION_INTERVAL_SECONDS
from pollypm.models import AccountConfig, ProviderKind, SessionConfig, SessionLaunchSpec
from pollypm.onboarding import _prime_claude_home
from pollypm.providers.base import LaunchCommand
from pollypm.projects import ensure_project_scaffold
from pollypm.projects import project_checkpoints_dir, project_transcripts_dir, project_worktrees_dir, release_session_lock
from pollypm.runtimes import get_runtime
from pollypm.schedulers import ScheduledJob, get_scheduler_backend
from pollypm.transcript_ledger import sync_token_ledger_for_config
from pollypm.storage.state import AlertRecord, LeaseRecord, StateStore
from pollypm.tmux.client import TmuxWindow
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pollypm.core import CoreRail


_OWNER_PREFIXES = {
    "heartbeat": "H:",
    "polly": "P:",
    "pollypm": "[PollyPM]",
    "operator": "P:",
}


# Map a session role to a persona marker that should appear in the
# pane after a successful kickoff. Used by the verify-after-kickoff
# backstop (see ``Supervisor._schedule_persona_verify``) to catch
# (launch, target) tuple crosses where one role's control prompt
# lands in a different role's window. ``worker`` has no stable
# persona and ``triage`` does not currently brand itself, so both
# are intentionally omitted.
_ROLE_PERSONA_MARKER: dict[str, str] = {
    "operator-pm": "Polly",
    "reviewer": "Russell",
    "heartbeat-supervisor": "Heartbeat",
}


def _prefix_for_owner(owner: str, text: str) -> str:
    """Prepend an owner tag so recipients can identify who injected a message."""
    prefix = _OWNER_PREFIXES.get(owner)
    if prefix is None:
        return text
    return f"{prefix} {text}"


# Flags that belong to a specific provider and should be stripped when
# the session's account uses a different provider.
_CODEX_ONLY_FLAGS = frozenset({
    "--dangerously-bypass-approvals-and-sandbox",
    "--sandbox",
    "--ask-for-approval",
})
_CLAUDE_ONLY_FLAGS = frozenset({
    "--dangerously-skip-permissions",
    "--allowedTools",
    "--disallowedTools",
})


def _sanitize_provider_args(args: list[str], provider: ProviderKind) -> list[str]:
    """Remove flags that belong to a different provider."""
    bad_flags = _CODEX_ONLY_FLAGS if provider is ProviderKind.CLAUDE else _CLAUDE_ONLY_FLAGS
    cleaned: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in bad_flags:
            # Flags like --sandbox take a value argument; skip it too
            if arg in ("--sandbox", "--ask-for-approval", "--allowedTools", "--disallowedTools"):
                skip_next = True
            continue
        cleaned.append(arg)
    # If we stripped everything, fall back to the provider's default open-permissions flag
    if not cleaned:
        if provider is ProviderKind.CLAUDE:
            return ["--dangerously-skip-permissions"]
        if provider is ProviderKind.CODEX:
            return ["--dangerously-bypass-approvals-and-sandbox"]
    return cleaned


# Per-project cache of review-task nudge lines keyed by ``state.db`` mtime.
# Populated by ``_review_tasks_for_project`` (called from
# ``Supervisor._build_review_nudge``). Unchanged projects skip SQLite entirely
# — mirrors ``_DASHBOARD_PROJECT_CACHE`` in cockpit.py and keeps the heartbeat
# tick from scaling linearly with project count (see #174).
_REVIEW_NUDGE_CACHE: dict[str, tuple[float, list[str]]] = {}


def _review_tasks_for_project(project_key: str, db_path: Path) -> list[str]:
    """Return nudge lines for a project's review-queue tasks.

    Opens SQLite only when ``state.db`` mtime differs from the cached entry;
    otherwise returns the cached lines. Mirrors ``_dashboard_project_tasks``.
    """
    try:
        db_mtime = db_path.stat().st_mtime
    except OSError:
        _REVIEW_NUDGE_CACHE.pop(project_key, None)
        return []
    cached = _REVIEW_NUDGE_CACHE.get(project_key)
    if cached is not None and cached[0] == db_mtime:
        return cached[1]

    from pollypm.work.sqlite_service import SQLiteWorkService

    entries: list[str] = []
    try:
        with SQLiteWorkService(db_path=db_path) as svc:
            tasks = svc.list_tasks(work_status="review", project=project_key)
            for task in tasks:
                # Skip human-review tasks — those need user approval.
                if task.current_node_id and "human" in task.current_node_id:
                    continue
                entries.append(f"  - {task.task_id}: {task.title}")
    except Exception:  # noqa: BLE001
        # Don't poison the cache on transient errors; just return empty.
        return []

    _REVIEW_NUDGE_CACHE[project_key] = (db_mtime, entries)
    return entries


class Supervisor:
    _CONTROL_ROLES = {"heartbeat-supervisor", "operator-pm", "triage", "reviewer"}
    # Roles that should receive an initial-input prompt on fresh launch.
    # Workers + architects are NOT control-plane sessions (not in
    # _CONTROL_ROLES — they're project-scoped), but they DO need their
    # profile prompt delivered on launch so the agent knows its persona.
    _INITIAL_INPUT_ROLES = _CONTROL_ROLES | {"worker", "architect"}
    #: Name of the PollyPM console/cockpit window inside a tmux session.
    CONSOLE_WINDOW = "PollyPM"
    _CONSOLE_WINDOW = CONSOLE_WINDOW  # deprecated alias — use CONSOLE_WINDOW
    _STORAGE_CLOSET_SESSION_SUFFIX = "-storage-closet"
    _CONTROL_HOMES_DIR = "control-homes"
    _RECOVERY_WINDOW = timedelta(minutes=30)
    _RECOVERY_LIMIT = 5
    _RECOVERY_HARD_LIMIT = 20  # stop entirely after this many total attempts
    _STALL_NUDGE_MESSAGE = (
        "You appear stalled. State the remaining task in one sentence, "
        "execute the next step now."
    )
    _WORKER_BLOCKED_PM_COMMANDS = {
        "up",
        "down",
        "reset",
        "send",
        "console",
        "worker-start",
        "stop-session",
        "remove-session",
        "switch-session-account",
    }

    def __init__(
        self,
        config: PollyPMConfig,
        *,
        readonly_state: bool = False,
        core_rail: "CoreRail | None" = None,
    ) -> None:
        self.config = config
        self.readonly_state = readonly_state
        if core_rail is not None:
            self._core_rail = core_rail
            self.store = core_rail.get_state_store()
        else:
            self.store = StateStore(config.project.state_db, readonly=readonly_state)
            # Lazy-import to avoid import cycles (core imports nothing from
            # supervisor, but keep the reference local to be safe).
            from pollypm.core import CoreRail as _CoreRail
            from pollypm.plugin_host import extension_host_for_root
            plugin_host = extension_host_for_root(str(config.project.root_dir.resolve()))
            self._core_rail = _CoreRail(config, self.store, plugin_host)
        # Lazy-init session service to avoid circular imports at construction
        self._session_service = None
        # Lazy-init recovery policy (resolved via plugin host)
        self._recovery_policy = None
        # Lazy-init launch planner. The planner owns plan_launches /
        # effective_session / tmux_session_for_launch / launch_by_session
        # — Supervisor delegates to it. Kept lazy so alt constructions
        # (``Supervisor.__new__`` in dashboard_data) can set the core
        # rail up by hand before first access.
        self._launch_planner_instance = None
        # Lazy-init console window manager — ConsoleWindowManager owns
        # the cockpit window create/repair/focus lifecycle.
        self._console_window_manager = None
        # Register ourselves as a subsystem so CoreRail.start()/stop() can
        # drive our lifecycle. Readonly supervisors (used by the cockpit
        # for passive inspection) don't register — they never drive boot.
        if not readonly_state:
            self._core_rail.register_subsystem(self)

    def _build_launch_planner(self):
        """Resolve the launch planner via the plugin host.

        Today the default planner ships as a built-in plugin and is the
        only registration under ``launch_planners`` — we request it by
        name (``"default"``). Future work can thread a config-driven
        planner name through here.
        """
        from pollypm.plugins_builtin.default_launch_planner.planner import (
            DefaultLaunchPlannerContext,
        )

        ctx = DefaultLaunchPlannerContext(
            config=self.config,
            store=self.store,
            readonly_state=self.readonly_state,
            effective_account=self._effective_account,
            apply_role_launch_restrictions=self._apply_role_launch_restrictions,
            resolve_profile_prompt=self._resolve_profile_prompt,
            storage_closet_session_name=self.storage_closet_session_name,
        )
        plugin_host = self._plugin_host_for_planner()
        return plugin_host.get_launch_planner("default", context=ctx)

    def _plugin_host_for_planner(self):
        """Return the plugin host to resolve the planner through.

        Prefers the core rail's host when present; falls back to the
        project-root host for ``Supervisor.__new__`` code paths (e.g.
        dashboard_data) that don't carry a rail.
        """
        rail = getattr(self, "_core_rail", None)
        if rail is not None:
            return rail.get_plugin_host()
        from pollypm.plugin_host import extension_host_for_root

        return extension_host_for_root(str(self.config.project.root_dir.resolve()))

    @property
    def launch_planner(self):
        """The LaunchPlanner used to produce session launch plans.

        Lazily constructed so Supervisor instances created via
        ``__new__`` (dashboard_data's read-only view) can wire up the
        bare minimum of state before first plan access.
        """
        if getattr(self, "_launch_planner_instance", None) is None:
            self._launch_planner_instance = self._build_launch_planner()
        return self._launch_planner_instance

    @property
    def core_rail(self) -> "CoreRail":
        """Return the CoreRail this Supervisor is bound to."""
        return self._core_rail

    # ── Startable lifecycle (driven by CoreRail.start()/stop()) ────────────

    def start(self) -> None:
        """Boot Supervisor-owned orchestration.

        Called by :meth:`pollypm.core.CoreRail.start`. Idempotent and
        safe to invoke directly for back-compat — callers that used to
        drive ``ensure_layout`` / ``ensure_heartbeat_schedule`` /
        ``ensure_knowledge_extraction_schedule`` manually can still do
        so; this method just consolidates them.
        """
        import logging as _logging
        _log = _logging.getLogger(__name__)
        _log.debug("Supervisor.start(): ensure_layout")
        self.ensure_layout()
        _log.debug("Supervisor.start(): ensure_heartbeat_schedule")
        self.ensure_heartbeat_schedule()
        if hasattr(self, "ensure_knowledge_extraction_schedule"):
            _log.debug("Supervisor.start(): ensure_knowledge_extraction_schedule")
            self.ensure_knowledge_extraction_schedule()
        # #268 Gap B: rebuild the sessions table from live tmux state so
        # SessionRoleIndex can resolve every running session. Older boots
        # only wrote rows for newly-created windows, leaving pre-existing
        # heartbeat/operator/reviewer/worker sessions unregistered. One-
        # shot scan per cockpit start; failures are swallowed.
        try:
            repaired = self.repair_sessions_table()
            if repaired:
                _log.debug("Supervisor.start(): repaired %d sessions rows", repaired)
        except Exception:  # noqa: BLE001
            _log.debug("Supervisor.start(): repair_sessions_table failed", exc_info=True)

    def stop(self) -> None:
        """Gracefully release Supervisor-owned resources.

        This is the paired teardown for :meth:`start`. It does NOT tear
        down tmux sessions — that's ``pm reset`` territory. Today we
        just close the state store connection if we opened it.
        """
        import logging as _logging
        _log = _logging.getLogger(__name__)
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            _log.debug("Supervisor.stop(): store.close raised", exc_info=True)

    @property
    def session_service(self):
        """The session service handles all tmux session mechanics.

        Uses getattr to tolerate ``Supervisor.__new__(Supervisor)``
        construction from dashboard_data / service_api readonly paths
        where ``__init__`` never ran.
        """
        if getattr(self, "_session_service", None) is None:
            from pollypm.session_services.tmux import TmuxSessionService
            self._session_service = TmuxSessionService(config=self.config, store=self.store)
        return self._session_service

    @property
    def recovery_policy(self):
        """The recovery policy decides classification + intervention.

        Resolved once via the plugin host. Applying interventions (the
        tmux / state mutations) stays on the Supervisor — the policy is
        a pure decision maker.
        """
        if getattr(self, "_recovery_policy", None) is None:
            try:
                from pollypm.recovery import get_recovery_policy
                self._recovery_policy = get_recovery_policy(
                    "default", root_dir=self.config.project.root_dir,
                )
            except Exception:  # noqa: BLE001
                # Plugin host unavailable (tests, partial boot) — fall back
                # to an in-process default so classification still works.
                from pollypm.recovery.default import DefaultRecoveryPolicy
                self._recovery_policy = DefaultRecoveryPolicy()
        return self._recovery_policy

    @property
    def tmux(self):
        """Shortcut to the session service's TmuxClient.

        External callers (cockpit, CLI, heartbeat, etc.) access
        ``supervisor.tmux`` — this property keeps that working while
        ensuring the single TmuxClient lives inside the session service.
        """
        return self.session_service.tmux

    @tmux.setter
    def tmux(self, value):
        """Allow external code (e.g. dashboard_data) to inject a TmuxClient."""
        self.session_service.tmux = value

    def effective_session(self, session: SessionConfig, controller_account: str | None = None) -> SessionConfig:
        """Return ``session`` with runtime account overrides applied.

        Thin delegator to :meth:`LaunchPlanner.effective_session`. See
        ``pollypm.launch_planner.base.LaunchPlanner`` for the contract.
        """
        return self.launch_planner.effective_session(session, controller_account)

    def _effective_session(self, session: SessionConfig, controller_account: str | None = None) -> SessionConfig:
        return self.effective_session(session, controller_account)

    def _default_agent_profile(self, session: SessionConfig) -> str | None:
        if session.role == "heartbeat-supervisor":
            return "heartbeat"
        if session.role == "operator-pm":
            return "polly"
        if session.role == "worker":
            return "worker"
        if session.role == "reviewer":
            return "russell"
        if session.role == "architect":
            return "architect"
        return None

    def _resolve_profile_prompt(self, session: SessionConfig, account: AccountConfig) -> str | None:
        profile_name = session.agent_profile or self._default_agent_profile(session)
        if not profile_name:
            return None
        profile = get_agent_profile(profile_name, root_dir=self.config.project.root_dir)
        return profile.build_prompt(
            AgentProfileContext(
                config=self.config,
                session=session,
                account=account,
            )
        )

    def storage_closet_session_name(self) -> str:
        return f"{self.config.project.tmux_session}{self._STORAGE_CLOSET_SESSION_SUFFIX}"

    def heartbeat_tmux_session_name(self) -> str:
        return self.storage_closet_session_name()

    def _tmux_session_for_role(self, role: str) -> str:
        return self.storage_closet_session_name()

    def tmux_session_for_launch(self, launch: SessionLaunchSpec) -> str:
        """Return the tmux session name that should host ``launch``.

        Thin delegator to :meth:`LaunchPlanner.tmux_session_for_launch`.
        """
        return self.launch_planner.tmux_session_for_launch(launch)

    def _tmux_session_for_launch(self, launch: SessionLaunchSpec) -> str:
        return self.tmux_session_for_launch(launch)

    def _tmux_session_for_session(self, session_name: str) -> str:
        launch = self._launch_by_session(session_name)
        return self._tmux_session_for_launch(launch)

    def _all_tmux_session_names(self) -> list[str]:
        names = [self.config.project.tmux_session]
        storage = self.storage_closet_session_name()
        if storage not in names:
            names.append(storage)
        return names

    def invalidate_launch_cache(self) -> None:
        """Drop the cached :meth:`plan_launches` result.

        Thin delegator to :meth:`LaunchPlanner.invalidate_cache`.
        """
        self.launch_planner.invalidate_cache()

    def plan_launches(self, *, controller_account: str | None = None) -> list[SessionLaunchSpec]:
        """Return the launch plan for every enabled session.

        Thin delegator to :meth:`LaunchPlanner.plan_launches`.
        """
        return self.launch_planner.plan_launches(controller_account=controller_account)

    def bootstrap_tmux(
        self,
        *,
        skip_probe: bool = False,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        session_name = self.config.project.tmux_session
        existing = [name for name in self._all_tmux_session_names() if self.session_service.tmux.has_session(name)]
        if existing:
            # Sessions already running — reconcile instead of failing
            return self._reconcile_existing(session_name, on_status=on_status)

        # Clear stale markers BEFORE plan_launches so launch commands don't
        # use --continue from a previous run's resume markers.
        self._bootstrap_clear_markers()

        failures: list[str] = []
        for controller_account in self._controller_candidates():
            launches = self.plan_launches(controller_account=controller_account)
            if not launches:
                raise RuntimeError("No enabled sessions found in config.")

            try:
                if skip_probe:
                    pass
                else:
                    self._probe_controller_account(controller_account)
                self._bootstrap_launches(session_name, launches, on_status=on_status)
                self.ensure_heartbeat_schedule()
                self.ensure_knowledge_extraction_schedule()
                self.store.record_event(
                    "pollypm",
                    "controller_selected",
                    f"Selected controller account {controller_account}",
                )
                return controller_account
            except RuntimeError as exc:
                failures.append(f"{controller_account}: {exc}")
                for tmux_session in self._all_tmux_session_names():
                    if self.session_service.tmux.has_session(tmux_session):
                        self.session_service.tmux.kill_session(tmux_session)

        raise RuntimeError("PollyPM could not launch any controller account: " + "; ".join(failures))

    def _bootstrap_clear_markers(self) -> None:
        """Clear stale session markers so all sessions start fresh."""
        for homes_dir in [self.config.project.base_dir / "homes", self.config.project.base_dir / "control-homes"]:
            if homes_dir.is_dir():
                for marker in homes_dir.glob("*/.pollypm/session-markers/*"):
                    marker.unlink(missing_ok=True)
        # Also clear markers in account homes (e.g. ~/.pollypm/agent_homes/claude_1)
        for account in self.config.accounts.values():
            if account.home is not None:
                markers_dir = account.home / ".pollypm" / "session-markers"
                if markers_dir.is_dir():
                    for marker in markers_dir.iterdir():
                        marker.unlink(missing_ok=True)

    def repair_sessions_table(self) -> int:
        """Upsert a ``sessions`` row for every configured session whose
        tmux window is currently alive.

        Repairs DBs that were populated by an older build that only
        registered newly-created windows (see #268 Gap B). Intended to
        run once at cockpit start — tmux is scanned in a single
        ``list_all_windows`` call so the cost is bounded.

        Returns the number of rows upserted.
        """
        storage_session = self.storage_closet_session_name()
        live_windows: set[str] = set()
        try:
            if self.session_service.tmux.has_session(storage_session):
                for w in self.session_service.tmux.list_windows(storage_session):
                    live_windows.add(w.name)
        except Exception:  # noqa: BLE001
            # If tmux is unreachable, quietly skip — we'll try again next
            # boot. Don't fail cockpit startup on a stat call.
            return 0

        if not live_windows:
            return 0

        try:
            launches = self.plan_launches()
        except Exception:  # noqa: BLE001
            return 0

        upserted = 0
        for launch in launches:
            if launch.window_name not in live_windows:
                continue
            try:
                self.store.upsert_session(
                    name=launch.session.name,
                    role=launch.session.role,
                    project=launch.session.project,
                    provider=launch.session.provider.value,
                    account=launch.account.name,
                    cwd=str(launch.session.cwd),
                    window_name=launch.window_name,
                )
                upserted += 1
            except Exception:  # noqa: BLE001
                # Per-session failure is non-fatal — we want to patch as
                # many rows as we can.
                continue
        return upserted

    def _reconcile_existing(
        self,
        session_name: str,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        """Reconcile running state: create missing windows without killing existing ones."""
        _status = on_status or (lambda _: None)
        storage_session = self.storage_closet_session_name()
        launches = self.plan_launches()
        existing_windows: set[str] = set()
        if self.session_service.tmux.has_session(storage_session):
            for w in self.session_service.tmux.list_windows(storage_session):
                existing_windows.add(w.name)

        created = 0
        for launch in launches:
            if launch.window_name in existing_windows:
                # Window already alive — still upsert the sessions row so
                # SessionRoleIndex can resolve role:<role> to this session.
                # #268 Gap B: pre-existing windows (heartbeat, operator,
                # reviewer, workers) would otherwise never get registered.
                self._record_launch(launch)
                continue
            _status(f"Recreating {launch.session.name}...")
            if not self.session_service.tmux.has_session(storage_session):
                self.session_service.tmux.create_session(storage_session, launch.window_name, launch.command)
            else:
                self.session_service.tmux.create_window(storage_session, launch.window_name, launch.command, detached=True)
            target = f"{storage_session}:{launch.window_name}"
            self.session_service.tmux.set_window_option(target, "allow-passthrough", "on")
            self.session_service.tmux.set_window_option(target, "focus-events", "on")
            self.session_service.tmux.pipe_pane(target, launch.log_path)
            self._record_launch(launch)
            created += 1

        if not self.session_service.tmux.has_session(session_name):
            self.session_service.tmux.create_session(session_name, self._CONSOLE_WINDOW, self._console_command(), remain_on_exit=False)

        self.ensure_heartbeat_schedule()
        self.ensure_knowledge_extraction_schedule()
        _status(f"Reconciled: {created} session(s) created, {len(existing_windows)} already running")
        return self.config.pollypm.controller_account

    def _bootstrap_launches(
        self,
        session_name: str,
        launches: list[SessionLaunchSpec],
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        storage_session = self.storage_closet_session_name()
        (self.config.project.base_dir / "cockpit_state.json").unlink(missing_ok=True)
        self._bootstrap_clear_markers()


        # Phase 1: Create all tmux windows up front (fast, no blocking).
        # Use pane IDs as targets for stabilization threads — window name
        # targeting can cause identity swaps when threads run in parallel.
        targets: list[tuple[SessionLaunchSpec, str]] = []
        if launches:
            first = launches[0]
            if on_status:
                on_status(f"Creating {first.session.name}...")
            first_pane_id = self.session_service.tmux.create_session(storage_session, first.window_name, first.command)
            window_target = f"{storage_session}:{first.window_name}"
            self.session_service.tmux.set_window_option(window_target, "allow-passthrough", "on")
            self.session_service.tmux.set_window_option(window_target, "focus-events", "on")
            self.session_service.tmux.pipe_pane(window_target, first.log_path)
            self._record_launch(first)
            # Use pane ID from create_session for unambiguous targeting
            pane_target = first_pane_id or window_target
            targets.append((first, pane_target))
            for launch in launches[1:]:
                if on_status:
                    on_status(f"Creating {launch.session.name}...")
                pane_id = self.session_service.tmux.create_window(storage_session, launch.window_name, launch.command, detached=True)
                window_target = f"{storage_session}:{launch.window_name}"
                self.session_service.tmux.set_window_option(window_target, "allow-passthrough", "on")
                self.session_service.tmux.set_window_option(window_target, "focus-events", "on")
                self.session_service.tmux.pipe_pane(window_target, launch.log_path)
                self._record_launch(launch)
                # Use pane ID returned by create_window for unambiguous targeting
                pane_target = pane_id or self._resolve_pane_id(storage_session, launch.window_name) or window_target
                targets.append((launch, pane_target))

        # Phase 2: Create the cockpit session so the user can attach immediately.
        self.session_service.tmux.create_session(session_name, self._CONSOLE_WINDOW, self._console_command(), remain_on_exit=False)
        console_target = f"{session_name}:{self._CONSOLE_WINDOW}"
        self.session_service.tmux.set_window_option(console_target, "allow-passthrough", "on")
        self.session_service.tmux.set_window_option(console_target, "focus-events", "on")
        self.session_service.tmux.set_window_option(console_target, "window-size", "latest")
        self.session_service.tmux.set_window_option(console_target, "aggressive-resize", "on")
        self.focus_console()

        # Phase 3: Stabilize sessions and send initial input.
        # Stabilization (dismissing prompts) runs in parallel threads.
        # Initial input sending runs sequentially afterward to avoid
        # tmux routing races that cause identity swaps.
        stabilized: list[tuple[SessionLaunchSpec, str]] = []
        lock = threading.Lock()

        def _stabilize_one(launch: SessionLaunchSpec, tgt: str) -> None:
            try:
                # Only stabilize (dismiss trust/theme prompts), don't send input
                name = launch.session.name
                if launch.session.provider is ProviderKind.CLAUDE:
                    self._stabilize_claude_launch(tgt)
                elif launch.session.provider is ProviderKind.CODEX:
                    self._stabilize_codex_launch(tgt)
                with lock:
                    stabilized.append((launch, tgt))
            except Exception as exc:  # noqa: BLE001
                try:
                    self.store.record_event(
                        launch.session.name,
                        "stabilize_failed",
                        f"Bootstrap stabilization failed: {exc}",
                    )
                    self.store.upsert_alert(
                        launch.session.name,
                        "stabilize_failed",
                        "warn",
                        f"Session {launch.session.name} failed to stabilize during bootstrap: {exc}",
                    )
                except Exception:  # noqa: BLE001
                    pass

        threads = []
        for launch, tgt in targets:
            t = threading.Thread(target=_stabilize_one, args=(launch, tgt), daemon=True)
            t.start()
            threads.append(t)

        # Wait for all stabilization to complete (with timeout)
        for t in threads:
            t.join(timeout=120)

        # Phase 4: Send initial input SEQUENTIALLY to avoid tmux routing races
        for launch, tgt in stabilized:
            self._send_initial_input_if_fresh(launch, tgt)
            self._mark_session_resume_ready(launch)

        # Phase 5: Dispatch SessionCreatedEvent so #246's task-assignment
        # listener can resume-ping any in-progress task owned by the new
        # session. The bootstrap path uses tmux.create_* directly (not
        # SessionService.create()), so the emitter baked into create()
        # doesn't fire for these launches. Emit explicitly here.
        try:
            from pollypm.session_services.base import (
                SessionCreatedEvent,
                dispatch_session_event,
            )
            for launch, _tgt in stabilized:
                try:
                    dispatch_session_event(
                        SessionCreatedEvent(
                            name=launch.session.name,
                            role=launch.session.role or "",
                            project=launch.session.project or "",
                            provider=launch.session.provider.value,
                        )
                    )
                except Exception:  # noqa: BLE001
                    self.store.record_event(
                        launch.session.name,
                        "session_created_dispatch_failed",
                        "bootstrap session.created dispatch failed",
                    )
        except ImportError:
            pass  # session_services.base missing dispatcher — no-op (pre-#246 build)

    def shutdown_tmux(self) -> None:
        for session_name in reversed(self._all_tmux_session_names()):
            if self.session_service.tmux.has_session(session_name):
                self.session_service.tmux.kill_session(session_name)

    def _resolve_pane_id(self, session_name: str, window_name: str) -> str | None:
        """Look up the pane ID for a window. Returns '%NNN' or None."""
        try:
            windows = self.session_service.tmux.list_windows(session_name)
            for w in windows:
                if w.name == window_name:
                    return w.pane_id
        except Exception:  # noqa: BLE001
            pass
        return None

    def _record_launch(self, launch: SessionLaunchSpec) -> None:
        self.store.upsert_session(
            name=launch.session.name,
            role=launch.session.role,
            project=launch.session.project,
            provider=launch.session.provider.value,
            account=launch.account.name,
            cwd=str(launch.session.cwd),
            window_name=launch.window_name,
        )
        self.store.clear_alert(launch.session.name, "missing_window")
        self.store.record_event(
            launch.session.name,
            "launch",
            f"Created tmux window {launch.window_name} with provider {launch.session.provider.value}",
        )

    def _session_name_by_window(self) -> dict[str, str]:
        return {
            (session.window_name or session.name): session.name
            for session in self.config.sessions.values()
            if session.enabled
        }

    def project_assignments(self) -> dict[str, list[SessionLaunchSpec]]:
        assignments: dict[str, list[SessionLaunchSpec]] = {}
        for launch in self.plan_launches():
            assignments.setdefault(launch.session.project, []).append(launch)
        return assignments

    def window_map(self) -> dict[str, TmuxWindow]:
        """Return ``{window_name: TmuxWindow}`` for every PollyPM-owned window.

        Inputs: none (reads live tmux state plus the cockpit mount override).
        Output: a dict keyed by window name containing ``TmuxWindow`` entries
        for the project and storage-closet sessions, plus any mounted window.
        """
        our_sessions = set(self._all_tmux_session_names())
        windows: dict[str, TmuxWindow] = {}
        for window in self.session_service.tmux.list_all_windows():
            if window.session in our_sessions:
                windows[window.name] = window
        mounted = self._mounted_window_override()
        if mounted is not None:
            windows[mounted.name] = mounted
        return windows

    def _window_map(self) -> dict[str, TmuxWindow]:
        return self.window_map()

    def _mounted_window_override(self) -> TmuxWindow | None:
        state_path = self.config.project.base_dir / "cockpit_state.json"
        if not state_path.exists():
            return None
        try:
            data = json.loads(state_path.read_text())
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, dict):
            return None
        mounted_session = data.get("mounted_session")
        if not isinstance(mounted_session, str) or not mounted_session:
            return None
        try:
            launch = self._launch_by_session(mounted_session)
        except KeyError:
            return None
        target = f"{self.config.project.tmux_session}:{self._CONSOLE_WINDOW}"
        try:
            panes = self.session_service.tmux.list_panes(target)
        except Exception:  # noqa: BLE001
            return None
        if len(panes) < 2:
            return None
        right_pane = max(panes, key=lambda pane: pane.pane_left)
        # Validate the mounted pane looks like the expected session.
        # A stale cockpit_state can claim a session is mounted when the
        # right pane is actually a static bash shell or a different session.
        pane_cmd = right_pane.pane_current_command or ""
        expected_provider = launch.session.provider.value
        if expected_provider == "claude" and pane_cmd in {"bash", "zsh", "sh", "fish"}:
            return None
        if expected_provider == "codex" and pane_cmd not in {"node"}:
            return None
        return TmuxWindow(
            session=self.config.project.tmux_session,
            index=0,
            name=launch.window_name,
            active=True,
            pane_id=right_pane.pane_id,
            pane_current_command=right_pane.pane_current_command,
            pane_current_path=right_pane.pane_current_path,
            pane_dead=right_pane.pane_dead,
        )

    def status(self) -> tuple[list[SessionLaunchSpec], list[TmuxWindow], list[AlertRecord], list[LeaseRecord], list[str]]:
        launches = self.plan_launches()
        errors: list[str] = []
        windows: list[TmuxWindow] = []

        # Single subprocess call to get ALL windows across all sessions
        try:
            all_windows = self.session_service.tmux.list_all_windows()
            our_sessions = set(self._all_tmux_session_names())
            windows = [w for w in all_windows if w.session in our_sessions]
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

        try:
            alerts = self.store.open_alerts()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"store.open_alerts: {exc}")
            alerts = []
        try:
            leases = self.store.list_leases()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"store.list_leases: {exc}")
            leases = []
        return launches, windows, alerts, leases, errors

    def ensure_layout(self) -> Path:
        project = self.config.project
        project.base_dir.mkdir(parents=True, exist_ok=True)
        project.logs_dir.mkdir(parents=True, exist_ok=True)
        project.snapshots_dir.mkdir(parents=True, exist_ok=True)
        ensure_project_scaffold(project.root_dir)
        for known_project in self.config.projects.values():
            ensure_project_scaffold(known_project.path)
        for account in self.config.accounts.values():
            if account.home is not None:
                account.home.mkdir(parents=True, exist_ok=True, mode=0o700)
                if account.provider is ProviderKind.CLAUDE:
                    _prime_claude_home(account.home)
                self._refresh_account_runtime_metadata(account.name)
        control_homes_root = self.config.project.base_dir / self._CONTROL_HOMES_DIR
        control_homes_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for session in self.config.sessions.values():
            if session.role not in self._CONTROL_ROLES:
                continue
            base_account = self.config.accounts.get(session.account)
            if base_account is None or base_account.home is None:
                continue
            self._sync_control_home(base_account, session.name)
        self.store.prune_sessions(
            {session.name for session in self.config.sessions.values() if session.enabled}
        )
        # One-shot migration of any legacy inbox messages — safe to call on
        # every boot; guarded by a durable marker so it only runs once.
        try:
            from pollypm.inbox_migration import run_inbox_migration_if_needed
            run_inbox_migration_if_needed(self.config)
        except Exception:  # noqa: BLE001 - never block startup on migration
            pass
        return project.base_dir

    @property
    def console_window(self):
        """The :class:`ConsoleWindowManager` that owns the cockpit window lifecycle."""
        if getattr(self, "_console_window_manager", None) is None:
            from pollypm.core.console_window import ConsoleWindowManager
            self._console_window_manager = ConsoleWindowManager(
                config=self.config,
                session_service=self.session_service,
                storage_closet_session_name=self.storage_closet_session_name,
                plan_launches=self.plan_launches,
            )
        return self._console_window_manager

    def console_window_name(self) -> str:
        """Return the tmux window name used for the PollyPM cockpit."""
        return self.CONSOLE_WINDOW

    def ensure_console_window(self) -> None:
        """Ensure the cockpit window exists in the PollyPM tmux session.

        Thin delegator to :meth:`ConsoleWindowManager.ensure`.
        """
        self.console_window.ensure()

    def _repair_console_if_broken(self, tmux_session: str) -> None:
        """Repair a cockpit window whose rail pane died (delegator)."""
        self.console_window.repair_if_broken(tmux_session)

    def focus_console(self) -> None:
        """Select the cockpit window, creating it if missing.

        Thin delegator to :meth:`ConsoleWindowManager.focus`.
        """
        self.console_window.focus()

    def run_heartbeat(self, snapshot_lines: int = 200) -> list[AlertRecord]:
        """Run one session-health sweep.

        Post-#184 this is the :class:`core_recurring` ``session.health_sweep``
        handler's core: capture pane snapshots, classify session health,
        record checkpoints, raise alerts. The formerly-inline "tick dispatch"
        pieces (transcript ingest, itsalive deploy sweep, lease GC,
        capacity probing, async job fanout) have all migrated to
        roster-registered recurring handlers drained by the HeartbeatRail
        worker pool — so this function does just the Phase 2 sweep now.

        See ``src/pollypm/plugins_builtin/core_recurring/plugin.py`` and
        ``src/pollypm/plugins_builtin/itsalive/plugin.py`` for the
        replacement handlers.
        """
        # Phase 1 residual: token-ledger sync is still inline because it
        # requires the main-thread SQLite connection and no equivalent
        # handler exists yet (post-v1 migration target).
        transcript_samples = sync_token_ledger_for_config(self.config)
        if transcript_samples:
            self.store.record_event(
                "heartbeat",
                "token_ledger",
                f"Synced {len(transcript_samples)} transcript token sample(s)",
            )

        # Phase 2: Fast synchronous sweep — capture + classify + alert
        backend = get_heartbeat_backend(
            self.config.pollypm.heartbeat_backend,
            root_dir=self.config.project.root_dir,
        )
        api = SupervisorHeartbeatAPI(self, snapshot_lines=snapshot_lines)
        alerts = backend.run(api, snapshot_lines=snapshot_lines)

        # Re-arm the heartbeat schedule so the next sweep is always queued.
        # Without this, the scheduler permanently stops if the heartbeat
        # session enters an interactive conversation and misses a sweep
        # window — dead workers go undetected indefinitely.
        try:
            self.ensure_heartbeat_schedule()
        except Exception:  # noqa: BLE001
            pass  # Schedule failure shouldn't discard a successful sweep

        return alerts

    def ensure_heartbeat_schedule(self) -> None:
        backend = get_scheduler_backend(
            self.config.pollypm.scheduler_backend,
            root_dir=self.config.project.root_dir,
        )
        self._dedup_recurring_jobs(backend, "heartbeat", 60)

    def ensure_knowledge_extraction_schedule(self) -> None:
        backend = get_scheduler_backend(
            self.config.pollypm.scheduler_backend,
            root_dir=self.config.project.root_dir,
        )
        self._dedup_recurring_jobs(backend, "knowledge_extract", EXTRACTION_INTERVAL_SECONDS, payload={"model": "haiku"})

    def _dedup_recurring_jobs(
        self,
        backend,
        kind: str,
        interval: int,
        payload: dict[str, object] | None = None,
    ) -> None:
        """Ensure exactly one pending recurring job of the given kind exists."""
        desired_payload = dict(payload or {})
        jobs = backend.list_jobs(self)
        matching = [job for job in jobs if job.kind == kind and job.interval_seconds == interval]
        pending = [job for job in matching if job.status == "pending"]
        exact_pending = [job for job in pending if job.payload == desired_payload]

        if len(matching) == 1 and len(exact_pending) == 1:
            return

        kept_job: ScheduledJob | None = None
        if exact_pending:
            kept_job = min(exact_pending, key=lambda job: job.run_at)
        elif pending:
            kept_job = min(pending, key=lambda job: job.run_at)
            kept_job.payload = desired_payload
            kept_job.last_error = None
        else:
            kept_job = ScheduledJob(
                job_id=uuid4().hex,
                kind=kind,
                run_at=datetime.now(UTC) + timedelta(seconds=interval),
                payload=desired_payload,
                interval_seconds=interval,
            )

        kept_job.status = "pending"
        remaining = [job for job in jobs if job not in matching]
        remaining.append(kept_job)

        save_jobs = getattr(backend, "_save_jobs", None)
        if callable(save_jobs):
            save_jobs(self, remaining)
            return

        if not matching:
            backend.schedule(
                self,
                kind=kind,
                run_at=kept_job.run_at,
                payload=kept_job.payload,
                interval_seconds=interval,
            )

    def _run_heartbeat_local(self, snapshot_lines: int = 200) -> list[AlertRecord]:
        window_map = self._window_map()
        name_by_window = self._session_name_by_window()

        for launch in self.plan_launches():
            window = window_map.get(launch.window_name)
            session_key = launch.session.name
            tmux_session = self._tmux_session_for_launch(launch)
            if window is None:
                self.store.upsert_alert(
                    session_key,
                    "missing_window",
                    "error",
                    f"Expected tmux window {launch.window_name} in session {tmux_session}",
                )
                self._maybe_recover_session(launch, failure_type="missing_window", failure_message="Expected tmux window is missing")
                continue

            snapshot_path, snapshot_content = self._write_snapshot(window, snapshot_lines)
            try:
                log_bytes = launch.log_path.stat().st_size
            except (FileNotFoundError, OSError):
                log_bytes = 0
            previous = self.store.latest_heartbeat(session_key)
            current_snapshot_hash = snapshot_hash(snapshot_content)

            self.store.record_heartbeat(
                session_name=session_key,
                tmux_window=window.name,
                pane_id=window.pane_id,
                pane_command=window.pane_current_command,
                pane_dead=window.pane_dead,
                log_bytes=log_bytes,
                snapshot_path=str(snapshot_path),
                snapshot_hash=current_snapshot_hash,
            )
            token_metrics = _extract_token_metrics(launch.session.provider, snapshot_content)
            if token_metrics is not None:
                delta = self.store.record_token_sample(
                    session_name=session_key,
                    account_name=launch.account.name,
                    provider=launch.session.provider.value,
                    model_name=token_metrics[0],
                    project_key=launch.session.project,
                    cumulative_tokens=token_metrics[1],
                )
                if delta > 0:
                    self.store.record_event(
                        session_key,
                        "token_usage",
                        f"Recorded {delta} tokens for {launch.session.project} on {launch.account.name} ({token_metrics[0]})",
                    )

            self.store.clear_alert(session_key, "missing_window")
            active_alerts = self._update_alerts(
                launch,
                window,
                pane_text=snapshot_content,
                previous_log_bytes=previous.log_bytes if previous else None,
                previous_snapshot_hash=previous.snapshot_hash if previous else None,
                current_log_bytes=log_bytes,
                current_snapshot_hash=current_snapshot_hash,
            )
            artifact = write_mechanical_checkpoint(
                self.config,
                launch,
                snapshot_path=snapshot_path,
                snapshot_content=snapshot_content,
                log_bytes=log_bytes,
                alerts=active_alerts,
            )
            record_checkpoint(
                self.store,
                launch,
                project_key=launch.session.project,
                level="level0",
                artifact=artifact,
                snapshot_path=snapshot_path,
                memory_backend_name=self.config.memory.backend,
            )
            # Proactive capacity rollover: if the account is near its limit
            # (≤ PROACTIVE_ROLLOVER_THRESHOLD_PCT remaining) and no hard
            # failure is already pending, mark capacity_low so the recovery
            # path below flips us onto the backup account before we run dry.
            self._maybe_mark_capacity_low(launch, active_alerts)
            failure = self._primary_failure(active_alerts)
            if failure is not None:
                self._maybe_recover_session(launch, failure_type=failure, failure_message=", ".join(active_alerts))

        for window_name, session_key in name_by_window.items():
            if window_name in window_map:
                continue
            self.store.upsert_alert(
                session_key,
                "missing_window",
                "error",
                f"Expected tmux window {window_name} in session {self._tmux_session_for_session(session_key)}",
            )

        current_alerts = self.store.open_alerts()
        self.store.record_event(
            "heartbeat",
            "heartbeat",
            f"Heartbeat sweep completed with {len(current_alerts)} open alerts",
        )
        return current_alerts

    def _apply_role_launch_restrictions(
        self,
        session: SessionConfig,
        launch: LaunchCommand,
    ) -> LaunchCommand:
        if self.readonly_state:
            return launch
        if session.role != "worker":
            return launch
        env = dict(launch.env)
        shim_dir = self._worker_restriction_shim_dir()
        current_path = env.get("PATH") or os.environ.get("PATH", "")
        env["PATH"] = f"{shim_dir}{os.pathsep}{current_path}" if current_path else str(shim_dir)
        return replace(launch, env=env)

    def _worker_restriction_shim_dir(self) -> Path:
        shim_dir = self.config.project.base_dir / "role-shims" / "worker"
        shim_dir.mkdir(parents=True, exist_ok=True)
        tmux_shim = shim_dir / "tmux"
        tmux_shim.write_text(
            "#!/bin/sh\n"
            "echo 'PollyPM restriction: worker sessions may not manage tmux directly.' >&2\n"
            "exit 1\n"
        )
        tmux_shim.chmod(0o755)

        real_pm = shutil.which("pm")
        python = shlex.quote(sys.executable)
        if real_pm:
            fallback = f"exec {shlex.quote(real_pm)} \"$@\""
        else:
            fallback = f"exec {python} -m pollypm.cli \"$@\""
        pm_shim = shim_dir / "pm"
        pm_shim.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            + "".join(
                f"  {command}) echo 'PollyPM restriction: worker sessions may not manage sessions with pm {command}.' >&2; exit 1 ;;\n"
                for command in sorted(self._WORKER_BLOCKED_PM_COMMANDS)
            )
            + "esac\n"
            + fallback
            + "\n"
        )
        pm_shim.chmod(0o755)
        return shim_dir

    def schedule_job(
        self,
        *,
        kind: str,
        run_at: datetime,
        payload: dict[str, object] | None = None,
        interval_seconds: int | None = None,
    ) -> ScheduledJob:
        backend = get_scheduler_backend(
            self.config.pollypm.scheduler_backend,
            root_dir=self.config.project.root_dir,
        )
        return backend.schedule(
            self,
            kind=kind,
            run_at=run_at,
            payload=payload,
            interval_seconds=interval_seconds,
        )

    def list_scheduled_jobs(self) -> list[ScheduledJob]:
        backend = get_scheduler_backend(
            self.config.pollypm.scheduler_backend,
            root_dir=self.config.project.root_dir,
        )
        return backend.list_jobs(self)

    def run_scheduled_jobs(self) -> list[ScheduledJob]:
        backend = get_scheduler_backend(
            self.config.pollypm.scheduler_backend,
            root_dir=self.config.project.root_dir,
        )
        return backend.run_due(self)

    def write_snapshot(self, window: TmuxWindow, snapshot_lines: int) -> tuple[Path, str]:
        """Capture ``window`` and persist its tail to a snapshot file.

        Inputs: a ``TmuxWindow`` and the number of trailing lines to capture.
        Output: a ``(snapshot_path, content)`` tuple — the file on disk and the
        captured text.
        """
        target = window.pane_id or f"{window.session}:{window.name}"
        content = self.session_service.tmux.capture_pane(target, lines=snapshot_lines)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        snapshot_path = self.config.project.snapshots_dir / f"{window.name}-{stamp}.txt"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(content)
        return snapshot_path, content

    def _write_snapshot(self, window: TmuxWindow, snapshot_lines: int) -> tuple[Path, str]:
        return self.write_snapshot(window, snapshot_lines)

    def _update_alerts(
        self,
        launch: SessionLaunchSpec,
        window: TmuxWindow,
        *,
        pane_text: str,
        previous_log_bytes: int | None,
        previous_snapshot_hash: str | None,
        current_log_bytes: int,
        current_snapshot_hash: str,
    ) -> list[str]:
        session_name = launch.session.name
        shell_commands = {"bash", "zsh", "sh", "fish"}
        active_alerts: list[str] = []

        if window.pane_dead:
            self.store.upsert_alert(
                session_name,
                "pane_dead",
                "error",
                f"Pane {window.pane_id} in window {window.name} has exited",
            )
            active_alerts.append("pane_dead")
        else:
            self.store.clear_alert(session_name, "pane_dead")

        if window.pane_current_command in shell_commands:
            self.store.upsert_alert(
                session_name,
                "shell_returned",
                "warn",
                f"Window {window.name} appears to be back at the shell prompt ({window.pane_current_command})",
            )
            active_alerts.append("shell_returned")
        else:
            self.store.clear_alert(session_name, "shell_returned")

        if previous_log_bytes is not None and current_log_bytes <= previous_log_bytes:
            self.store.upsert_alert(
                session_name,
                "idle_output",
                "warn",
                f"No new pane output since the previous heartbeat for window {window.name}",
            )
            active_alerts.append("idle_output")
        else:
            self.store.clear_alert(session_name, "idle_output")

        if previous_snapshot_hash and previous_snapshot_hash == current_snapshot_hash:
            history = self.store.recent_heartbeats(session_name, limit=3)
            recent_hashes = [item.snapshot_hash for item in history[:3]]
            if len(recent_hashes) == 3 and len(set(recent_hashes)) == 1:
                self.store.upsert_alert(
                    session_name,
                    "suspected_loop",
                    "warn",
                    f"Window {window.name} has produced effectively the same snapshot for 3 heartbeats",
                )
                active_alerts.append("suspected_loop")
                longer_history = self.store.recent_heartbeats(session_name, limit=5)
                longer_hashes = [item.snapshot_hash for item in longer_history[:5]]
                if len(longer_hashes) == 5 and len(set(longer_hashes)) == 1:
                    self._maybe_nudge_stalled_session(launch)
            else:
                self.store.clear_alert(session_name, "suspected_loop")
        else:
            self.store.clear_alert(session_name, "suspected_loop")

        lower_pane = pane_text.lower()
        if self._pane_has_auth_failure(lower_pane):
            self.store.upsert_alert(
                session_name,
                "auth_broken",
                "error",
                f"Window {window.name} reported authentication failure",
            )
            self.store.upsert_account_runtime(
                account_name=launch.account.name,
                provider=launch.account.provider.value,
                status="auth_broken",
                reason="live session reported authentication failure",
            )
            active_alerts.append("auth_broken")
        else:
            self.store.clear_alert(session_name, "auth_broken")

        if self._pane_has_capacity_failure(lower_pane):
            self.store.upsert_alert(
                session_name,
                "capacity_exhausted",
                "error",
                f"Window {window.name} reported a usage or quota limit",
            )
            self.store.upsert_account_runtime(
                account_name=launch.account.name,
                provider=launch.account.provider.value,
                status="exhausted",
                reason="live session reported capacity exhaustion",
            )
            active_alerts.append("capacity_exhausted")
        else:
            self.store.clear_alert(session_name, "capacity_exhausted")

        if self._pane_has_provider_outage(lower_pane):
            self.store.upsert_alert(
                session_name,
                "provider_outage",
                "warn",
                f"Window {window.name} appears to be hitting a provider outage",
            )
            self.store.upsert_account_runtime(
                account_name=launch.account.name,
                provider=launch.account.provider.value,
                status="provider_outage",
                reason="live session reported upstream provider instability",
                available_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            )
            active_alerts.append("provider_outage")
        else:
            self.store.clear_alert(session_name, "provider_outage")

        return active_alerts

    def _maybe_nudge_stalled_session(self, launch: SessionLaunchSpec) -> None:
        if launch.session.role == "reviewer":
            self._maybe_nudge_reviewer_review(launch)
            return
        if launch.session.role == "operator-pm":
            return  # Polly no longer reviews — Russell handles it
        if launch.session.role != "worker":
            return
        lease = self.store.get_lease(launch.session.name)
        if lease is not None and lease.owner == "human":
            self.store.record_event(
                launch.session.name,
                "heartbeat_nudge_skipped",
                "Skipped stalled-worker nudge because session is leased to human",
            )
            return
        # Check if there's a queued task the worker could pick up
        nudge = self._build_task_nudge(launch)
        self.send_input(
            launch.session.name,
            nudge or self._STALL_NUDGE_MESSAGE,
            owner="heartbeat",
            force=lease is not None and lease.owner != "human",
        )

    def _maybe_nudge_reviewer_review(self, launch: SessionLaunchSpec) -> None:
        """Nudge the reviewer (Russell) if tasks are waiting for review."""
        lease = self.store.get_lease(launch.session.name)
        if lease is not None and lease.owner == "human":
            return
        nudge = self._build_review_nudge()
        if nudge is None:
            return
        try:
            self.send_input(
                launch.session.name,
                nudge,
                owner="heartbeat",
                force=lease is not None and lease.owner != "human",
            )
        except Exception:  # noqa: BLE001
            pass

    def _build_review_nudge(self) -> str | None:
        """Check all projects for tasks in review state.

        Per-project review-task lists are cached in ``_REVIEW_NUDGE_CACHE``
        keyed by ``state.db`` mtime — unchanged projects skip SQLite entirely.
        Mirrors the pattern used by ``_dashboard_project_tasks`` (cockpit.py)
        so the heartbeat cost scales with changed projects, not total projects.
        """
        try:
            from pollypm.work.cli import _resolve_db_path

            review_tasks: list[str] = []
            live_keys: set[str] = set()
            for project_key in self.config.projects:
                live_keys.add(project_key)
                db_path = _resolve_db_path(".pollypm/state.db", project=project_key)
                if not db_path.exists():
                    _REVIEW_NUDGE_CACHE.pop(project_key, None)
                    continue
                entries = _review_tasks_for_project(project_key, db_path)
                review_tasks.extend(entries)
            # Evict cache entries for projects no longer in config.
            for stale in set(_REVIEW_NUDGE_CACHE) - live_keys:
                _REVIEW_NUDGE_CACHE.pop(stale, None)
            if not review_tasks:
                return None
            lines = [
                f"You have {len(review_tasks)} task(s) waiting for your review:",
                *review_tasks,
                "",
                "Review with: pm task status <id>, then pm task approve <id> --actor russell or pm task reject <id> --actor russell --reason \"...\"",
            ]
            return "\n".join(lines)
        except Exception:  # noqa: BLE001
            return None

    def _build_task_nudge(self, launch: SessionLaunchSpec) -> str | None:
        """Check for queued tasks assigned to this worker and build a nudge message."""
        try:
            from pollypm.work.cli import _resolve_db_path
            from pollypm.work.sqlite_service import SQLiteWorkService

            project = launch.session.project
            db_path = _resolve_db_path(".pollypm/state.db", project=project)
            if not db_path.exists():
                return None
            with SQLiteWorkService(db_path=db_path) as svc:
                task = svc.next(project=project)
                if task is None:
                    return None
                return (
                    f"You have work waiting. Task {task.task_id} — \"{task.title}\" "
                    f"is queued for your project. "
                    f"Claim it: pm task claim {task.task_id}"
                )
        except Exception:
            return None

    def _lease_timeout(self) -> timedelta:
        return timedelta(minutes=self.config.pollypm.lease_timeout_minutes)

    def release_expired_leases(self, *, now: datetime | None = None) -> list[LeaseRecord]:
        current_time = now or datetime.now(UTC)
        released: list[LeaseRecord] = []
        for lease in self.store.list_leases():
            try:
                updated_at = datetime.fromisoformat(lease.updated_at)
            except ValueError:
                updated_at = current_time - self._lease_timeout() - timedelta(seconds=1)
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            age = current_time - updated_at.astimezone(UTC)
            if age < self._lease_timeout():
                continue
            self.store.clear_lease(lease.session_name)
            self.store.record_event(
                lease.session_name,
                "lease",
                (
                    f"Auto-released expired lease held by {lease.owner} "
                    f"after {self.config.pollypm.lease_timeout_minutes} minutes"
                ),
            )
            released.append(lease)
        return released

    def claim_lease(self, session_name: str, owner: str, note: str = "") -> None:
        self._require_session(session_name)
        self._assert_lease_available(
            session_name,
            owner=owner,
            action="claim a lease for",
        )
        self.store.set_lease(session_name, owner, note)
        message = f"Lease claimed by {owner}"
        if note:
            message = f"{message}: {note}"
        self.store.record_event(session_name, "lease", message)
        # Schedule auto-release after timeout
        try:
            backend = get_scheduler_backend(
                self.config.pollypm.scheduler_backend,
                root_dir=self.config.project.root_dir,
            )
            backend.schedule(
                self,
                kind="release_lease",
                run_at=datetime.now(UTC) + self._lease_timeout(),
                payload={"session_name": session_name, "owner": owner},
            )
        except Exception:  # noqa: BLE001
            pass  # Best-effort — lease still works without auto-release

    def release_lease(self, session_name: str, expected_owner: str | None = None) -> None:
        self._require_session(session_name)
        if expected_owner is not None:
            current = self.store.get_lease(session_name)
            if current is None or current.owner != expected_owner:
                return  # Lease was already released or reclaimed by someone else
        self.store.clear_lease(session_name)
        self.store.record_event(session_name, "lease", "Lease released")

    def _resolve_send_target(self, launch: SessionLaunchSpec) -> str:
        """Find the actual tmux target for a session, checking both storage closet and cockpit mount."""
        storage = self.storage_closet_session_name()
        storage_target = f"{storage}:{launch.window_name}"
        # Check if the window exists in the storage closet
        if self.session_service.tmux.has_session(storage):
            windows = {w.name for w in self.session_service.tmux.list_windows(storage)}
            if launch.window_name in windows:
                return storage_target
        # Not in storage — check if it's mounted in the cockpit
        cockpit_session = self.config.project.tmux_session
        state_path = self.config.project.base_dir / "cockpit_state.json"
        if state_path.exists():
            try:
                import json
                state = json.loads(state_path.read_text())
                if state.get("mounted_session") == launch.session.name:
                    right_pane = state.get("right_pane_id")
                    if right_pane:
                        # Validate the pane still exists in tmux
                        try:
                            cockpit_window = f"{cockpit_session}:{self._CONSOLE_WINDOW}"
                            panes = self.session_service.tmux.list_panes(cockpit_window)
                            if any(p.pane_id == right_pane for p in panes):
                                return right_pane
                        except Exception:  # noqa: BLE001
                            pass
                        # Stale pane — clear state
                        state.pop("right_pane_id", None)
                        state.pop("mounted_session", None)
                        from pollypm.atomic_io import atomic_write_json
                        atomic_write_json(state_path, state)
            except Exception:  # noqa: BLE001
                pass
        # Session not found anywhere — raise a clear error
        raise RuntimeError(
            f"Session '{launch.session.name}' (window '{launch.window_name}') not found in "
            f"storage closet or cockpit. The worker may have crashed or been decommissioned. "
            f"Try `pm worker-start {launch.session.project}` to relaunch it."
        )

    def send_input(
        self,
        session_name: str,
        text: str,
        *,
        owner: str = "pollypm",
        force: bool = False,
        press_enter: bool = True,
    ) -> None:
        launch = self._launch_by_session(session_name)
        self._assert_lease_available(session_name, owner=owner, force=force, action="send input to")

        target = self._resolve_send_target(launch)
        prefixed = _prefix_for_owner(owner, text)
        self.session_service.tmux.send_keys(target, prefixed, press_enter=press_enter)
        # Codex CLI buffers input and requires a second Enter to submit.
        if press_enter and launch.session.provider is ProviderKind.CODEX:
            import time
            time.sleep(0.3)
            self.session_service.tmux.send_keys(target, "", press_enter=True)
        # Verify the message left the input bar.
        if press_enter:
            self._verify_input_submitted(target, text, launch)
        if owner == "human":
            self.store.set_lease(session_name, "human", "automatic lease from direct human input")
        self.store.record_event(session_name, "send_input", f"{owner} sent input: {text}")

    def _verify_input_submitted(
        self,
        target: str,
        text: str,
        launch: SessionLaunchSpec,
        max_retries: int = 3,
    ) -> None:
        """Check that sent text is no longer sitting in the input bar.

        Captures the last few lines of the pane. If the text still appears
        on the final line (the input prompt), press Enter again.
        """
        import time

        # Use a prefix of the message for matching (input may be truncated)
        check_text = text[:60].strip()
        if not check_text:
            return

        for attempt in range(max_retries):
            time.sleep(0.4)
            try:
                snapshot = self.session_service.tmux.capture_pane(target, lines=5)
            except Exception:
                return  # Can't capture — skip verification
            lines = snapshot.strip().splitlines()
            if not lines:
                return
            # The input bar is typically the last non-empty line starting with
            # the prompt marker (❯ for Claude, › for Codex, or just text)
            last_lines = "\n".join(lines[-3:])
            if check_text in last_lines:
                # Message is still in the input bar — press Enter again
                try:
                    self.session_service.tmux.send_keys(target, "", press_enter=True)
                except Exception:
                    return
            else:
                return  # Message was submitted

    def open_alerts(self) -> list[AlertRecord]:
        return self.store.open_alerts()

    def leases(self) -> list[LeaseRecord]:
        return self.store.list_leases()

    def _pane_has_auth_failure(self, lowered_pane: str) -> bool:
        patterns = [
            "please run /login",
            "invalid authentication credentials",
            "authentication_error",
            "not authenticated",
        ]
        return any(pattern in lowered_pane for pattern in patterns)

    def _pane_has_capacity_failure(self, lowered_pane: str) -> bool:
        patterns = [
            "usage limit",
            "quota exceeded",
            "0% left",
            "out of credits",
            "credit balance is too low",
        ]
        return any(pattern in lowered_pane for pattern in patterns)

    def _pane_has_provider_outage(self, lowered_pane: str) -> bool:
        patterns = [
            "temporarily unavailable",
            "try again later",
            "server error",
            "overloaded",
            "service unavailable",
        ]
        return any(pattern in lowered_pane for pattern in patterns)

    def _primary_failure(self, alerts: list[str]) -> str | None:
        for alert_type in [
            "auth_broken",
            "capacity_exhausted",
            "provider_outage",
            "pane_dead",
            "shell_returned",
            "missing_window",
            # Proactive: rolled over before the hard capacity cutoff.
            "capacity_low",
        ]:
            if alert_type in alerts:
                return alert_type
        return None

    def _maybe_mark_capacity_low(
        self, launch: SessionLaunchSpec, active_alerts: list[str],
    ) -> None:
        """Raise a `capacity_low` alert when the account is at or below the
        proactive-rollover threshold and no hard failure is already open.

        The recovery flow treats ``capacity_low`` like ``capacity_exhausted``
        (force failover, no retry on the same account) so a graceful rollover
        happens before the account is fully drained.
        """
        if launch.session.role == "heartbeat-supervisor":
            return  # the heartbeat itself should not roll; it'd create a gap.
        # If a hard failure is already recorded, defer to that path.
        for hard in (
            "auth_broken", "capacity_exhausted", "provider_outage",
            "pane_dead", "shell_returned", "missing_window",
        ):
            if hard in active_alerts:
                return
        try:
            from pollypm.capacity import account_needs_proactive_rollover
            needs_roll, probe = account_needs_proactive_rollover(
                self.config, self.store, launch.account.name,
            )
        except Exception:  # noqa: BLE001
            return
        if not needs_roll:
            self.store.clear_alert(launch.session.name, "capacity_low")
            return
        # Skip if we already rolled this session for low capacity in the
        # current recovery window — avoids oscillating between accounts.
        runtime = self.store.get_session_runtime(launch.session.name)
        if runtime and runtime.last_failure_type == "capacity_low" and runtime.status == "recovering":
            return
        pct = probe.remaining_pct if probe.remaining_pct is not None else -1
        self.store.upsert_alert(
            launch.session.name,
            "capacity_low",
            "warn",
            f"Account {launch.account.name} at {pct}% left — proactively rolling over",
        )
        self.store.record_event(
            launch.session.name,
            "proactive_rollover",
            f"Account {launch.account.name} at {pct}% left; triggering failover",
        )
        active_alerts.append("capacity_low")

    def _refresh_account_runtime_metadata(self, account_name: str) -> None:
        account = self.config.accounts[account_name]
        access_expires_at: str | None = None
        refresh_available = False
        if account.provider is ProviderKind.CLAUDE and account.home is not None:
            credentials_path = account.home / ".claude" / ".credentials.json"
            if credentials_path.exists():
                try:
                    import json

                    data = json.loads(credentials_path.read_text())
                    oauth = data.get("claudeAiOauth", {})
                    expires_at = oauth.get("expiresAt")
                    if isinstance(expires_at, (int, float)):
                        access_expires_at = datetime.fromtimestamp(expires_at / 1000, UTC).isoformat()
                    refresh_available = bool(oauth.get("refreshToken"))
                except Exception:  # noqa: BLE001
                    access_expires_at = None
        self.store.upsert_account_runtime(
            account_name=account_name,
            provider=account.provider.value,
            status="healthy",
            reason="local auth metadata loaded",
            access_expires_at=access_expires_at,
            refresh_available=refresh_available,
        )

    def _account_is_viable(self, account_name: str) -> bool:
        runtime = self.store.get_account_runtime(account_name)
        if runtime is not None and runtime.status in {"auth_broken", "exhausted", "provider_outage"}:
            return False
        account = self.config.accounts[account_name]
        if account.home is None:
            return False
        if account.provider is ProviderKind.CLAUDE:
            claude_dir = account.home / ".claude"
            # Claude Code may use .credentials.json (file auth) or macOS
            # Keychain (.claude.json contains the session config).  Accept
            # either as evidence the account has been set up.
            return (
                (claude_dir / ".credentials.json").exists()
                or (claude_dir / ".claude.json").exists()
            )
        if account.provider is ProviderKind.CODEX:
            return (account.home / ".codex" / "auth.json").exists()
        return False

    def _candidate_accounts(self, launch: SessionLaunchSpec, *, allow_same: bool) -> list[str]:
        preferred = []
        current = launch.account.name
        if allow_same:
            preferred.append(current)
        for name in self.config.pollypm.failover_accounts:
            if name not in preferred:
                preferred.append(name)
        controller = self.config.pollypm.controller_account
        if controller and controller not in preferred:
            preferred.append(controller)
        for name in self.config.accounts:
            if name not in preferred:
                preferred.append(name)
        same_provider = [name for name in preferred if self.config.accounts[name].provider is launch.session.provider]
        cross_provider = [name for name in preferred if self.config.accounts[name].provider is not launch.session.provider]
        ordered = same_provider + cross_provider
        return [name for name in ordered if self._account_is_viable(name)]

    def _record_recovery_attempt(self, session_name: str, *, status: str, failure_type: str, failure_message: str) -> tuple[bool, int]:
        now = datetime.now(UTC)
        runtime = self.store.get_session_runtime(session_name)
        attempts = 1
        started_at = now.isoformat()
        if runtime is not None and runtime.recovery_window_started_at:
            try:
                previous_start = datetime.fromisoformat(runtime.recovery_window_started_at)
            except ValueError:
                previous_start = now
            if now - previous_start <= self._RECOVERY_WINDOW:
                attempts = runtime.recovery_attempts + 1
                started_at = runtime.recovery_window_started_at
        self.store.upsert_session_runtime(
            session_name=session_name,
            status=status,
            effective_account=runtime.effective_account if runtime else None,
            effective_provider=runtime.effective_provider if runtime else None,
            recovery_attempts=attempts,
            recovery_window_started_at=started_at,
            last_failure_type=failure_type,
            last_failure_message=failure_message,
        )
        # Hard limit: stop entirely after too many total attempts across all windows
        if attempts > self._RECOVERY_HARD_LIMIT:
            return False, attempts
        return attempts <= self._RECOVERY_LIMIT, attempts

    # Failure types where the session is gone — no live interaction to protect.
    _DEAD_SESSION_FAILURES = frozenset({"missing_window", "pane_dead", "shell_returned"})

    # Map detected failure types to the raw signals the RecoveryPolicy
    # expects. Keeps the policy decoupled from Supervisor's vocabulary.
    _FAILURE_TO_SIGNAL_KW = {
        "missing_window": {"window_present": False},
        "pane_dead": {"pane_dead": True},
        "shell_returned": {"pane_dead": True},
        "auth_broken": {"auth_failure": True},
    }

    def _policy_recommendation(
        self,
        launch: SessionLaunchSpec,
        failure_type: str,
    ) -> object | None:
        """Ask the recovery policy for an intervention given ``failure_type``.

        Returns ``None`` when the policy declines (e.g. already healthy).
        Never raises — a broken policy falls back to no-recommendation so
        the existing apply path still runs.
        """
        try:
            from pollypm.capacity import CapacityState
            from pollypm.recovery.base import (
                InterventionHistoryEntry,
                SessionSignals,
            )

            signal_kwargs: dict[str, object] = {
                "session_name": launch.session.name,
            }
            signal_kwargs.update(self._FAILURE_TO_SIGNAL_KW.get(failure_type, {}))
            if failure_type in ("capacity_exhausted", "capacity_low"):
                # Both map to a failover-triggering state — capacity_low
                # proactively rolls over before hitting exhaustion.
                signal_kwargs["capacity_state"] = CapacityState.EXHAUSTED
            signals = SessionSignals(**signal_kwargs)  # type: ignore[arg-type]

            runtime = self.store.get_session_runtime(launch.session.name)
            previous = runtime.recovery_attempts if runtime else 0
            history = [
                InterventionHistoryEntry(action="") for _ in range(previous)
            ]

            health = self.recovery_policy.classify(signals)
            return self.recovery_policy.select_intervention(health, signals, history)
        except Exception:  # noqa: BLE001
            return None

    def _maybe_recover_session(self, launch: SessionLaunchSpec, *, failure_type: str, failure_message: str) -> None:
        return self.maybe_recover_session(launch, failure_type=failure_type, failure_message=failure_message)

    def maybe_recover_session(self, launch: SessionLaunchSpec, *, failure_type: str, failure_message: str) -> None:
        """Attempt automatic recovery for ``launch`` given a detected failure.

        Inputs: the ``SessionLaunchSpec``, a ``failure_type`` label (e.g.
        ``"pane_dead"``, ``"auth_broken"``), and a human-readable
        ``failure_message``. Output: ``None`` — side effects include alert
        upserts, lease handling, and session restarts per recovery policy.

        The decision side (what action to take) runs through the pluggable
        :class:`pollypm.recovery.RecoveryPolicy`. The apply side (which
        tmux / state mutations happen) lives in this method and is slated
        to move to a SessionRecovery subsystem in Step 8 of the Supervisor
        decomposition.
        """
        # Consult the recovery policy for a canonical intervention
        # recommendation. The apply side below currently only branches on
        # ``failure_type`` — Step 8 will fold these into intervention-kind
        # dispatch. Recording the recommendation now means events/alerts
        # carry the policy-selected action kind.
        recommendation = self._policy_recommendation(launch, failure_type)
        if recommendation is not None:
            self.store.record_event(
                launch.session.name,
                "recovery_recommendation",
                f"policy={self.recovery_policy.name} action={recommendation.action} "
                f"reason={recommendation.reason[:120]}",
            )

        lease = self.store.get_lease(launch.session.name)
        if lease is not None and lease.owner != "pollypm":
            if failure_type in self._DEAD_SESSION_FAILURES:
                # Session is dead — the lease is protecting nothing.  Release it
                # so recovery can proceed immediately.
                self.store.clear_lease(launch.session.name)
                self.store.clear_alert(launch.session.name, "recovery_waiting_on_human")
                self.store.record_event(
                    launch.session.name,
                    "lease_override",
                    f"Auto-released stale lease (owner={lease.owner}) — session is {failure_type}",
                )
            else:
                self.store.upsert_alert(
                    launch.session.name,
                    "recovery_waiting_on_human",
                    "warn",
                    f"Recovery is queued behind lease owner {lease.owner} for {launch.window_name}",
                )
                return
        if failure_type == "provider_outage":
            self.store.upsert_session_runtime(
                session_name=launch.session.name,
                status="blocked",
                effective_account=launch.account.name,
                effective_provider=launch.account.provider.value,
                retry_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                last_failure_type=failure_type,
                last_failure_message=failure_message,
            )
            return

        allowed, attempts = self._record_recovery_attempt(
            launch.session.name,
            status="recovering",
            failure_type=failure_type,
            failure_message=failure_message,
        )
        if not allowed:
            msg = f"Automatic recovery paused after {attempts} rapid failures"
            if attempts >= self._RECOVERY_HARD_LIMIT:
                msg = (
                    f"Automatic recovery STOPPED after {attempts} total failures. "
                    f"Session requires manual intervention (pm up or account re-auth)."
                )
            self.store.upsert_alert(
                launch.session.name,
                "recovery_limit",
                "error",
                msg,
            )
            self.store.upsert_session_runtime(
                session_name=launch.session.name,
                status="degraded",
                last_failure_type=failure_type,
                last_failure_message=failure_message,
            )
            return

        # For auth_broken on Claude accounts, allow retrying the same account —
        # the refresh token is long-lived and Claude Code will refresh on restart.
        # Force failover for capacity_exhausted (genuine account limit) and
        # capacity_low (proactive rollover near the limit — staying on the
        # same account defeats the purpose).
        allow_same = failure_type not in {"capacity_exhausted", "capacity_low"}
        candidates = self._candidate_accounts(launch, allow_same=allow_same)
        if not candidates:
            self.store.upsert_alert(
                launch.session.name,
                "blocked_no_capacity",
                "error",
                f"No viable account is currently available to recover {launch.window_name}",
            )
            self.store.upsert_session_runtime(
                session_name=launch.session.name,
                status="blocked",
                last_failure_type=failure_type,
                last_failure_message=failure_message,
                retry_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            )
            return

        last_error = ""
        for selected in candidates:
            try:
                self._restart_session(launch.session.name, selected, failure_type=failure_type)
                return
            except RuntimeError as exc:
                last_error = str(exc)
                candidate = self.config.accounts[selected]
                status = "auth_broken" if "authentication" in last_error.lower() or "/login" in last_error.lower() else "exhausted" if "usage limit" in last_error.lower() else "provider_outage" if "unavailable" in last_error.lower() or "server error" in last_error.lower() else "degraded"
                self.store.upsert_account_runtime(
                    account_name=selected,
                    provider=candidate.provider.value,
                    status=status,
                    reason=f"startup recovery failed: {last_error}",
                    available_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat() if status == "provider_outage" else None,
                )
                self.store.record_event(
                    launch.session.name,
                    "recovery_candidate_failed",
                    f"Recovery candidate {selected} failed: {last_error}",
                )

        self.store.upsert_alert(
            launch.session.name,
            "blocked_no_capacity",
            "error",
            f"Recovery failed for all viable accounts: {last_error or 'no candidate succeeded'}",
        )
        self.store.upsert_session_runtime(
            session_name=launch.session.name,
            status="blocked",
            last_failure_type=failure_type,
            last_failure_message=last_error or failure_message,
            retry_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        )

    def _restart_session(self, session_name: str, account_name: str, *, failure_type: str) -> None:
        return self.restart_session(session_name, account_name, failure_type=failure_type)

    def restart_session(self, session_name: str, account_name: str, *, failure_type: str) -> None:
        """Restart ``session_name`` on ``account_name`` after a failure.

        Inputs: the session name, the target account to switch to, and the
        ``failure_type`` that triggered the restart. Output: ``None`` — tears
        down the existing window, re-launches under the chosen account, and
        updates runtime/alert state.
        """
        launch = self._launch_by_session(session_name)
        self._assert_lease_available(
            session_name,
            owner="pollypm",
            action="restart",
        )
        tmux_session = self._tmux_session_for_launch(launch)
        previous_runtime = self.store.get_session_runtime(session_name)
        if self.session_service.tmux.has_session(tmux_session):
            window_map = self._window_map()
            if launch.window_name in window_map:
                self.session_service.tmux.kill_window(f"{tmux_session}:{launch.window_name}")
        account = self.config.accounts[account_name]
        self.store.upsert_session_runtime(
            session_name=session_name,
            status="recovering",
            effective_account=account_name,
            effective_provider=account.provider.value,
            last_failure_type=failure_type,
            last_failure_message=f"recovering on {account_name}",
        )
        try:
            self.launch_session(session_name)
        except RuntimeError as exc:
            # Retry once if window name collision
            if "already exists" in str(exc).lower() or "duplicate" in str(exc).lower():
                import time as _time
                _time.sleep(0.5)
                try:
                    self.launch_session(session_name)
                except Exception:
                    pass
        except Exception:
            self.store.upsert_session_runtime(
                session_name=session_name,
                status=previous_runtime.status if previous_runtime else "degraded",
                effective_account=previous_runtime.effective_account if previous_runtime else None,
                effective_provider=previous_runtime.effective_provider if previous_runtime else None,
                recovery_attempts=previous_runtime.recovery_attempts if previous_runtime else 0,
                recovery_window_started_at=previous_runtime.recovery_window_started_at if previous_runtime else None,
                last_failure_type=failure_type,
                last_failure_message=f"failed to recover on {account_name}",
                last_checkpoint_path=previous_runtime.last_checkpoint_path if previous_runtime else None,
                retry_at=previous_runtime.retry_at if previous_runtime else None,
                last_recovered_at=previous_runtime.last_recovered_at if previous_runtime else None,
            )
            raise
        # Inject recovery prompt so the agent knows what it was doing
        try:
            from pollypm.recovery_prompt import build_recovery_prompt
            recovery = build_recovery_prompt(
                self.config, session_name, launch.session.project,
                provider=launch.session.provider,
            )
            rendered = recovery.render()
            if rendered.strip():
                target = self._resolve_send_target(launch)
                self.session_service.tmux.send_keys(target, rendered)
                self.store.record_event(session_name, "recovery_prompt", "Injected recovery prompt with checkpoint context")
        except Exception:  # noqa: BLE001
            pass  # recovery prompt is best-effort

        self.store.record_event(
            session_name,
            "recovered",
            f"Recovered {launch.window_name} in place using {account_name}",
        )
        # If recovered on the config-default account, clear the override
        # so the session doesn't carry a stale effective_account that blocks
        # future recovery from choosing the correct provider.
        config_default = launch.session.account if hasattr(launch.session, "account") else None
        if account_name == config_default:
            eff_account = None
            eff_provider = None
        else:
            eff_account = account_name
            eff_provider = account.provider.value
        self.store.upsert_session_runtime(
            session_name=session_name,
            status="healthy",
            effective_account=eff_account,
            effective_provider=eff_provider,
            last_recovered_at=datetime.now(UTC).isoformat(),
        )
        for alert_type in [
            "pane_dead",
            "shell_returned",
            "auth_broken",
            "capacity_exhausted",
            "capacity_low",
            "provider_outage",
            "missing_window",
            "recovery_waiting_on_human",
            "blocked_no_capacity",
        ]:
            self.store.clear_alert(session_name, alert_type)

    def launch_by_session(self, session_name: str) -> SessionLaunchSpec:
        """Return the ``SessionLaunchSpec`` for ``session_name``.

        Thin delegator to :meth:`LaunchPlanner.launch_by_session`.
        """
        return self.launch_planner.launch_by_session(session_name)

    def _launch_by_session(self, session_name: str) -> SessionLaunchSpec:
        return self.launch_by_session(session_name)

    def require_session(self, session_name: str) -> None:
        """Raise ``KeyError`` if ``session_name`` is not a planned session.

        Input: a session name. Output: ``None`` on success; used as a guard
        before operating on a session.
        """
        self.launch_by_session(session_name)

    def _require_session(self, session_name: str) -> None:
        return self.require_session(session_name)

    def launch_session(
        self,
        session_name: str,
        on_status: Callable[[str], None] | None = None,
    ) -> SessionLaunchSpec:
        launch, target = self.create_session_window(session_name, on_status=on_status)
        if target is not None:
            self._stabilize_launch(launch, target, on_status=on_status)
        return launch

    def create_session_window(
        self,
        session_name: str,
        on_status: Callable[[str], None] | None = None,
    ) -> tuple[SessionLaunchSpec, str | None]:
        """Create the tmux window for a session without stabilizing it.

        Returns ``(launch, target)`` where *target* is the tmux pane
        address.  If the window already exists, *target* is ``None``.
        """
        launch = self._launch_by_session(session_name)
        tmux_session = self._tmux_session_for_launch(launch)
        window_map = self._window_map()
        if launch.window_name in window_map:
            return launch, None

        if on_status:
            on_status(f"Creating tmux window for {session_name}...")
        if not self.session_service.tmux.has_session(tmux_session):
            self.session_service.tmux.create_session(tmux_session, launch.window_name, launch.command)
            target = f"{tmux_session}:0"
            self.session_service.tmux.set_window_option(target, "allow-passthrough", "on")
        else:
            self.session_service.tmux.create_window(tmux_session, launch.window_name, launch.command, detached=True)
            target = f"{tmux_session}:{launch.window_name}"
            self.session_service.tmux.set_window_option(target, "allow-passthrough", "on")
        # Cap scrollback to prevent slow pane-switching in the cockpit
        self.session_service.tmux.set_pane_history_limit(target, 200)
        self.session_service.tmux.pipe_pane(target, launch.log_path)
        self._record_launch(launch)
        return launch, target

    def stop_session(self, session_name: str) -> None:
        launch = self._launch_by_session(session_name)
        self._assert_lease_available(
            session_name,
            owner="pollypm",
            action="stop",
        )
        tmux_session = self._tmux_session_for_launch(launch)
        if not self.session_service.tmux.has_session(tmux_session):
            return
        window_map = self._window_map()
        if launch.window_name not in window_map:
            return
        self.session_service.tmux.kill_window(f"{tmux_session}:{launch.window_name}")
        self._release_session_locks(launch)
        self.store.record_event(session_name, "stop", f"Stopped tmux window {launch.window_name}")

    def focus_session(self, session_name: str) -> None:
        launch = self._launch_by_session(session_name)
        tmux_session = self._tmux_session_for_launch(launch)
        if not self.session_service.tmux.has_session(tmux_session):
            raise RuntimeError(f"tmux session does not exist: {tmux_session}")
        self.session_service.tmux.select_window(f"{tmux_session}:{launch.window_name}")

    def switch_session_account(self, session_name: str, account_name: str) -> None:
        launch = self._launch_by_session(session_name)
        self._assert_lease_available(
            session_name,
            owner="pollypm",
            action="switch the account for",
        )
        account = self.config.accounts.get(account_name)
        if account is None:
            raise KeyError(f"Unknown account: {account_name}")
        self.store.upsert_session_runtime(
            session_name=session_name,
            status="recovering",
            effective_account=account_name,
            effective_provider=account.provider.value,
            last_failure_type="manual_switch",
            last_failure_message=f"manually switched to {account_name}",
        )
        self._restart_session(session_name, account_name, failure_type="manual_switch")
        self.store.record_event(
            session_name,
            "manual_switch",
            f"Switched {launch.window_name} to {account_name}",
        )

    def _assert_lease_available(
        self,
        session_name: str,
        *,
        owner: str,
        action: str,
        force: bool = False,
    ) -> None:
        lease = self.store.get_lease(session_name)
        if lease is None or lease.owner == owner or force:
            return
        raise RuntimeError(
            f"Cannot {action} {session_name}: session is currently leased to {lease.owner}; use --force to bypass"
        )

    def _release_session_locks(self, launch: SessionLaunchSpec) -> None:
        release_session_lock(self.config.project.logs_dir / launch.session.name, launch.session.name)
        project_path = self._project_path_for_session(launch.session.project)
        release_session_lock(project_checkpoints_dir(project_path) / launch.session.name, launch.session.name)
        release_session_lock(project_transcripts_dir(project_path) / launch.session.name, launch.session.name)
        worktrees_root = project_worktrees_dir(project_path)
        try:
            relative = launch.session.cwd.resolve().relative_to(worktrees_root.resolve())
        except ValueError:
            return
        parts = relative.parts
        if parts:
            release_session_lock(worktrees_root / parts[0], launch.session.name)

    def _project_path_for_session(self, project_key: str) -> Path:
        project = self.config.projects.get(project_key)
        if project is not None:
            return project.path
        return self.config.project.root_dir

    def _cockpit_cmd(self) -> str:
        """The pm cockpit command string for the current environment."""
        import shutil
        pm_path = shutil.which("pm")
        if pm_path:
            return shlex.quote(pm_path) + " cockpit"
        root = shlex.quote(str(self.config.project.root_dir))
        return f"cd {root} && uv run pm cockpit"

    def console_command(self) -> str:
        """Return the shell command for the cockpit rail pane.

        Thin delegator to :meth:`ConsoleWindowManager.console_command`.
        """
        return self.console_window.console_command()

    def _console_command(self) -> str:
        return self.console_command()

    def start_cockpit_tui(self, session_name: str) -> None:
        """Send the cockpit TUI command to the rail pane with a restart loop."""
        cockpit_cmd = self._cockpit_cmd()
        target = f"{session_name}:{self._CONSOLE_WINDOW}"
        panes = self.session_service.tmux.list_panes(target)
        rail_pane = min(panes, key=lambda p: int(getattr(p, "pane_left", 0)))
        loop_cmd = (
            f"while true; do {cockpit_cmd}; "
            f'echo "[Rail exited — restarting in 2s]"; sleep 2; done'
        )
        self.session_service.tmux.send_keys(rail_pane.pane_id, loop_cmd)

    def _controller_candidates(self) -> list[str]:
        ordered = [self.config.pollypm.controller_account, *self.config.pollypm.failover_accounts]
        candidates: list[str] = []
        seen: set[str] = set()
        for name in ordered:
            if not name or name in seen or name not in self.config.accounts:
                continue
            seen.add(name)
            candidates.append(name)
        return candidates

    def _probe_controller_account(self, account_name: str) -> None:
        account = self.config.accounts[account_name]
        operator_session = self._effective_session(self.config.sessions["operator"], controller_account=account_name)
        account = self._effective_account(operator_session, self.config.accounts[operator_session.account])
        output = self._run_probe(account)
        lowered = output.lower()
        pane_tail = _last_lines(output, n=5)
        if account.provider is ProviderKind.CLAUDE:
            if "ok" in lowered and "authentication" not in lowered:
                return
            raise RuntimeError(
                format_probe_failure(
                    provider="Claude",
                    account_name=account_name,
                    account_email=account.email,
                    reason=(
                        "the `claude -p 'Reply with ok'` probe did not "
                        "return 'ok' within the probe window"
                    ),
                    pane_tail=pane_tail,
                    fix=(
                        f"run `pm accounts` to check login state, then "
                        f"`pm relogin {account_name}` if the session expired."
                    ),
                )
            )
        if account.provider is ProviderKind.CODEX:
            if "usage limit" in lowered:
                raise RuntimeError(
                    format_probe_failure(
                        provider="Codex",
                        account_name=account_name,
                        account_email=account.email,
                        reason="the account is out of credits",
                        pane_tail=pane_tail,
                        fix=(
                            f"switch the controller to a different account "
                            f"with `pm failover` (see `pm accounts` for "
                            f"current state), or top up '{account_name}' "
                            f"and rerun `pm up`."
                        ),
                    )
                )
            if "not logged" in lowered or "login" in lowered:
                raise RuntimeError(
                    format_probe_failure(
                        provider="Codex",
                        account_name=account_name,
                        account_email=account.email,
                        reason="the account is not authenticated",
                        pane_tail=pane_tail,
                        fix=(
                            f"run `pm relogin {account_name}` and retry "
                            f"`pm up`."
                        ),
                    )
                )
            if "error:" in lowered:
                raise RuntimeError(
                    format_probe_failure(
                        provider="Codex",
                        account_name=account_name,
                        account_email=account.email,
                        reason=(
                            "the `codex exec 'Reply with ok'` probe "
                            "returned an unexpected response"
                        ),
                        pane_tail=pane_tail,
                        fix=(
                            f"`pm relogin {account_name}` usually clears "
                            f"this. If it persists, run the probe command "
                            f"manually to see the raw output."
                        ),
                    )
                )
            return
        raise RuntimeError(f"Unsupported controller provider: {account.provider.value}")

    def _run_probe(self, account: AccountConfig) -> str:
        if account.provider is ProviderKind.CLAUDE:
            probe = LaunchCommand(
                argv=["claude", "-p", "Reply with ok"],
                env=dict(account.env),
                cwd=self.config.project.root_dir,
            )
        elif account.provider is ProviderKind.CODEX:
            probe = LaunchCommand(
                argv=["codex", "exec", "--skip-git-repo-check", "Reply with ok and nothing else"],
                env=dict(account.env),
                cwd=self.config.project.root_dir,
            )
        else:
            raise RuntimeError(f"Unsupported controller provider: {account.provider.value}")

        runtime = get_runtime(account.runtime, root_dir=self.config.project.root_dir)
        command = runtime.wrap_command(probe, account, self.config.project)
        result = subprocess.run(
            command,
            check=False,
            shell=True,
            text=True,
            capture_output=True,
            timeout=90,
            executable="/bin/zsh",
        )
        return "\n".join(part for part in [result.stdout, result.stderr] if part)

    def _control_home(self, session_name: str) -> Path:
        return self.config.project.base_dir / self._CONTROL_HOMES_DIR / session_name

    def _effective_account(self, session: SessionConfig, account: AccountConfig) -> AccountConfig:
        if session.role not in self._CONTROL_ROLES or account.home is None:
            return account
        if self.readonly_state:
            return account
        # Claude auth tokens live in the macOS Keychain, keyed to the CLAUDE_CONFIG_DIR path hash.
        # Using a different home (control-homes/) would lose the keychain entry.
        # For Claude accounts, use the original account home directly.
        if account.provider is ProviderKind.CLAUDE:
            _prime_claude_home(account.home)
            return account
        control_home = self._sync_control_home(account, session.name)
        return replace(account, home=control_home)

    def _sync_control_home(self, account: AccountConfig, session_name: str) -> Path:
        if account.home is None:
            raise RuntimeError(f"Account {account.name} has no home configured")
        source_home = account.home
        target_home = self._control_home(session_name)
        target_home.mkdir(parents=True, exist_ok=True, mode=0o700)

        if account.provider is ProviderKind.CLAUDE:
            self._sync_file(source_home / ".claude.json", target_home / ".claude.json")
            self._sync_file(source_home / ".claude" / ".credentials.json", target_home / ".claude" / ".credentials.json")
            self._sync_file(source_home / ".claude" / "settings.json", target_home / ".claude" / "settings.json")
            _prime_claude_home(target_home)
        elif account.provider is ProviderKind.CODEX:
            self._sync_file(source_home / ".codex" / ".codex-global-state.json", target_home / ".codex" / ".codex-global-state.json")
            self._sync_file(source_home / ".codex" / "auth.json", target_home / ".codex" / "auth.json")
            self._sync_file(source_home / ".codex" / "config.toml", target_home / ".codex" / "config.toml")

        return target_home

    def _sync_file(self, source: Path, target: Path) -> None:
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif target.exists() and not source.exists():
            # Reverse sync: if the control-home has the file but the
            # account home lost it (e.g. during a recovery reset),
            # copy it back to prevent auth loss.
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, source)

    def _stabilize_launch(
        self,
        launch: SessionLaunchSpec,
        target: str,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        return self.stabilize_launch(launch, target, on_status=on_status)

    def stabilize_launch(
        self,
        launch: SessionLaunchSpec,
        target: str,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        """Wait for ``launch`` to reach a ready state at tmux ``target``.

        Inputs: the ``SessionLaunchSpec``, the ``"session:window"`` target
        string, and an optional ``on_status`` callback for progress messages.
        Output: ``None`` — blocks until provider-specific stabilization
        succeeds, then sends initial input if the launch is fresh.
        """
        name = launch.session.name

        def _prefixed_status(msg: str) -> None:
            if on_status:
                on_status(f"[{name}] {msg}")

        if launch.session.provider is ProviderKind.CLAUDE:
            self._stabilize_claude_launch(target, on_status=_prefixed_status)
        elif launch.session.provider is ProviderKind.CODEX:
            self._stabilize_codex_launch(
                target, on_status=_prefixed_status, account=launch.account,
            )
        _prefixed_status("Sending initial input...")
        self._send_initial_input_if_fresh(launch, target)
        self._mark_session_resume_ready(launch)

    def _mark_session_resume_ready(self, launch: SessionLaunchSpec) -> None:
        marker = launch.resume_marker
        if marker is None:
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now(UTC).isoformat().replace("+00:00", "Z") + "\n")

    def _send_initial_input_if_fresh(self, launch: SessionLaunchSpec, target: str) -> None:
        if launch.session.role not in self._INITIAL_INPUT_ROLES:
            return
        initial_input = launch.initial_input
        fresh_marker = launch.fresh_launch_marker
        if not initial_input or fresh_marker is None or not fresh_marker.exists():
            return
        kickoff = self._prepare_initial_input(launch.session.name, initial_input)
        # Small delay to let Claude Code's input bar fully initialize
        time.sleep(0.5)
        self.session_service.tmux.send_keys(target, kickoff)
        self._verify_input_submitted(target, kickoff, launch)
        fresh_marker.unlink(missing_ok=True)
        # Backup defense against (launch, target) crossed tuples: capture
        # the pane a few seconds later and confirm the expected persona
        # marker shows up. Non-blocking — fire-and-forget.
        self._schedule_persona_verify(launch, target)

    def _assert_session_launch_matches(
        self, session_name: str, initial_input: str,
    ) -> None:
        """Fail loud if ``session_name`` doesn't resolve to a matching launch.

        Two conditions are checked before we write a control-prompt file
        or hand text to the pane:

        1. ``launch.session.name == session_name`` — the planner returned
           a launch for the name we were asked to prepare for.
        2. ``launch.window_name`` matches the session's configured window
           (``SessionConfig.window_name`` or, when unset, the session
           name) — the (launch, target) tuple hasn't been crossed.

        On mismatch we log loudly, record a ``persona_swap_detected``
        event, and raise. A stuck pane is far easier to debug than a
        reviewer masquerading as Polly, which is the failure mode we
        observed overnight (2026-04-16: ``pm-operator`` window was
        running Russell's prompt; root cause in the recovery/bootstrap
        threading path is untraced).
        """
        try:
            launch = self.launch_by_session(session_name)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "persona_swap_detected: no launch for session_name=%r: %s",
                session_name, exc,
            )
            try:
                self.store.record_event(
                    session_name,
                    "persona_swap_detected",
                    f"no launch resolves for session_name={session_name!r}: {exc}",
                )
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                f"persona_swap_detected: no launch for session_name={session_name!r}"
            ) from exc

        cfg = self.config.sessions.get(session_name)
        expected_window = None
        if cfg is not None:
            expected_window = cfg.window_name or cfg.name

        mismatch_name = launch.session.name != session_name
        mismatch_window = (
            expected_window is not None and launch.window_name != expected_window
        )
        if mismatch_name or mismatch_window:
            logger.error(
                "persona_swap_detected: session_name=%r launch.session.name=%r "
                "launch.window_name=%r expected_window=%r role=%r",
                session_name,
                launch.session.name,
                launch.window_name,
                expected_window,
                launch.session.role,
            )
            details = (
                f"session_name={session_name!r} "
                f"launch.session.name={launch.session.name!r} "
                f"launch.window_name={launch.window_name!r} "
                f"expected_window={expected_window!r} "
                f"role={launch.session.role!r}"
            )
            try:
                self.store.record_event(
                    session_name, "persona_swap_detected", details,
                )
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"persona_swap_detected: {details}")

    def _prepare_initial_input(self, session_name: str, initial_input: str) -> str:
        # Fail-loud persona-swap guard. Raises before we touch disk or
        # the pane when the (launch, target) tuple looks wrong.
        self._assert_session_launch_matches(session_name, initial_input)
        if len(initial_input) <= 280:
            return initial_input
        prompts_dir = self.config.project.base_dir / "control-prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompts_dir / f"{session_name}.md"
        prompt_path.write_text(initial_input.rstrip() + "\n")
        # Use absolute paths so the kickoff resolves regardless of the
        # worker's cwd (workers run from their worktree, not project root).
        # See issue #263.
        display_path = prompt_path
        # Point to both SYSTEM.md (PollyPM reference) and the control prompt (role)
        instruct_path = self.config.project.root_dir / ".pollypm" / "docs" / "SYSTEM.md"
        if instruct_path.exists():
            instruct_display = instruct_path
            return (
                f'Read {instruct_display} for system context, then read {display_path} for your role. '
                f'Adopt both as your operating instructions, reply only "ready", then wait.'
            )
        return (
            f'Read {display_path}, adopt it as your operating instructions, reply only "ready", then wait.'
        )

    def _schedule_persona_verify(self, launch: SessionLaunchSpec, target: str) -> None:
        """Schedule a one-shot verify-after-kickoff on a background thread.

        Backup defense against crossed ``(launch, target)`` tuples. The
        strict assertion in :meth:`_prepare_initial_input` is the
        primary line of defense; this runs 5 s after the kickoff send
        and confirms the pane actually contains the expected persona
        marker. If the pane instead shows a *different* persona, we
        record ``persona_swap_verified`` and re-send the correct prompt
        (which will itself fail-safe through the assertion if something
        is still wrong).

        Tolerant by design: one capture attempt, one retry send, then
        log and give up. Never loops.
        """
        role = launch.session.role
        expected = _ROLE_PERSONA_MARKER.get(role)
        if expected is None:
            # Worker / triage: no stable persona marker — nothing to verify.
            return

        def _run() -> None:
            try:
                time.sleep(5.0)
                try:
                    pane = self.session_service.tmux.capture_pane(target, lines=50)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "persona verify: capture_pane failed for %s: %s",
                        launch.session.name, exc,
                    )
                    return
                unexpected = [
                    marker for other_role, marker in _ROLE_PERSONA_MARKER.items()
                    if other_role != role and marker in pane
                ]
                if expected in pane and not unexpected:
                    return  # All good.
                if expected not in pane and unexpected:
                    details = (
                        f"session_name={launch.session.name!r} "
                        f"role={role!r} expected={expected!r} "
                        f"found_markers={unexpected!r} target={target!r}"
                    )
                    logger.error("persona_swap_verified: %s", details)
                    try:
                        self.store.record_event(
                            launch.session.name,
                            "persona_swap_verified",
                            details,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    # Attempt one recovery resend. _prepare_initial_input
                    # will re-run the strict assertion and raise if the
                    # (launch, target) tuple is still crossed, which is
                    # the fail-safe we want.
                    initial_input = launch.initial_input
                    if not initial_input:
                        return
                    try:
                        kickoff = self._prepare_initial_input(
                            launch.session.name, initial_input,
                        )
                        self.session_service.tmux.send_keys(target, kickoff)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "persona verify: resend failed for %s: %s",
                            launch.session.name, exc,
                        )
                # Otherwise: marker not present yet (pane still
                # rendering), or both expected and unexpected present
                # (control prompt being read, mentions other persona).
                # Don't overreact.
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "persona verify thread crashed for %s: %s",
                    launch.session.name, exc,
                )

        t = threading.Thread(
            target=_run,
            name=f"persona-verify-{launch.session.name}",
            daemon=True,
        )
        t.start()

    def _stabilize_claude_launch(
        self, target: str, on_status: Callable[[str], None] | None = None,
    ) -> None:
        timeout = 90
        start = time.monotonic()
        deadline = start + timeout
        last_action = ""
        poll_interval = 0.2  # Start fast, back off after actions
        _status = on_status or (lambda _msg: None)
        _status("Waiting for Claude Code to start...")
        while time.monotonic() < deadline:
            elapsed = int(time.monotonic() - start)
            try:
                pane = self.session_service.tmux.capture_pane(target, lines=320)
            except Exception:  # noqa: BLE001
                _status(f"Waiting for Claude Code to start... ({elapsed}s)")
                time.sleep(poll_interval)
                continue
            lowered = pane.lower()

            if "select login method:" in lowered or "paste code here if prompted" in lowered:
                _status("Login required — authenticate from the cockpit")
                return
            if "please run /login" in lowered or "invalid authentication credentials" in lowered:
                _status("Login required — re-authenticate interactively")
                return

            if "choose the text style that looks best with your terminal" in lowered:
                if last_action != "theme":
                    _status(f"Dismissing theme picker... ({elapsed}s)")
                    self.session_service.tmux.send_keys(target, "", press_enter=True)
                    last_action = "theme"
                    poll_interval = 0.5  # Wait a bit after sending keys
                time.sleep(poll_interval)
                continue

            if "quick safety check" in lowered and "yes, i trust this folder" in lowered:
                if last_action != "trust":
                    _status(f"Accepting trust prompt... ({elapsed}s)")
                    self.session_service.tmux.send_keys(target, "", press_enter=True)
                    last_action = "trust"
                    poll_interval = 0.5
                time.sleep(poll_interval)
                continue

            if "warning: claude code running in bypass permissions mode" in lowered:
                if last_action != "bypass-confirm":
                    _status(f"Confirming bypass permissions mode... ({elapsed}s)")
                    self.session_service.tmux.send_keys(target, "2", press_enter=False)
                    self.session_service.tmux.send_keys(target, "", press_enter=True)
                    last_action = "bypass-confirm"
                    poll_interval = 0.5
                time.sleep(poll_interval)
                continue

            if "we recommend medium effort for opus" in lowered:
                if last_action != "effort":
                    _status(f"Dismissing effort recommendation... ({elapsed}s)")
                    self.session_service.tmux.send_keys(target, "", press_enter=True)
                    last_action = "effort"
                    poll_interval = 0.5
                time.sleep(poll_interval)
                continue

            if "❯" in pane and (
                "welcome back" in lowered
                or "0 tokens" in lowered
                or "/buddy" in pane
                or "bypass permissions" in lowered
                or "shift+tab" in lowered
            ):
                _status("Claude Code ready")
                return

            _status(f"Waiting for Claude Code to start... ({elapsed}s)")
            # Adaptive backoff: start at 0.2s, increase to max 1s
            poll_interval = min(poll_interval + 0.1, 1.0)
            time.sleep(poll_interval)

        _status("Timed out waiting for Claude Code")
        return

    def _stabilize_codex_launch(
        self,
        target: str,
        on_status: Callable[[str], None] | None = None,
        account: AccountConfig | None = None,
    ) -> None:
        timeout = 60
        start = time.monotonic()
        deadline = start + timeout
        last_action = ""
        ready_streak = 0
        poll_interval = 0.2
        _status = on_status or (lambda _msg: None)
        _status("Waiting for Codex to start...")
        while time.monotonic() < deadline:
            elapsed = int(time.monotonic() - start)
            try:
                pane = self.session_service.tmux.capture_pane(target, lines=260)
            except Exception:  # noqa: BLE001
                _status(f"Waiting for Codex to start... ({elapsed}s)")
                time.sleep(poll_interval)
                continue
            lowered = pane.lower()

            if "approaching rate limits" in lowered and "switch to gpt-5.1-codex-mini" in lowered:
                if last_action != "switch-mini":
                    _status(f"Switching to codex-mini due to rate limits... ({elapsed}s)")
                    self.session_service.tmux.send_keys(target, "", press_enter=True)
                    last_action = "switch-mini"
                    poll_interval = 0.5
                time.sleep(poll_interval)
                continue
            if "usage limit" in lowered:
                account_name = account.name if account else "<controller>"
                account_email = account.email if account else None
                raise RuntimeError(
                    format_probe_failure(
                        provider="Codex",
                        account_name=account_name,
                        account_email=account_email,
                        reason="the account is out of credits",
                        pane_tail=_last_lines(pane, n=5),
                        fix=(
                            f"switch the controller to a different account "
                            f"with `pm failover` (see `pm accounts`), or top "
                            f"up '{account_name}' and rerun `pm up`."
                        ),
                    )
                )
            if "press enter to continue" in lowered:
                if last_action != "continue":
                    _status(f"Dismissing continue prompt... ({elapsed}s)")
                    self.session_service.tmux.send_keys(target, "", press_enter=True)
                    last_action = "continue"
                    poll_interval = 0.5
                time.sleep(poll_interval)
                continue
            if "do you trust the contents of this directory" in lowered and "1. yes, continue" in lowered:
                if last_action != "trust":
                    _status(f"Accepting trust prompt... ({elapsed}s)")
                    self.session_service.tmux.send_keys(target, "", press_enter=True)
                    last_action = "trust"
                    poll_interval = 0.5
                time.sleep(poll_interval)
                continue
            prompt_visible = "% left" in lowered or "›" in pane
            working = "working (" in lowered and "esc to interrupt" in lowered
            booting = "booting mcp server" in lowered
            if "openai codex" in lowered and (prompt_visible or working) and not booting:
                ready_streak += 1
                if ready_streak >= 2:
                    _status("Codex ready")
                    return
                time.sleep(0.3)  # Quick recheck for streak confirmation
                continue
            ready_streak = 0
            _status(f"Waiting for Codex to start... ({elapsed}s)")
            poll_interval = min(poll_interval + 0.1, 1.0)
            time.sleep(poll_interval)

        _status("Timed out waiting for Codex")
        return


_TOKEN_RE = re.compile(r"(\d[\d,]*)\s+tokens\b", re.IGNORECASE)
_CLAUDE_MODEL_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9 .+-]*?(?:\([^)]+\))?)\s+·\s+Claude\b")
_CODEX_FOOTER_MODEL_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*(?:\s+[A-Za-z0-9._-]+)?)\s+·\s+\d+% left\b")
_CODEX_BANNER_MODEL_RE = re.compile(r"model:\s*([^\n]+)", re.IGNORECASE)


def _extract_token_metrics(provider: ProviderKind, pane_text: str) -> tuple[str, int] | None:
    model = _extract_claude_model_name(pane_text) if provider is ProviderKind.CLAUDE else _extract_codex_model_name(pane_text)
    if not model:
        return None
    match = None
    for candidate in _TOKEN_RE.finditer(pane_text):
        match = candidate
    if match is None:
        return None
    return (model, int(match.group(1).replace(",", "")))


def _extract_claude_model_name(pane_text: str) -> str | None:
    for line in pane_text.splitlines():
        match = _CLAUDE_MODEL_RE.search(line)
        if match:
            return " ".join(match.group(1).split())
    return None


def _extract_codex_model_name(pane_text: str) -> str | None:
    for line in reversed(pane_text.splitlines()):
        footer = _CODEX_FOOTER_MODEL_RE.search(line)
        if footer:
            return " ".join(footer.group(1).split())
        banner = _CODEX_BANNER_MODEL_RE.search(line)
        if banner:
            return " ".join(banner.group(1).split())
    return None
