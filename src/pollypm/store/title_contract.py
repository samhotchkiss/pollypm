"""Title-shape contract — every inbox subject starts with a ``[Tag]``.

Users who live in ``pm inbox`` told us the message titles were not pulling
their weight: a column of subjects like ``"deploy finished"`` forced the
operator to open each row to know whether it needed action. The fix lives
at the writer boundary — :meth:`SQLAlchemyStore.enqueue_message` callers
route their ``subject`` through :func:`apply_title_contract` so every
stored row starts with a bracketed tag the operator can scan at a glance:

* ``[Action]``  — tier='immediate' notify: the user has to do something.
* ``[FYI]``     — tier='digest' notify: routine progress, read in rollup.
* ``[Audit]``   — tier='silent' anything: log-only, never surfaces.
* ``[Alert]``   — type='alert': session-health or quota bark.
* ``[Task]``    — type='inbox_task': a work-service task assigned via inbox.

Callers that have already formatted their subject with a bracket tag
(``"[Done] milestone 02"``) are left alone — this helper only adds a tag
when one is missing, so no ``[Foo][Action]`` double-prefix ever appears.

Issue #340. Paired with the writer rewrite so the contract applies to
every new row going forward; historical rows that predate the contract
are left untouched (Issue E will migrate readers to tolerate both).
"""

from __future__ import annotations

import re


# Compiled once — the regex matches "any non-whitespace run at the start
# of the string, followed by the first ``]``, followed by whitespace or
# end of string". We intentionally don't validate the tag content; a
# caller that wrote ``[Custom]`` knows what they're doing and we stay
# out of their way.
_LEADING_BRACKET_TAG = re.compile(r"^\s*\[[^\]]+\]")


# Tag lookup table. Keys are ``(tier, type)`` where either slot can be
# ``None`` to mean "wildcard — any value". Specific pairs win over
# wildcards; alerts/inbox_tasks get their tag from ``type`` alone.
_TAG_TABLE: tuple[tuple[tuple[str | None, str | None], str], ...] = (
    # Specific (tier, type) pairs first.
    (("immediate", "notify"), "[Action]"),
    (("digest", "notify"), "[FYI]"),
    # ``silent`` tier is always audit regardless of type.
    (("silent", None), "[Audit]"),
    # Type-driven fallbacks (no tier match above).
    ((None, "alert"), "[Alert]"),
    ((None, "inbox_task"), "[Task]"),
)


def _lookup_tag(tier: str | None, type: str | None) -> str:
    """Return the bracket tag string for ``(tier, type)``.

    Falls back to ``[Note]`` when nothing matches so we never emit a
    tagless subject — the contract is "every subject starts with a
    bracket tag", and a silent default is safer than raising.
    """
    tier_norm = (tier or "").strip().lower() or None
    type_norm = (type or "").strip().lower() or None
    for (want_tier, want_type), tag in _TAG_TABLE:
        tier_ok = want_tier is None or want_tier == tier_norm
        type_ok = want_type is None or want_type == type_norm
        if tier_ok and type_ok:
            return tag
    return "[Note]"


def has_bracket_tag(subject: str) -> bool:
    """Return ``True`` if ``subject`` already starts with a ``[...]`` tag.

    Accepts leading whitespace before the bracket so subjects accidentally
    prefixed with a space still count as pre-tagged (the caller knew
    what they were doing).
    """
    return bool(_LEADING_BRACKET_TAG.match(subject or ""))


def apply_title_contract(
    subject: str,
    *,
    tier: str | None = None,
    type: str | None = None,
) -> str:
    """Return ``subject`` with a bracket tag prepended when missing.

    Parameters
    ----------
    subject
        The caller-supplied subject line.
    tier
        Message tier (``'immediate'`` / ``'digest'`` / ``'silent'``).
        Combined with ``type`` to derive the tag.
    type
        Message type (``'notify'`` / ``'alert'`` / ``'inbox_task'`` /
        ``'event'`` / …). Combined with ``tier`` to derive the tag.

    Returns
    -------
    str
        The subject, unchanged if it already started with ``[...]``;
        otherwise the computed tag plus a single space plus the original
        subject.
    """
    text = (subject or "").strip()
    if not text:
        # Empty subjects should be caught by the caller's validation
        # before we ever see them — returning the tag alone is still
        # better than an empty stored row.
        return _lookup_tag(tier, type)
    if has_bracket_tag(text):
        return text
    return f"{_lookup_tag(tier, type)} {text}"


__all__ = [
    "apply_title_contract",
    "has_bracket_tag",
]
