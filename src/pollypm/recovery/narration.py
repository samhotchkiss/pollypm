"""Plain-language narration for recovery and self-heal audit events."""

from __future__ import annotations

import re
from typing import Any, Mapping


RECOVERY_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "heartbeat.missing",
        "recovery.spawn",
        "session.spawn",
        "account.failover.proactive",
        "account.failover.engaged",
        "account.failover.blocked",
        "account.failover.failed",
        "account.failover.suppressed",
        "watchdog.escalation_dispatched",
        "audit.worker_lane_spawned",
        "audit.worker_lane_failed",
        "cockpit.session_respawned",
    }
)

# Events worth calling out in the morning/dashboard brief. ``session.spawn``
# is intentionally omitted because recovery relaunches emit both
# ``recovery.spawn`` and ``session.spawn`` for the same action.
RECOVERY_BRIEF_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "recovery.spawn",
        "account.failover.proactive",
        "account.failover.engaged",
        "watchdog.escalation_dispatched",
        "audit.worker_lane_spawned",
        "cockpit.session_respawned",
    }
)

_ROLE_PREFIX_RE = re.compile(
    r"^(?P<role>worker|architect|reviewer|operator_pm|heartbeat)[_-](?P<project>.+)$"
)
_TASK_REF_RE = re.compile(r"^(?P<project>[A-Za-z][A-Za-z0-9_-]*)/(?P<number>\d+)$")

_FAILURE_PHRASES: dict[str, str] = {
    "capacity_exhausted": "capacity was exhausted",
    "capacity_low": "capacity was low",
    "heartbeat_missing": "a heartbeat disappeared",
    "missing_heartbeat": "a heartbeat disappeared",
    "no_session": "a session was missing",
    "session_missing": "a session was missing",
    "pane_dead": "a pane stopped responding",
    "window_missing": "a window disappeared",
    "runtime_missing": "runtime state was missing",
    "provider_error": "the provider failed",
    "startup_failed": "startup failed",
}

_FINDING_PHRASES: dict[str, str] = {
    "cancellation_churn": "repeated task cancellation",
    "cancellation_no_promotion": "a cancelled task that needed follow-up",
    "queue_without_motion": "queued work that had stopped moving",
    "role_session_missing": "a missing role lane",
    "stuck_draft": "a stuck draft",
    "task_on_hold_stale": "stale on-hold work",
    "task_review_stale": "stale review work",
    "worker_session_dead_loop": "a worker session restart loop",
}


def is_recovery_event(event_name: str | None) -> bool:
    """Return True when ``event_name`` belongs to the recovery/self-heal family."""

    event = (event_name or "").strip()
    if event in RECOVERY_EVENT_NAMES:
        return True
    return event.startswith("recovery.") or event.startswith("account.failover.")


def narrate_recovery_event(
    event_name: str | None,
    metadata: Mapping[str, Any] | None = None,
    *,
    subject: str | None = None,
    project: str | None = None,
    status: str | None = None,
) -> str | None:
    """Render one recovery event as a calm operator-facing sentence."""

    event = (event_name or "").strip()
    if not is_recovery_event(event):
        return None

    meta = dict(metadata or {})
    project_label = _project_label(
        project or _text(meta, "project", "project_key")
    )
    event_subject = _text(meta, "subject") or subject or ""
    session = _text(meta, "target_session", "session", "session_name") or event_subject
    target = _session_label(
        session,
        role=_text(meta, "role"),
        project=project or _text(meta, "project", "project_key"),
    )
    failure = _failure_label(
        _text(meta, "failure_type", "failure_message", "reason")
    )
    problem = _finding_label(_text(meta, "finding_type", "rule", "reason"))
    subject_label = _subject_label(event_subject, project=project or project_label)

    if event in {"recovery.spawn", "session.spawn"}:
        action = "restarted" if _ok_status(status) else "tried to restart"
        if failure:
            return _sentence(f"I {action} {target} after {failure}")
        return _sentence(f"I {action} {target} as part of recovery")

    if event == "heartbeat.missing":
        if failure:
            return _sentence(f"I noticed {target} was unhealthy because {failure}")
        return _sentence(f"I noticed {target} was missing and started recovery")

    if event == "watchdog.escalation_dispatched":
        target_subject = subject_label or f"{problem} in {project_label}"
        if problem and subject_label:
            target_subject = f"{problem} on {subject_label}"
        return _sentence(
            f"I sent an unstick brief for {target_subject} so the project could keep moving"
        )

    if event == "audit.worker_lane_spawned":
        role = _role_label(_text(meta, "role") or "worker")
        task = _subject_label(_text(meta, "task_subject") or event_subject)
        if task:
            return _sentence(f"I opened a {role} lane for {task}")
        return _sentence(f"I opened a {role} lane for {project_label}")

    if event == "audit.worker_lane_failed":
        role = _role_label(_text(meta, "role") or "worker")
        reason = _failure_label(_text(meta, "reason")) or "the launch guard blocked it"
        return _sentence(f"I tried to open a {role} lane for {project_label}, but {reason}")

    if event in {"account.failover.proactive", "account.failover.engaged"}:
        source = _account_label(_text(meta, "from", "previous_account"))
        destination = _account_label(_text(meta, "to", "account"))
        suffix = f" after {failure}" if failure else ""
        if source and destination:
            return _sentence(f"I moved {target} from {source} to {destination}{suffix}")
        if destination:
            return _sentence(f"I moved {target} to {destination}{suffix}")
        return _sentence(f"I moved {target} to a backup account{suffix}")

    if event == "account.failover.blocked":
        reason = _failure_label(_text(meta, "reason")) or "no backup account was available"
        return _sentence(f"I checked failover for {target}, but {reason}")

    if event == "account.failover.failed":
        reason = _failure_label(_text(meta, "last_error", "reason")) or "every backup launch failed"
        return _sentence(f"I tried failover for {target}, but {reason}")

    if event == "account.failover.suppressed":
        reason = _failure_label(_text(meta, "reason")) or "the selected account was already active"
        return _sentence(f"I checked failover for {target}; {reason}")

    if event == "cockpit.session_respawned":
        return _sentence(f"I restored the cockpit view for {target}")

    if subject_label:
        return _sentence(f"I handled a recovery event for {subject_label}")
    return _sentence(f"I handled a recovery event in {project_label}")


