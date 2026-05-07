"""Plan-approval celebration helpers (#1402).

Pure-text utilities used when the user approves a plan_review item from
either the cockpit inbox or a project drilldown. Two responsibilities:

* :func:`compose_celebration_message` — render the persona-driven cooking
  idiom toast string. The copy adapts by sub-task count: a one-task plan
  gets a "quick bite" line, a 12+ plan gets a "big batch" line, and the
  default sits in the middle ("out of the oven"). Persona name is the
  project's PM persona; falls back to ``Polly`` when not configured.

* :func:`sparkle_frames` — yield a short sequence of pure-text sparkle
  strings the cockpit can cycle through for ~1s when the toast appears.
  No emoji. Just unicode geometric stars.

The 10-second undo window length lives here as a single constant so the
inbox + drilldown + tests share the source of truth.
"""

from __future__ import annotations

from typing import Iterable

# Single source of truth for the undo window. The cockpit defers
# ``svc.approve()`` by this many seconds — pressing ``u`` inside the
# window cancels the deferred call and leaves the plan task in review.
APPROVE_UNDO_WINDOW_SECONDS: float = 10.0

# Sparkle frames that animate the toast region for ~1s. Pure text — the
# stars are unicode geometric shapes (U+2726 / U+2727), not emoji.
_SPARKLE_FRAMES: tuple[str, ...] = (
    "✦ ✧",
    "✧ ✦ ✧",
    "✦ ✧ ✦ ✧",
    "✧ ✦ ✧ ✦ ✧",
    "✦ ✧ ✦ ✧",
    "✧ ✦ ✧",
    "✦",
)
SPARKLE_DURATION_SECONDS: float = 1.0


def sparkle_frames() -> tuple[str, ...]:
    """Return the ordered sparkle animation frames (pure text)."""
    return _SPARKLE_FRAMES


def compose_celebration_message(
    *,
    persona_name: str | None,
    sub_task_count: int,
) -> str:
    """Build the persona-driven approval toast string.

    Copy adapts by ``sub_task_count``:

    * ``count <= 1`` → ``Quick bite — N task plated.``
    * ``2 <= count <= 11`` → ``Out of the oven! N tasks plated.``
    * ``count >= 12`` → ``Big batch — N tasks plated.``

    ``persona_name`` is the project's PM persona (e.g. ``Sage``). When
    missing or empty we fall back to ``Polly`` so the toast still has a
    voice. Pure text — no emoji.
    """
    name = (persona_name or "").strip() or "Polly"
    count = max(0, int(sub_task_count or 0))
    if count <= 1:
        body = f"Quick bite — {count} task plated."
    elif count >= 12:
        body = f"Big batch — {count} tasks plated."
    else:
        body = f"Out of the oven! {count} tasks plated."
    return f"{name}: {body}"


def undo_hint(seconds_left: float | None = None) -> str:
    """Render the keybinding hint shown alongside the celebration toast.

    Cockpit surfaces the hint as a secondary line so the user sees both
    the celebration and the recovery option without scanning. When a
    countdown is available we surface the remaining seconds; otherwise
    we render the bare ``[u] undo`` affordance.
    """
    if seconds_left is None:
        return "[u] undo"
    secs = max(0, int(round(float(seconds_left))))
    return f"[u] undo ({secs}s)"


__all__ = [
    "APPROVE_UNDO_WINDOW_SECONDS",
    "SPARKLE_DURATION_SECONDS",
    "compose_celebration_message",
    "sparkle_frames",
    "undo_hint",
]
