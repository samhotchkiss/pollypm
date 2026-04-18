"""Keyword-driven priority classifier for :mod:`pollypm.cli` ``pm notify``.

Extracted from :mod:`pollypm.notification_staging` in issue #340 when
writers moved off the legacy staging table onto :class:`Store`. The
classifier is pure text → tier and has no dependency on either storage
layer, so it lives here in ``pollypm.store`` alongside the new writers.

Back-compat: :mod:`pollypm.notification_staging` re-exports
:func:`classify_priority` and :func:`validate_priority` from this module
so existing importers continue to work until Issue F retires the
``notification_staging`` module entirely.

The keyword lists are preserved verbatim from the pre-move implementation
— a drift here would silently reroute "please verify" notifications to
digest, which burned Sam once already (see #335).
"""

from __future__ import annotations

import re
from typing import Iterable


# ---------------------------------------------------------------------------
# Priority-classification keyword lists (unchanged — see module docstring)
# ---------------------------------------------------------------------------

_IMMEDIATE_KEYWORDS: tuple[str, ...] = (
    "blocker",
    "question",
    "rejected",
    "needs decision",
    "stuck",
    "failed",
    "persona swap",
    # Bug-report shape — operator (Polly) flagged that "dogfood
    # finding" / "Archie skips per-stage pm task done" / "Gap A
    # fallback doesn't fire" notifications were silently classified as
    # digest because they happened to contain a completion keyword
    # ("done"). Bug reports must surface immediately so the user can
    # triage before a milestone flush.
    "bug",
    "gap",
    "finding",
    "regression",
    "broken",
    "misclassification",
    "skips",
)

_DIGEST_KEYWORDS: tuple[str, ...] = (
    "done",
    "shipped",
    "merged",
    "approved",
    "completed",
    "complete",
)

_SILENT_KEYWORDS: tuple[str, ...] = (
    "test pass",
    "audit",
    "recorded",
)

# Action-requiring phrases — when paired with a completion marker, the
# notification upgrades to immediate so "done — please verify" doesn't
# get trapped in the milestone digest.
_ACTION_REQUIRING_PHRASES: tuple[str, ...] = (
    "ready for testing",
    "ready for review",
    "ready for approval",
    "ready for account",
    "needs your attention",
    "needs your review",
    "needs your input",
    "needs your approval",
    "awaiting your",
    "awaiting approval",
    "awaiting review",
    "safe to",
    "time to",
    "please verify",
    "please review",
    "please approve",
    "clear — ready",
    "clear - ready",
    "clear, ready",
    "done — ready",
    "done - ready",
    "done, ready",
    "shipped — ready",
    "shipped - ready",
    "shipped, ready",
)

# Extra completion markers for the action-requiring upgrade path only.
_COMPLETION_MARKERS: tuple[str, ...] = (
    "done",
    "shipped",
    "merged",
    "approved",
    "completed",
    "clear",
    "complete",
    "finished",
    "ready",
    "passed",
    "test pass",
    "tests pass",
    "suite pass",
)

_VALID_PRIORITIES: frozenset[str] = frozenset({"immediate", "digest", "silent"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _has_keyword(text: str, keywords: Iterable[str]) -> bool:
    """Case-insensitive substring match with whitespace normalisation.

    Single-word tokens must match on a word boundary (so "done" hits
    "Done: deploy" but not "condone"). Multi-word phrases match as
    literal substrings after collapsing runs of whitespace.
    """
    haystack = re.sub(r"\s+", " ", text.lower())
    for raw in keywords:
        kw = raw.lower().strip()
        if not kw:
            continue
        if " " in kw:
            if kw in haystack:
                return True
        else:
            pattern = rf"\b{re.escape(kw)}\b"
            if re.search(pattern, haystack):
                return True
    return False


def classify_priority(subject: str, body: str) -> str:
    """Infer notify priority from subject + body keywords.

    Precedence:

    1. **immediate** — explicit urgency keywords (blocker, question, …).
    2. **immediate (action-requiring completion)** — completion marker
       (done/shipped/clear/ready/…) co-occurring with an action-requiring
       phrase (ready for testing / please verify / safe to / …).
    3. **silent** — routine audit-trail markers.
    4. **digest** — completion-only updates with no action-requiring phrase.
    5. **default: immediate** — ambiguous input over-notifies rather
       than silently dropping.

    Returns one of ``'immediate'`` / ``'digest'`` / ``'silent'``.
    """
    text = f"{subject}\n{body}"
    if _has_keyword(text, _IMMEDIATE_KEYWORDS):
        return "immediate"
    if _has_keyword(text, _ACTION_REQUIRING_PHRASES) and _has_keyword(
        text, _COMPLETION_MARKERS,
    ):
        return "immediate"
    if _has_keyword(text, _SILENT_KEYWORDS):
        return "silent"
    if _has_keyword(text, _DIGEST_KEYWORDS):
        return "digest"
    return "immediate"


def validate_priority(priority: str) -> str:
    """Normalise and validate a priority string.

    Raises
    ------
    ValueError
        When ``priority`` is not one of the three valid tiers. Error
        names the invalid value, lists the accepted set, and points the
        caller at the ``--priority`` flag so they know how to fix
        (three-question rule — #240).
    """
    p = (priority or "").strip().lower()
    if p not in _VALID_PRIORITIES:
        raise ValueError(
            f"Invalid priority {priority!r}. "
            f"The notify classifier only accepts "
            f"{sorted(_VALID_PRIORITIES)}, but {p!r} was supplied so "
            f"the tier would be undefined. "
            f"Fix: pass one of 'immediate' / 'digest' / 'silent' via "
            f"the ``--priority`` flag, or omit the flag to auto-classify."
        )
    return p


__all__ = [
    "classify_priority",
    "validate_priority",
]
