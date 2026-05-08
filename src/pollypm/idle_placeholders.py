"""Shared idle-placeholder detection (#1010).

The Codex CLI shows a rotating "idle-input placeholder" hint in its
input box when the agent is waiting for the user to type something —
strings like ``› Improve documentation in @filename`` or ``› Summarize
recent commits``. The Claude CLI shows a blank ``❯`` prompt with no
recent assistant output in the same situation. Both are UI suggestion
chrome, NOT actual session activity / actual stuck content.

#994 added :func:`_is_codex_idle_placeholder` in
:mod:`pollypm.dashboard_data` so the cockpit's "Now" panel could stop
leaking the placeholder text as if it were the agent's current
activity. #1010 needed the same false-positive class addressed in the
session-health classifier path that #1008's auto-clear consults — an
alive Codex pane showing the placeholder must be classified as
``idle-but-healthy`` so the heartbeat doesn't keep re-raising
``stuck_session`` against it (which would block the 90s healthy-streak
debounce from accumulating).

Extracted to a shared module so the dashboard renderer, the heartbeat
classifier, and any future surface that asks "is this pane just idle
chrome?" all share one definition. Adding a new placeholder string
(Codex ships them on a rotation) lands in one place rather than two.
"""

from __future__ import annotations

# Known rotating placeholder hints the Codex CLI shows in an empty
# input box when the agent is idle. The list is a defensive net behind
# the leading-``›`` prompt-arrow skip; matching only verified-known
# strings keeps the false-positive risk low.
CODEX_IDLE_PLACEHOLDERS: tuple[str, ...] = (
    "run /review on my current changes",
    "explain this codebase",
    "write tests for @filename",
    "summarize recent commits",
    "use /skills to list available skills",
    "find and fix a bug in @filename",
    "improve documentation in @filename",
)


def is_codex_idle_placeholder(line: str) -> bool:
    """True if ``line`` looks like a Codex idle-input placeholder hint.

    Strips an optional leading ``›`` (U+203A) prompt-arrow glyph and
    lowercases before matching. Returns ``False`` on empty input so a
    blank pane line never registers as a placeholder.
    """
    text = line.strip().lstrip("›").strip().lower()
    if not text:
        return False
    return any(text.startswith(hint) for hint in CODEX_IDLE_PLACEHOLDERS)


