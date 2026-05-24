"""JSONL writer + reader for the audit log.

See ``pollypm.audit`` package docstring for the why and the schema.

This module is intentionally tiny and dependency-free (stdlib only)
so it can be imported from anywhere in the codebase — work-service,
session-services, tmux, supervisor — without creating import cycles.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Rotation defaults — mirrored by :class:`pollypm.models.AuditSettings`.
# These constants are the fallback when config loading fails so a
# broken config never turns rotation off by stealth. Operators who
# want a different policy set ``[audit] rotate_size_mb`` /
# ``retention_count`` in pollypm.toml; see ``_resolve_rotation_policy``.
_DEFAULT_ROTATE_SIZE_MB = 50
_DEFAULT_RETENTION_COUNT = 4
# Env-var escape hatch for tests + ops debugging. When set to ``"1"``
# / ``"true"`` rotation is short-circuited even if the config says to
# rotate. The config ``disable_rotation`` knob is the supported user
# surface; this env var exists so tests in CI don't have to mutate
# ``~/.pollypm/pollypm.toml`` to assert non-rotating behaviour.
_DISABLE_ENV = "POLLYPM_AUDIT_DISABLE_ROTATION"
# Tests override these via monkeypatch to drive rotation without
# building 50 MB log files. The resolver below honours
# ``_test_rotate_size_bytes`` / ``_test_retention_count`` before
# falling through to config / defaults.
_test_rotate_size_bytes: int | None = None
_test_retention_count: int | None = None

# Override for tests + central tail relocation. When set, the central
# tail goes under ``$POLLYPM_AUDIT_HOME/<project>.jsonl``. The
# per-project log is unaffected — it always lives at
# ``<project>/.pollypm/audit.jsonl`` because that path is the
# project's own source of truth.
_HOME_ENV = "POLLYPM_AUDIT_HOME"

# Schema version. Bump when the on-disk shape changes incompatibly.
SCHEMA_VERSION = 1

# Stable event names — extend cautiously, the heartbeat consumer pins
# these strings. New events should follow ``noun.verb`` form.
EVENT_TASK_CREATED = "task.created"
EVENT_TASK_STATUS_CHANGED = "task.status_changed"
EVENT_TASK_CLAIMED_BY_SESSION = "task.claimed_by_session"
EVENT_TASK_CANCEL_WARNED = "task.cancel.warned"
EVENT_TASK_CANCEL_CONFIRMED = "task.cancel.confirmed"
EVENT_TASK_DELETED = "task.deleted"
EVENT_MARKER_CREATED = "marker.created"
EVENT_MARKER_RELEASED = "marker.released"
EVENT_MARKER_CREATE_FAILED = "marker.create_failed"
EVENT_MARKER_LEAKED = "marker.leaked"
EVENT_WORK_TABLE_CLEARED = "work_table.cleared"
# #savethenovel-followup: emitted from ``SQLiteWorkService.__init__``
# every time the service stamps work tables onto a SQLite file. The
# dual-DB layout (``~/.pollypm/state.db`` vs
# ``<workspace>/.pollypm/state.db``) made it easy for callsites to
# pass the wrong path and silently create empty work tables on the
# messages-side DB. Pairing this with the ``had_messages_table_pre_open``
# flag in metadata gives operators an immediate "wrong DB"
# breadcrumb — see comment in ``SQLiteWorkService.__init__`` for the
# reasoning.
EVENT_WORK_DB_OPENED = "work_db.opened"
# #1370 — emitted from ``JobWorkerPool.stop()`` whenever a worker
# thread does not exit within the join deadline. Pre-fix this fired
# 8841 times in errors.log because ``_run_one`` spawned a fresh
# ``threading.Thread`` per job attempt and abandoned it on handler
# timeout. The fix routes invocations through a per-worker
# ``ThreadPoolExecutor(max_workers=1)`` so the worker reuses one
# executor thread instead of leaking one per attempt; this event lets
# the fleet quantify whether the fix is holding without grepping
# ``errors.log``.
EVENT_WORKER_THREAD_LEAKED = "worker.thread_leaked"
# #1368 — emitted by ``cockpit_socket_reaper`` when it unlinks a stale
# ``cockpit-<pid>.sock`` whose owning PID is no longer alive. Lets
# operators forensically count leak rates without scraping logs.
EVENT_SOCKET_REAPED = "socket.reaped"
# #1432 — emitted by ``rail_daemon_reaper`` when supervisor bootstrap
# kills a stale ``pollypm.rail_daemon`` process from a prior boot.
# Carries ``role`` (always ``"rail"`` today, leaves room for a future
# ``"work"`` daemon), ``pid``, ``age_s``, and ``reason`` so operators
# can quantify how often the field machine is accumulating sibling
# daemons without scraping ``rail_daemon.log``.
EVENT_DAEMON_REAPED = "daemon.reaped"
# #1546-followup — emitted by ``rail_daemon_supervisor`` when the
# layer-2 watchdog (cockpit periodic timer or ``pm heartbeat`` cron
# path) detects a dead/stuck rail daemon and respawns it. Carries
# ``role`` (``"rail"``), ``state`` (the diagnose decision label),
# ``previous_pid``, ``last_tick_age_s``, ``revived``, ``spawn_error``,
# and ``kill_signal``. Lets operators quantify how often the layer-2
# supervisor catches a dead daemon — chronic revivals indicate a real
# problem (OOM, leak, crash-loop) that needs investigation, not just
# trust in the supervisor.
EVENT_DAEMON_REVIVED = "daemon.revived"
# #1398 — plan task evolution. ``plan.version_incremented`` fires when
# a plan task is refined in place (same task_id, version bump);
# ``plan.successor_created`` fires when a replan creates a new task
# linked to a predecessor. Together they let the heartbeat / inbox
# render plan-history breadcrumbs without scanning task content.
EVENT_PLAN_VERSION_INCREMENTED = "plan.version_incremented"
EVENT_PLAN_SUCCESSOR_CREATED = "plan.successor_created"
# #1414 — auto-unstick infrastructure. ``worker.session_reaped`` fires
# whenever the worker-marker reaper unlinks an orphan marker (one event
# per reaped marker); the watchdog's dead-loop detector counts these
# per-task to spot a reaper firing repeatedly on the same task without
# the underlying problem being fixed. ``watchdog.escalation_dispatched``
# fires from the watchdog cadence handler when a finding is routed to
# the project's architect with an unstick brief — the throttle window
# query treats the audit log itself as the source of truth, so this
# event must be present (and queryable by ``finding_type`` + ``subject``)
# for the dedup to work.
EVENT_WORKER_SESSION_REAPED = "worker.session_reaped"
EVENT_WATCHDOG_ESCALATION_DISPATCHED = "watchdog.escalation_dispatched"
# #1546 — operator-tier dispatch event. Fires when a tier-3 finding is
# routed to the user's inbox via the ``pm notify``-shaped path. The
# throttle window queries this event for dedup so a heartbeat-process
# restart doesn't reset the per-rule cooldown. Symmetric to
# ``EVENT_WATCHDOG_ESCALATION_DISPATCHED`` but for the operator leg of
# the cascade — the leg that exists when a tier-2 architect dispatch
# has already been tried (or is structurally unavailable) and the next
# rung up is a human in the loop. The event metadata carries
# ``finding_type``, ``subject``, and ``inbox_task_id`` (when the inbox
# task creation succeeded) so forensic reads can correlate with the
# inbox row directly.
EVENT_WATCHDOG_OPERATOR_DISPATCHED = "watchdog.operator_dispatched"
# #1546 — fires when the operator-dispatch leg attempted an inbox
# write but the write raised. Carries the same ``finding_type`` /
# ``subject`` metadata as ``EVENT_WATCHDOG_OPERATOR_DISPATCHED`` so
# the next tick can detect the failure case and not throttle on
# behalf of a row that never landed. Distinct event so the throttle
# query (which only counts ``operator_dispatched`` rows) doesn't
# accidentally suppress retries.
EVENT_WATCHDOG_TIER3_DISPATCH_FAILED = "watchdog.tier3_dispatch_failed"
# #1546 — fires when the watchdog spawns a missing role lane (the
# ``role_session_missing`` self-heal action). One event per spawned
# lane; metadata carries ``role``, ``project``, and ``task_subject``
# so forensic reads can confirm which queued / in-flight task drove
# the spawn.
EVENT_WATCHDOG_WORKER_LANE_SPAWNED = "audit.worker_lane_spawned"
# #1546 — fires when the watchdog repairs a tracked project whose
# canonical ``.pollypm/state.db`` is missing (only legacy archives
# remain). Metadata carries ``project_key`` and ``project_path`` so
# forensic reads can confirm which project was repaired.
EVENT_WATCHDOG_PROJECT_TRACKED_MODE_REPAIRED = "audit.project_tracked_mode_repaired"
# #1553 — tier-4 cascade events. Mirror the tier-3 dispatch event shape
# but for the broadened-authority leg of the cascade. Each event's
# metadata MUST carry enough for forensic reads to reconstruct the
# decision without re-running the cascade:
#
# * ``EVENT_WATCHDOG_OPERATOR_TIER4_DISPATCHED`` — fires once per tier-4
#   dispatch (auto-promoted OR self-promoted). Metadata carries
#   ``finding_type``, ``subject``, ``root_cause_hash``,
#   ``promotion_path`` (``"watchdog"`` / ``"self"``), ``inbox_task_id``.
# * ``EVENT_TIER4_PROMOTED`` — fires before the dispatch so the
#   promotion event is observable even if inbox creation fails.
#   Metadata: ``finding_type``, ``subject``, ``root_cause_hash``,
#   ``promotion_path``, ``justification`` (self-promote only).
# * ``EVENT_TIER4_ACTION`` / ``EVENT_TIER4_GLOBAL_ACTION`` — emitted by
#   tier-4 Polly when she takes a project-scoped or system-scoped
#   action under the broadened authority. The global variant is the
#   "louder" signature: it MUST be paired with a desktop-notification
#   call so the user knows the system was bounced. Metadata carries
#   ``action`` (free-form), ``root_cause_hash``, and any caller-supplied
#   forensic context.
# * ``EVENT_TIER4_DEMOTED`` — fires when the finding clears (status
#   moves to terminal-good) and the tracker auto-clears tier-4 state.
#   Metadata: ``root_cause_hash``, ``reason`` (``"finding_cleared"``).
# * ``EVENT_TIER4_BUDGET_EXHAUSTED`` — fires when the 2h wall-clock
#   budget at tier-4 elapses without resolution. The cascade then
#   routes the finding to terminal (product-broken + urgent inbox).
#   Metadata: ``root_cause_hash``, ``elapsed_seconds``,
#   ``budget_seconds``, ``urgent_handoff_subject``.
EVENT_WATCHDOG_OPERATOR_TIER4_DISPATCHED = "watchdog.operator_tier4_dispatched"
EVENT_TIER4_PROMOTED = "audit.tier4_promoted"
EVENT_TIER4_ACTION = "audit.tier4_action"
EVENT_TIER4_GLOBAL_ACTION = "audit.tier4_global_action"
EVENT_TIER4_DEMOTED = "audit.tier4_demoted"
EVENT_TIER4_BUDGET_EXHAUSTED = "audit.tier4_budget_exhausted"
# #1413 — emitted whenever the supervisor / on-demand path provisions a
# project-scoped role session (e.g. reviewer-<project>) that did not
# previously exist. The watchdog (#1414) reads this stream to surface
# "tracked project shipped without a reviewer for N hours" without
# scraping logs. ``metadata`` carries ``role`` (``"reviewer"`` /
# ``"architect"``), ``project``, and ``reason`` (``"bootstrap"`` /
# ``"on_demand_review"``).
EVENT_SESSION_PROVISIONED = "session.provisioned"
# #1562 — cockpit session lifecycle events. Diagnostic forensics for
# the "top-level Polly conversation disappears on rail switch" symptom.
# Emitted at three rail-mount transition boundaries so the next repro
# pinpoints which path fired:
#
# * ``EVENT_COCKPIT_SESSION_PARKED`` — fires after ``_park_mounted_session``
#   successfully breaks the operator/reviewer pane back into a
#   storage-closet window. Informational. Metadata: ``session_name``,
#   ``window_name``, ``storage_session``, ``storage_windows_after``.
# * ``EVENT_COCKPIT_SESSION_RESPAWNED`` — fires when ``_show_live_session``
#   spawns a FRESH session because the storage window was missing. This
#   is the conversation-loss path — every emission is a bug to
#   investigate. Status ``"warn"``. Metadata: ``session_name``,
#   ``role``, ``storage_windows_present``, ``reason``.
# * ``EVENT_COCKPIT_DUPLICATE_WINDOW_KILLED`` — fires when
#   ``_cleanup_duplicate_windows`` kills a duplicate-named storage
#   window (only ``pane_dead=True`` candidates). When two live duplicates
#   exist, no kill happens and a ``warn`` event with
#   ``action="skipped_live_duplicates"`` fires instead. Metadata:
#   ``storage_session``, ``window_name``, ``killed_index`` (kill path)
#   or ``live_duplicates`` (skip path), ``kept_indices``.
# * ``EVENT_COCKPIT_PARK_SKIPPED_EXISTING`` — fires when
#   ``_park_mounted_session`` detects that the storage closet already
#   has a live window of the same name and refuses to call
#   ``tmux break-pane`` (which would otherwise create a duplicate-named
#   second window — the upstream root cause of #1635). Status
#   ``"warn"``. Metadata: ``session_name``, ``window_name``,
#   ``storage_session``, ``live_duplicate_indices``,
#   ``dead_duplicate_indices``, ``reason``.
EVENT_COCKPIT_SESSION_PARKED = "cockpit.session_parked"
EVENT_COCKPIT_SESSION_RESPAWNED = "cockpit.session_respawned"
EVENT_COCKPIT_DUPLICATE_WINDOW_KILLED = "cockpit.duplicate_window_killed"
EVENT_COCKPIT_PARK_SKIPPED_EXISTING = "cockpit.park_skipped_existing"
# #1570 — emitted once per row that ``pm inbox backfill-kinds --commit``
# reclassifies from ``kind='legacy'`` to a real
# :class:`pollypm.inbox.kind.InboxItemKind`. Carries ``msg_id``,
# ``old_kind`` (always ``"legacy"``), ``new_kind``, and ``heuristic``
# (the label of the matched rule in
# :mod:`pollypm.inbox.backfill_heuristics`) so an operator can later
# answer "why did this row become completion_fyi?" by tail-grepping
# the audit log instead of re-running the heuristic.
EVENT_INBOX_KIND_BACKFILLED = "inbox.kind_backfilled"

# #1809 — advisor cadence forensics. ``advisor.tick`` was previously
# silent in the audit log even when it ran: no operator could tell
# from grep whether the cadence handler was firing, skipping
# projects, or never invoked at all. ``advisor.tick.fired`` is
# emitted once per tick to the workspace audit log with the tracked
# projects + per-project outcomes in metadata so an operator can
# trail-grep ``~/.pollypm/audit/_workspace.jsonl`` for cadence
# evidence.
EVENT_ADVISOR_TICK_FIRED = "advisor.tick.fired"
EVENT_ADVISOR_TICK_SKIPPED = "advisor.tick.skipped"
# Recovery cascade breadcrumbs. ``heartbeat.missing`` records the
# supervisor/heartbeat detection edge for a tracked session whose tmux
# window or pane disappeared; ``recovery.spawn`` records the canonical
# recovery relaunch edge; ``session.spawn`` is the broader session
# lifecycle synonym retained for existing consumers; ``task.reclaimed``
# records the task layer picking work back up after a dead worker claim.
EVENT_HEARTBEAT_MISSING = "heartbeat.missing"
EVENT_RECOVERY_SPAWN = "recovery.spawn"
EVENT_SESSION_SPAWN = "session.spawn"
EVENT_TASK_RECLAIMED = "task.reclaimed"


@dataclass(slots=True, frozen=True)
class AuditEvent:
    """Parsed audit-log line.

    Returned by :func:`read_events`. Writers do not construct these
    directly; they call :func:`emit` with kwargs and we shape the
    JSON record internally so the schema stays in one place.
    """

    ts: str
    project: str
    event: str
    subject: str
    actor: str
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)
    schema: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditEvent":
        return cls(
            ts=str(data.get("ts", "")),
            project=str(data.get("project", "")),
            event=str(data.get("event", "")),
            subject=str(data.get("subject", "")),
            actor=str(data.get("actor", "")),
            status=str(data.get("status", "ok")),
            metadata=dict(data.get("metadata", {}) or {}),
            schema=int(data.get("schema", SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _central_root() -> Path:
    """Root for the central-tail mirror.

    Defaults to ``~/.pollypm/audit/``. Honours ``$POLLYPM_AUDIT_HOME``
    so tests can redirect without touching the user's real log dir.
    """
    override = os.environ.get(_HOME_ENV)
    if override:
        return Path(override).expanduser()

    # Mirror the convention used by error_log._log_path() — base off
    # ``DEFAULT_CONFIG_PATH.parent`` (typically ``~/.pollypm``) so a
    # custom config home stays internally consistent.
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, GLOBAL_CONFIG_DIR

        return Path(DEFAULT_CONFIG_PATH).parent / "audit"
    except Exception:  # noqa: BLE001 — never fail audit on config errors
        # #1355: previously silent. Log so a config-resolution failure
        # doesn't quietly redirect audit output to the home-dir fallback.
        logger.warning(
            "audit.log: DEFAULT_CONFIG_PATH resolution failed; "
            "falling back to ~/.pollypm/audit",
            exc_info=True,
        )
        return GLOBAL_CONFIG_DIR / "audit"


def _safe_project_filename(project: str) -> str:
    """Sanitize project key for filesystem use.

    Project keys are typically slug-like already, but we belt-and-
    suspenders against ``../``, ``/``, and empty strings so a
    misbehaving caller can't escape the central root.
    """
    if not project:
        return "_unknown"
    # Strip path separators and leading dots; anything else is fine
    # because audit logs are never executed, only read as text.
    safe = project.replace("/", "_").replace("\\", "_").lstrip(".")
    return safe or "_unknown"


def central_log_path(project: str) -> Path:
    """Return the central-tail path for ``project``.

    Always returns a path; does not check for existence. Caller
    paths that may not exist yet are created on first :func:`emit`.
    """
    return _central_root() / f"{_safe_project_filename(project)}.jsonl"


def project_log_path(project_path: Path | str | None) -> Path | None:
    """Return the per-project audit log path, or ``None`` when unknown.

    A ``None`` return means the writer should fall back to central-
    only — used by codepaths that fire after a project root has been
    torn down, or by tests that don't materialize a project tree.

    Routes through :func:`pollypm.projects.project_audit_log_path`
    (#1972) so the doubled-pollypm-path guard fires when the caller
    happens to pass ``GLOBAL_CONFIG_DIR`` itself as the project
    root — the exact reproducer from #1966 (``work_db.opened``
    emitting with ``project_path = ~/.pollypm`` and landing in
    ``~/.pollypm/.pollypm/audit.jsonl``).
    """
    if project_path is None:
        return None
    # Lazy import to keep this module dependency-free at top level.
    from pollypm.projects import project_audit_log_path as _audit_path

    return _audit_path(Path(project_path))


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO-8601 UTC with microseconds — sortable, unambiguous."""
    return datetime.now(timezone.utc).isoformat()


