"""Structured user-facing message shape + rendering (#760).

Every user-facing message from PollyPM — CLI errors, inbox
notifications, cockpit toasts — should have a consistent four-field
shape so users learn the pattern once and can parse any of them at a
glance:

- **summary**  one sentence in plain English about what happened.
- **why**      one sentence about why the user should care.
- **next**     the concrete action to take (ideally copy-pasteable).
- **details**  technical specifics for debugging, hidden by default.

Rendering helpers render the shape consistently across channels so
the user's mental model travels. CLI gets plain text with section
headers; the inbox detail pane (see #761) gets rich widgets that
consume the same dataclass.

This module is deliberately simple — no humanize-via-agent-model
pass yet (that's the larger phase of #760). Authored-by-hand
structured messages for the top error classes migrate here over
time; the shape is the contract, and every site that adopts it
immediately reads better.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field


_DEFAULT_WRAP = 80


@dataclass(slots=True, frozen=True)
class StructuredUserMessage:
    """A user-facing message in the canonical four-field shape.

    ``summary`` is required — every message needs a one-sentence
    anchor. The other three fields are optional but strongly
    encouraged — a message with only a summary line gives the user no
    context and no next step.
    """

    summary: str
    why: str = ""
    next_action: str = ""
    details: str = ""
    # Reserved for future expansion: a list of (label, command) tuples
    # for messages that offer multiple concrete actions. Not rendered
    # yet; callers that need it populate it so the data is preserved.
    suggested_actions: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    # ---------------------------------------------------------------
    # CLI rendering
    # ---------------------------------------------------------------

    def render_cli(
        self,
        *,
        icon: str = "✗",
        wrap: int = _DEFAULT_WRAP,
        show_details: bool = False,
    ) -> str:
        """Render this message for terminal output.

        Layout:

        ::

            <icon> <summary>

            <why (wrapped)>

            Next: <next_action>

            > details (press d to expand)            [if details + not show_details]
            <details (wrapped)>                       [if details + show_details]

        ``icon`` defaults to ``✗`` (failure); callers rendering info /
        success messages should override. ``wrap`` controls paragraph
        wrap width; pass ``0`` to disable wrapping (useful when the
        caller knows the terminal is wider or when the content itself
        has structure that shouldn't be reflowed, like code blocks).
        """
        lines: list[str] = []
        summary = (self.summary or "").strip()
        if summary:
            lines.append(f"{icon} {summary}" if icon else summary)

        if self.why:
            lines.append("")
            lines.append(_paragraph(self.why, wrap))

        if self.next_action:
            lines.append("")
            lines.append(f"Next: {self.next_action.strip()}")

        if self.details:
            lines.append("")
            if show_details:
                lines.append(_paragraph(self.details, wrap))
            else:
                lines.append("> details (pass --verbose or press d to expand)")

        return "\n".join(lines).rstrip() + "\n"


def _paragraph(text: str, wrap: int) -> str:
    """Wrap a paragraph preserving explicit line breaks.

    Paragraphs that contain ANY indented or bullet-shaped line are
    emitted verbatim — those shapes carry meaning (code blocks,
    lists, structured detail dumps) that a reflow would destroy.
    Everything else gets wrapped at ``wrap`` columns.
    """
    text = (text or "").strip()
    if not text:
        return ""
    if wrap <= 0:
        return text
    paragraphs = text.split("\n\n")
    wrapped = []
    for para in paragraphs:
        # Preserve any block whose lines read as pre-formatted — at
        # least one line starts with whitespace, a bullet glyph, or a
        # common bracket prefix (``[work]`` / ``[state]`` for migration
        # details, etc.).
        if _looks_preformatted(para):
            wrapped.append(para)
            continue
        wrapped.append(textwrap.fill(para.replace("\n", " "), width=wrap))
    return "\n\n".join(wrapped)


def _looks_preformatted(para: str) -> bool:
    for line in para.splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            return True
        if line.lstrip().startswith(("- ", "* ", "• ", "[")):
            return True
    return False


__all__ = ["StructuredUserMessage"]
