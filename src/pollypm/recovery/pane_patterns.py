"""Pane-text semantic classifier — issue #250.

The 10s ``session.health_sweep`` classifies sessions on mechanical tmux
signals (pane alive? turn active? snapshots repeating?) and on
work-service state (stale claim? drift?). None of those *read* what the
session is saying in its pane. This module fills the gap: a pure,
stateless library of regex/heuristic rules that inspect captured pane
text and return a list of matched rule names.

Scope contract (PR lands *detection + alerts only*):

* No send-keys intervention. The handler that runs these rules raises
  an alert (``pane:<rule>:<session>``) and, for user-actionable rules,
  emits an inbox task. Sending ``/compact``, ``Esc``, or the auto-accept
  keypresses belongs to a follow-up PR (``TODO(#250-followup)``) so
  Sam can review before we start poking live sessions.
* Pure functions here. Side effects (alerts, inbox, event ledger) live
  in the wiring layer — ``core_recurring`` calls ``classify_pane`` and
  decides what to do with the list of names.

Rules (see issue #250 for the full spec):

1. ``context_full`` — Claude or Codex is warning that the context
   window is near full. Case-insensitive match against any of several
   phrasings ("context is getting full", "approaching context limit",
   "context window", "i need to summarize"). User-actionable: the
   handler emits an inbox task so Sam can run ``/compact`` himself.

2. ``stuck_on_error`` — a Python traceback or an "Error:" block is
   visible in the captured pane. Freshness (no subsequent progress) is
   a separate concern; the handler combines this detection with the
   existing stale-claim signals. We deliberately *only* surface the
   error in this library so the classifier stays pure.

3. ``permission_prompt`` — Claude Code is asking a yes/no permission
   prompt ("Do you want to proceed?"). User-actionable: the handler
   emits an inbox task so Sam can approve.

4. ``theme_trust_modal`` — the post-launch "Select a theme" /
   "Do you trust this workspace?" modals that ``_stabilize_claude_launch``
   is supposed to auto-dismiss but sometimes leaks. Detection only
   in this PR; the send-keys auto-dismiss is in the follow-up.

(``suspected_loop`` from the issue is intentionally skipped — the
existing ``SessionSignals.snapshot_repeated`` → ``SessionHealth.LOOPING``
path already classifies this cleanly in
``pollypm/recovery/default.py``. Duplicating it here would create a
second source of truth.)

The rules are ordered by priority — ``classify_pane`` returns matches
in declaration order so callers that want a single-shot classification
can take the first element.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


# ---------------------------------------------------------------------------
# Rule types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassifierRule:
    """A single pane-text classifier rule.

    ``name`` — stable identifier used as the alert type suffix
    (``pane:<name>:<session>``) and as the inbox label tag. Do not
    rename a rule without migrating the open alerts that reference it.

    ``matcher`` — pure ``str -> bool`` callable. Rules that are pure
    regex compile the pattern once at import and bind the ``search``
    method here; rules that need multi-pattern / windowed heuristics
    use a named module-level helper so the test suite can call it
    directly.

    ``severity`` — either ``"warn"`` or ``"error"``. Feeds straight
    through to ``StateStore.upsert_alert``.
    """

    name: str
    matcher: Callable[[str], bool]
    severity: str


# ---------------------------------------------------------------------------
# Individual matchers
# ---------------------------------------------------------------------------


# Compiled once at import — ``re.IGNORECASE`` so we match whatever
# casing Claude or Codex happens to emit this week. The alternation
# covers the explicit operator-visible phrasings *and* Claude's own
# "I need to summarize" self-narration (which sometimes appears before
# the header banner does).
_CONTEXT_FULL_RE = re.compile(
    r"(?:"
    r"context\s+is\s+getting\s+full"
    r"|approaching\s+context\s+limit"
    r"|context\s+window\s+(?:is\s+)?(?:almost\s+)?(?:nearly\s+)?full"
    r"|\u26a0\ufe0f?\s*context\s+window"        # ⚠️ context window
    r"|\u26a0\ufe0f?\s*context\s+low"           # ⚠️ context low
    r"|context\s+low"
    r"|i\s+(?:need|should)\s+to\s+summariz[e]"
    r"|let\s+me\s+summariz[e]\s+(?:the\s+)?conversation"
    r"|i['\u2019]ll\s+summariz[e]\s+(?:the\s+)?conversation"
    r")",
    re.IGNORECASE,
)


def _match_context_full(pane_text: str) -> bool:
    if not pane_text:
        return False
    return _CONTEXT_FULL_RE.search(pane_text) is not None


# Traceback / explicit Error: block. The literal ``Traceback (most
# recent call last)`` is the strongest signal; ``Error:`` with a colon
# is softer so we require either an uppercase ``Error:`` at line start
# (common in JS/TS/tool output) or one of the common named exceptions
# tool output carries. The regex is deliberately anchored to avoid
# matching prose like "That was a reasoning error:" mid-sentence.
_ERROR_RE = re.compile(
    r"(?:"
    r"Traceback\s+\(most\s+recent\s+call\s+last\)"
    r"|^Error:\s"
    r"|^\s*[A-Z][A-Za-z]+Error:\s"               # PythonError: / TypeError:
    r"|^\s*[A-Z][A-Za-z]+Exception:\s"
    r"|^\s*ERROR\s+\[[^\]]+\]"                    # ERROR [tag] ...
    r"|command\s+failed\s+with\s+exit\s+code\s+\d+"
    r")",
    re.MULTILINE,
)


def _match_stuck_on_error(pane_text: str) -> bool:
    if not pane_text:
        return False
    return _ERROR_RE.search(pane_text) is not None


# Permission prompt. Claude Code renders these as a framed yes/no
# prompt with ``Do you want to proceed?`` on a line of its own, plus
# a numbered ``1. Yes`` / ``2. No`` footer. Codex uses
# ``Do you approve?``. We match either the question or the
# ``permissions`` chrome because the latter survives ANSI stripping
# even when the former scrolls off the top of the capture.
_PERMISSION_RE = re.compile(
    r"(?:"
    r"Do\s+you\s+want\s+to\s+proceed\?"
    r"|Do\s+you\s+approve\?"
    r"|\u2394\s*permissions?\s+prompt"            # ⎔ permissions prompt
    r"|\bAllow\s+once\b"
    r"|\b1\.\s*Yes\b[^\n]*\n[^\n]*\b2\.\s*No\b"   # 1. Yes / 2. No menu
    r")",
    re.IGNORECASE,
)


def _match_permission_prompt(pane_text: str) -> bool:
    if not pane_text:
        return False
    return _PERMISSION_RE.search(pane_text) is not None


# Theme / trust / bypass post-launch modals. ``_stabilize_claude_launch``
# is supposed to catch these but occasionally they leak (race on the
# initial render, provider restart, etc.). Detection only — the
# auto-dismiss send-keys is in the follow-up PR so Sam can review the
# safety envelope first.
_TRUST_MODAL_RE = re.compile(
    r"(?:"
    r"Do\s+you\s+trust\s+(?:the\s+)?files?\s+in\s+this\s+(?:folder|workspace)"
    r"|Do\s+you\s+trust\s+this\s+(?:folder|workspace|directory)"
    r"|Trust\s+this\s+folder\?"
    r")",
    re.IGNORECASE,
)

# Theme select modals are identified by the ``theme`` word paired with
# a ``select`` verb on a nearby line — raw "theme" alone is too broad
# (Claude routinely discusses UI themes in code review). Require both
# within a short window.
_THEME_SELECT_RE = re.compile(
    r"(?:select|choose)\s+(?:a\s+|your\s+)?(?:color\s+)?theme",
    re.IGNORECASE,
)


def _match_theme_trust_modal(pane_text: str) -> bool:
    if not pane_text:
        return False
    if _TRUST_MODAL_RE.search(pane_text) is not None:
        return True
    if _THEME_SELECT_RE.search(pane_text) is not None:
        return True
    return False


# ---------------------------------------------------------------------------
# Rule table
# ---------------------------------------------------------------------------


# Order matters — ``classify_pane`` returns hits in declaration order so
# a single-classification caller can take the first element. Priority is:
# context_full (most actionable) → stuck_on_error → permission_prompt →
# theme_trust_modal (most often self-heals).
RULES: list[ClassifierRule] = [
    ClassifierRule(
        name="context_full",
        matcher=_match_context_full,
        severity="warn",
    ),
    ClassifierRule(
        name="stuck_on_error",
        matcher=_match_stuck_on_error,
        severity="warn",
    ),
    ClassifierRule(
        name="permission_prompt",
        matcher=_match_permission_prompt,
        severity="warn",
    ),
    ClassifierRule(
        name="theme_trust_modal",
        matcher=_match_theme_trust_modal,
        severity="warn",
    ),
]


# Rules that should also emit a user-visible inbox task when matched.
# The rest only raise an alert (cockpit-visible, no push notification).
# Membership is data so a follow-up can promote / demote a rule without
# touching the wiring layer.
USER_VISIBLE_RULES: frozenset[str] = frozenset({
    "context_full",
    "permission_prompt",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_pane(pane_text: str) -> list[str]:
    """Return the names of every rule that matches ``pane_text``.

    Results are returned in ``RULES`` declaration order (highest
    priority first). An empty or whitespace-only capture yields an
    empty list — we never fabricate a classification on no signal.

    Pure function: no I/O, no globals beyond the compiled patterns.
    Safe to call from any thread / handler.
    """
    if not pane_text or not pane_text.strip():
        return []
    return [rule.name for rule in RULES if rule.matcher(pane_text)]


def rule_by_name(name: str) -> ClassifierRule | None:
    """Look up a rule by name. Returns ``None`` if unknown.

    Used by the wiring layer to fetch severity when raising an alert —
    we don't want the handler to hard-code severity per rule.
    """
    for rule in RULES:
        if rule.name == name:
            return rule
    return None
