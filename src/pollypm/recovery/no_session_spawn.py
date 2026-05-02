"""Auto-recovery for ``<role>/no_session`` alerts (#1005).

Background
----------
When a task is routed to a role (typically ``reviewer``, ``architect``, or
``worker``) and no live session matches, the task-assignment plugin
raises a ``no_session`` alert. The alert message includes a ``Try:`` hint
suggesting ``pm worker-start --role <role> <project>`` — but until #1005
this was a manual instruction. A task in ``review`` waits forever on a
reviewer that no human has remembered to spin up.

This module watches the open alert set on each heartbeat sweep and
attempts that exact ``worker-start`` automatically. The heartbeat-side
recovery_limit pattern is the model: bounded retries with backoff, then
escalate to a sibling alert that calls a human in.

Behaviour
---------
On each call:

1. Enumerate open alerts whose ``alert_type == "no_session"``.
2. For each, derive ``(role, project)`` from the alert sender + body.
3. Skip alerts younger than ``threshold_seconds`` (default 60s) — gives
   the task-claim path a moment to win the race before we step in.
4. Count prior spawn-attempt events for this ``(role, project)`` and
   honour an exponential backoff between attempts
   (``backoff_base_seconds * 2**attempt``, capped at ``backoff_cap_seconds``).
5. After ``max_attempts`` failures, raise a sibling
   ``<expected_session>/no_session_spawn_failed`` alert and stop trying.
6. Otherwise, call the same code path as ``pm worker-start`` and record a
   ``no_session_spawn_attempt`` event so future ticks see the attempt.

The successful-path teardown of the original ``no_session`` alert is
handled elsewhere — once the spawned session is live, the next sweep tick
finds the role and ``_emit_no_session_alert`` stops re-emitting (the
``upsert_alert`` row drops out of the open set when ``clear_alert`` fires
on session attach, or naturally ages out via the existing flow).

Constraints
-----------
* Does NOT touch the per-task ``no_session_for_assignment:<id>`` family —
  those clear themselves when the task transitions, and a per-task spawn
  would race with ``task claim``. The project-level ``<role>/no_session``
  alert is the canonical signal that the project's role lane is empty.
* Treats unconfigured projects as a no-op (the registry guard already
  drops ghost-project alerts elsewhere; we mirror it here so a partially-
  loaded ``services`` object can't trigger a spawn).
* Worker-role alerts are skipped: per-task workers are spawned by
  ``pm task claim``, not by ``pm worker-start --role worker`` (which is
  deprecated — see ``cli_features/workers.py``). The ``Try:`` hint
  intentionally surfaces ``pm task claim`` first for that case.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Roles we attempt auto-spawn for. ``worker`` is intentionally excluded —
# per-task workers are provisioned by ``pm task claim`` via the work
# service, not by ``pm worker-start``. ``operator`` / ``heartbeat`` /
# ``triage`` are workspace singletons managed by the supervisor's own
# launch path; they should never appear as the subject of a
# ``no_session`` alert in normal operation.
_AUTO_SPAWN_ROLES: frozenset[str] = frozenset({"reviewer", "architect"})

# Defaults — overridable per-call so tests can force the behaviour
# without sleeping.
DEFAULT_THRESHOLD_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 60
DEFAULT_BACKOFF_CAP_SECONDS = 600

SPAWN_ATTEMPT_EVENT_TYPE = "no_session_spawn_attempt"
SPAWN_FAILED_ALERT_TYPE = "no_session_spawn_failed"
# #1028 — heartbeat marker recorded when the auto-clear sweep observes a
# previously-failing session as healthy and closes the
# ``no_session_spawn_failed`` alert. Acts as the "since" floor for
# :func:`_attempt_history` so the breaker has its full attempt budget
# available again on the next legitimate ``no_session`` episode rather
# than starting tripped on stale attempts from the cleared episode.
SPAWN_BREAKER_RESET_EVENT_TYPE = "no_session_spawn_breaker_reset"


@dataclass(slots=True)
class SpawnDecision:
    """One alert's per-tick decision — useful for tests + observability."""

    session_name: str
    role: str
    project: str
    outcome: str  # "spawned", "skipped_young", "skipped_backoff",
                  # "skipped_role", "skipped_unknown_project",
                  # "spawn_failed", "escalated"
    attempt_number: int = 0
    detail: str = ""


