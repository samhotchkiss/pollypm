"""Heuristic classifier for legacy inbox rows (#1570, #1564 follow-up).

Pre-#1565 messages all surface with ``kind='legacy'`` because the
column did not exist when they were written. The
:func:`pollypm.inbox.awaits_user` predicate treats ``legacy`` as
"awaits user" so nothing is silently hidden during the migration
window — but the dashboard's "Waiting on you" section then lumps
~133 historical rows together. This module is the one-time pass
that reclassifies those rows by matching title + sender + project
patterns observed in the real inbox.

Two classifier entry points:

* :func:`classify_legacy` — messages-shaped rows (title / sender /
  scope). Original surface from #1570.
* :func:`classify_legacy_task` — work_tasks-shaped rows (title /
  created_by). Added in the #1564 follow-up so the dashboard's
  "Watchdog escalated: …" rows (which come from the work_tasks
  table, not messages) can also be retagged off ``legacy``.

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

The task-side classifier (:func:`classify_legacy_task`) is
deliberately narrower: tasks don't carry a ``sender`` / ``scope``
pair the way messages do, and the watchdog-created task rows the
2026-05-17 dashboard surfaced are dominated by the
queue-without-motion subject. Adding heuristics there that don't
have field-observed evidence would just guess.
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


# ---------------------------------------------------------------------------
# Task-side heuristics (#1564 follow-up)
# ---------------------------------------------------------------------------


# Substring of the queue_without_motion watchdog finding's message —
# the exact title every legacy ``audit_watchdog``-created task in the
# field carries when the watchdog escalated a wedged queue to the
# operator. Match is case-insensitive; only the distinctive middle of
# the phrase is checked so a future minor copy edit (e.g. "claim or
# execution") doesn't silently break the rule.
_WATCHDOG_QUEUE_WITHOUT_MOTION_TOKEN = "queued task"
_WATCHDOG_QUEUE_WITHOUT_MOTION_TAIL = "claim / execution"

# Watchdog operator-dispatch tasks created via
# :func:`pollypm.dashboard.categorization.why_waiting` carry the prefix
# below in any rerouted form. The cockpit copy "Watchdog escalated:"
# is rendered only — it never ends up in storage — but we include the
# token so a future producer that does name itself this way is caught.
_WATCHDOG_ESCALATED_TOKEN = "watchdog escalated"

# Tasks the watchdog files via
# :func:`pollypm.work.plan_review_emit.emit_plan_review` use the
# ``"Plan ready for review"`` prefix. Matching this here would
# duplicate the message-side heuristic; the prefix check below reuses
# the same constant so the two surfaces classify identically.


def classify_legacy_task(
    *,
    title: str,
    created_by: str,
) -> Classification | None:
    """Map a legacy ``work_tasks`` row to a new :class:`InboxItemKind`.

    Returns ``None`` when no heuristic matches. The CLI uses ``None``
    as the signal to leave the row's ``kind`` at ``legacy`` and add
    it to the unmatched-row count — never as an excuse to guess.

    Args:
        title: the task's ``title`` column.
        created_by: the task's ``created_by`` column (e.g.
            ``"audit_watchdog"`` for watchdog-emitted dispatches).
    """
    title_l = (title or "").lower()
    created_by_l = (created_by or "").lower()

    # Rule 1: watchdog-emitted queue-without-motion escalations →
    # WATCHDOG_OPERATOR_DISPATCH. Two shapes are matched:
    # the literal "queued task(s) but no claim / execution …" finding
    # body, and any future "Watchdog escalated:" prefix.
    if created_by_l == "audit_watchdog":
        if (
            _WATCHDOG_QUEUE_WITHOUT_MOTION_TOKEN in title_l
            and _WATCHDOG_QUEUE_WITHOUT_MOTION_TAIL in title_l
        ):
            return Classification(
                kind=InboxItemKind.WATCHDOG_OPERATOR_DISPATCH,
                heuristic="watchdog_queue_without_motion",
            )
        if title_l.startswith(_WATCHDOG_ESCALATED_TOKEN):
            return Classification(
                kind=InboxItemKind.WATCHDOG_OPERATOR_DISPATCH,
                heuristic="watchdog_escalated_prefix",
            )

    # Rule 2: plan-review tasks (watchdog or architect emitter) →
    # PLAN_REVIEW_PENDING. Matches the messages-side prefix rule so
    # the two surfaces classify a plan-review pair identically.
    if title_l.startswith(_PLAN_REVIEW_PREFIX):
        return Classification(
            kind=InboxItemKind.PLAN_REVIEW_PENDING,
            heuristic="title_prefix:plan_ready_for_review",
        )

    return None


__all__ = ["Classification", "classify_legacy", "classify_legacy_task"]