def _build_record(
    *,
    project: str,
    event: str,
    subject: str,
    actor: str,
    status: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ts": _now_iso(),
        "project": project or "",
        "event": event,
        "subject": subject or "",
        "actor": actor or "",
        "status": status or "ok",
        "metadata": metadata or {},
    }


def _assert_no_doubled_pollypm(path: Path) -> None:
    """Raise loudly if ``path`` contains ``.pollypm/.pollypm`` (#1972).

    Belt-and-suspenders for the typed-helpers design fix: a writer that
    bypasses the helpers and constructs a doubled-path target is a bug.
    Converting the silent leak (~280K files / 1.8 GB in production —
    see #1810) into a loud crash makes the regression catch fire in
    CI / smoke tests instead of accumulating on user disks. The lint
    gate (``tests/test_no_raw_pollypm_path_joins.py``) is the primary
    defence; this is the runtime backstop.
    """
    if ".pollypm/.pollypm" in str(path) or ".pollypm\\.pollypm" in str(path):
        raise RuntimeError(
            f"doubled-pollypm-path write blocked (#1972): {path!s}. "
            "Construct via pollypm.projects.project_audit_log_path() "
            "or another typed helper instead of joining '.pollypm' inline."
        )


def _resolve_rotation_policy() -> tuple[int, int, bool]:
    """Return ``(max_bytes, retention_count, disabled)`` for rotation.

    Resolution order:

    1. Test overrides (``_test_rotate_size_bytes`` /
       ``_test_retention_count``) win so unit tests can drive rotation
       on tiny files without rewriting ``pollypm.toml``.
    2. Env var ``POLLYPM_AUDIT_DISABLE_ROTATION`` (``"1"`` / ``"true"``)
       forces ``disabled=True``. This is the ops-debugging escape hatch
       that doesn't require editing config.
    3. Live config ``[audit]`` section — lazy-imported so this module
       stays import-cycle-safe.
    4. Module defaults (50 MB / 4 retentions).

    Config-loading failures fall back to defaults rather than crashing
    the audit write — rotation is hygiene, never load-bearing.
    """
    # Test overrides take precedence so a unit test can pin a 1 KB
    # threshold without disturbing the user's real config.
    if _test_rotate_size_bytes is not None or _test_retention_count is not None:
        max_bytes = (
            int(_test_rotate_size_bytes)
            if _test_rotate_size_bytes is not None
            else _DEFAULT_ROTATE_SIZE_MB * 1024 * 1024
        )
        retention = (
            int(_test_retention_count)
            if _test_retention_count is not None
            else _DEFAULT_RETENTION_COUNT
        )
        env_disabled = os.environ.get(_DISABLE_ENV, "").strip().lower() in ("1", "true", "yes")
        return max(1, max_bytes), max(0, retention), env_disabled

    env_disabled = os.environ.get(_DISABLE_ENV, "").strip().lower() in ("1", "true", "yes")
    cfg_size_mb: int | None = None
    cfg_retention: int | None = None
    cfg_disabled: bool = False
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, load_config

        config = load_config(Path(DEFAULT_CONFIG_PATH))
    except Exception:  # noqa: BLE001 — never break audit on config errors
        config = None
    if config is not None:
        try:
            cfg_size_mb = int(config.audit.rotate_size_mb)
            cfg_retention = int(config.audit.retention_count)
            cfg_disabled = bool(config.audit.disable_rotation)
        except Exception:  # noqa: BLE001
            cfg_size_mb = None
            cfg_retention = None
            cfg_disabled = False
    size_mb = cfg_size_mb if cfg_size_mb and cfg_size_mb > 0 else _DEFAULT_ROTATE_SIZE_MB
    retention = (
        cfg_retention
        if cfg_retention is not None and cfg_retention >= 0
        else _DEFAULT_RETENTION_COUNT
    )
    return max(1, size_mb * 1024 * 1024), retention, (env_disabled or cfg_disabled)


