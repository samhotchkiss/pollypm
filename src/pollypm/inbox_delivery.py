"""Inbox delivery — ensures agent-targeted messages get worked on.

Runs as an async job on the heartbeat cycle. For each agent with open
inbox items, checks if they're in an active turn. If idle, pokes them
once: "you have N items, check pm mail." The agent works through their
queue. The heartbeat keeps checking every cycle until the queue is clear.

Also provides deliver_single_message() for instant delivery when a
reply is sent from the cockpit UI or CLI.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pollypm.inbox_v2 import list_messages, mark_delivered

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)

# Don't poke the same agent more than once per this window
_POKE_COOLDOWN_SECONDS = 90

# Map recipient names to session names
_RECIPIENT_SESSION_MAP = {
    "polly": "operator",
    "operator": "operator",
}


def _resolve_session_name(recipient: str) -> str:
    """Map a recipient name to a tmux session name."""
    return _RECIPIENT_SESSION_MAP.get(recipient, recipient)


def ensure_inbox_progress(config: PollyPMConfig) -> dict[str, int]:
    """Ensure every agent with open inbox items is actively working.

    Groups open messages by recipient agent. For each agent:
    - If agent is in an active turn → leave them alone
    - If agent is idle → poke: "you have N items, check pm mail"
    - Mark pending messages as delivered after poking

    Returns counts: poked, active, skipped.
    """
    from pollypm.supervisor import Supervisor
    from pollypm.storage.state import StateStore

    counts = {"poked": 0, "active": 0, "skipped": 0}
    root = config.project.root_dir

    # Group open agent-targeted messages by recipient
    messages = list_messages(root, status="open")
    by_agent: dict[str, list] = defaultdict(list)
    for m in messages:
        if m.to and m.to != "user":
            by_agent[m.to].append(m)

    if not by_agent:
        return counts

    sup = Supervisor(config)
    store = StateStore(config.project.state_db)

    for agent, items in by_agent.items():
        session_name = _resolve_session_name(agent)

        # Is the agent actually in an active turn?
        # Check both runtime status AND whether the pane output is changing.
        # An agent classified as "healthy" or "needs_followup" might actually
        # be idle at a prompt — the text classifier is unreliable.
        rt = store.get_session_runtime(session_name)
        is_active = False
        if rt and rt.status in ("healthy", "needs_followup"):
            # Double-check: are recent snapshots identical? If so, agent is idle.
            try:
                hashes = store.execute(
                    "SELECT snapshot_hash FROM heartbeats WHERE session_name = ? ORDER BY id DESC LIMIT 2",
                    (session_name,),
                ).fetchall()
                if len(hashes) >= 2 and hashes[0][0] != hashes[1][0]:
                    is_active = True  # Pane is changing — genuinely active
                # If hashes are identical, agent is idle despite status
            except Exception:  # noqa: BLE001
                is_active = True  # Assume active on error
        if is_active:
            counts["active"] += 1
            for m in items:
                if m.delivery_state == "pending":
                    mark_delivered(root, m.id, state="delivered")
            continue

        # Agent is idle — should we poke?
        # Check cooldown: don't poke same agent more than once per window
        last_poke = store.last_event_at(session_name, "inbox_poke")
        if last_poke:
            try:
                age = (datetime.now(UTC) - datetime.fromisoformat(last_poke)).total_seconds()
                if age < _POKE_COOLDOWN_SECONDS:
                    counts["skipped"] += 1
                    continue
            except (ValueError, TypeError):
                pass

        # Poke: one short message listing the count
        n = len(items)
        poke = f"[Inbox] You have {n} item(s) waiting. Run: pm mail"

        try:
            sup.send_input(session_name, poke, owner="pollypm", force=True)
            store.record_event(session_name, "inbox_poke", f"Poked: {n} items waiting")
            counts["poked"] += 1
            # Mark all pending items as delivered
            for m in items:
                if m.delivery_state in ("pending", "failed"):
                    mark_delivered(root, m.id, state="delivered")
            logger.info("Poked %s: %d items waiting", session_name, n)
        except Exception as exc:  # noqa: BLE001
            counts["skipped"] += 1
            logger.warning("Failed to poke %s: %s", session_name, exc)

    store.close()
    sup.store.close()
    return counts


def deliver_single_message(config: PollyPMConfig, msg_id: str) -> bool:
    """Deliver a single message immediately. Returns True on success.

    Called from cockpit UI reply and CLI reply for instant delivery
    instead of waiting for the next heartbeat cycle.
    """
    from pollypm.supervisor import Supervisor

    root = config.project.root_dir
    messages = list_messages(root, status="open")
    msg = next((m for m in messages if m.id == msg_id), None)

    if msg is None or msg.to == "user" or msg.to == "":
        return False

    session_name = _resolve_session_name(msg.to)
    payload = f"[Inbox] New message: {msg.subject[:60]} — run: pm mail {msg_id}"

    sup = Supervisor(config)
    try:
        sup.send_input(session_name, payload, owner="pollypm", force=True)
        mark_delivered(root, msg_id, state="delivered")
        logger.info("Immediate delivery of %s to %s", msg_id[:30], session_name)
        return True
    except Exception as exc:  # noqa: BLE001
        mark_delivered(root, msg_id, state="failed")
        logger.warning("Immediate delivery failed for %s to %s: %s", msg_id[:30], session_name, exc)
        return False
    finally:
        sup.store.close()
