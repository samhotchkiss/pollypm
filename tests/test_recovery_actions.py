"""Tests for the ``recovery_action_for`` dispatch (#1016).

The fix's UX promise is that anywhere a stuck task shows up, the
operator sees a concrete next step. These tests pin the dispatch
table's behaviour for each known reason prefix and the generic
fall-through, plus the rendered shape so renderer integrations can
rely on stable output.
"""

from __future__ import annotations

from pollypm.recovery_actions import (
    RecoveryAction,
    recovery_action_for,
    render_recovery_action_block,
    render_recovery_action_summary,
)


def _proxy(
    *,
    task_id: str = "demo/1",
    status: str = "on_hold",
    reason: str = "",
    blocked_by: list[str] | None = None,
) -> dict:
    """Build a minimal task-shaped dict the helper accepts."""
    proxy: dict = {
        "task_id": task_id,
        "task_number": int(task_id.split("/", 1)[1]) if "/" in task_id else 1,
        "work_status": status,
        "reason": reason,
    }
    if blocked_by is not None:
        proxy["blocked_by"] = blocked_by
    return proxy


# ---------------------------------------------------------------------------
# Bikepath/8 canonical case from the issue body.
# ---------------------------------------------------------------------------


def test_bikepath_dirty_root_produces_commit_then_retry_steps() -> None:
    """The exact reason text from the issue body must produce the
    exact recovery shape the issue body shows: ``git -C <path> add
    .gitignore docs/ issues/`` + a commit + ``pm task approve
    bikepath/8 --retry``.
    """
    proxy = _proxy(
        task_id="bikepath/8",
        status="on_hold",
        reason=(
            "Waiting on operator: code review passed, but pm task "
            "approve cannot auto-merge because project root "
            "/Users/sam/dev/bikepath has unrelated uncommitted "
            ".gitignore, docs/, and issues/."
        ),
    )

    action = recovery_action_for(proxy)

    assert action is not None
    assert "uncommitted project root" in action.title
    assert "bikepath/8" in action.title
    assert action.cli_steps == [
        "git -C /Users/sam/dev/bikepath add .gitignore docs/ issues/",
        'git -C /Users/sam/dev/bikepath commit -m "Project root setup"',
        "pm task approve bikepath/8 --retry",
    ]
    assert action.keybinding == "R"


def test_bikepath_block_renders_with_press_r_hint() -> None:
    proxy = _proxy(
        task_id="bikepath/8",
        status="on_hold",
        reason=(
            "Waiting on operator: code review passed, but pm task "
            "approve cannot auto-merge because project root "
            "/Users/sam/dev/bikepath has unrelated uncommitted "
            ".gitignore, docs/, and issues/."
        ),
    )

    action = recovery_action_for(proxy)
    assert action is not None
    block = "\n".join(render_recovery_action_block(action))

    assert "Recovery action for bikepath/8" in block
    assert "$ git -C /Users/sam/dev/bikepath add" in block
    assert "$ pm task approve bikepath/8 --retry" in block
    assert "[press R to do all of this]" in block


# ---------------------------------------------------------------------------
# Each dispatch prefix the issue body specifies.
# ---------------------------------------------------------------------------


def test_dirty_root_generic_when_path_missing() -> None:
    """The reason mentions the project root being dirty but not the
    specific path. Falls through to the generic dirty-root affordance,
    which is still actionable (``git status``, then commit/stash, then
    retry approve)."""
    proxy = _proxy(
        status="on_hold",
        reason="paused: project root has uncommitted changes; please retry.",
    )

    action = recovery_action_for(proxy)

    assert action is not None
    assert "uncommitted project root" in action.title
    joined = " ".join(action.cli_steps)
    assert "git status" in joined
    assert "pm task approve demo/1 --retry" in joined


def test_auto_merge_refused_routes_to_retry_action() -> None:
    """When the reason mentions ``cannot auto-merge`` WITHOUT a
    project-root-dirty marker, we surface the underlying merge cause
    and a ``pm task approve --retry`` step."""
    proxy = _proxy(
        status="on_hold",
        reason=(
            "paused: review passed but auto-merge refused: branch "
            "diverged from main"
        ),
    )

    action = recovery_action_for(proxy)

    assert action is not None
    assert "auto-merge refused" in action.title
    assert any("--retry" in step for step in action.cli_steps)


def test_human_needed_hold_routes_to_operator_input_action() -> None:
    proxy = _proxy(
        status="on_hold",
        reason="[human-needed] external credentials only S.E. can supply",
    )

    action = recovery_action_for(proxy)

    assert action is not None
    assert "operator input" in action.title
    assert "external credentials only S.E. can supply" in action.detail
    assert action.cli_steps[-1] == "pm task resume demo/1"


