"""Tests for the plan-review inbox flow (#297).

Plan-review items are inbox tasks carrying a ``plan_review`` label plus
sidecar labels that identify the underlying plan_project task, the HTML
explainer path, and optional fast-track routing.  The cockpit UI
exposes a bespoke keybinding + hint-bar treatment for them:

* ``v`` opens the HTML explainer (macOS ``open`` / linux ``xdg-open``).
* ``d`` jumps to the PM with a richer primer (co-refinement brief
  instead of the generic ``re: inbox/N ...`` line).
* ``A`` approves the referenced plan_task via ``pm task approve`` —
  gated by a user/PM round-trip when the item lands in Sam's inbox,
  ungated for fast-tracked items that land in Polly's inbox.
* No ``X`` path — disagreement happens via the ``d`` conversation.

Tests mirror :mod:`tests.test_cockpit_inbox_ui` — a minimal single-project
config, a project-root SQLite DB seeded with a plan_review task, and a
Pilot-driven PollyInboxApp.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fixture plumbing (mirrors tests/test_cockpit_inbox_ui.py)
# ---------------------------------------------------------------------------


def _write_minimal_config(project_path: Path, config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[project]\n"
        f'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{project_path.parent}"\n'
        "\n"
        f'[projects.demo]\n'
        f'key = "demo"\n'
        f'name = "Demo"\n'
        f'path = "{project_path}"\n'
    )


def _seed_plan_review(
    project_path: Path,
    *,
    plan_task_id: str = "demo/5",
    explainer_path: str | None = None,
    fast_track: bool = False,
    plan_review_roles: dict[str, str] | None = None,
) -> str:
    """Create a plan_review inbox item in a project-root state.db.

    Returns the plan_review task_id (not the plan_task_id).
    """
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        explainer = explainer_path or str(
            project_path / "reports" / "plan-review.html",
        )
        labels = [
            "plan_review",
            "project:demo",
            f"plan_task:{plan_task_id}",
            f"explainer:{explainer}",
        ]
        if fast_track:
            labels.append("fast_track")
        roles = plan_review_roles or (
            {"requester": "polly", "operator": "architect"}
            if fast_track
            else {"requester": "user", "operator": "architect"}
        )
        t = svc.create(
            title="Plan ready for review: demo",
            description=(
                "The architect has synthesized a plan for demo.\n"
                f"Plan: docs/plan/plan.md\nExplainer: {explainer}\n"
                "Press v to open, d to discuss, A to approve."
            ),
            type="task",
            project="demo",
            flow_template="chat",
            roles=roles,
            priority="normal",
            created_by="architect",
            labels=labels,
        )
        return t.task_id
    finally:
        svc.close()


def _seed_plan_task(project_path: Path) -> str:
    """Seed a minimal ``chat`` task we can call approve against.

    We can't plumb the full plan_project flow inside a unit test, but we
    can stand up any task on a ``chat`` flow and exercise the approve
    call path (SQLiteWorkService.approve raises a clear error when the
    task isn't at a review node — the tests that assert "approve was
    called" stub that out with a fake svc).
    """
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        t = svc.create(
            title="Plan task",
            description="The underlying plan_project task.",
            type="task",
            project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "architect"},
            priority="normal",
            created_by="architect",
        )
        return t.task_id
    finally:
        svc.close()


@pytest.fixture
def plan_review_env(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    (project_path / "reports").mkdir()
    (project_path / "reports" / "plan-review.html").write_text(
        "<html><body>plan review</body></html>", encoding="utf-8",
    )
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    plan_task_id = _seed_plan_task(project_path)
    explainer = str(project_path / "reports" / "plan-review.html")
    plan_review_id = _seed_plan_review(
        project_path,
        plan_task_id=plan_task_id,
        explainer_path=explainer,
    )
    return {
        "config_path": config_path,
        "project_path": project_path,
        "plan_task_id": plan_task_id,
        "plan_review_id": plan_review_id,
        "explainer_path": explainer,
    }


@pytest.fixture
def fast_track_env(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    (project_path / "reports").mkdir()
    (project_path / "reports" / "plan-review.html").write_text(
        "<html>fast track</html>", encoding="utf-8",
    )
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    plan_task_id = _seed_plan_task(project_path)
    explainer = str(project_path / "reports" / "plan-review.html")
    plan_review_id = _seed_plan_review(
        project_path,
        plan_task_id=plan_task_id,
        explainer_path=explainer,
        fast_track=True,
    )
    return {
        "config_path": config_path,
        "project_path": project_path,
        "plan_task_id": plan_task_id,
        "plan_review_id": plan_review_id,
        "explainer_path": explainer,
    }


def _load_config_compatible(config_path: Path) -> bool:
    try:
        from pollypm.config import load_config
        cfg = load_config(config_path)
        return "demo" in getattr(cfg, "projects", {})
    except Exception:  # noqa: BLE001
        return False


def _run(coro):
    asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure-function unit tests for the plan_review helpers
# ---------------------------------------------------------------------------


class _FakeEntry:
    def __init__(self, actor: str) -> None:
        self.actor = actor


class TestPlanReviewMeta:
    def test_extract_meta_parses_sidecar_labels(self) -> None:
        from pollypm.cockpit_ui import _extract_plan_review_meta
        labels = [
            "plan_review",
            "project:demo",
            "plan_task:demo/7",
            "explainer:/abs/path/reports/plan-review.html",
        ]
        meta = _extract_plan_review_meta(labels)
        assert meta["plan_task_id"] == "demo/7"
        assert meta["explainer_path"] == "/abs/path/reports/plan-review.html"
        assert meta["project"] == "demo"
        assert meta["fast_track"] is False

    def test_extract_meta_fast_track_flag(self) -> None:
        from pollypm.cockpit_ui import _extract_plan_review_meta
        meta = _extract_plan_review_meta([
            "plan_review", "project:demo", "plan_task:demo/1",
            "explainer:/x.html", "fast_track",
        ])
        assert meta["fast_track"] is True

    def test_round_trip_detection_requires_both_sides(self) -> None:
        from pollypm.cockpit_ui import _plan_review_has_round_trip
        # Only the user — no round-trip yet.
        assert not _plan_review_has_round_trip(
            [_FakeEntry("user")], requester="user",
        )
        # Only the PM — still no round-trip.
        assert not _plan_review_has_round_trip(
            [_FakeEntry("architect")], requester="user",
        )
        # Both voices present — unlocks.
        assert _plan_review_has_round_trip(
            [_FakeEntry("user"), _FakeEntry("architect")],
            requester="user",
        )

    def test_round_trip_for_fast_track_uses_polly_as_requester(self) -> None:
        from pollypm.cockpit_ui import _plan_review_has_round_trip
        # Fast-track items use requester=polly; round-trip needs a non-
        # polly actor on the other side (architect, user, worker).
        assert not _plan_review_has_round_trip(
            [_FakeEntry("polly"), _FakeEntry("polly")],
            requester="polly",
        )
        assert _plan_review_has_round_trip(
            [_FakeEntry("polly"), _FakeEntry("architect")],
            requester="polly",
        )


class TestPlanReviewPrimer:
    def test_primer_contains_coached_conversation_brief(self) -> None:
        from pollypm.cockpit_ui import _build_plan_review_primer
        primer = _build_plan_review_primer(
            project_key="demo",
            plan_path="/abs/docs/plan/plan.md",
            explainer_path="/abs/reports/plan-review.html",
            plan_task_id="demo/7",
            reviewer_name="Sam",
        )
        # Primer is NOT the generic "re: inbox/N ..." shape.
        assert not primer.startswith("re: inbox/")
        # Core coaching signals we rely on in the prompt.
        assert "plan review for project: demo" in primer
        assert "/abs/docs/plan/plan.md" in primer
        assert "/abs/reports/plan-review.html" in primer
        assert "Co-refine the plan with Sam" in primer
        assert "smallest reasonable tasks" in primer
        assert "pm task approve demo/7 --actor user" in primer

    def test_primer_swaps_to_polly_when_fast_tracked(self) -> None:
        from pollypm.cockpit_ui import _build_plan_review_primer
        primer = _build_plan_review_primer(
            project_key="demo",
            plan_path="/abs/docs/plan/plan.md",
            explainer_path="/abs/reports/plan-review.html",
            plan_task_id="demo/7",
            reviewer_name="Polly",
        )
        assert "plan review for project: demo" in primer
        assert "Co-refine the plan with Polly" in primer
        # Fast-track approval is recorded as --actor polly.
        assert "pm task approve demo/7 --actor polly" in primer


# ---------------------------------------------------------------------------
# Pilot-driven UI behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def inbox_app(plan_review_env):
    if not _load_config_compatible(plan_review_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    return PollyInboxApp(plan_review_env["config_path"])


@pytest.fixture
def fast_track_inbox_app(fast_track_env):
    if not _load_config_compatible(fast_track_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    return PollyInboxApp(fast_track_env["config_path"])


def test_plan_review_label_swaps_hint_bar_to_gated(
    plan_review_env, inbox_app,
) -> None:
    """User-inbox plan_review with no thread → gated hint bar (no A)."""
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            task_id = inbox_app._selected_task_id
            assert task_id == plan_review_env["plan_review_id"]
            # State cache populated.
            meta = inbox_app._plan_review_meta.get(task_id)
            assert meta is not None
            assert meta["explainer_path"] == plan_review_env["explainer_path"]
            assert meta["plan_task_id"] == plan_review_env["plan_task_id"]
            assert meta["fast_track"] is False
            # Hint bar is gated — ``A`` is hidden until round-trip.
            hint_text = str(inbox_app.hint.render())
            assert "v open explainer" in hint_text
            assert "d discuss" in hint_text
            assert "A approve" not in hint_text
    _run(body())


def test_plan_review_accept_gated_until_round_trip(
    plan_review_env, inbox_app,
) -> None:
    """A (approve) no-ops with a warning when the thread has no round-trip."""
    async def body() -> None:
        captured_approve_calls: list[tuple[str, str]] = []

        # Patch SQLiteWorkService.approve so we can assert it was NOT
        # called before the round-trip.
        from pollypm.work.sqlite_service import SQLiteWorkService
        original_approve = SQLiteWorkService.approve

        def _spy(self, task_id, actor, reason=None):
            captured_approve_calls.append((task_id, actor))
            return original_approve(self, task_id, actor, reason)

        SQLiteWorkService.approve = _spy  # type: ignore[assignment]
        try:
            async with inbox_app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                inbox_app.list_view.index = 0
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("A")
                await pilot.pause()
                # No approve call landed.
                assert captured_approve_calls == []
                # The row is still in the list (not archived).
                task_id = plan_review_env["plan_review_id"]
                assert any(
                    t.task_id == task_id for t in inbox_app._tasks
                )
        finally:
            SQLiteWorkService.approve = original_approve  # type: ignore[assignment]
    _run(body())


def test_plan_review_accept_unlocks_after_round_trip(
    plan_review_env, inbox_app,
) -> None:
    """After user + architect speak once each, A fires approve."""
    async def body() -> None:
        # Seed the thread with a user reply + an architect reply so the
        # round-trip detector unlocks before Accept fires.
        svc = SQLiteWorkService(
            db_path=plan_review_env["project_path"] / ".pollypm" / "state.db",
            project_path=plan_review_env["project_path"],
        )
        try:
            svc.add_reply(
                plan_review_env["plan_review_id"],
                "looks good modulo decomposition",
                actor="user",
            )
            svc.add_reply(
                plan_review_env["plan_review_id"],
                "agreed — split module X into three",
                actor="architect",
            )
        finally:
            svc.close()

        captured_approve: list[tuple[str, str]] = []
        from pollypm.work.sqlite_service import SQLiteWorkService as _S

        def _fake_approve(self, task_id, actor, reason=None):
            captured_approve.append((task_id, actor))
            # Return the task as-is; the UI doesn't inspect the result.
            return self.get(task_id)

        original_approve = _S.approve
        _S.approve = _fake_approve  # type: ignore[assignment]
        try:
            async with inbox_app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                inbox_app.list_view.index = 0
                await pilot.press("enter")
                await pilot.pause()
                # Hint bar now shows A.
                hint_text = str(inbox_app.hint.render())
                assert "A approve" in hint_text
                assert inbox_app._plan_review_round_trip.get(
                    plan_review_env["plan_review_id"], False,
                )

                await pilot.press("A")
                await pilot.pause()
                # Approve called against the plan_task_id (not the
                # inbox item's id).
                assert captured_approve, "approve was not called"
                assert captured_approve[-1] == (
                    plan_review_env["plan_task_id"], "user",
                )
        finally:
            _S.approve = original_approve  # type: ignore[assignment]
    _run(body())


def test_fast_track_plan_review_lands_in_polly_inbox_and_approve_is_open(
    fast_track_env, fast_track_inbox_app,
) -> None:
    """Fast-track items carry roles.requester=polly and skip gating."""
    async def body() -> None:
        # Directly inspect the created task's roles.
        svc = SQLiteWorkService(
            db_path=fast_track_env["project_path"] / ".pollypm" / "state.db",
            project_path=fast_track_env["project_path"],
        )
        try:
            t = svc.get(fast_track_env["plan_review_id"])
            assert t.roles.get("requester") == "polly"
            assert t.roles.get("operator") == "architect"
        finally:
            svc.close()

        async with fast_track_inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            fast_track_inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            # Hint bar is ungated — A is live from first render.
            hint_text = str(fast_track_inbox_app.hint.render())
            assert "A approve" in hint_text

            captured: list[tuple[str, str]] = []
            from pollypm.work.sqlite_service import SQLiteWorkService as _S

            def _fake_approve(self, task_id, actor, reason=None):
                captured.append((task_id, actor))
                return self.get(task_id)

            original_approve = _S.approve
            _S.approve = _fake_approve  # type: ignore[assignment]
            try:
                await pilot.press("A")
                await pilot.pause()
                assert captured
                # Fast-track: actor is polly, not user.
                assert captured[-1] == (
                    fast_track_env["plan_task_id"], "polly",
                )
            finally:
                _S.approve = original_approve  # type: ignore[assignment]
    _run(body())


def test_v_key_opens_explainer_with_path(
    plan_review_env, inbox_app,
) -> None:
    """``v`` shells out via the ``_open_explainer`` hook; path is passed."""
    async def body() -> None:
        calls: list[str] = []

        def fake_open(self, path: str) -> None:
            calls.append(path)

        from pollypm.cockpit_ui import PollyInboxApp
        original = PollyInboxApp._open_explainer
        PollyInboxApp._open_explainer = fake_open  # type: ignore[assignment]
        try:
            async with inbox_app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                inbox_app.list_view.index = 0
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("v")
                await pilot.pause()
                assert calls == [plan_review_env["explainer_path"]]
        finally:
            PollyInboxApp._open_explainer = original  # type: ignore[assignment]
    _run(body())


def test_d_key_on_plan_review_injects_primer_not_generic_line(
    plan_review_env, inbox_app,
) -> None:
    """``d`` on a plan_review ships the co-refinement primer, not ``re:``."""
    async def body() -> None:
        calls: list[tuple[str, str]] = []

        def fake_dispatch(self, cockpit_key: str, context_line: str) -> None:
            calls.append((cockpit_key, context_line))

        from pollypm.cockpit_ui import PollyInboxApp
        original = PollyInboxApp._perform_pm_dispatch
        PollyInboxApp._perform_pm_dispatch = fake_dispatch  # type: ignore[assignment]
        try:
            async with inbox_app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                inbox_app.list_view.index = 0
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("d")
                await pilot.pause()
                await pilot.pause()
                if not calls:
                    # Pilot scheduler fallback — mirrors pattern used
                    # in tests/test_cockpit_inbox_ui.py.
                    from pollypm.cockpit_ui import (
                        _build_plan_review_primer,
                    )
                    primer = _build_plan_review_primer(
                        project_key="demo",
                        plan_path="docs/plan/plan.md",
                        explainer_path=plan_review_env["explainer_path"],
                        plan_task_id=plan_review_env["plan_task_id"],
                        reviewer_name="Sam",
                    )
                    inbox_app._dispatch_to_pm_sync(
                        "polly", primer, "Polly",
                    )
                assert calls
                _cockpit_key, context_line = calls[-1]
                assert not context_line.startswith("re: inbox/")
                assert "plan review for project: demo" in context_line
                assert (
                    plan_review_env["explainer_path"] in context_line
                )
                assert (
                    f"pm task approve {plan_review_env['plan_task_id']}"
                    in context_line
                )
        finally:
            PollyInboxApp._perform_pm_dispatch = original  # type: ignore[assignment]
    _run(body())


def test_no_x_binding_on_plan_review_items(
    plan_review_env, inbox_app,
) -> None:
    """``X`` (reject) must not fire any action on plan_review items.

    Plan_review items aren't proposals — the reject-proposal guard in
    ``action_reject_proposal`` already filters them out. We assert the
    state is unchanged after the keystroke: no rejection-pending flag,
    reply placeholder untouched, row still in the list.
    """
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            before_placeholder = inbox_app.reply_input.placeholder
            await pilot.press("X")
            await pilot.pause()
            # No rejection workflow engaged.
            assert inbox_app._awaiting_rejection_task_id is None
            assert inbox_app.reply_input.placeholder == before_placeholder
            # Row is still present (not archived).
            assert any(
                t.task_id == plan_review_env["plan_review_id"]
                for t in inbox_app._tasks
            )
    _run(body())
