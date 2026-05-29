"""Shared parsing for operator-owned on-hold task reasons.

Contract:
- Inputs: free-form task hold reasons, usually from ``Task.transitions``.
- Outputs: stable routing tags and operator-facing cleaned prompts.
- Side effects: none.
- Invariants: the ``human-needed`` prefix is the single source of truth
  for holds that should wait on the operator instead of re-escalating to
  an architect.
"""

from __future__ import annotations

import re


ARCHITECT_ACTIONABLE_TAG = "architect-actionable"
HUMAN_NEEDED_TAG = "human-needed"

_HUMAN_NEEDED_PREFIX_RE = re.compile(
    r"^\s*(?:\[" + re.escape(HUMAN_NEEDED_TAG) + r"\]|"
    + re.escape(HUMAN_NEEDED_TAG) + r")\s*[:\]-]?\s*",
    re.IGNORECASE,
)


def classify_on_hold_reason(reason: str | None) -> str:
    """Return the routing tag implied by an ``on_hold`` reason."""
    if is_human_needed_hold_reason(reason):
        return HUMAN_NEEDED_TAG
    return ARCHITECT_ACTIONABLE_TAG


def is_human_needed_hold_reason(reason: str | None) -> bool:
    """True when ``reason`` starts with the reserved human-needed tag."""
    if not reason:
        return False
    return _HUMAN_NEEDED_PREFIX_RE.match(reason) is not None


def human_needed_hold_prompt(reason: str | None) -> str:
    """Strip routing syntax and return the operator-facing ask."""
    if not reason:
        return ""
    cleaned = _HUMAN_NEEDED_PREFIX_RE.sub("", reason, count=1)
    return " ".join(part.strip() for part in cleaned.splitlines() if part.strip())


__all__ = [
    "ARCHITECT_ACTIONABLE_TAG",
    "HUMAN_NEEDED_TAG",
    "classify_on_hold_reason",
    "human_needed_hold_prompt",
    "is_human_needed_hold_reason",
]