def summarize_audit_event(
    *,
    event_name: str | None,
    subject: str | None = None,
    actor: str | None = None,
    status: str | None = None,
    project: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Return a user-facing summary for an audit event."""

    recovery = narrate_recovery_event(
        event_name,
        metadata,
        subject=subject,
        project=project,
        status=status,
    )
    if recovery:
        return recovery
    parts = [(event_name or "audit").strip() or "audit"]
    if subject:
        parts.append(_subject_label(subject, project=project) or subject)
    if status:
        parts.append(str(status))
    if actor:
        parts.append(f"by {_actor_label(actor)}")
    return " · ".join(part for part in parts if part)


def _text(metadata: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _ok_status(status: str | None) -> bool:
    return (status or "ok").strip().lower() not in {"warn", "warning", "error", "failed"}


def _sentence(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return ""
    return cleaned if cleaned.endswith((".", "!", "?")) else cleaned + "."


def _humanize_token(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if not text:
        return ""
    if text in _FAILURE_PHRASES:
        return _FAILURE_PHRASES[text]
    if text in _FINDING_PHRASES:
        return _FINDING_PHRASES[text]
    return text.replace("_", " ").replace("-", " ")


def _failure_label(value: str) -> str:
    if not value:
        return ""
    return _humanize_token(value)


def _finding_label(value: str) -> str:
    if not value:
        return "the issue"
    return _humanize_token(value)


def _project_label(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return "the project"
    return text.replace("_", " ").replace("-", " ")


def _role_label(value: str | None) -> str:
    text = (value or "").strip().lower()
    if text == "operator_pm":
        return "operator PM"
    return text.replace("_", " ").replace("-", " ") or "worker"


def _account_label(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return text.replace("_", " ").replace("-", " ") + " account"


def _actor_label(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return "system"
    match = _ROLE_PREFIX_RE.match(text)
    if match:
        return f"{_role_label(match.group('role'))} for {_project_label(match.group('project'))}"
    return text.replace("_", " ").replace("-", " ")


def _session_label(
    value: str | None,
    *,
    role: str | None = None,
    project: str | None = None,
) -> str:
    text = (value or "").strip()
    role_text = _role_label(role) if role else ""
    project_text = _project_label(project) if project else ""
    match = _ROLE_PREFIX_RE.match(text)
    if match:
        role_text = _role_label(match.group("role"))
        project_text = _project_label(project or match.group("project"))
    if role_text and project_text:
        return f"the {role_text} for {project_text}"
    if role_text:
        return f"the {role_text} session"
    if text:
        return f"the {_actor_label(text)} session"
    if project_text:
        return f"the project session for {project_text}"
    return "the session"


def _subject_label(value: str | None, *, project: str | None = None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    match = _TASK_REF_RE.match(text)
    if match:
        return (
            f"task {match.group('number')} in "
            f"{_project_label(project or match.group('project'))}"
        )
    role_match = _ROLE_PREFIX_RE.match(text)
    if role_match:
        return _session_label(text, project=project)
    return text.replace("_", " ").replace("-", " ")


__all__ = [
    "RECOVERY_BRIEF_EVENT_NAMES",
    "RECOVERY_EVENT_NAMES",
    "is_recovery_event",
    "narrate_recovery_event",
    "summarize_audit_event",
]