def pane_shows_codex_idle_placeholder(pane_text: str) -> bool:
    """True if any tail line of ``pane_text`` matches a Codex placeholder.

    The placeholder hint sits inside the Codex CLI's input box, so it
    typically lives in one of the last few non-empty lines of the
    pane snapshot. Walking from the tail keeps the check cheap and
    matches what :func:`_snapshot_activity` does in
    :mod:`pollypm.dashboard_data`.
    """
    if not pane_text:
        return False
    for raw_line in reversed(pane_text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        if is_codex_idle_placeholder(line):
            return True
    return False


def pane_shows_claude_empty_prompt(pane_text: str) -> bool:
    """True if the pane shows the Claude CLI's blank input prompt.

    The Claude CLI renders a standalone ``❯`` (U+276F) glyph as the
    input prompt when it's idle waiting for user input — typically the
    last non-chrome line in the snapshot is just ``❯ `` with nothing
    after it (any keybinding hints / "bypass permissions on" boilerplate
    follows below the prompt and is part of the static TUI footer).

    Mirrors the Codex placeholder check (``› <suggestion>``) — both are
    UI chrome that means "the agent is alive and waiting for input",
    NOT "the session is stuck".
    """
    if not pane_text:
        return False
    for raw_line in reversed(pane_text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        # Skip Claude TUI footer lines that survive on every snapshot
        # (the bypass-permissions hint, keybinding cues). They're not
        # the input prompt itself.
        lower = line.lower()
        if (
            "bypass permissions" in lower
            or "shift+tab to cycle" in lower
            or "ctrl+t to" in lower
            or line.startswith("⏵⏵")  # ⏵⏵
        ):
            continue
        # Box-drawing horizontal rules surround the input box; skip
        # them so we don't false-negative on the rule above the prompt.
        if set(line) <= {"─", "━", " "}:  # ─ ━
            continue
        # The empty-prompt line is just ``❯`` (optionally with trailing
        # whitespace). A non-empty body after the arrow means the user
        # is mid-typing — not idle.
        stripped = line.lstrip("❯").strip()
        if line.startswith("❯") and not stripped:
            return True
        # First non-chrome, non-empty line wasn't the prompt — pane has
        # actual content above it; not an idle empty prompt.
        return False
    return False


def pane_shows_no_tasks_available(pane_text: str) -> bool:
    """True if the pane's most recent assistant response was ``No tasks available.``.

    A worker that just ran ``pm task next`` and received the canonical
    "No tasks available." reply IS responding to the heartbeat — it's an
    alive-but-idle worker, NOT silent. The recovery classifier's
    ``SILENT_WORKER`` rung otherwise treats this as a missed-queue-event
    case and escalates to ``alert`` after three cycles (#1084).

    Walks from the tail and returns ``True`` when the most recent
    informative output line (skipping TUI footer chrome and box-drawing
    runs) contains the exact CLI-produced ``No tasks available.``
    substring.
    """
    if not pane_text:
        return False
    if "No tasks available." not in pane_text:
        return False
    for raw_line in reversed(pane_text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        # Skip the same TUI footer chrome the empty-prompt check skips.
        lower = line.lower()
        if (
            "bypass permissions" in lower
            or "shift+tab to cycle" in lower
            or "ctrl+t to" in lower
            or line.startswith("⏵⏵")  # ⏵⏵
        ):
            continue
        # Box-drawing horizontal rules surround the Claude/Codex input
        # box — skip them so we don't false-negative on the divider that
        # sits above the most recent reply.
        if set(line) <= {"─", "━", " "}:  # ─ ━
            continue
        # Codex rotating placeholder hints can also sit at the tail
        # below the actual reply — treat them as chrome here so the
        # detector still finds the "No tasks available." line above.
        if is_codex_idle_placeholder(line):
            continue
        # The Claude empty-prompt glyph ``❯`` followed by no content is
        # also chrome that can sit below the reply.
        if line.startswith("❯") and not line.lstrip("❯").strip():
            continue
        return "No tasks available." in line
    return False


def pane_is_idle_placeholder(pane_text: str) -> bool:
    """True if ``pane_text`` shows a known idle-placeholder UI state.

    Combines the Codex rotating-placeholder check, the Claude
    empty-prompt check, and the worker "No tasks available." idle reply
    (#1084) — all are alive-but-idle agents and should NOT be classified
    as ``unhealthy`` by the session-health classifier (#1010).
    """
    return (
        pane_shows_codex_idle_placeholder(pane_text)
        or pane_shows_claude_empty_prompt(pane_text)
        or pane_shows_no_tasks_available(pane_text)
    )


# Phrase fragments that, in combination with sitting at the empty
# Claude prompt, flag the agent as "awaiting an operator answer."
# Lower-cased substring match against the joined assistant block.
# Conservative list — we want to catch the obvious "ready to proceed?"
# / "shall I?" patterns, not any text that happens to use a question
# mark.
_QUESTION_PATTERNS: tuple[str, ...] = (
    "ready to proceed",
    "should i ",
    "should i?",
    "shall i ",
    "shall i?",
    "would you ",
    "do you want ",
    "anything you want",
    "anything else you ",
    "want me to ",
    "may i ",
    "let me know ",
    "let me know if",
    "ok to proceed",
    "okay to proceed",
    "give the nod",
)


def pane_ends_with_unanswered_question(pane_text: str) -> str | None:
    """Return the agent's question text iff the pane ends awaiting one.

    Detects the exact failure-mode the heartbeat would otherwise miss:
    the worker (or any Claude-CLI session) has just emitted a turn
    that ends in a question to the operator and is now sitting at the
    empty ``❯`` prompt waiting for an answer. ``pm status`` reports
    ``healthy`` (the session is alive and responsive), but no work is
    happening and the operator has no idea anything was asked.

    Returns the joined assistant text (trimmed) when:

    1. The pane tail is the Claude empty-``❯`` prompt — i.e., agent
       finished its turn and is awaiting input.
    2. The most recent assistant block (lines above the prompt, before
       the previous user message) ends in a question pattern: either
       a literal ``?``, or one of the recognized phrase fragments
       (``"ready to proceed"``, ``"should I"``, ``"shall I"``, etc.).

    Returns ``None`` otherwise — including when the agent is mid-turn,
    when the assistant's last block has no question pattern, or when a
    fresh user input line sits between the question and the prompt
    (operator already answered).
    """
    if not pane_text:
        return None
    if not pane_shows_claude_empty_prompt(pane_text):
        return None

    collected: list[str] = []
    for raw_line in reversed(pane_text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        # Skip Claude TUI footer chrome (mirrors pane_shows_claude_empty_prompt).
        if (
            "bypass permissions" in lower
            or "shift+tab to cycle" in lower
            or "ctrl+t to" in lower
            or line.startswith("⏵⏵")  # ⏵⏵
        ):
            continue
        # Skip box-drawing horizontal rules (── ━).
        if set(line) <= {"─", "━", " "}:
            continue
        # Skip the empty prompt line itself.
        if line.startswith("❯") and not line.lstrip("❯").strip():
            continue
        # Skip Claude's status spinner artifacts: ``✻ Cogitated for 1m 34s``,
        # ``✶ Sautéed…``, ``✽ Rendering…`` etc.
        if line.startswith(("✻", "✶", "✽", "·")):
            continue
        # Skip Codex rotating placeholder hints if they sit below the
        # assistant block (rare in Claude panes but defensive).
        if is_codex_idle_placeholder(line):
            continue
        # A user-input line (``❯`` followed by content) means the operator
        # already answered — there is no unanswered question.
        if line.startswith("❯") and line.lstrip("❯").strip():
            return None
        # Reached the assistant block's first line (``⏺ <text>`` marker).
        if line.startswith("⏺"):
            content = line.lstrip("⏺").strip()
            if content:
                collected.append(content)
            break
        # Regular content line within the assistant block.
        collected.append(line)
        # Cap walk distance to keep this O(1) on huge panes.
        if len(collected) >= 40:
            break

    if not collected:
        return None

    # Reverse to forward order; join with single spaces (multi-line
    # paragraphs render as one continuous string for substring match).
    block_text = " ".join(reversed(collected))
    block_lower = block_text.lower()

    has_question_mark = "?" in block_text
    has_pattern = any(pattern in block_lower for pattern in _QUESTION_PATTERNS)
    if not (has_question_mark or has_pattern):
        return None

    # Trim for inbox-display friendliness. Keep the tail since the
    # question typically sits at the end of the block.
    if len(block_text) > 280:
        return "…" + block_text[-279:]
    return block_text


__all__ = [
    "CODEX_IDLE_PLACEHOLDERS",
    "is_codex_idle_placeholder",
    "pane_shows_codex_idle_placeholder",
    "pane_shows_claude_empty_prompt",
    "pane_shows_no_tasks_available",
    "pane_is_idle_placeholder",
    "pane_ends_with_unanswered_question",
]
