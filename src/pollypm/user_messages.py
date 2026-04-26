"""Structured user-facing messages — one shape, many surfaces (#760).

Every notification, CLI refusal, and inbox action card the user sees
should render with the same four-field shape:

* ``summary`` — one-sentence plain-English description of what
  happened. No PollyPM internals, no task IDs without context.
* ``why_it_matters`` — one sentence explaining the impact if the
  user ignores it. Anchors the urgency ("harmless but clutter" vs
  "your review pipeline thinks it has work that doesn't exist").
* ``next_action`` — a single concrete step or 2–3 labeled choices.
  Tells the user what to do *now*.
* ``details`` — the raw payload (task IDs, stack traces, internal
  state). Hidden by default in the inbox; shown collapsed at the
  end of CLI refusals so the operator can debug if they want.

The pre-#760 inbox / CLI surfaces leaked agent-to-agent slang
("Misrouted review ping: proj/1") that read as gibberish to a human
user. Routing every message through this helper enforces the
discipline: every outbound message has been translated into a shape
the operator can act on without PollyPM domain knowledge.

Hand-authored top-N coverage lives in :data:`KNOWN_ERROR_CLASSES`;
long-tail messages can later route through a model translation
pass that fills the four fields from the raw agent payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class StructuredMessage:
    """Four-field user-facing message contract (#760).

    Every field is plain English. ``details`` is the only place
    PollyPM internals (task IDs, paths, stack traces) belong.
    """

    summary: str
    why_it_matters: str = ""
    next_action: str = ""
    details: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "summary": self.summary,
            "why_it_matters": self.why_it_matters,
            "next_action": self.next_action,
            "details": self.details,
        }


def render_cli_message(msg: StructuredMessage, *, prefix: str = "✗") -> str:
    """Render a :class:`StructuredMessage` for CLI output.

    Layout (matches the issue's "joy-maximizing" example):

        ✗ Cannot start — pending schema migrations on state.db.

        Your PollyPM was upgraded since last boot and the database
        needs one small update before any session can connect.

        Next: run   pm migrate --apply

        > details (schema diff, migration list, DB path)
    """
    parts: list[str] = [f"{prefix} {msg.summary}"]
    if msg.why_it_matters:
        parts.append("")
        parts.append(msg.why_it_matters)
    if msg.next_action:
        parts.append("")
        parts.append(f"Next: {msg.next_action}")
    if msg.details:
        parts.append("")
        parts.append("> details")
        for line in msg.details.splitlines():
            parts.append(f"  {line}")
    return "\n".join(parts)


def render_inbox_action_card(msg: StructuredMessage) -> dict[str, Any]:
    """Convert a :class:`StructuredMessage` into the inbox-action-card
    payload shape the cockpit dashboard renderer expects (#760).

    The dashboard's ``_render_action_card_body`` reads ``plain_prompt``
    + ``unblock_steps`` + ``decision_question``. Map the four-field
    shape onto those keys so the card renders with the same content
    the CLI sees, in the same order.
    """
    lines: list[str] = []
    if msg.why_it_matters:
        lines.append(msg.why_it_matters)
    return {
        "plain_prompt": msg.summary,
        "why_it_matters": msg.why_it_matters,
        "unblock_steps": [msg.next_action] if msg.next_action else [],
        "steps_heading": "What to do",
        "decision_question": "",
        "details": msg.details,
    }


# ---------------------------------------------------------------------------
# Hand-authored top-N error classes (#760's MVP plan).
# ---------------------------------------------------------------------------
#
# Keys are stable error-class identifiers callers pass when they
# raise a refusal. Values are the full structured shape. Add an
# entry here whenever a CLI / agent path emits a recurring error;
# the registry doubles as the test fixture for the rendering layer.

KNOWN_ERROR_CLASSES: dict[str, StructuredMessage] = {
    "migration_pending": StructuredMessage(
        summary="Cannot start — pending schema migrations on state.db.",
        why_it_matters=(
            "Your PollyPM was upgraded since last boot and the database "
            "needs one small update before any session can connect."
        ),
        next_action="run   pm migrate --apply",
        details="(see `pm migrate --check` for the pending migration list)",
    ),
    "account_expired": StructuredMessage(
        summary="Your provider account credentials have expired.",
        why_it_matters=(
            "Workers and the supervisor cannot reach the model until "
            "you re-authenticate — every outbound call will fail."
        ),
        next_action="run   pm accounts relogin <account-name>",
        details="",
    ),
    "task_not_found": StructuredMessage(
        summary="Polly tried to act on a task that no longer exists.",
        why_it_matters=(
            "A stale reference to a task that was cancelled or "
            "purged — harmless to your data, but Polly will keep "
            "tripping on it until the reference is cleaned up."
        ),
        next_action="run   pm task cleanup --dry-run   to see other stale refs",
        details="",
    ),
    "plan_rejected": StructuredMessage(
        summary="The architect's plan was rejected.",
        why_it_matters=(
            "Implementation can't start until the architect produces "
            "a revised plan that addresses the rejection feedback."
        ),
        next_action="run   pm project replan <project>   to start a fresh planning round",
        details="",
    ),
}


def known_error(name: str) -> StructuredMessage | None:
    """Look up a hand-authored top-N error class. Returns ``None`` on miss."""
    return KNOWN_ERROR_CLASSES.get(name)


__all__ = [
    "StructuredMessage",
    "render_cli_message",
    "render_inbox_action_card",
    "KNOWN_ERROR_CLASSES",
    "known_error",
]