def test_blocked_dep_routes_to_unblock_dep_first() -> None:
    proxy = _proxy(
        status="blocked",
        reason="blocked: waiting on demo/2",
    )

    action = recovery_action_for(proxy)

    assert action is not None
    assert "blocked on demo/2" in action.title
    assert any("pm task get demo/2" in step for step in action.cli_steps)


def test_operator_decision_routes_to_inbox_resume() -> None:
    proxy = _proxy(
        status="on_hold",
        reason=(
            "on_hold: waiting on operator decision: should we revert "
            "the storage migration?"
        ),
    )

    action = recovery_action_for(proxy)

    assert action is not None
    assert "operator decision" in action.title
    assert "revert the storage migration" in action.detail
    assert any(step.startswith("pm task resume") for step in action.cli_steps)


def test_permission_prompt_routes_to_worker_pane() -> None:
    proxy = _proxy(
        status="on_hold",
        reason="on_hold: permission prompt is open",
    )

    action = recovery_action_for(proxy)

    assert action is not None
    assert "permission prompt" in action.title
    assert any("pm task get" in step for step in action.cli_steps)


def test_unknown_reason_falls_through_to_generic_action() -> None:
    """An unknown reason still gets a recovery action — the generic
    "open the task to investigate" affordance. The fix's UX promise
    is that EVERY stuck task surface shows a next step, even when the
    dispatch table doesn't recognise the reason."""
    proxy = _proxy(
        status="on_hold",
        reason="something the dispatch table has never seen before",
    )

    action = recovery_action_for(proxy)

    assert action is not None
    assert "investigate" in action.title
    assert any("pm task get demo/1" in step for step in action.cli_steps)


def test_healthy_task_returns_none() -> None:
    """Tasks that aren't stuck (in-progress, queued, review) get no
    recovery action — they're not the bug's surface area."""
    for status in ("in_progress", "queued", "review", "done"):
        proxy = _proxy(status=status, reason="any reason here")
        assert recovery_action_for(proxy) is None, status


def test_stuck_task_with_empty_reason_still_gets_generic_action() -> None:
    """A stuck task with no recorded reason (rare, but possible after
    a manual ``pm task hold`` without ``--reason``) must still produce
    a recovery action. The generic affordance points the operator at
    the task's detail view."""
    proxy = _proxy(status="on_hold", reason="")

    action = recovery_action_for(proxy)

    assert action is not None
    assert "investigate" in action.title


# ---------------------------------------------------------------------------
# Renderer helpers.
# ---------------------------------------------------------------------------


def test_render_block_lays_out_title_detail_steps_and_keybinding() -> None:
    action = RecoveryAction(
        title="Recovery action for foo/1 — test",
        detail="A short detail line.",
        cli_steps=["git status", "# a comment", "pm task get foo/1"],
        keybinding="R",
    )

    lines = render_recovery_action_block(action)

    assert lines[0].startswith("◆ ")
    assert "test" in lines[0]
    # Detail under the title.
    assert "A short detail line." in lines[1]
    # CLI steps prefixed with "$ "; comments without.
    body = "\n".join(lines)
    assert "$ git status" in body
    assert "  # a comment" in body
    assert "$ pm task get foo/1" in body
    assert "[press R to do all of this]" in lines[-1]


def test_render_summary_drops_recovery_preamble_for_inline_use() -> None:
    """The Tasks pane row uses the summary form — one line — and
    drops the ``Recovery action for #N — `` preamble because the row
    already names the task. The keybinding hint stays inline."""
    action = RecoveryAction(
        title="Recovery action for foo/1 — uncommitted project root",
        detail="ignored",
        cli_steps=[],
        keybinding="R",
    )

    summary = render_recovery_action_summary(action)

    assert "Recovery action" not in summary
    assert "uncommitted project root" in summary
    assert "(press R)" in summary


# ---------------------------------------------------------------------------
# Hydrated-task path (transitions instead of inline ``reason`` field).
# ---------------------------------------------------------------------------


def test_recovery_reads_reason_from_last_on_hold_transition() -> None:
    """Hydrated ``Task`` objects don't expose ``reason`` directly;
    the helper falls back to scanning ``transitions`` for the most
    recent ``on_hold`` entry. This pins that read so the rendering
    layer can pass a Task in directly without flattening fields."""

    class FakeStatus:
        value = "on_hold"

    class FakeTransition:
        def __init__(self, to_state: str, reason: str) -> None:
            self.to_state = to_state
            self.reason = reason

    class FakeTask:
        task_id = "demo/9"
        task_number = 9
        work_status = FakeStatus()
        transitions = [
            FakeTransition("in_progress", "first start"),
            FakeTransition("on_hold", "blocked: waiting on demo/8"),
        ]

    action = recovery_action_for(FakeTask())
    assert action is not None
    assert "demo/8" in action.title