# ``reviewer/no_session`` and ``architect/no_session`` carry the project in
# the message body, not the sender (the sender is the candidate session
# name from ``role_candidate_names`` — a singleton like ``reviewer`` for
# project-scoped roles that pre-#1005 weren't really project-scoped at
# all). Parse it back out so we know which project to spawn for.
#
# Message shape from ``_emit_no_session_alert``:
#   "No worker is running for the <role> role on '<project>' — task ..."
_PROJECT_FROM_MESSAGE_RE = re.compile(
    r"No worker is running for the (?P<role>\S+) role on '(?P<project>[^']+)'"
)


def _parse_project_from_alert(alert: Any) -> tuple[str | None, str | None]:
    """Return (role, project) parsed from a ``no_session`` alert."""
    body = getattr(alert, "message", None) or ""
    match = _PROJECT_FROM_MESSAGE_RE.search(body)
    if match is not None:
        return match.group("role"), match.group("project")
    return None, None


def _expected_session_name(role: str, project: str) -> str:
    """Mirror ``role_candidate_names``'s first preference."""
    from pollypm.work.task_assignment import role_candidate_names

    candidates = role_candidate_names(role, project)
    return candidates[0] if candidates else f"{role}-{project}"


def _parse_iso(value: Any) -> datetime | None:
    """Coerce a timestamp into a UTC ``datetime``.

    Accepts either an ISO-8601 string (legacy ``StateStore``) or a
    ``datetime`` object (unified ``SQLAlchemyStore``). Naive datetimes
    are treated as UTC, matching the writer convention on both stores.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        # SQLite stores naive ISO-8601; treat as UTC.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        # SQLite ``YYYY-MM-DD HH:MM:SS[.ffffff]`` uses a space; ISO uses T.
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _latest_breaker_reset(
    store: Any, session_name: str, *, limit: int = 50,
) -> datetime | None:
    """Return the most recent breaker-reset marker timestamp, or ``None``.

    The auto-clear sweep records a ``no_session_spawn_breaker_reset``
    event when it closes a ``no_session_spawn_failed`` alert after a
    healthy streak (#1028). :func:`_attempt_history` treats that
    timestamp as a floor on the attempt window so the breaker resets to
    its full ``DEFAULT_MAX_ATTEMPTS`` budget for the next legitimate
    ``no_session`` episode — without it, stale attempts from the cleared
    episode would push the new alert straight into immediate escalation.
    """
    query = getattr(store, "query_messages", None)
    if not callable(query):
        return None
    try:
        rows = query(type="event", scope=session_name, limit=limit)
    except Exception:  # noqa: BLE001
        logger.debug(
            "no_session_spawn: query_messages(reset) failed for %s",
            session_name, exc_info=True,
        )
        return None
    latest: datetime | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        sender = str(row.get("sender") or "")
        subject = str(row.get("subject") or "")
        if (
            sender != SPAWN_BREAKER_RESET_EVENT_TYPE
            and subject != SPAWN_BREAKER_RESET_EVENT_TYPE
        ):
            continue
        ts = _parse_iso(row.get("created_at"))
        if ts is None:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def _attempt_history(
    store: Any,
    session_name: str,
    *,
    since: datetime | None = None,
    limit: int = 200,
) -> list[datetime]:
    """Return prior spawn-attempt timestamps for ``session_name``.

    Reads ``no_session_spawn_attempt`` events recorded by this module on
    earlier ticks. Tolerant of either the legacy ``StateStore``
    (``recent_events`` returning ``EventRecord`` rows) or the unified
    ``Store`` (``query_messages(type='event', ...)`` returning dicts).

    ``since`` — when supplied, only attempts whose timestamp is at or
    after ``since`` are returned. The caller passes the alert's
    ``created_at`` so the attempt counter resets on each new
    open-alert episode (a fresh re-raise after a clear is treated as
    attempt zero). Without this scoping, every succeeded recovery would
    leave attempt history that pushed the next legitimate episode into
    immediate escalation.

    Note on schema asymmetry: the two store implementations disagree on
    where ``event_type`` lives in the row.

    * ``StateStore.record_event(session_name, event_type, message)``
      writes ``sender=session_name, subject=event_type``.
    * ``SQLAlchemyStore.record_event(scope, sender, subject, payload)``
      called positionally with ``(session_name, event_type, message)``
      writes ``sender=event_type, subject=message``.

    We therefore accept a match on either column (the value we wrote is
    ``SPAWN_ATTEMPT_EVENT_TYPE`` and any cross-talk would require a
    foreign producer to use the same literal).
    """
    # #1028 — the most recent breaker-reset marker acts as a floor on
    # the attempt window. When the auto-clear sweep closes a
    # ``no_session_spawn_failed`` alert after a healthy streak, it
    # records the marker so stale attempts from the cleared episode
    # don't push a fresh ``no_session`` alert into immediate escalation.
    reset_floor = _latest_breaker_reset(store, session_name)
    effective_since = since
    if reset_floor is not None:
        effective_since = (
            reset_floor if effective_since is None
            else max(effective_since, reset_floor)
        )

    out: list[datetime] = []
    query = getattr(store, "query_messages", None)
    if callable(query):
        try:
            rows = query(
                type="event", scope=session_name, limit=limit,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "no_session_spawn: query_messages failed for %s",
                session_name, exc_info=True,
            )
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sender = str(row.get("sender") or "")
            subject = str(row.get("subject") or "")
            if (
                sender != SPAWN_ATTEMPT_EVENT_TYPE
                and subject != SPAWN_ATTEMPT_EVENT_TYPE
            ):
                continue
            ts = _parse_iso(row.get("created_at"))
            if ts is None:
                continue
            if effective_since is not None and ts < effective_since:
                continue
            out.append(ts)
        if out:
            return out
    recent = getattr(store, "recent_events", None)
    if not callable(recent):
        return out
    try:
        events = recent(limit)
    except Exception:  # noqa: BLE001
        logger.debug(
            "no_session_spawn: recent_events failed for %s",
            session_name, exc_info=True,
        )
        return out
    for event in events:
        if getattr(event, "session_name", None) != session_name:
            continue
        if getattr(event, "event_type", None) != SPAWN_ATTEMPT_EVENT_TYPE:
            continue
        ts = _parse_iso(getattr(event, "created_at", None))
        if ts is None:
            continue
        if effective_since is not None and ts < effective_since:
            continue
        out.append(ts)
    return out


def _backoff_due(
    history: list[datetime],
    *,
    now: datetime,
    base: int,
    cap: int,
) -> tuple[bool, int]:
    """Return ``(due, attempt_number)`` for the next attempt.

    ``attempt_number`` is the count of previous attempts (so the next
    attempt's exponent is ``attempt_number``). ``due`` is True when the
    backoff window since the most recent attempt has elapsed.
    """
    attempt_number = len(history)
    if not history:
        return True, 0
    last = max(history)
    delay = min(base * (2 ** (attempt_number - 1)), cap)
    due = now >= last + timedelta(seconds=delay)
    return due, attempt_number


def _project_is_known(services: Any, project: str) -> bool:
    """Mirror ``_known_project_keys`` from the sweep — keep it cheap."""
    keys = {
        getattr(p, "key", None)
        for p in (getattr(services, "known_projects", ()) or ())
    }
    keys.discard(None)
    if not keys:
        # Empty registry — preserve the legacy "don't filter" stance so
        # tests / config-less runs keep working.
        return True
    return project in keys


@dataclass(slots=True)
class _AlertView:
    """Normalised view of an open ``no_session`` alert.

    Both the legacy ``StateStore`` and the unified ``Store`` are supported
    via the conversion in :func:`_open_no_session_alerts` — callers
    interact with this dataclass instead of branching on store shape.
    """

    session_name: str
    alert_type: str
    message: str
    created_at: str


def _spawn(
    *,
    config_path: Path,
    project: str,
    role: str,
) -> tuple[bool, str]:
    """Run ``pm worker-start --role <role> <project>`` programmatically.

    Returns (success, detail). Raises nothing — exceptions become a
    ``(False, str(exc))`` return so a misconfigured project can't crash
    the sweep tick.
    """
    try:
        from pollypm.workers import (
            create_worker_session,
            launch_worker_session,
        )
        from pollypm.config import load_config
    except Exception as exc:  # noqa: BLE001
        return False, f"import failed: {exc}"

    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        return False, f"load_config failed: {exc}"

    # If a session for (role, project) already exists in config, just
    # relaunch it — same shape as the CLI's ``existing or create`` branch.
    existing = next(
        (
            session
            for session in config.sessions.values()
            if session.role == role
            and session.project == project
            and session.enabled
        ),
        None,
    )
    try:
        if existing is not None:
            session = existing
        else:
            session = create_worker_session(
                config_path,
                project_key=project,
                prompt=None,
                role=role,
            )
    except Exception as exc:  # noqa: BLE001
        return False, f"create_worker_session failed: {exc}"

    try:
        launch_worker_session(config_path, session.name)
    except Exception as exc:  # noqa: BLE001
        return False, f"launch_worker_session failed: {exc}"

    return True, f"session={session.name}"


def _record_attempt(store: Any, session_name: str, message: str) -> None:
    record = getattr(store, "record_event", None)
    if not callable(record):
        return
    try:
        record(session_name, SPAWN_ATTEMPT_EVENT_TYPE, message)
    except Exception:  # noqa: BLE001
        logger.debug(
            "no_session_spawn: record_event failed for %s",
            session_name, exc_info=True,
        )


def record_breaker_reset(store: Any, session_name: str, message: str = "") -> None:
    """Record a marker that resets the spawn-attempt counter to zero.

    Called from the heartbeat's auto-clear sweep when it observes a
    previously-failing session as healthy and closes the corresponding
    ``no_session_spawn_failed`` alert (#1028). The marker's timestamp
    becomes the floor for :func:`_attempt_history`'s effective ``since``,
    so any attempts recorded before the reset are no longer counted.
    The result: future ``no_session`` episodes start with the full
    ``DEFAULT_MAX_ATTEMPTS`` budget instead of inheriting the tripped
    state from the cleared episode.

    Best-effort: silently swallows store errors so a flaky write can't
    block the auto-clear path for unrelated alerts.
    """
    record = getattr(store, "record_event", None)
    if not callable(record):
        return
    try:
        record(session_name, SPAWN_BREAKER_RESET_EVENT_TYPE, message)
    except Exception:  # noqa: BLE001
        logger.debug(
            "no_session_spawn: record_event(breaker_reset) failed for %s",
            session_name, exc_info=True,
        )


def _escalate_failure(
    store: Any, session_name: str, role: str, project: str, attempts: int,
) -> None:
    upsert = getattr(store, "upsert_alert", None)
    if not callable(upsert):
        return
    message = (
        f"Auto-recovery for {role}/{project} failed after {attempts} "
        f"spawn attempts. Run `pm worker-start --role {role} {project}` "
        "manually and check logs for the underlying error."
    )
    try:
        upsert(session_name, SPAWN_FAILED_ALERT_TYPE, "error", message)
    except Exception:  # noqa: BLE001
        logger.debug(
            "no_session_spawn: escalation upsert_alert failed for %s",
            session_name, exc_info=True,
        )


def _strip_alert_subject(subject: str) -> str:
    """Mirror ``StateStore._strip_alert_subject`` for unified-store rows."""
    if subject.startswith("[Alert] "):
        return subject[len("[Alert] "):]
    return subject


def _open_no_session_alerts(store: Any) -> list[_AlertView]:
    """Return open ``no_session`` alerts as a normalised view list.

    Prefers the unified ``query_messages`` API (every modern store has
    it); falls back to the legacy ``open_alerts`` for the ``StateStore``
    direct-access path.
    """
    query = getattr(store, "query_messages", None)
    if callable(query):
        try:
            rows = query(type="alert", state="open", sender="no_session")
        except Exception:  # noqa: BLE001
            logger.debug(
                "no_session_spawn: query_messages(no_session) failed",
                exc_info=True,
            )
            rows = []
        out: list[_AlertView] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            subject = str(row.get("subject") or "")
            out.append(
                _AlertView(
                    session_name=str(row.get("scope") or ""),
                    alert_type=str(row.get("sender") or ""),
                    message=_strip_alert_subject(subject),
                    created_at=str(row.get("created_at") or ""),
                )
            )
        if out:
            return out
    list_open = getattr(store, "open_alerts", None)
    if not callable(list_open):
        return []
    try:
        rows = list_open()
    except Exception:  # noqa: BLE001
        logger.debug(
            "no_session_spawn: open_alerts enumeration failed",
            exc_info=True,
        )
        return []
    out = []
    for alert in rows:
        if getattr(alert, "alert_type", None) != "no_session":
            continue
        out.append(
            _AlertView(
                session_name=str(getattr(alert, "session_name", "") or ""),
                alert_type=str(getattr(alert, "alert_type", "") or ""),
                message=str(getattr(alert, "message", "") or ""),
                created_at=str(getattr(alert, "created_at", "") or ""),
            )
        )
    return out


def auto_recover_no_session_alerts(
    services: Any,
    *,
    config_path: Path | None = None,
    now: datetime | None = None,
    threshold_seconds: int = DEFAULT_THRESHOLD_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: int = DEFAULT_BACKOFF_BASE_SECONDS,
    backoff_cap_seconds: int = DEFAULT_BACKOFF_CAP_SECONDS,
    spawn: Any | None = None,
) -> list[SpawnDecision]:
    """Walk open ``no_session`` alerts and auto-spawn any that need it.

    Returns the per-alert decisions for the caller to log / aggregate.
    The ``spawn`` argument is a test seam — defaults to :func:`_spawn`.
    """
    store = (
        getattr(services, "msg_store", None)
        or getattr(services, "state_store", None)
    )
    if store is None:
        return []
    now = now or datetime.now(timezone.utc)
    spawn_fn = spawn or _spawn

    decisions: list[SpawnDecision] = []
    for alert in _open_no_session_alerts(store):
        session_name = getattr(alert, "session_name", "") or ""
        role, project = _parse_project_from_alert(alert)
        if role is None or project is None:
            decisions.append(
                SpawnDecision(
                    session_name=session_name,
                    role=role or "",
                    project=project or "",
                    outcome="skipped_unparseable",
                    detail="alert message did not match the expected shape",
                )
            )
            continue
        if role not in _AUTO_SPAWN_ROLES:
            decisions.append(
                SpawnDecision(
                    session_name=session_name,
                    role=role,
                    project=project,
                    outcome="skipped_role",
                    detail=f"{role!r} not in auto-spawn allowlist",
                )
            )
            continue
        if not _project_is_known(services, project):
            decisions.append(
                SpawnDecision(
                    session_name=session_name,
                    role=role,
                    project=project,
                    outcome="skipped_unknown_project",
                )
            )
            continue
        opened_at = _parse_iso(getattr(alert, "created_at", None))
        if opened_at is not None and now - opened_at < timedelta(
            seconds=threshold_seconds,
        ):
            decisions.append(
                SpawnDecision(
                    session_name=session_name,
                    role=role,
                    project=project,
                    outcome="skipped_young",
                    detail=(
                        f"alert age "
                        f"{(now - opened_at).total_seconds():.0f}s < "
                        f"{threshold_seconds}s"
                    ),
                )
            )
            continue

        history = _attempt_history(store, session_name, since=opened_at)
        attempt_number = len(history)
        if attempt_number >= max_attempts:
            decisions.append(
                SpawnDecision(
                    session_name=session_name,
                    role=role,
                    project=project,
                    outcome="escalated",
                    attempt_number=attempt_number,
                    detail=(
                        f"max_attempts={max_attempts} exhausted — escalating"
                    ),
                )
            )
            _escalate_failure(
                store, session_name, role, project, attempt_number,
            )
            continue
        due, _ = _backoff_due(
            history,
            now=now,
            base=backoff_base_seconds,
            cap=backoff_cap_seconds,
        )
        if not due:
            decisions.append(
                SpawnDecision(
                    session_name=session_name,
                    role=role,
                    project=project,
                    outcome="skipped_backoff",
                    attempt_number=attempt_number,
                )
            )
            continue

        # Resolve config path: prefer explicit param, fall back to the
        # services' loaded config so the sweep handler doesn't need to
        # plumb anything new.
        resolved_config_path: Path | None = config_path
        if resolved_config_path is None:
            cfg = getattr(services, "config", None)
            cfg_path = getattr(cfg, "config_path", None)
            if cfg_path is not None:
                resolved_config_path = Path(cfg_path)
        if resolved_config_path is None:
            from pollypm.config import DEFAULT_CONFIG_PATH

            resolved_config_path = DEFAULT_CONFIG_PATH
        ok, detail = spawn_fn(
            config_path=resolved_config_path,
            project=project,
            role=role,
        )
        attempt_number_after = attempt_number + 1
        _record_attempt(
            store,
            session_name,
            f"role={role} project={project} ok={ok} attempt={attempt_number_after} detail={detail}",
        )
        if ok:
            decisions.append(
                SpawnDecision(
                    session_name=session_name,
                    role=role,
                    project=project,
                    outcome="spawned",
                    attempt_number=attempt_number_after,
                    detail=detail,
                )
            )
            # Clear the spawn-failed sibling — we recovered.
            clear = getattr(store, "clear_alert", None)
            if callable(clear):
                try:
                    clear(session_name, SPAWN_FAILED_ALERT_TYPE)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "no_session_spawn: clear_alert(spawn_failed) "
                        "failed for %s", session_name, exc_info=True,
                    )
            continue
        decisions.append(
            SpawnDecision(
                session_name=session_name,
                role=role,
                project=project,
                outcome="spawn_failed",
                attempt_number=attempt_number_after,
                detail=detail,
            )
        )
        if attempt_number_after >= max_attempts:
            _escalate_failure(
                store, session_name, role, project, attempt_number_after,
            )
    return decisions


def summarize_decisions(decisions: Iterable[SpawnDecision]) -> dict[str, int]:
    """Aggregate decisions by outcome — handy for sweep telemetry."""
    out: dict[str, int] = {}
    for decision in decisions:
        out[decision.outcome] = out.get(decision.outcome, 0) + 1
    return out


__all__ = [
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BACKOFF_CAP_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_THRESHOLD_SECONDS",
    "SPAWN_ATTEMPT_EVENT_TYPE",
    "SPAWN_BREAKER_RESET_EVENT_TYPE",
    "SPAWN_FAILED_ALERT_TYPE",
    "SpawnDecision",
    "auto_recover_no_session_alerts",
    "record_breaker_reset",
    "summarize_decisions",
]
