"""Project drilldown plan-review surface (#1401).

When a project has a task in ``status=review`` parked at the canonical
``user_approval`` node, the project drilldown shifts from the regular
dashboard to this dedicated plan-review surface. The surface stacks
top-down:

1. Header strip — ``Plan: <project>/<task#> · Architect: <persona> ·
   Generated <age> · v<plan_version>``.
2. ``## Summary`` block (architect synthesis from #1408).
3. ``## Judgment calls`` bulleted list (extracted via #1410's
   ``_extract_plan_judgment_calls``).
4. The rest of the plan body — decomposition, test strategy, critic
   synthesis — rendered inline through #1410's ``_md_to_rich``.
5. Action bar at the bottom with visibly-distinct labeled buttons +
   keybinding hints: ``[a] Approve   [c] Chat to refine   [d] Deny
   [esc] Back``. Approve/deny/chat handlers live in their own PRs
   (#1402/#1411/#1409) — this surface only exposes the labels.

The trigger and the regular dashboard fall-back live in
``project_dashboard._render_project_dashboard``. This module owns the
rendering primitives so the orchestrator stays a thin selector.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pollypm.cockpit_sections.base import (
    _DASHBOARD_BULLET,
    _DASHBOARD_DIVIDER_WIDTH,
    _age_from_dt,
    _dashboard_divider,
    _iso_to_dt,
)


# ---------------------------------------------------------------------------
# Action-bar tokens
# ---------------------------------------------------------------------------
#
# Each label uses Rich-markup colour + bold so the active hotkey reads as
# a button on Textual Static widgets. The plain-text fallback (no markup
# stripping) renders ``[a] Approve  [c] Chat to refine  [d] Deny  [esc]
# Back`` for tests + non-Rich consumers — see ``render_plan_review_action_bar_plain``.

_ACTION_BAR_PLAIN = (
    "[a] Approve   [c] Chat to refine   [d] Deny   [esc] Back"
)

# Header strip colours — keep narrow enough to render inside the
# 72-column dashboard divider. Bold + colour so the eye snaps to it.
_HEADER_STRIP_PREFIX = _DASHBOARD_BULLET


def render_plan_review_action_bar_plain() -> str:
    """Plain-text action bar (no Rich markup).

    Tests and the dashboard fallback path use this — the Rich-markup
    version layers in colour for the live cockpit.
    """
    return _ACTION_BAR_PLAIN


def render_plan_review_action_bar() -> str:
    """Rich-markup action bar — visibly distinct labelled buttons.

    Each ``[key]`` reads as a button (bold + colour) and the action
    label sits next to it in normal weight so the hotkey jumps out.
    """
    parts = [
        "[bold green][a][/bold green] [bold]Approve[/bold]",
        "[bold cyan][c][/bold cyan] Chat to refine",
        "[bold red][d][/bold red] Deny",
        "[bold]\\[esc][/bold] Back",
    ]
    return _DASHBOARD_BULLET + "   ".join(parts)


def find_plan_review_task(tasks: list) -> object | None:
    """Return the first task in ``status=review`` parked at user_approval.

    The drilldown trigger: a project has a plan-review surface when at
    least one task is ``WorkStatus.REVIEW`` AND the canonical
    ``current_node_id == "user_approval"``. Multiple plan-reviews on
    one project pick the most-recently-updated.
    """
    candidates = [
        t for t in tasks or []
        if (getattr(t, "work_status", None) is not None
            and getattr(t.work_status, "value", "") == "review"
            and (getattr(t, "current_node_id", "") or "") == "user_approval")
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda t: _iso_to_dt(getattr(t, "updated_at", None)) or 0,
        reverse=True,
    )
    return candidates[0]


def _plan_task_label_ref(task) -> str | None:
    """Extract ``plan_task:<project>/<n>`` ref from a backstop task's labels."""
    labels = getattr(task, "labels", None) or []
    for label in labels:
        text = str(label or "")
        if text.startswith("plan_task:"):
            ref = text[len("plan_task:"):].strip()
            if "/" in ref:
                return ref
    return None


