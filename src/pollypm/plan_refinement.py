"""Chat-to-refine flow — primer + architect revision pass (#1404).

The user lands on the project drilldown plan-review surface (#1401) with
a plan parked at ``user_approval``. They press ``[c]`` to chat-to-refine:
the cockpit opens the project's PM chat thread and seeds the input with
a refinement primer that frames the conversation as in-place plan
revision, not abandonment. After the user discusses tweaks, they emit
the refinement signal (``go architect`` / ``revise the plan`` / etc.);
the architect then runs a revision pass that rewrites the plan body in
place under the SAME task id, with ``plan_version`` bumping via
:meth:`WorkService.increment_plan_version` (#1407 SDK).

Distinct from the deny flow (#1403) — deny CANCELS the current plan task
and creates a successor with a ``predecessor_task_id`` link. Refine
KEEPS the same task; the version counter records the revision history
so plan-history consumers can walk it.

The module is intentionally framework-agnostic: the cockpit UI calls
:func:`build_plan_refinement_primer` to seed the chat, and a downstream
process (the architect's revision turn, kicked off when the user signals
"go architect") calls :func:`apply_plan_refinement` to write the new
plan body and bump the version.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Phrases that, when present in the user's chat message, signal "we're
# done discussing — go run the architect's revision pass on the new
# plan". Match is case-insensitive and substring-based; we accept the
# canonical "go architect" gesture plus a small set of natural-language
# equivalents so the user doesn't have to memorise an exact incantation.
#
# Kept module-level so tests can introspect the list and the cockpit
# input surface (when wired) can render a hint listing the recognised
# phrases.
REFINEMENT_SIGNAL_PHRASES: tuple[str, ...] = (
    "go architect",
    "ship the revision",
    "revise the plan",
    "rewrite the plan",
    "apply the revision",
    "run the revision",
    "send to architect",
)


def is_refinement_signal(text: str | None) -> bool:
    """Return ``True`` when ``text`` carries a "go architect" gesture.

    Case-insensitive substring match against
    :data:`REFINEMENT_SIGNAL_PHRASES`. Returns ``False`` for ``None`` /
    empty input so the caller can pass raw user messages without
    pre-validating.
    """

    if not text:
        return False
    haystack = str(text).lower()
    return any(phrase in haystack for phrase in REFINEMENT_SIGNAL_PHRASES)


def build_plan_refinement_primer(
    *,
    project_key: str,
    plan_task_id: str,
    plan_version: int,
    plan_body: str,
    plan_path: str | None = None,
    reviewer_name: str = "Sam",
) -> str:
    """Build the PM chat primer for the chat-to-refine flow (#1404).

    The primer frames the conversation as IN-PLACE plan revision (same
    task id, version counter bumps), distinct from the deny-and-replan
    flow which spawns a successor task. The architect that picks up the
    refinement signal will rewrite the plan body in place via
    :func:`apply_plan_refinement`.

    The plan body is quoted directly so the PM has the canonical text
    to refer to without re-reading the file from disk; ``plan_path`` is
    appended as a pointer when supplied so the PM can deep-read sections
    that don't fit in the primer (long plans).

    Mirrors the shape of ``_build_plan_review_primer`` (the existing
    inbox-row discuss primer, used by ``[d]``) so both surfaces feel
    consistent: same architect-as-co-refiner framing, same approval
    handoff. The refinement primer ADDS the explicit "this is in-place,
    same task, version will bump" framing, the canonical refinement
    signal phrases, and the quoted plan body.
    """

    person = reviewer_name.strip() or "Sam"
    quoted_body = _quote_plan_body(plan_body)
    plan_path_line = f"Plan file: {plan_path}\n" if plan_path else ""
    signal_examples = ", ".join(f'"{phrase}"' for phrase in REFINEMENT_SIGNAL_PHRASES[:3])
    return (
        f"{person} is here to refine plan {plan_task_id} v{plan_version} "
        f"for project {project_key}.\n"
        f"This is an in-place revision: same task id, plan_version will "
        f"bump to v{plan_version + 1} once the architect ships the rewrite. "
        f"Do NOT create a successor task; the deny flow handles that.\n"
        f"{plan_path_line}"
        "\n"
        "Current plan body (v"
        f"{plan_version}):\n"
        f"{quoted_body}\n"
        "\n"
        "Your job in this conversation:\n"
        f"- Co-refine the plan with {person}; capture concrete tweaks, "
        "not vibes\n"
        "- Push hard on decomposition, magic, and risk decisions when "
        f"{person} flags them\n"
        f"- When {person} signals 'go architect' (or one of: "
        f"{signal_examples}), hand off to the architect for the revision "
        "pass — DO NOT rewrite the plan yourself in this chat\n"
        f"- The architect will rewrite the plan body in place, bump "
        f"plan_version to v{plan_version + 1}, and re-park the task at "
        "user_approval; the cockpit picks up the new version inline\n"
        f"- If {person} pings without a specific tweak, your default "
        "opener is to walk through the riskiest decisions in the current "
        "plan and ask which one to dig into first"
    )


def _quote_plan_body(body: str | None) -> str:
    """Return ``body`` with each line prefixed by ``> `` for chat quoting.

    The ``>`` form is the standard markdown blockquote and renders
    distinctly in every chat surface we use (Codex, Claude). Lines that
    are already blockquoted stay single-prefixed so we don't end up with
    ``> > line``.
    """

    if not body:
        return "> (plan body is empty)"
    lines = []
    for raw in str(body).splitlines() or [str(body)]:
        if raw.startswith(">"):
            lines.append(raw)
        else:
            lines.append(f"> {raw}" if raw else ">")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Architect revision pass — applies a refined plan body to disk + bumps the
# plan_version on the task. Pure I/O at this layer; the architect's actual
# rewrite turn (the LLM call) lives upstream in the planner plugin.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanRefinementResult:
    """Outcome of a chat-to-refine architect revision pass (#1404).

    Carries enough state for the cockpit to show the new version inline
    (the inline-render PR #1397 reads ``plan_version`` off the task
    directly) and for tests to assert the SDK + filesystem side effects
    landed.
    """

    task_id: str
    old_version: int
    new_version: int
    plan_path: Path | None
    bytes_written: int


def apply_plan_refinement(
    work_service,
    *,
    task_id: str,
    new_plan_body: str,
    project_path: Path | None = None,
    actor: str = "architect",
    reason: str | None = None,
) -> PlanRefinementResult:
    """Apply a refined plan body in place and bump ``plan_version``.

    Steps (all best-effort except the version bump, which is the
    canonical revision marker):

    1. Resolve the canonical plan file path under ``project_path`` —
       ``docs/plan/plan.md`` wins over ``docs/project-plan.md`` to match
       the precedence in :data:`CANONICAL_PLAN_RELATIVE_PATHS`. When the
       project has no plan file yet (synthesize hasn't landed), we write
       ``docs/project-plan.md`` since that's where the synthesize stage
       puts it.
    2. Write ``new_plan_body`` to the resolved path. Failure here
       degrades to a logged warning rather than aborting — the version
       bump is the actual contract; the file write is a convenience for
       the inline-render path.
    3. Call :meth:`WorkService.increment_plan_version` to bump the
       version and emit the ``plan.version_incremented`` audit event
       (#1407). The reason defaults to ``"chat-to-refine"`` so audit
       consumers can distinguish refinement bumps from architect
       internal revisions.

    Returns a :class:`PlanRefinementResult` with the old/new version and
    the plan-file metadata so the caller can surface the bump in the UI
    without re-reading the task.
    """

    bump_reason = reason or "chat-to-refine"
    plan_path: Path | None = None
    bytes_written = 0

    if project_path is not None:
        plan_path = _resolve_plan_path(project_path)
        try:
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            data = new_plan_body if new_plan_body.endswith("\n") else new_plan_body + "\n"
            plan_path.write_text(data, encoding="utf-8")
            bytes_written = len(data.encode("utf-8"))
        except OSError:
            # Filesystem failures shouldn't block the version bump — the
            # bump is the canonical revision marker. The file write is a
            # best-effort to keep the on-disk plan in sync with what the
            # task records.
            plan_path = None
            bytes_written = 0

    refreshed = work_service.increment_plan_version(
        task_id, actor=actor, reason=bump_reason,
    )
    new_version = int(getattr(refreshed, "plan_version", 0) or 0)
    old_version = max(new_version - 1, 0)
    return PlanRefinementResult(
        task_id=task_id,
        old_version=old_version,
        new_version=new_version,
        plan_path=plan_path,
        bytes_written=bytes_written,
    )


def select_chat_primer_for_project_dashboard(
    dashboard_data,
    *,
    reviewer_name: str = "Sam",
) -> str | None:
    """Return a refinement primer when the project drilldown is showing a
    pending plan-review surface, else ``None``.

    Inputs: a ``ProjectDashboardData`` (or any duck-typed object exposing
    ``project_key``, ``action_items``, ``task_buckets``, ``plan_text``,
    ``plan_path``). When the dashboard's pending action items include a
    ``plan_review`` row OR the task buckets include a ``review`` task
    parked at ``user_approval``, we treat the surface as a plan-review
    drilldown and return a refinement primer pre-filled with the plan
    body.

    Returns ``None`` when the surface isn't a plan-review (the caller
    falls back to its existing context-line heuristics) so the routing
    is opt-in and degrades gracefully on dashboards without the right
    context.

    Kept here (next to the primer + apply helpers) so the cockpit's
    ``action_chat_pm`` can call into a single module to decide whether
    to route to refine-mode without re-implementing the gating logic.
    Coordinates with the deny flow (#1403): both want a single chat
    primer infra; the deny path will surface its own primer-builder via
    a sibling helper that this routing function NEVER masks (it returns
    ``None`` when no plan_review is pending so deny-only surfaces are
    untouched).
    """

    project_key = getattr(dashboard_data, "project_key", "")
    plan_review_item = _find_plan_review_action_item(dashboard_data)
    if plan_review_item is None:
        return None
    plan_task_id = str(
        plan_review_item.get("plan_task_id")
        or plan_review_item.get("primary_ref")
        or plan_review_item.get("task_id")
        or ""
    )
    if not plan_task_id:
        return None
    plan_version = int(plan_review_item.get("plan_version", 1) or 1)
    plan_body = str(getattr(dashboard_data, "plan_text", "") or "")
    plan_path = getattr(dashboard_data, "plan_path", None)
    plan_path_str = str(plan_path) if plan_path else None
    return build_plan_refinement_primer(
        project_key=project_key,
        plan_task_id=plan_task_id,
        plan_version=plan_version,
        plan_body=plan_body,
        plan_path=plan_path_str,
        reviewer_name=reviewer_name,
    )


def _find_plan_review_action_item(dashboard_data) -> dict | None:
    """Return the dashboard's pending plan_review action item, if any.

    Looks first at ``action_items`` (the action-card row items the
    dashboard renders) for an entry flagged ``is_plan_review`` or with
    ``plan_review`` in labels. Falls back to scanning ``task_buckets``
    for a ``review`` task at the ``user_approval`` node — that's the
    raw work-service signal even when no inbox row has been emitted
    yet.
    """

    action_items = getattr(dashboard_data, "action_items", None) or []
    for item in action_items:
        if not isinstance(item, dict):
            continue
        if item.get("is_plan_review"):
            return item
        labels = item.get("labels") or []
        if isinstance(labels, list) and "plan_review" in labels:
            return item
    buckets = getattr(dashboard_data, "task_buckets", None) or {}
    review_bucket = buckets.get("review", []) if isinstance(buckets, dict) else []
    for entry in review_bucket:
        if not isinstance(entry, dict):
            continue
        if entry.get("current_node_id") == "user_approval":
            return entry
        labels = entry.get("labels") or []
        if isinstance(labels, list) and "plan_review" in labels:
            return entry
    return None


def _resolve_plan_path(project_path: Path) -> Path:
    """Return the canonical plan file path for ``project_path``.

    Precedence:

    1. ``docs/plan/plan.md`` if it exists (planner-plugin convention).
    2. ``docs/project-plan.md`` if it exists (synthesize-stage default).
    3. ``docs/project-plan.md`` as the fallback for fresh projects so
       the architect's revision lands somewhere the inline-render path
       (#1397) will pick up.

    Mirrors :data:`pollypm.plugins_builtin.project_planning.plan_presence.CANONICAL_PLAN_RELATIVE_PATHS`
    so both reads + writes follow the same path resolution.
    """

    plan_dir_plan = project_path / "docs" / "plan" / "plan.md"
    project_plan = project_path / "docs" / "project-plan.md"
    if plan_dir_plan.is_file():
        return plan_dir_plan
    if project_plan.is_file():
        return project_plan
    return project_plan