def _prune_old_audit_archives(path: Path, retention_count: int) -> None:
    """Delete archived ``<path>.<ts>[.bump].gz`` siblings beyond cap.

    Sorts surviving archives by mtime descending so the newest are
    kept even if a timestamp collision tripped the parser. Errors
    are silently logged at DEBUG — pruning is hygiene, not critical.
    """
    if retention_count < 0:
        return
    parent = path.parent
    prefix = path.name + "."
    candidates: list[tuple[float, Path]] = []
    try:
        for sibling in parent.iterdir():
            if not sibling.is_file():
                continue
            if not sibling.name.startswith(prefix):
                continue
            if not sibling.name.endswith(".gz"):
                continue
            try:
                mtime = sibling.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, sibling))
    except OSError:
        return
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _mtime, stale in candidates[retention_count:]:
        try:
            stale.unlink()
        except OSError:
            logger.debug(
                "audit.log: prune failed for %s", stale, exc_info=True,
            )


def _maybe_rotate(path: Path) -> None:
    """Rotate ``path`` if it exceeds the configured size threshold.

    Atomicity strategy:

    1. ``os.rename(audit.jsonl, audit.jsonl.<ts>.rotating)`` — POSIX
       rename is atomic, so concurrent readers either see the live
       file at its old name (pre-rename) or no file there (post-
       rename; ``_append_line`` will recreate on the next call).
       They never see a half-state.
    2. Gzip the renamed file into ``audit.jsonl.<ts>.gz.partial``
       (writing to a sibling tempfile keeps the final ``.gz`` name
       invisible until the compression finishes).
    3. Atomic rename ``.gz.partial`` -> ``.gz`` so readers that walk
       the directory for archived rotations only see complete files.
    4. Unlink the uncompressed ``.rotating`` rename.
    5. Prune old ``.gz`` siblings beyond the retention count.

    Best-effort: any OSError is logged at WARNING and rotation skips;
    the caller still proceeds to ``_append_line`` so the audit write
    never fails because of housekeeping. If step 1 succeeded but a
    later step failed we leave the ``.rotating`` rename in place
    rather than losing the data — a follow-up rotation will find and
    process it (filename collisions are handled by a bump suffix).
    """
    max_bytes, retention, disabled = _resolve_rotation_policy()
    if disabled:
        return
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.debug(
            "audit.log: stat failed for %s during rotation check: %s",
            path, exc,
        )
        return
    if st.st_size <= max_bytes:
        return

    # Pick a name that is free for BOTH the in-flight ``.rotating``
    # rename target AND the final ``.gz`` archive. A second rotation
    # in the same wall-clock second would otherwise collide on the
    # ``.gz`` name and ``os.rename(gz_partial, gz_final)`` would
    # silently overwrite the previous archive (POSIX rename
    # semantics) — losing the older chunk of events. The bump
    # suffix keeps the names unique within the second.
    ts = int(time.time())
    bump = 0
    while True:
        if bump == 0:
            stem = f".{ts}"
        else:
            stem = f".{ts}.{bump}"
        rotating = path.with_suffix(path.suffix + stem + ".rotating")
        gz_final = path.with_suffix(path.suffix + stem + ".gz")
        if not rotating.exists() and not gz_final.exists():
            break
        bump += 1
        if bump > 10_000:
            # Defensive cap: if we somehow can't find a free name in
            # 10K bumps within the same second, abort the rotation
            # rather than spin forever. The append below still fires.
            logger.warning(
                "audit.log: rotation name-collision loop exhausted for %s",
                path,
            )
            return

    # Step 1: atomic rename moves the live file out of the way.
    try:
        os.rename(path, rotating)
    except OSError as exc:
        logger.warning(
            "audit.log: rotation rename failed for %s: %s", path, exc,
        )
        return

    # Steps 2-4: gzip + atomic rename. If anything fails we still
    # return cleanly so the caller's append fires against a fresh
    # empty file (the rename above already created that condition).
    gz_partial = Path(str(gz_final) + ".partial")
    try:
        with open(rotating, "rb") as src, gzip.open(gz_partial, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.rename(gz_partial, gz_final)
        try:
            rotating.unlink()
        except OSError:
            logger.debug(
                "audit.log: post-gzip unlink failed for %s",
                rotating, exc_info=True,
            )
    except OSError as exc:
        logger.warning(
            "audit.log: gzip failed for %s: %s", rotating, exc,
        )
        # Clean up the partial gz so a future rotation isn't tripped
        # by half-written archives. Leave the ``.rotating`` rename in
        # place — it still holds the data and a future rotation will
        # find + retry it.
        try:
            if gz_partial.exists():
                gz_partial.unlink()
        except OSError:
            pass

    # Step 5: prune old archives. Best-effort, isolated from rotation.
    try:
        _prune_old_audit_archives(path, retention)
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.log: prune sweep failed for %s",
            path, exc_info=True,
        )