def _resolve_plan_task_for_backstop(
    backstop_task, all_tasks: list,
) -> object:
    """Return the underlying plan task referenced by a backstop emit row.

    The #1511 backstop creates a ``chat``-flow notify-only stub labeled
    ``plan_review`` + ``plan_task:<project>/<n>``. The drilldown surface
    needs the *real* plan task (the one whose plan body the user wants
    to read) so the header strip names the right task number / persona
    and the surface reads like the canonical #1399 reflection emit.

    Falls back to the backstop task itself when the referenced plan
    task can't be located (so the surface still renders something the
    user can act on rather than disappearing).
    """
    ref = _plan_task_label_ref(backstop_task)
    if not ref:
        return backstop_task
    for t in all_tasks or []:
        if getattr(t, "task_id", None) == ref:
            return t
    return backstop_task


def find_actionable_plan_review_task(tasks: list) -> object | None:
    """Return the task that should drive the plan-review surface, or None.

    #1531 — broader matcher than :func:`find_plan_review_task`. Used by
    the drilldown render so projects whose plan-review row was emitted
    by the #1511 watchdog backstop (chat-flow ``done`` stub, NOT
    ``plan_project`` flow at ``user_approval``) still surface the
    review UI. The earlier matcher only matched the canonical
    ``plan_project`` reflection node and left every backstop-covered
    project structurally unreachable from the dashboard.

    Resolution order:

    1. The canonical match (review status + user_approval node).
       Returned as-is — that's the architect's reflection emit.
    2. A task carrying ``plan_review`` + ``plan_task:<id>`` labels
       whose work_status is not archived. We resolve to the *referenced*
       plan task (so the surface header / persona / age read as the
       plan, not the backstop notify stub), falling back to the
       backstop task itself when the ref can't be located.

    Returns ``None`` when no plan-review-shaped task is present.
    """
    canonical = find_plan_review_task(tasks)
    if canonical is not None:
        return canonical
    # Backstop fallback: any task carrying both ``plan_review`` and a
    # ``plan_task:<id>`` ref. Sort by updated_at so multiple stale
    # stubs pick the freshest one.
    #
    # Note: ``archive_task`` flips a chat-flow row to ``done`` (no
    # distinct archived status exists), so we can't filter dismissed
    # rows by status alone. The cockpit inbox already filters
    # already-handled plan_review messages via #1103's per-render
    # phantom sweep; relying on that keeps the matcher narrow.
    candidates: list = []
    for t in tasks or []:
        labels = {str(lbl or "") for lbl in (getattr(t, "labels", None) or [])}
        if "plan_review" not in labels:
            continue
        if not any(lbl.startswith("plan_task:") for lbl in labels):
            continue
        # Skip cancelled rows — those were explicitly dismissed.
        status = getattr(t, "work_status", None)
        status_value = getattr(status, "value", status)
        if status_value == "cancelled":
            continue
        candidates.append(t)
    if not candidates:
        return None
    candidates.sort(
        key=lambda t: _iso_to_dt(getattr(t, "updated_at", None)) or 0,
        reverse=True,
    )
    backstop = candidates[0]
    return _resolve_plan_task_for_backstop(backstop, tasks)


def _resolve_architect_persona(task) -> str:
    """Pull the architect persona from the task's roles → assignee → fallback."""
    roles = getattr(task, "roles", None) or {}
    if isinstance(roles, dict):
        for key in ("architect", "planner", "worker"):
            value = roles.get(key)
            if value:
                return str(value)
    assignee = getattr(task, "assignee", None)
    if assignee:
        return str(assignee)
    return "architect"


def _plan_generated_age(task) -> str:
    """Age of the plan based on ``updated_at`` falling back to ``created_at``."""
    for attr in ("updated_at", "created_at"):
        ts = getattr(task, attr, None)
        dt = _iso_to_dt(ts)
        if dt is not None:
            return _age_from_dt(dt) or "just now"
    return "unknown"


