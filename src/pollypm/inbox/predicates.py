"""Canonical inbox predicates (#1566).

This module is the **single source of truth** for "does this inbox
item await the user?" Before #1566 there were three competing
"what's in the inbox" predicates (CLI default = 44, ``--include-inbox``
= 133, rail = 97) and none of them agreed; the rail badge, the
dashboard, the archive view, and the ``pm inbox --awaits-user``
CLI flag all read from :func:`awaits_user` so the answer is the
same everywhere.

If you find yourself defining a parallel "is actionable" predicate
on another surface, stop and import this one instead — reintroducing
a second definition is the exact failure mode that motivated the
issue.

The module is a leaf in the import graph: it depends only on the
:class:`InboxItemKind` enum and reads the ``kind`` attribute off an
inbox-entry-shaped object. No cockpit, no Supervisor, no DB. That
keeps it importable from every surface (cockpit, CLI, web API,
work-service, tests).
"""

from __future__ import annotations

from typing import Protocol

from pollypm.inbox.kind import InboxItemKind, coerce_kind


class _HasKind(Protocol):
    """Structural shape — anything carrying a ``kind`` attribute fits."""

    kind: object


# Kinds whose row genuinely needs a human decision. Frozen so a caller
# can't mutate it; named at module scope so tests can pin the exact set.
_AWAITS_USER_KINDS: frozenset[InboxItemKind] = frozenset(
    {
        InboxItemKind.PLAN_REVIEW_PENDING,
        InboxItemKind.APPROVAL_REQUEST,
        InboxItemKind.PM_QUESTION_UNANSWERED,
        InboxItemKind.WATCHDOG_OPERATOR_DISPATCH,
        InboxItemKind.MANUAL_DECISION,
    }
)


def awaits_user(item: _HasKind) -> bool:
    """Return True iff ``item`` needs the user's attention.

    The decision is keyed entirely on ``item.kind``. Five kinds are
    user-facing decisions (plan review, approval request, unanswered
    PM question, watchdog operator dispatch, manual decision); every
    other tagged kind is informational and returns False.

    :attr:`InboxItemKind.LEGACY` returns ``True`` on purpose: rows
    written before #1565 landed haven't been classified yet, and the
    backfill in #1570 hasn't run. Treating them as "awaits user"
    means a pre-migration row stays visible until the backfill
    reclassifies it — safer than silently hiding work behind the
    new predicate. Once #1570 lands and no ``LEGACY`` rows remain,
    this default has no observable effect.
    """
    kind = coerce_kind(getattr(item, "kind", None))
    if kind is InboxItemKind.LEGACY:
        return True
    return kind in _AWAITS_USER_KINDS
