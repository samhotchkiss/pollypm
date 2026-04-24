"""Helpers for interpreting review-related inbox notifications.

Contract:
- Inputs: a notify subject/body pair emitted by reviewer/operator flows.
- Outputs: boolean classification for whether the message means
  "park the review task and wait on operator help" versus
  "this task is simply ready for review / approval".
- Side effects: none.
- Invariants: review-ready handoffs must NOT be reclassified as blocked
  holds, or the user loses the accept path for tasks that are actually
  ready for approval.
"""

from __future__ import annotations

import re

_BLOCKER_PATTERNS = (
    re.compile(r"\bwaiting on\b", re.IGNORECASE),
    re.compile(r"\bblocked\b", re.IGNORECASE),
    re.compile(r"\bneeds? operator help\b", re.IGNORECASE),
    re.compile(r"\bneed your call\b", re.IGNORECASE),
    re.compile(r"\bneed polly'?s call\b", re.IGNORECASE),
    re.compile(r"\bscope escalation\b", re.IGNORECASE),
    re.compile(r"\boperator input\b", re.IGNORECASE),
    re.compile(r"\bdeploy credentials\b", re.IGNORECASE),
    re.compile(r"\baccount access expired\b", re.IGNORECASE),
)

_REVIEW_READY_PATTERNS = (
    re.compile(r"\bplan ready for review\b", re.IGNORECASE),
    re.compile(r"\bready for review\b", re.IGNORECASE),
    re.compile(r"\bhanded to review\b", re.IGNORECASE),
    re.compile(r"^\s*\[action\]\s*done:", re.IGNORECASE),
    re.compile(r"\bpress a to approve\b", re.IGNORECASE),
    re.compile(r"\badvances to emit\b", re.IGNORECASE),
)


def notify_requires_review_hold(*, subject: str, body: str) -> bool:
    """True when a notify implies "block review, wait on operator".

    This is intentionally narrower than "any notify mentioning a review
    task". Review-ready handoffs and plan-approval messages should leave
    the task in ``review`` so the human can still approve/reject it.
    """

    text = "\n".join(
        part.strip() for part in (subject, body) if part and part.strip()
    )
    if not text:
        return False
    if any(pattern.search(text) for pattern in _REVIEW_READY_PATTERNS):
        return False
    return any(pattern.search(text) for pattern in _BLOCKER_PATTERNS)


__all__ = ["notify_requires_review_hold"]
