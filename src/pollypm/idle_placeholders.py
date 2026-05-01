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


def pane_is_idle_placeholder(pane_text: str) -> bool:
    """True if ``pane_text`` shows a known idle-placeholder UI state.

    Combines the Codex rotating-placeholder check and the Claude
    empty-prompt check — both are alive-but-idle agents and should
    NOT be classified as ``unhealthy`` by the session-health classifier
    (#1010).
    """
    return (
        pane_shows_codex_idle_placeholder(pane_text)
        or pane_shows_claude_empty_prompt(pane_text)
    )


__all__ = [
    "CODEX_IDLE_PLACEHOLDERS",
    "is_codex_idle_placeholder",
    "pane_shows_codex_idle_placeholder",
    "pane_shows_claude_empty_prompt",
    "pane_is_idle_placeholder",
]
