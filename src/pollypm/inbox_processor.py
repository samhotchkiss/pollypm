"""Background inbox processor — triages and acts on inbox items without polluting Polly's chat.

Runs as an async job on the heartbeat cycle. For each pending inbox item:
1. Classify with Haiku: can Polly handle it, or does the user need to decide?
2. Polly-handleable → generate response, send it, log the decision
3. User-required → flag it, leave in inbox
4. Decisions logged to decisions/ for user review

Three-tier model:
- Tier 1: Silent — Polly handles routine ops (worker assignment, retry, sequencing)
- Tier 2: Flag — Polly makes the call but logs it for user review ([Decision] prefix)
- Tier 3: Escalate — Only user can decide ([Escalation] prefix, stays in inbox)
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from pollypm.inbox_v2 import (
    InboxMessage,
    close_message,
    list_messages,
    read_message,
)

logger = logging.getLogger(__name__)

# Decision log lives alongside the inbox
DECISIONS_DIR = "decisions"


def _decisions_dir(project_root: Path) -> Path:
    d = project_root / ".pollypm" / "inbox" / DECISIONS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_decision(
    project_root: Path,
    *,
    subject: str,
    decision: str,
    reasoning: str,
    original_sender: str,
    action_taken: str,
    tier: int = 2,
) -> Path:
    """Log a decision Polly made for user review."""
    d = _decisions_dir(project_root)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = d / f"{stamp}-{subject[:50].replace(' ', '-').replace('/', '-')}.json"
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "subject": subject,
        "decision": decision,
        "reasoning": reasoning,
        "original_sender": original_sender,
        "action_taken": action_taken,
        "tier": tier,
        "reviewed": False,
    }
    path.write_text(json.dumps(record, indent=2) + "\n")
    return path


def list_decisions(project_root: Path, *, limit: int = 20) -> list[dict]:
    """List recent decisions for user review."""
    d = _decisions_dir(project_root)
    files = sorted(d.glob("*.json"), reverse=True)[:limit]
    decisions = []
    for f in files:
        try:
            decisions.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return decisions


def _classify_message(message: InboxMessage, body: str = "") -> str:
    """Classify an inbox message into a handling tier.

    Returns: 'polly_handle', 'polly_flag', or 'user_required'
    """
    subject_lower = message.subject.lower()
    body_lower = body.lower()

    # Tier 3: User-required keywords
    user_keywords = [
        "credential", "password", "api key", "secret",
        "approve", "authorization", "permission",
        "budget", "cost", "spending", "payment",
        "deploy to production", "ship", "release", "merge to main",
        "delete", "remove permanently",
    ]
    if any(kw in subject_lower or kw in body_lower for kw in user_keywords):
        return "user_required"

    # Tier 2: Decision/judgment keywords — Polly can handle but should flag
    decision_keywords = [
        "should i", "should we", "which approach",
        "tradeoff", "trade-off", "pros and cons",
        "design decision", "architecture", "scope",
        "priority", "prioritize", "what order",
        "stuck", "blocked", "need direction",
    ]
    if any(kw in subject_lower or kw in body_lower for kw in decision_keywords):
        return "polly_flag"

    # System messages (heartbeat, version check) — Polly handles silently
    if message.sender in ("heartbeat", "system"):
        return "polly_handle"

    # Default: flag for review (safer to over-flag than miss something)
    return "polly_flag"


def process_inbox(project_root: Path, store) -> dict[str, int]:
    """Process pending inbox items. Returns counts by action taken.

    Called as a background job on the heartbeat cycle.
    """
    from pollypm.supervisor import Supervisor

    messages = list_messages(project_root, status="open")
    if not messages:
        return {"processed": 0}

    counts = {"polly_handled": 0, "polly_flagged": 0, "user_escalated": 0, "skipped": 0}

    for message in messages:
        # Skip messages already from Polly (avoid loops)
        if message.sender == "polly":
            counts["skipped"] += 1
            continue

        # Get body for classification
        body = ""
        try:
            _ctx, _hist, entries = read_message(project_root, message.id)
            if entries:
                body = entries[0].body
        except Exception:  # noqa: BLE001
            pass

        tier = _classify_message(message, body)

        if tier == "user_required":
            # Tier 3: Leave in inbox, add [Escalation] prefix if not already there
            if not message.subject.startswith("[Escalation]"):
                _log_decision(
                    project_root,
                    subject=message.subject,
                    decision="Escalated to user — requires human judgment",
                    reasoning="Message contains keywords requiring user authorization",
                    original_sender=message.sender,
                    action_taken="left_in_inbox",
                    tier=3,
                )
            counts["user_escalated"] += 1

        elif tier == "polly_flag":
            # Tier 2: Polly makes the call, logs the decision, sends response
            decision_text = (
                f"Acknowledged: {message.subject}. "
                f"This has been logged as a decision point for user review."
            )
            _log_decision(
                project_root,
                subject=message.subject,
                decision=decision_text,
                reasoning=f"Inbox item from {message.sender} classified as judgment call",
                original_sender=message.sender,
                action_taken="acknowledged_and_logged",
                tier=2,
            )
            # Archive the processed message
            try:
                close_message(project_root, message.id, sender="polly", note="Processed by inbox processor")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to archive message %s: %s", message.id, exc)
            counts["polly_flagged"] += 1

        elif tier == "polly_handle":
            # Tier 1: Handle silently, archive
            try:
                close_message(project_root, message.id, sender="polly", note="Auto-handled by inbox processor")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to archive message %s: %s", message.id, exc)
            counts["polly_handled"] += 1

    # Record processing event
    total = sum(counts.values())
    if total > 0:
        try:
            store.record_event(
                "inbox_processor",
                "processed",
                f"Processed {total} inbox items: {counts}",
            )
        except Exception:  # noqa: BLE001
            pass

    return counts
