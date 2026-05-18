"""Heuristic classifier for legacy inbox rows (#1570).

Pre-#1565 messages all surface with ``kind='legacy'`` because the
column did not exist when they were written. The
:func:`pollypm.inbox.awaits_user` predicate treats ``legacy`` as
"awaits user" so nothing is silently hidden during the migration
window — but the dashboard's "Waiting on you" section then lumps
~133 historical rows together. This module is the one-time pass
that reclassifies those rows by matching title + sender + project
patterns observed in the real inbox.

Pure leaf: depends only on the :class:`InboxItemKind` enum so it
can be imported from the CLI command + tests without dragging in
any DB layer. The CLI is the only writer; this module decides what
the new kind should be but never touches storage.

Heuristic order is significant — the first match wins. The order
mirrors the precedence in #1570's spec:

1. completion-shaped titles (``complete`` / ``done`` /
   ``resubmitted``) → :attr:`InboxItemKind.COMPLETION_FYI`
2. self-bug-report titles (``Misrouted`` / ``bogus`` / ``proj/1`` /
   ``Repeated stale``) → :attr:`InboxItemKind.SELF_BUG_REPORT`
3. plan-review titles (``Plan ready for review`` prefix) →
   :attr:`InboxItemKind.PLAN_REVIEW_PENDING`
4. polly-authored digest into the inbox project →
   :attr:`InboxItemKind.MANUAL_DECISION`
5. audit-watchdog action dispatches →
   :attr:`InboxItemKind.WATCHDOG_OPERATOR_DISPATCH`
6. unmatched → ``None`` (leave the row as legacy; the awaits-user
   predicate still surfaces it for manual triage)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pollypm.inbox.kind import InboxItemKind


# Title-contract tags that :func:`apply_title_contract` stamps onto
# every stored ``messages.subject`` (see :mod:`pollypm.store.title_contract`).
# Heuristics in this module are written against the user's authored title,
# not the wire format, so we strip the tag before matching — otherwise
# ``"Plan ready for review"`` would never match a row whose stored
# subject is ``"[Action] Plan ready for review …"``.
_TITLE_CONTRACT_TAG_RE = re.compile(
    r"^\s*\[(?:action|fyi|audit|alert|task|note)\]\s*[:\-—]*\s*",
    re.IGNORECASE,
)


def _strip_title_contract_tag(title: str) -> str:
    """Drop a leading title-contract tag from ``title``, if present."""
    return _TITLE_CONTRACT_TAG_RE.sub("", title or "", count=1)


@dataclass(slots=True, frozen=True)
class Classification:
    """Result of a successful classification.

    ``heuristic`` is the human-readable label of the matched rule —
    surfaced in CLI output and stamped onto the audit-log metadata
    so an operator can later answer "why did this row become
    completion_fyi?" by tail-grepping the audit log.
    """

    kind: InboxItemKind
    heuristic: str


# Substrings (case-insensitive) that flag a "task is done" announcement.
_COMPLETION_TOKENS = ("complete", "done", "resubmitted")

# Substrings (case-insensitive) that flag Polly filing a bug report
# about her own system. ``proj/1`` is a particularly common giveaway:
# Polly's first-ever misroute went to a dummy ``proj/1`` task that
# does not exist.
_SELF_BUG_REPORT_TOKENS = ("misrouted", "bogus", "proj/1", "repeated stale")

_PLAN_REVIEW_PREFIX = "plan ready for review"


def classify_legacy(
    *,
    title: str,
    sender: str,
    project: str,
) -> Classification | None:
    """Map a legacy inbox row to a new :class:`InboxItemKind`.

    Returns ``None`` when no heuristic matches. The CLI uses ``None``
    as the signal to leave the row's ``kind`` at ``legacy`` and add
    it to the unmatched-row count — never as an excuse to guess.

    Inputs are normalised to lowercase before matching so producers
    that capitalise inconsistently (Polly mixes ``Complete`` /
    ``complete``) still classify uniformly.

    Args:
        title: the row's subject / title text.
        sender: the row's ``sender`` column (typically ``"polly"`` /
            ``"audit_watchdog"`` / a worker session name).
        project: the row's ``scope`` / project key. The polly-digest
            heuristic requires the literal string ``"inbox"`` here.
    """
    raw_title_l = (title or "").lower()
    title_l = _strip_title_contract_tag(title or "").lower()
    sender_l = (sender or "").lower()
    project_l = (project or "").lower()

    for token in _COMPLETION_TOKENS:
        if token in title_l:
            return Classification(
                kind=InboxItemKind.COMPLETION_FYI,
                heuristic=f"title_contains:{token}",
            )

    for token in _SELF_BUG_REPORT_TOKENS:
        if token in title_l:
            return Classification(
                kind=InboxItemKind.SELF_BUG_REPORT,
                heuristic=f"title_contains:{token}",
            )

    if title_l.startswith(_PLAN_REVIEW_PREFIX):
        return Classification(
            kind=InboxItemKind.PLAN_REVIEW_PENDING,
            heuristic="title_prefix:plan_ready_for_review",
        )

    if (
        "digest:" in title_l
        and sender_l == "polly"
        and project_l == "inbox"
    ):
        return Classification(
            kind=InboxItemKind.MANUAL_DECISION,
            heuristic="polly_digest_in_inbox_project",
        )

    # The audit_watchdog emit site stamps its rows with the ``[Action]``
    # title-contract tag, so we deliberately check the raw (unstripped)
    # title — the tag IS the "Action" signal here, not user prose.
    if sender_l == "audit_watchdog" and "action" in raw_title_l:
        return Classification(
            kind=InboxItemKind.WATCHDOG_OPERATOR_DISPATCH,
            heuristic="audit_watchdog_action",
        )

    return None


__all__ = ["Classification", "classify_legacy"]
