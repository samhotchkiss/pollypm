"""Project Plan section for the regular dashboard (#1620).

Surfaces the synthesized plan inline when it exists, with a clear CTA
banner directing the user to the next action. Renders in two shapes:

1. ``plan_text`` is non-empty (architect's synthesize has emitted a
   plan): a "◇ Plan ready" banner + preview block + "press p to view
   full plan" hint. This is the case Sam sees the morning after the
   architect finishes — he opens his project and the plan is right
   there waiting for him.
2. ``plan_text`` is empty: section is suppressed entirely (the regular
   dashboard sections still render below).

The banner mirrors the operator-dashboard plan-ready glyph (◇) so the
vocabulary stays consistent across surfaces (#1572).
"""

from __future__ import annotations

from pathlib import Path

from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _DASHBOARD_DIVIDER_WIDTH,
    _dashboard_divider,
)
from pollypm.cockpit_sections.plan_review import load_plan_text


# Preview line cap — keeps the section compact on the regular
# dashboard. The full plan is reachable via the plan-review surface
# (press ``p``) or by reading the file directly.
_PLAN_PREVIEW_LINE_LIMIT = 24


def _render_plan_ready_banner() -> str:
    """``◇ Plan ready — your turn`` banner, two lines + actions.

    Visual goal: when Sam opens his project dashboard he sees a clear
    "the architect has finished, here's what's next" affordance at the
    top of the Plan section. The banner does NOT replace the
    plan-review surface — when a ``plan_review`` task is actively
    parked at user_approval, the orchestrator already switches to the
    full surface (project_dashboard.py:141). This banner covers the
    case where the plan exists but the inbox row has been archived /
    fast-tracked / never emitted — Sam still gets visibility on the
    plan and a path to act on it.
    """
    return (
        f"{_DASHBOARD_BULLET}[bold green]◇ Plan ready — your turn[/bold green]\n"
        f"{_DASHBOARD_BULLET}[dim]Press [bold]p[/bold] to open the full "
        f"plan-review surface · [bold]A[/bold] to approve · [bold]c[/bold] "
        f"to chat with the PM[/dim]"
    )


def _preview_plan(plan_text: str, *, line_limit: int = _PLAN_PREVIEW_LINE_LIMIT) -> str:
    """Return the first ``line_limit`` lines of the plan, with a tail hint.

    Strips obvious chrome (blank-only leading lines) and appends a
    ``…`` marker when the plan was truncated so the user sees there's
    more to read.
    """
    text = (plan_text or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    preview = lines[:line_limit]
    rendered = "\n".join(
        f"{_DASHBOARD_BULLET}{line}" for line in preview
    )
    if len(lines) > line_limit:
        rendered += (
            f"\n{_DASHBOARD_BULLET}[dim]… {len(lines) - line_limit} more lines "
            f"— press p to view full plan[/dim]"
        )
    return rendered


def _section_plan(project_path: Path) -> list[str]:
    """Plan section for the regular project dashboard.

    Returns the dividers + banner + preview when a plan exists on disk
    (or in a worktree via the #1620 fallback). Returns an empty list
    when there is no plan — callers should not render a divider for an
    empty section.
    """
    plan_text = load_plan_text(project_path)
    if not plan_text:
        return []
    lines: list[str] = [
        _dashboard_divider("Plan"),
        _render_plan_ready_banner(),
        "",
        _preview_plan(plan_text),
        "",
    ]
    return lines