def _append_line(path: Path, line: str) -> None:
    """Append a single line to ``path``, creating parents as needed.

    Fires rotation (``_maybe_rotate``) *before* the append so the
    append always lands in a file that is under the size threshold.
    Rotation failures are swallowed inside ``_maybe_rotate`` — the
    append fires unconditionally so a housekeeping bug can never
    cause audit data loss.

    Uses POSIX append-mode (``"a"``) so concurrent writers from
    multiple processes interleave at the line level — provided the
    line is under PIPE_BUF (4096 bytes), which our records always
    are. We open + write + close on every call rather than holding
    a long-lived handle, so a crashed writer can't leak an FD into
    the audit file.
    """
    _assert_no_doubled_pollypm(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Rotate first so the impending append starts a fresh file when
    # we cross the threshold. Wrap in a broad try because nothing
    # about rotation should ever prevent an audit write — that is
    # the central guarantee of this module.
    try:
        _maybe_rotate(path)
    except Exception:  # noqa: BLE001 — never block audit on housekeeping
        logger.debug(
            "audit.log: _maybe_rotate raised unexpectedly for %s",
            path, exc_info=True,
        )
    # Newline added here so the encoded record stays a single
    # JSON object on its own line.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.write("\n")


def emit(
    *,
    event: str,
    project: str,
    subject: str = "",
    actor: str = "",
    status: str = "ok",
    metadata: dict[str, Any] | None = None,
    project_path: Path | str | None = None,
) -> None:
    """Append one event to the per-project log + central tail.

    Best-effort. Never raises — a failed audit write logs a warning
    and returns. Callers are mutation paths; blocking the mutation
    on an audit failure is strictly worse than missing one event.

    Args:
        event: stable event name (see ``EVENT_*`` constants in this
            module). New events should follow ``noun.verb`` form.
        project: project key. Empty string is allowed for events
            that don't belong to a single project (e.g. a future
            ``workspace.*`` event); they only land in central.
        subject: free-form identifier — typically ``project/N``,
            a worker marker filename, or a session name.
        actor: who triggered this — user, agent name, ``"system"``,
            ``"polly"``, etc. Empty is fine when truly unknown.
        status: ``"ok"`` (default), ``"warn"``, ``"error"``.
        metadata: free-form JSON-serializable per-event payload.
        project_path: project root for the per-project log. When
            ``None``, only the central tail is written. Pass the
            ``SQLiteWorkService._project_path`` for work-service
            hooks; pass ``None`` from delete-project codepaths
            that have already torn down the project root.
    """
    record = _build_record(
        project=project,
        event=event,
        subject=subject,
        actor=actor,
        status=status,
        metadata=metadata,
    )
    try:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        # Metadata had something non-serializable. Log + skip rather
        # than crash the caller. We still try to emit a stripped
        # version so the event itself is recorded.
        logger.warning(
            "audit.emit: dropping non-serializable metadata for %s/%s: %s",
            project, event, exc,
        )
        record["metadata"] = {"_error": "metadata_not_serializable"}
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        except Exception:  # noqa: BLE001
            # #1355: previously silent. Even the stripped fallback failed
            # to serialize — log so a dropped audit event leaves a trail
            # instead of vanishing.
            logger.warning(
                "audit.emit: stripped record still not serializable for %s/%s; dropping",
                project,
                event,
                exc_info=True,
            )
            return

    # Per-project log first (authoritative), central tail second
    # (mirror). Each is best-effort and isolated so a failure on
    # one does not skip the other.
    #
    # Only write the per-project log when ``<root>/.pollypm/`` already
    # exists. Creating that directory ourselves would dirty the git
    # tree of projects that haven't been initialized for PollyPM —
    # the approve-gate's status check (see
    # ``_status_is_only_pollypm_scaffold``) only allowlists specific
    # scaffold paths, so a stray ``.pollypm/audit.jsonl`` would
    # bounce auto-merges. In normal usage ``.pollypm/`` already
    # exists (created by ``ensure_project_scaffold``), so this is a
    # no-op for real projects and a clean no-op for the few tests
    # that exercise the work-service against a bare git repo.
    if project_path is not None:
        try:
            per_project = project_log_path(project_path)
            if per_project is not None and per_project.parent.exists():
                _append_line(per_project, line)
        except OSError as exc:
            logger.warning(
                "audit.emit: per-project log write failed (%s): %s",
                project_path, exc,
            )

    if project:
        try:
            _append_line(central_log_path(project), line)
        except OSError as exc:
            logger.warning(
                "audit.emit: central log write failed (%s): %s",
                project, exc,
            )


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def _iter_log_lines(path: Path) -> Iterable[dict[str, Any]]:
    """Yield decoded JSON objects from ``path``, skipping malformed lines.

    Audit logs are append-only so partial writes (process killed
    mid-write) can leave a truncated final line. Skip those rather
    than crashing the reader — the heartbeat must keep working
    even when the log has a junk tail.

    Routes ``.gz`` archives through :func:`gzip.open` in text mode so
    callers walking a rotation chain (live ``.jsonl`` + ``.gz``
    siblings) get the same line-iteration shape regardless of file
    format. See :func:`_walk_log_chain` for the chain producer.
    """
    if not path.exists():
        return
    try:
        if path.suffix == ".gz":
            fh = gzip.open(path, "rt", encoding="utf-8")
        else:
            fh = open(path, "r", encoding="utf-8")
    except OSError as exc:
        logger.warning("audit.read: open failed for %s: %s", path, exc)
        return
    try:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                # Truncated / corrupt line — skip silently. We
                # do not log here because a single bad tail line
                # would otherwise spam the error log on every
                # heartbeat tick.
                continue
            if isinstance(obj, dict):
                yield obj
    except OSError as exc:
        logger.warning("audit.read: read failed for %s: %s", path, exc)
    finally:
        try:
            fh.close()
        except Exception:  # noqa: BLE001 — close-time errors are noise
            pass


def _archive_sort_key(path: Path) -> tuple[float, str]:
    """Sort key for ``audit.jsonl.<ts>[.bump].gz`` archives.

    We want newest-first iteration. Use mtime as primary because the
    rotation timestamp is embedded in the filename but tests + edge
    cases can leave clock-skew; fall back to filename for stable
    ordering within the same mtime second.

    Mirrors the helper in ``pollypm.cli_features.audit`` (PR #2036).
    Duplicated rather than imported because that module pulls in
    typer and would create an import cycle from this stdlib-only
    audit primitive.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (mtime, path.name)


def _walk_log_chain(live: Path) -> Iterable[Path]:
    """Yield ``live`` first, then ``live.<ts>[.bump].gz`` archives newest-first.

    Yields only paths that exist. Used by :func:`read_events` so
    rotation never hides events from consumers (#2032 codex blocker):
    once ``_maybe_rotate`` moves ``audit.jsonl`` into ``audit.jsonl.<ts>.gz``
    the rotated chunk must remain visible to the watchdog dedupe
    window, SSE, morning briefing, and doctor checks — otherwise a
    rotation would silently allow duplicate escalation dispatches.

    Mirrors the helper in ``pollypm.cli_features.audit`` (PR #2036).
    """
    if live.exists():
        yield live
    parent = live.parent
    if not parent.exists():
        return
    prefix = live.name + "."
    archives: list[Path] = []
    try:
        for sibling in parent.iterdir():
            if not sibling.is_file():
                continue
            if not sibling.name.startswith(prefix):
                continue
            if not sibling.name.endswith(".gz"):
                continue
            archives.append(sibling)
    except OSError:
        return
    archives.sort(key=_archive_sort_key, reverse=True)
    for archive in archives:
        yield archive


def read_events(
    project: str,
    *,
    since: str | None = None,
    limit: int | None = None,
    event: str | None = None,
    project_path: Path | str | None = None,
) -> list[AuditEvent]:
    """Read recent audit events for ``project``.

    Source preference:

    1. If ``project_path`` is provided AND the per-project log
       exists, read from there + walk its rotated ``.gz`` siblings.
       This is the source of truth and carries events from before
       any central-tail rotation.
    2. Otherwise read from the central tail at
       ``~/.pollypm/audit/<project>.jsonl`` + its rotated ``.gz``
       siblings.

    Rotation visibility (#2032 codex blocker): each source above is a
    *chain* — live ``.jsonl`` first, then ``.gz`` archives newest-
    first. Once a rotation moves recent events into a ``.gz``, those
    rows must still satisfy ``read_events(..., since=..., event=...)``
    queries. The watchdog dedupe window depends on this: if the prior
    ``watchdog.escalation_dispatched`` row disappears after rotation
    the next tick would dispatch a duplicate.

    Filters apply in order:

    * ``event``: only events matching this exact name.
    * ``since``: only events with ``ts > since``. Compare as
      strings — ISO-8601 UTC sorts lexicographically.
    * ``limit``: keep at most the last N matching events
      (post-filter, in chronological order).

    Returns a list (not a generator) because typical callers want
    to count / slice / re-iterate. Audit logs are bounded — even a
    chatty project lands in the low thousands per day — so loading
    fully into memory is fine.
    """
    per_project = project_log_path(project_path)
    if per_project is not None and per_project.exists():
        live = per_project
    else:
        live = central_log_path(project)

    # ``_walk_log_chain`` yields live first then archives newest-first.
    # Each file is a chronological run of records (older-first within
    # the file), so the cross-file order is:
    #   live (oldest→newest in live), then archive_N (newest archive),
    #   then archive_N-1, ... archive_1 (oldest archive).
    # We collect per-file then reverse the inter-file order so the
    # final list is globally chronological (oldest→newest).
    per_file: list[list[AuditEvent]] = []
    for path in _walk_log_chain(live):
        bucket: list[AuditEvent] = []
        for obj in _iter_log_lines(path):
            if event is not None and obj.get("event") != event:
                continue
            if since is not None:
                ts = str(obj.get("ts", ""))
                if ts <= since:
                    continue
            # Skip cross-project rows that snuck into a per-project
            # file (shouldn't happen, but defensive — the per-project
            # log only ever receives writes from one project's
            # mutation hooks).
            if obj.get("project") and obj.get("project") != project:
                continue
            bucket.append(AuditEvent.from_dict(obj))
        per_file.append(bucket)

    # Inter-file order from walker: [live, newest_gz, ..., oldest_gz].
    # Chronological (oldest→newest) is the reverse: [oldest_gz, ...,
    # newest_gz, live]. Within each file, records are already in
    # write-order (oldest→newest).
    events: list[AuditEvent] = []
    for bucket in reversed(per_file):
        events.extend(bucket)

    # Re-sort by ts to defend against clock-skew between rotations
    # (the walker uses mtime which is approximate, but ts is the
    # actual write time). ISO-8601 UTC sorts lexicographically.
    events.sort(key=lambda e: e.ts)

    if limit is not None and limit >= 0:
        return events[-limit:]
    return events


__all__ = [
    "SCHEMA_VERSION",
    "EVENT_TASK_CREATED",
    "EVENT_TASK_STATUS_CHANGED",
    "EVENT_TASK_CLAIMED_BY_SESSION",
    "EVENT_TASK_CANCEL_WARNED",
    "EVENT_TASK_CANCEL_CONFIRMED",
    "EVENT_TASK_DELETED",
    "EVENT_MARKER_CREATED",
    "EVENT_MARKER_RELEASED",
    "EVENT_MARKER_CREATE_FAILED",
    "EVENT_MARKER_LEAKED",
    "EVENT_WORK_TABLE_CLEARED",
    "EVENT_WORK_DB_OPENED",
    "EVENT_WORKER_THREAD_LEAKED",
    "EVENT_SOCKET_REAPED",
    "EVENT_PLAN_VERSION_INCREMENTED",
    "EVENT_PLAN_SUCCESSOR_CREATED",
    "EVENT_WORKER_SESSION_REAPED",
    "EVENT_WATCHDOG_ESCALATION_DISPATCHED",
    "EVENT_WATCHDOG_OPERATOR_DISPATCHED",
    "EVENT_WATCHDOG_TIER3_DISPATCH_FAILED",
    "EVENT_WATCHDOG_WORKER_LANE_SPAWNED",
    "EVENT_WATCHDOG_PROJECT_TRACKED_MODE_REPAIRED",
    "EVENT_DAEMON_REAPED",
    "EVENT_DAEMON_REVIVED",
    "EVENT_WATCHDOG_OPERATOR_TIER4_DISPATCHED",
    "EVENT_TIER4_PROMOTED",
    "EVENT_TIER4_ACTION",
    "EVENT_TIER4_GLOBAL_ACTION",
    "EVENT_TIER4_DEMOTED",
    "EVENT_TIER4_BUDGET_EXHAUSTED",
    "EVENT_COCKPIT_SESSION_PARKED",
    "EVENT_COCKPIT_SESSION_RESPAWNED",
    "EVENT_COCKPIT_DUPLICATE_WINDOW_KILLED",
    "EVENT_COCKPIT_PARK_SKIPPED_EXISTING",
    "EVENT_INBOX_KIND_BACKFILLED",
    "EVENT_HEARTBEAT_MISSING",
    "EVENT_RECOVERY_SPAWN",
    "EVENT_SESSION_SPAWN",
    "EVENT_TASK_RECLAIMED",
    "AuditEvent",
    "central_log_path",
    "emit",
    "project_log_path",
    "read_events",
]