def render_plan_review_header_strip(
    *,
    project_key: str,
    task,
) -> str:
    """``Plan: project/task# · Architect: persona · Generated age · v<n>``."""
    task_number = getattr(task, "task_number", None) or "?"
    persona = _resolve_architect_persona(task)
    age = _plan_generated_age(task)
    version = int(getattr(task, "plan_version", 1) or 1)
    return (
        f"{_HEADER_STRIP_PREFIX}"
        f"Plan: {project_key}/{task_number}"
        f" · Architect: {persona}"
        f" · Generated {age}"
        f" · v{version}"
    )


def _strip_known_sections(plan_text: str) -> str:
    """Drop the leading ``## Summary`` + ``## Judgment calls`` blocks.

    The summary + judgment-call blocks render in their own dedicated
    sections. Stripping them from the body keeps the rest (decomposition,
    test strategy, critic synthesis) free of duplicate content.
    """
    text = (plan_text or "").strip()
    if not text:
        return ""
    keep_lines: list[str] = []
    skip = False
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped in {
            "## summary", "# summary", "### summary",
            "## judgment calls", "## judgement calls",
            "### judgment calls", "### judgement calls",
        }:
            skip = True
            continue
        if skip and stripped.startswith("#"):
            skip = False
        if skip:
            continue
        keep_lines.append(line)
    return "\n".join(keep_lines).strip()


def render_plan_review_surface(
    *,
    project_key: str,
    project_name: str,
    task,
    plan_text: str,
) -> str:
    """Render the full plan-review surface (header + body + action bar).

    All five sections always render — when the plan body is empty we
    still emit the header strip + action bar so the user has somewhere
    to act from. Inline markdown rendering uses the cockpit_ui
    ``_md_to_rich`` helper (lazy-imported to avoid a circular dep with
    the cockpit module on import).
    """
    # Lazy import — `_md_to_rich` lives in ``cockpit_ui`` which already
    # imports from ``cockpit_sections``; importing it at module level
    # creates a circular dependency.
    from pollypm.cockpit_ui import (
        _extract_plan_judgment_calls,
        _extract_plan_summary_block,
        _md_to_rich,
    )

    summary = _extract_plan_summary_block(plan_text or "")
    judgments = _extract_plan_judgment_calls(plan_text or "")
    body_rest = _strip_known_sections(plan_text or "")

    out: list[str] = []
    # 1) Header strip + divider.
    out.append(render_plan_review_header_strip(
        project_key=project_key, task=task,
    ))
    out.append(_DASHBOARD_BULLET + "─" * (_DASHBOARD_DIVIDER_WIDTH - 2))
    out.append("")

    # 2) Summary section.
    out.append(_dashboard_divider("Summary"))
    if summary:
        out.append(_DASHBOARD_BULLET + summary)
    else:
        out.append(_DASHBOARD_BULLET + "(no summary block in plan)")
    out.append("")

    # 3) Judgment calls section.
    out.append(_dashboard_divider("Judgment calls"))
    if judgments:
        for point in judgments:
            out.append(_DASHBOARD_BULLET + "• " + point)
    else:
        out.append(_DASHBOARD_BULLET + "(no judgment calls flagged)")
    out.append("")

    # 4) Plan body.
    out.append(_dashboard_divider("Plan body"))
    if body_rest:
        rendered = _md_to_rich(body_rest)
        for line in rendered.splitlines():
            out.append(_DASHBOARD_BULLET + line)
    else:
        out.append(_DASHBOARD_BULLET + "(plan body is empty)")
    out.append("")

    # 5) Action bar.
    out.append(_dashboard_divider("Actions"))
    out.append(render_plan_review_action_bar())
    out.append(_DASHBOARD_BULLET + "(plain) " + render_plan_review_action_bar_plain())

    # Suppress unused warning — project_name is part of the API for
    # callers that want a richer header in future rev. Keeping the
    # parameter avoids churn when #1402 lands.
    _ = project_name
    return "\n".join(out)


def load_plan_text(project_path: Path) -> str:
    """Read the canonical plan markdown if it exists; '' otherwise."""
    # Lazy import to avoid circular dep with cockpit_ui on package import.
    from pollypm.cockpit_ui import _dashboard_plan_path

    plan_path = _dashboard_plan_path(project_path)
    if plan_path is None:
        return ""
    try:
        return plan_path.read_text(encoding="utf-8")
    except OSError:
        return ""
