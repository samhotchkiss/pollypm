"""Escalate waiting_on_user sessions to the inbox.

If a session has been waiting on user input for more than a few minutes,
create an inbox item so the user notices — whether they're at the desk
or away. Dedup prevents noise (one item per session per hour).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.messaging import create_message, list_open_messages
from pollypm.storage.state import StateStore


# How long a session must be waiting_on_user before escalating to inbox
_ESCALATION_DELAY = timedelta(minutes=5)

# Don't create duplicate inbox items for the same session within this window
_DEDUP_WINDOW = timedelta(hours=1)


def _last_user_interaction(store: StateStore) -> datetime | None:
    """Find the most recent user-initiated event (send, lease claim, etc.)."""
    # User actions show up as events with specific types
    _USER_EVENT_TYPES = {"send_input", "lease", "human_send"}
    try:
        recent = store.recent_events(limit=200)
        for event in recent:
            if event.event_type in _USER_EVENT_TYPES:
                # Check if it was a human action, not heartbeat/automation
                if "heartbeat" not in event.message.lower() and "pm-bot" not in event.message.lower():
                    return datetime.fromisoformat(event.created_at)
    except Exception:  # noqa: BLE001
        pass
    return None


def escalate_waiting_sessions(store: StateStore, project_root: Path) -> list[str]:
    """Check for sessions waiting on user input with no recent user activity.

    Returns list of session names that were escalated to inbox.
    """
    now = datetime.now(UTC)
    escalated: list[str] = []

    # Find sessions that are waiting_on_user
    runtimes = store.list_session_runtimes()
    # Check both open AND closed messages to avoid re-creating archived items
    from pollypm.messaging import list_closed_messages
    existing_messages = list_open_messages(project_root) + list_closed_messages(project_root)
    existing_subjects = {msg.subject for msg in existing_messages}

    for rt in runtimes:
        if rt.status != "waiting_on_user":
            continue

        # Check how long it's been waiting
        updated_at = rt.updated_at or rt.last_recovered_at
        if updated_at:
            try:
                waiting_since = datetime.fromisoformat(updated_at)
                if (now - waiting_since) < _ESCALATION_DELAY:
                    continue  # not waiting long enough
            except (ValueError, TypeError):
                pass

        # Don't duplicate inbox items
        subject = f"Session '{rt.session_name}' is waiting for your input"
        if subject in existing_subjects:
            continue

        # Check recent events to avoid re-escalating within the dedup window
        try:
            recent = store.recent_events(limit=100)
            already_escalated = any(
                e.session_name == rt.session_name
                and e.event_type == "inbox_escalation"
                and (now - datetime.fromisoformat(e.created_at)) < _DEDUP_WINDOW
                for e in recent
            )
            if already_escalated:
                continue
        except Exception:  # noqa: BLE001
            pass

        create_message(
            project_root,
            sender="heartbeat",
            subject=subject,
            body=(
                f"The session '{rt.session_name}' has been waiting for your input.\n"
                f"\n"
                f"To respond:\n"
                f"  pm send {rt.session_name} \"<your message>\"\n"
                f"\n"
                f"Or open the cockpit and click on the session."
            ),
        )
        store.record_event(rt.session_name, "inbox_escalation", f"Escalated to inbox: {subject}")
        # Send OS notification so the user knows even if they're not watching
        try:
            from pollypm.notifications import send_notification
            send_notification("PollyPM", f"{rt.session_name} needs your input")
        except Exception:  # noqa: BLE001
            pass
        escalated.append(rt.session_name)

    return escalated
