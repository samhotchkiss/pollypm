"""JSONL writer + reader for the audit log.

See ``pollypm.audit`` package docstring for the why and the schema.

This module is intentionally tiny and dependency-free (stdlib only)
so it can be imported from anywhere in the codebase — work-service,
session-services, tmux, supervisor — without creating import cycles.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

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
        from pollypm.config import DEFAULT_CONFIG_PATH

        return Path(DEFAULT_CONFIG_PATH).parent / "audit"
    except Exception:  # noqa: BLE001 — never fail audit on config errors
        return Path.home() / ".pollypm" / "audit"


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
    """
    if project_path is None:
        return None
    p = Path(project_path)
    return p / ".pollypm" / "audit.jsonl"


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


def _append_line(path: Path, line: str) -> None:
    """Append a single line to ``path``, creating parents as needed.

    Uses POSIX append-mode (``"a"``) so concurrent writers from
    multiple processes interleave at the line level — provided the
    line is under PIPE_BUF (4096 bytes), which our records always
    are. We open + write + close on every call rather than holding
    a long-lived handle, so a crashed writer can't leak an FD into
    the audit file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
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
    """
    if not path.exists():
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
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
        logger.warning("audit.read: open failed for %s: %s", path, exc)


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
       exists, read from there. This is the source of truth and
       carries events from before any central-tail rotation.
    2. Otherwise read from the central tail at
       ``~/.pollypm/audit/<project>.jsonl``.

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
    paths_to_try: list[Path] = []
    per_project = project_log_path(project_path)
    if per_project is not None and per_project.exists():
        paths_to_try.append(per_project)
    else:
        paths_to_try.append(central_log_path(project))

    events: list[AuditEvent] = []
    for path in paths_to_try:
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
            events.append(AuditEvent.from_dict(obj))
        # Once we've found a real source, don't fall through.
        if events:
            break

    if limit is not None and limit >= 0:
        return events[-limit:]
    return events


__all__ = [
    "SCHEMA_VERSION",
    "EVENT_TASK_CREATED",
    "EVENT_TASK_STATUS_CHANGED",
    "EVENT_TASK_DELETED",
    "EVENT_MARKER_CREATED",
    "EVENT_MARKER_RELEASED",
    "EVENT_MARKER_CREATE_FAILED",
    "EVENT_MARKER_LEAKED",
    "EVENT_WORK_TABLE_CLEARED",
    "EVENT_WORK_DB_OPENED",
    "EVENT_WORKER_THREAD_LEAKED",
    "AuditEvent",
    "central_log_path",
    "emit",
    "project_log_path",
    "read_events",
]
