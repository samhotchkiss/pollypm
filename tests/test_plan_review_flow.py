"""Tests for the plan-review inbox flow (#297).

Plan-review items are inbox tasks carrying a ``plan_review`` label plus
sidecar labels that identify the underlying plan_project task, the HTML
explainer path, and optional fast-track routing.  The cockpit UI
exposes a bespoke keybinding + hint-bar treatment for them:

* ``v`` opens the HTML explainer (macOS ``open`` / linux ``xdg-open``).
* ``d`` jumps to the PM with a richer primer (co-refinement brief
  instead of the generic ``re: inbox/N ...`` line).
* ``A`` approves the referenced plan_task through the work service —
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

import pytest

from pollypm.store import SQLAlchemyStore
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


def _seed_plan_review_message(
    project_path: Path,
    *,
    plan_task_id: str,
    body: str | None = None,
    explainer_path: str | None = None,
) -> str:
    """Create a Store-backed plan_review notification row."""
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    labels = ["plan_review", "project:demo", f"plan_task:{plan_task_id}"]
    if explainer_path:
        labels.append(f"explainer:{explainer_path}")
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        message_id = store.enqueue_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="architect",
            subject="Plan ready for review: demo",
            body=body or (
                "Plan: docs/project-plan.md\n\n"
                "Press v to open the explainer (unavailable), "
                "d to discuss with the PM, A to approve."
            ),
            scope="demo",
            labels=labels,
        )
    finally:
        store.close()
    return f"msg:demo:{message_id}"


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
def plan_review_message_env(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    plan_task_id = _seed_plan_task(project_path)
    message_id = _seed_plan_review_message(
        project_path,
        plan_task_id=plan_task_id,
    )
    return {
        "config_path": config_path,
        "project_path": project_path,
        "plan_task_id": plan_task_id,
        "message_id": message_id,
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
        assert "record approval for plan task demo/7 as user" in primer
        assert "pm task approve" not in primer

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
        assert "record approval for plan task demo/7 as polly" in primer
        assert "pm task approve" not in primer


# ---------------------------------------------------------------------------
# Pilot-driven UI behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def inbox_app(plan_review_env):
    if not _load_config_compatible(plan_review_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(plan_review_env["config_path"])
    # #1402: existing tests assert ``svc.approve`` fires synchronously
    # on the ``A`` press. Disable the 10s undo window so they keep
    # passing without timer awaits. The dedicated #1402 tests below
    # exercise the deferred path explicitly.
    app._approve_undo_window_seconds = 0.0
    return app


@pytest.fixture
def plan_review_message_app(plan_review_message_env):
    if not _load_config_compatible(plan_review_message_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(plan_review_message_env["config_path"])
    app._approve_undo_window_seconds = 0.0
    return app


@pytest.fixture
def fast_track_inbox_app(fast_track_env):
    if not _load_config_compatible(fast_track_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(fast_track_env["config_path"])
    app._approve_undo_window_seconds = 0.0
    return app


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
            # ``v open explainer`` was removed in #1405 (broken keybinding).
            hint_text = str(inbox_app.hint.render())
            assert "v open explainer" not in hint_text
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


def test_plan_review_message_uses_plan_review_controls(
    plan_review_message_env, plan_review_message_app,
) -> None:
    """Store-backed plan_review notifications should not render read-only."""
    async def body() -> None:
        async with plan_review_message_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            task_id = plan_review_message_env["message_id"]
            plan_review_message_app._selected_task_id = task_id
            plan_review_message_app._render_detail(task_id)
            await pilot.pause()

            detail_text = str(plan_review_message_app.detail.render())
            hint_text = str(plan_review_message_app.hint.render())

            # #1397: the file-pointer body has been replaced. When no
            # plan markdown is on disk, we surface a fallback rather
            # than the legacy "Press v" / explainer-unavailable copy.
            assert "Press v to open the explainer" not in detail_text
            assert "No visual explainer is available for this plan" not in detail_text
            assert (
                "Plan content is not available on disk yet" in detail_text
                or "## " in detail_text
            )
            assert "v open explainer" not in hint_text
            assert "d discuss" in hint_text
            assert "A approve" in hint_text
            assert "Notifications are read-only" not in (
                plan_review_message_app.reply_input.placeholder or ""
            )
            assert "press d to discuss" in (
                plan_review_message_app.reply_input.placeholder or ""
            )

    _run(body())


def test_plan_review_message_approve_archives_notification(
    plan_review_message_env, plan_review_message_app,
) -> None:
    """A on a Store-backed plan_review approves the plan task and closes it."""
    async def body() -> None:
        captured: list[tuple[str, str]] = []
        from pollypm.work.sqlite_service import SQLiteWorkService as _S

        def _fake_approve(self, task_id, actor, reason=None):
            captured.append((task_id, actor))
            return self.get(task_id)

        original_approve = _S.approve
        _S.approve = _fake_approve  # type: ignore[assignment]
        try:
            async with plan_review_message_app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                task_id = plan_review_message_env["message_id"]
                plan_review_message_app._selected_task_id = task_id
                plan_review_message_app._render_detail(task_id)
                await pilot.press("A")
                await pilot.pause()

                assert captured == [
                    (plan_review_message_env["plan_task_id"], "user"),
                ]
                assert all(
                    item.task_id != task_id for item in plan_review_message_app._tasks
                )
        finally:
            _S.approve = original_approve  # type: ignore[assignment]

        db_path = plan_review_message_env["project_path"] / ".pollypm" / "state.db"
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            assert store.query_messages(state="open") == []
            closed = store.query_messages(state="closed")
        finally:
            store.close()
        assert len(closed) == 1

    _run(body())


def test_v_key_is_unbound_after_1405(
    plan_review_env, inbox_app,
) -> None:
    """``v`` no longer triggers the explainer hook (binding removed in #1405)."""
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
                assert calls == [], (
                    "v keybinding was removed in #1405 "
                    "(broken visual explainer, deferred to v1+)"
                )
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
                    "record approval for plan task "
                    f"{plan_review_env['plan_task_id']}" in context_line
                )
                assert "pm task approve" not in context_line
        finally:
            PollyInboxApp._perform_pm_dispatch = original  # type: ignore[assignment]
    _run(body())


def test_d_key_on_plan_review_records_discussion_unlock(
    plan_review_env, inbox_app,
) -> None:
    """A successful PM dispatch unlocks approval for the same plan row."""
    async def body() -> None:
        calls: list[tuple[str, str]] = []
        captured_approve: list[tuple[str, str]] = []

        from pollypm.cockpit_ui import (
            PollyInboxApp,
            _PLAN_REVIEW_DISCUSSION_ENTRY_TYPE,
        )
        from pollypm.work.sqlite_service import SQLiteWorkService as _S

        def fake_dispatch(self, cockpit_key: str, context_line: str) -> None:
            calls.append((cockpit_key, context_line))

        def fake_approve(self, task_id, actor, reason=None):
            captured_approve.append((task_id, actor))
            return self.get(task_id)

        original_dispatch = PollyInboxApp._perform_pm_dispatch
        original_approve = _S.approve
        PollyInboxApp._perform_pm_dispatch = fake_dispatch  # type: ignore[assignment]
        _S.approve = fake_approve  # type: ignore[assignment]
        try:
            async with inbox_app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                inbox_app.list_view.index = 0
                await pilot.press("enter")
                await pilot.pause()

                task_id = plan_review_env["plan_review_id"]
                assert not inbox_app._plan_review_round_trip.get(task_id, False)

                await pilot.press("d")
                for _ in range(10):
                    await pilot.pause(0.1)
                    if inbox_app._plan_review_round_trip.get(task_id, False):
                        break

                assert calls
                assert inbox_app._plan_review_round_trip.get(task_id, False)

                svc = _S(
                    db_path=plan_review_env["project_path"]
                    / ".pollypm"
                    / "state.db",
                    project_path=plan_review_env["project_path"],
                )
                try:
                    markers = svc.get_context(
                        task_id,
                        entry_type=_PLAN_REVIEW_DISCUSSION_ENTRY_TYPE,
                    )
                finally:
                    svc.close()
                assert len(markers) == 1

                await pilot.press("A")
                await pilot.pause()

                assert captured_approve[-1] == (
                    plan_review_env["plan_task_id"],
                    "user",
                )
        finally:
            PollyInboxApp._perform_pm_dispatch = original_dispatch  # type: ignore[assignment]
            _S.approve = original_approve  # type: ignore[assignment]
    _run(body())


def test_plan_review_accept_rechecks_persisted_discussion_marker(
    plan_review_env, inbox_app,
) -> None:
    """A stale detail cache must not keep a discussed plan_review gated."""
    async def body() -> None:
        captured_approve: list[tuple[str, str]] = []

        from pollypm.cockpit_ui import _PLAN_REVIEW_DISCUSSION_ENTRY_TYPE
        from pollypm.work.sqlite_service import SQLiteWorkService as _S

        def fake_approve(self, task_id, actor, reason=None):
            captured_approve.append((task_id, actor))
            return self.get(task_id)

        original_approve = _S.approve
        _S.approve = fake_approve  # type: ignore[assignment]
        try:
            async with inbox_app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                inbox_app.list_view.index = 0
                await pilot.press("enter")
                await pilot.pause()

                task_id = plan_review_env["plan_review_id"]
                assert not inbox_app._plan_review_round_trip.get(task_id, False)

                svc = _S(
                    db_path=plan_review_env["project_path"]
                    / ".pollypm"
                    / "state.db",
                    project_path=plan_review_env["project_path"],
                )
                try:
                    svc.add_context(
                        task_id,
                        actor="user",
                        text="Plan review routed to PM discussion.",
                        entry_type=_PLAN_REVIEW_DISCUSSION_ENTRY_TYPE,
                    )
                finally:
                    svc.close()

                await pilot.press("A")
                await pilot.pause()

                assert captured_approve[-1] == (
                    plan_review_env["plan_task_id"],
                    "user",
                )
        finally:
            _S.approve = original_approve  # type: ignore[assignment]
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


# ---------------------------------------------------------------------------
# #1397 — render plan content inline in the cockpit
# ---------------------------------------------------------------------------


class TestPlanInlineRendering:
    """Helpers that turn a plan body into structured preview blocks."""

    def test_summary_block_extracts_paragraph_under_summary_header(self) -> None:
        from pollypm.cockpit_ui import _extract_plan_summary_block
        text = (
            "# Project plan\n\n"
            "## Summary\n"
            "Ship the rendering pipeline in three steps; each step lands\n"
            "as its own task so we can pause between phases.\n\n"
            "## Judgment calls\n- foo\n"
        )
        summary = _extract_plan_summary_block(text)
        assert summary.startswith("Ship the rendering pipeline")
        assert "three steps" in summary
        assert "foo" not in summary  # stops at the next header

    def test_summary_block_falls_back_to_first_paragraph(self) -> None:
        from pollypm.cockpit_ui import _extract_plan_summary_block
        text = (
            "# Old-style plan\n\n"
            "This is the leading paragraph that doubles as a summary.\n\n"
            "## Some other header\nThe rest.\n"
        )
        summary = _extract_plan_summary_block(text)
        assert summary.startswith("This is the leading paragraph")

    def test_judgment_calls_extracts_bullet_list(self) -> None:
        from pollypm.cockpit_ui import _extract_plan_judgment_calls
        text = (
            "## Summary\nA paragraph.\n\n"
            "## Judgment calls\n"
            "- Whether to ship the rename in a single PR or split it\n"
            "- The cache eviction strategy\n"
            "- Stamping plan_version on every transition or only on user_approval\n\n"
            "## Plan body\nActual plan...\n"
        )
        points = _extract_plan_judgment_calls(text)
        assert len(points) == 3
        assert points[0].startswith("Whether to ship the rename")
        assert "cache eviction" in points[1]

    def test_judgment_calls_returns_empty_when_section_missing(self) -> None:
        from pollypm.cockpit_ui import _extract_plan_judgment_calls
        assert _extract_plan_judgment_calls("# A plan\n\nNo judgment section.") == []

    def test_action_card_renders_judgment_calls_for_plan_review(self) -> None:
        """The Action Needed card on the project drilldown surfaces
        the architect's flagged judgment-call points right under the
        summary so the user knows what to weigh in on."""
        from pollypm.cockpit_ui import PollyProjectDashboardApp
        # Just exercise the pure render helper — no Pilot needed.
        app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
        item = {
            "is_plan_review": True,
            "plain_prompt": "A short plan summary appears here.",
            "judgment_calls": [
                "Whether to ship in one PR or split it.",
                "The cache eviction strategy.",
            ],
            "unblock_steps": ["Open the plan review surface."],
            "decision_question": "Is this ready to become tasks?",
        }
        body = app._render_action_card_body(item, compact=False)
        assert "Flagged judgment calls" in body
        assert "Whether to ship in one PR" in body
        assert "cache eviction" in body
        assert "A short plan summary appears here" in body

    def test_action_card_caps_long_summary_at_80_chars(self) -> None:
        from pollypm.cockpit_ui import PollyProjectDashboardApp
        app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
        long_summary = "x" * 200
        item = {
            "is_plan_review": True,
            "plain_prompt": long_summary,
            "judgment_calls": [],
            "unblock_steps": [],
            "decision_question": "Ready?",
        }
        body = app._render_action_card_body(item, compact=False)
        # Summary is truncated with an ellipsis, so the rendered ``x``
        # run is well under 200 chars.
        assert "x" * 100 not in body
        assert "..." in body

    def test_action_card_omits_judgment_calls_when_not_plan_review(self) -> None:
        from pollypm.cockpit_ui import PollyProjectDashboardApp
        app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
        item = {
            "plain_prompt": "Generic message.",
            "unblock_steps": ["Step 1.", "Step 2."],
            "decision_question": "Choose.",
        }
        body = app._render_action_card_body(item, compact=False)
        assert "Flagged judgment calls" not in body


def test_plan_review_message_detail_renders_plan_inline_when_available(
    tmp_path: Path,
) -> None:
    """Issue #1397: when a plan_review message renders, the detail pane
    shows the plan markdown body inline instead of the file-pointer
    text the architect emits."""
    from pollypm.cockpit_ui import PollyInboxApp

    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    (project_path / "docs").mkdir()
    plan_md = (
        "# Demo plan\n\n"
        "## Summary\n"
        "We will ship the rendering pipeline as three small tasks.\n\n"
        "## Judgment calls\n"
        "- Whether to keep the legacy code path during migration.\n"
        "- Cache eviction strategy in the renderer.\n\n"
        "## Plan body\n"
        "1. Wire the renderer.\n2. Migrate callers.\n3. Drop the legacy path.\n"
    )
    (project_path / "docs" / "project-plan.md").write_text(plan_md, encoding="utf-8")
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    plan_task_id = _seed_plan_task(project_path)
    message_id = _seed_plan_review_message(
        project_path,
        plan_task_id=plan_task_id,
        body=(
            f"Plan: {project_path}/docs/project-plan.md\n"
            "Press v to open the explainer (unavailable), "
            "d to discuss with the PM, A to approve."
        ),
    )
    if not _load_config_compatible(config_path):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    app = PollyInboxApp(config_path)

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_task_id = message_id
            app._render_detail(message_id)
            await pilot.pause()
            detail = str(app.detail.render())
            assert "Demo plan" in detail
            assert "rendering pipeline as three small tasks" in detail
            # File path NOT surfaced in the body — implementation detail.
            assert "/docs/project-plan.md" not in detail
            assert "Press v to open the explainer" not in detail

    _run(body())


# ---------------------------------------------------------------------------
# Deny flow (#1403) — capital D opens reason prompt, captures reason,
# cancels the underlying plan_task, creates a successor with
# ``predecessor_task_id``, and routes to the PM with a denial primer.
# ---------------------------------------------------------------------------


class TestPlanReviewDenialPrimer:
    def test_primer_includes_denial_reason_and_successor(self) -> None:
        from pollypm.cockpit_ui import _build_plan_review_denial_primer

        primer = _build_plan_review_denial_primer(
            project_key="demo",
            cancelled_plan_task_id="demo/3",
            successor_plan_task_id="demo/4",
            denial_reason="Backlog is too coarse — break it down further.",
            reviewer_name="Sam",
        )
        assert "denied plan task demo/3" in primer
        assert "Successor plan task: demo/4" in primer
        assert "Backlog is too coarse" in primer
        assert "Sit with Sam on the concerns" in primer
        assert "plan_review_denied" in primer

    def test_primer_swaps_to_polly_when_fast_track(self) -> None:
        from pollypm.cockpit_ui import _build_plan_review_denial_primer

        primer = _build_plan_review_denial_primer(
            project_key="demo",
            cancelled_plan_task_id="demo/3",
            successor_plan_task_id="demo/4",
            denial_reason="not enough decomposition",
            reviewer_name="Polly",
        )
        assert "Polly just denied plan task demo/3" in primer
        assert "Sit with Polly on the concerns" in primer


def test_capital_D_opens_denial_reason_prompt(
    plan_review_env, inbox_app,
) -> None:
    """``D`` on a plan_review row repurposes the reply input as a
    denial-reason prompt and arms ``_awaiting_denial_task_id``."""

    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            task_id = inbox_app._selected_task_id
            assert task_id == plan_review_env["plan_review_id"]
            assert inbox_app._awaiting_denial_task_id is None

            await pilot.press("D")
            await pilot.pause()

            assert inbox_app._awaiting_denial_task_id == task_id
            placeholder = inbox_app.reply_input.placeholder or ""
            assert "Denial reason" in placeholder
            assert inbox_app.reply_input.has_focus

    _run(body())


def test_plan_review_message_detail_falls_back_when_plan_missing(
    plan_review_message_env, plan_review_message_app,
) -> None:
    """When no plan markdown is on disk, the detail pane shows a
    discoverability hint rather than the file-pointer body."""
    async def body() -> None:
        async with plan_review_message_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            task_id = plan_review_message_env["message_id"]
            plan_review_message_app._selected_task_id = task_id
            plan_review_message_app._render_detail(task_id)
            await pilot.pause()
            detail = str(plan_review_message_app.detail.render())
            # Fallback hint replaces the file-pointer body.
            assert "Plan content is not available on disk yet" in detail
            assert "Plan: docs/project-plan.md" not in detail

    _run(body())


def test_long_plan_renders_in_scrollable_detail_pane(tmp_path: Path) -> None:
    """A long plan should not break the inbox detail layout; the host
    pane is a VerticalScroll so the long render scrolls inside it."""
    from pollypm.cockpit_ui import PollyInboxApp
    from textual.containers import VerticalScroll

    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    (project_path / "docs").mkdir()
    big_body = ["# Demo plan", "", "## Summary", "Tiny summary.", ""]
    for idx in range(120):
        big_body.append(f"## Section {idx}")
        big_body.append(f"Body for section {idx}.")
        big_body.append("")
    (project_path / "docs" / "project-plan.md").write_text(
        "\n".join(big_body), encoding="utf-8",
    )
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    plan_task_id = _seed_plan_task(project_path)
    message_id = _seed_plan_review_message(
        project_path,
        plan_task_id=plan_task_id,
    )
    if not _load_config_compatible(config_path):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    app = PollyInboxApp(config_path)

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_task_id = message_id
            app._render_detail(message_id)
            await pilot.pause()
            # The host scroll exists and contains the detail Static.
            scroll = app.query_one("#inbox-detail-scroll", VerticalScroll)
            assert scroll is not None
            detail = str(app.detail.render())
            # Many sections rendered without crashing layout.
            assert "Section 0" in detail
            assert "Section 119" in detail

    _run(body())


def test_project_drilldown_plan_review_card_surfaces_plan_summary(
    tmp_path: Path,
) -> None:
    """Drilling into a project with a pending plan_review action card
    surfaces the plan summary + judgment-call points inline (the rich
    project drilldown surface that #1397 calls out)."""
    from pollypm.cockpit_ui import PollyProjectDashboardApp
    from pollypm.work.sqlite_service import SQLiteWorkService

    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    (project_path / "docs").mkdir()
    plan_md = (
        "# Demo plan\n\n"
        "## Summary\n"
        "Three-phase rollout: scaffolding, migration, cleanup.\n\n"
        "## Judgment calls\n"
        "- Whether to keep the legacy path during migration.\n"
        "- Where to cut the cache invalidation.\n\n"
        "## Plan body\nDetails...\n"
    )
    (project_path / "docs" / "project-plan.md").write_text(plan_md, encoding="utf-8")
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    plan_task_id = _seed_plan_task(project_path)
    # Use the work service so the row carries the right needs_action /
    # triage_bucket flags the dashboard's action_items extractor uses.
    db_path = project_path / ".pollypm" / "state.db"
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        from pollypm.store import SQLAlchemyStore
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            store.enqueue_message(
                type="notify",
                tier="immediate",
                recipient="user",
                sender="architect",
                subject="Plan ready for review: demo",
                body=(
                    f"Plan: {project_path}/docs/project-plan.md\n\n"
                    "Press v to open the explainer (unavailable), "
                    "d to discuss with the PM, A to approve."
                ),
                scope="demo",
                labels=[
                    "plan_review",
                    "project:demo",
                    f"plan_task:{plan_task_id}",
                ],
                payload={
                    "actor": "architect",
                    "project": "demo",
                    "user_prompt": {
                        "summary": "A full project plan is ready for your review.",
                        "actions": [
                            {"label": "Review plan", "kind": "review_plan"},
                            {"label": "Open task", "kind": "open_task",
                             "task_id": plan_task_id},
                        ],
                    },
                },
                state="open",
            )
        finally:
            store.close()
    finally:
        svc.close()
    if not _load_config_compatible(config_path):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm import cockpit_ui as _cockpit_ui
    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
    _cockpit_ui._DASHBOARD_INBOX_CACHE.clear()
    app = PollyProjectDashboardApp(config_path, "demo")

    async def body() -> None:
        async with app.run_test(size=(140, 60)) as pilot:
            await pilot.pause()
            rendered = app._inbox_section_text()
            # Plan summary surfaced (at minimum, the leading clause).
            assert "Three-phase rollout" in rendered
            # Judgment calls surfaced as the flagged-points block.
            assert "Flagged judgment calls" in rendered
            assert "legacy path during migration" in rendered
            assert "cache invalidation" in rendered
            # File path NOT surfaced in the action card.
            assert "docs/project-plan.md" not in rendered

    _run(body())


def test_capital_D_no_op_on_non_plan_review(tmp_path: Path) -> None:
    """``D`` on a generic (non-plan_review) inbox row is a no-op."""

    async def body() -> None:
        project_path = tmp_path / "demo"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        _write_minimal_config(project_path, config_path)

        # Seed a generic chat task — no plan_review label.
        db_path = project_path / ".pollypm" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
        try:
            svc.create(
                title="Just a regular task",
                description="Generic body.",
                type="task",
                project="demo",
                flow_template="chat",
                roles={"requester": "user", "operator": "polly"},
                priority="normal",
                created_by="polly",
            )
        finally:
            svc.close()

        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        from pollypm.cockpit_ui import PollyInboxApp

        app = PollyInboxApp(config_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            if not app._tasks:
                pytest.skip("seed did not produce inbox rows")
            app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            assert app._awaiting_denial_task_id is None
            await pilot.press("D")
            await pilot.pause()
            assert app._awaiting_denial_task_id is None

    _run(body())


def test_finish_deny_cancels_plan_task_and_creates_successor(
    plan_review_env, inbox_app,
) -> None:
    """End-to-end: ``D`` → type reason → Enter cancels the plan_task,
    seeds a successor with ``predecessor_task_id``, and stamps the
    denial reason on both the cancelled task and the successor."""

    async def body() -> None:
        # Stub the PM dispatch so the test doesn't try to talk to tmux.
        from pollypm.cockpit_ui import PollyInboxApp

        dispatch_calls: list[tuple[str, str, str]] = []

        def fake_dispatch(self, cockpit_key, context_line, pm_label, **_kw):
            dispatch_calls.append((cockpit_key, context_line, pm_label))

        original = PollyInboxApp._dispatch_to_pm_sync
        PollyInboxApp._dispatch_to_pm_sync = fake_dispatch  # type: ignore[assignment]
        try:
            async with inbox_app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                inbox_app.list_view.index = 0
                await pilot.press("enter")
                await pilot.pause()
                task_id = inbox_app._selected_task_id
                plan_task_id = plan_review_env["plan_task_id"]

                await pilot.press("D")
                await pilot.pause()
                assert inbox_app._awaiting_denial_task_id == task_id

                inbox_app.reply_input.value = (
                    "Tasks are too coarse — break the auth flow into "
                    "three separate tasks."
                )
                await pilot.press("enter")
                await pilot.pause()

                # The plan_task is cancelled with the reason.
                svc = SQLiteWorkService(
                    db_path=plan_review_env["project_path"] / ".pollypm" / "state.db",
                    project_path=plan_review_env["project_path"],
                )
                try:
                    cancelled = svc.get(plan_task_id)
                    cancelled_status = (
                        cancelled.work_status.value
                        if hasattr(cancelled.work_status, "value")
                        else cancelled.work_status
                    )
                    assert cancelled_status == "cancelled"
                    # Cancellation reason is the user's first message —
                    # the transition row stamps it under to_state=cancelled.
                    cancel_reasons = [
                        getattr(t, "reason", "") or ""
                        for t in (cancelled.transitions or [])
                        if getattr(t, "to_state", "") == "cancelled"
                    ]
                    assert any(
                        "Tasks are too coarse" in r for r in cancel_reasons
                    ), f"no matching transition reason: {cancel_reasons}"

                    # Successor exists with ``predecessor_task_id``.
                    successors = svc.list_successors(plan_task_id)
                    assert len(successors) == 1
                    successor = successors[0]
                    assert successor.predecessor_task_id == plan_task_id
                    assert successor.flow_template_id == "plan_project"

                    # Denial reason is stamped on the successor as
                    # ``plan_review_denied`` context.
                    den_ctx = svc.get_context(
                        successor.task_id,
                        entry_type="plan_review_denied",
                        limit=5,
                    )
                    den_text = " ".join(
                        getattr(e, "text", "") or "" for e in den_ctx
                    )
                    assert "Tasks are too coarse" in den_text
                finally:
                    svc.close()

                # Inbox row dropped from the cached list.
                assert all(
                    t.task_id != task_id for t in inbox_app._tasks
                )
                # Denial-pending state cleared.
                assert inbox_app._awaiting_denial_task_id is None

                # PM dispatch fired with a denial primer (best-effort,
                # may be queued via run_worker so we tolerate an empty
                # list under run_test).
                if dispatch_calls:
                    _, context_line, _ = dispatch_calls[-1]
                    assert "denied plan task" in context_line
                    assert "Tasks are too coarse" in context_line
        finally:
            PollyInboxApp._dispatch_to_pm_sync = original  # type: ignore[assignment]

    _run(body())


def test_esc_cancels_pending_denial(plan_review_env, inbox_app) -> None:
    """Pressing Esc while ``_awaiting_denial_task_id`` is set clears
    the pending denial without firing the cancel cascade."""

    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            task_id = inbox_app._selected_task_id

            await pilot.press("D")
            await pilot.pause()
            assert inbox_app._awaiting_denial_task_id == task_id

            await pilot.press("escape")
            await pilot.pause()
            assert inbox_app._awaiting_denial_task_id is None

            # Plan task is still alive (no cancel fired).
            svc = SQLiteWorkService(
                db_path=plan_review_env["project_path"] / ".pollypm" / "state.db",
                project_path=plan_review_env["project_path"],
            )
            try:
                plan_task = svc.get(plan_review_env["plan_task_id"])
                assert plan_task.work_status != "cancelled"
            finally:
                svc.close()

    _run(body())


# ---------------------------------------------------------------------------
# #1402 — approve flow celebration toast + 10s undo window
# ---------------------------------------------------------------------------


@pytest.fixture
def deferred_inbox_app(plan_review_env):
    """Inbox fixture that keeps the deferred undo path enabled.

    The shared ``inbox_app`` fixture sets the window to 0 so legacy
    tests can press ``A`` and immediately observe ``svc.approve``.
    The #1402 tests need the deferred path: a short window long enough
    to land an undo, short enough to drive without long awaits.
    """
    if not _load_config_compatible(plan_review_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(plan_review_env["config_path"])
    app._approve_undo_window_seconds = 0.3
    return app


def _seed_round_trip(plan_review_env: dict) -> None:
    """Mirror ``test_plan_review_accept_unlocks_after_round_trip``."""
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
            "agreed - split module X into three",
            actor="architect",
        )
    finally:
        svc.close()


class TestApproveCelebrationToast:
    def test_approve_emits_persona_celebration_toast(
        self, plan_review_env, deferred_inbox_app,
    ) -> None:
        """Pressing ``A`` surfaces a persona-driven celebration line."""
        async def body() -> None:
            _seed_round_trip(plan_review_env)
            captured: list[str] = []

            original_notify = deferred_inbox_app.notify

            def fake_notify(message, *args, **kwargs):
                captured.append(str(message))
                return original_notify(message, *args, **kwargs)

            deferred_inbox_app.notify = fake_notify  # type: ignore[assignment]
            from pollypm.work.sqlite_service import SQLiteWorkService as _S
            original_approve = _S.approve

            def _fake_approve(self, task_id, actor, reason=None):
                return self.get(task_id)

            _S.approve = _fake_approve  # type: ignore[assignment]
            try:
                async with deferred_inbox_app.run_test(size=(140, 40)) as pilot:
                    await pilot.pause()
                    deferred_inbox_app.list_view.index = 0
                    await pilot.press("enter")
                    await pilot.pause()
                    await pilot.press("A")
                    await pilot.pause(0.05)
                    # The toast text contains the persona-driven line.
                    joined = "\n".join(captured)
                    # Plan task has zero children in the seeded fixture
                    # so we should land in the "Quick bite" / 0-1 branch.
                    assert (
                        "Quick bite" in joined
                        or "Out of the oven" in joined
                        or "Big batch" in joined
                    ), f"missing celebration line: {joined!r}"
                    assert "[u] undo" in joined
                    # Pending approval state recorded.
                    assert deferred_inbox_app._pending_plan_approval is not None
            finally:
                _S.approve = original_approve  # type: ignore[assignment]
        _run(body())


class TestApproveDeferredUndo:
    def test_undo_within_window_keeps_plan_in_review(
        self, plan_review_env, deferred_inbox_app,
    ) -> None:
        """``u`` inside the window cancels the deferred ``svc.approve``."""
        async def body() -> None:
            _seed_round_trip(plan_review_env)
            captured: list[tuple[str, str]] = []
            from pollypm.work.sqlite_service import SQLiteWorkService as _S
            original_approve = _S.approve

            def _fake_approve(self, task_id, actor, reason=None):
                captured.append((task_id, actor))
                return self.get(task_id)

            _S.approve = _fake_approve  # type: ignore[assignment]
            try:
                async with deferred_inbox_app.run_test(size=(140, 40)) as pilot:
                    await pilot.pause()
                    deferred_inbox_app.list_view.index = 0
                    await pilot.press("enter")
                    await pilot.pause()
                    await pilot.press("A")
                    await pilot.pause(0.05)
                    assert deferred_inbox_app._pending_plan_approval is not None
                    # Undo before the timer fires.
                    await pilot.press("u")
                    await pilot.pause(0.5)
                    # No approve call landed.
                    assert captured == []
                    # Pending state cleared.
                    assert deferred_inbox_app._pending_plan_approval is None
            finally:
                _S.approve = original_approve  # type: ignore[assignment]
        _run(body())

    def test_window_elapses_then_approve_fires(
        self, plan_review_env, deferred_inbox_app,
    ) -> None:
        """After the 10s window (here: 0.3s) the deferred approve fires."""
        async def body() -> None:
            _seed_round_trip(plan_review_env)
            captured: list[tuple[str, str]] = []
            from pollypm.work.sqlite_service import SQLiteWorkService as _S
            original_approve = _S.approve

            def _fake_approve(self, task_id, actor, reason=None):
                captured.append((task_id, actor))
                return self.get(task_id)

            _S.approve = _fake_approve  # type: ignore[assignment]
            try:
                async with deferred_inbox_app.run_test(size=(140, 40)) as pilot:
                    await pilot.pause()
                    deferred_inbox_app.list_view.index = 0
                    await pilot.press("enter")
                    await pilot.pause()
                    await pilot.press("A")
                    # Wait past the window so the timer fires.
                    for _ in range(20):
                        await pilot.pause(0.05)
                        if captured:
                            break
                    assert captured, "approve never fired after window"
                    assert captured[-1] == (
                        plan_review_env["plan_task_id"], "user",
                    )
                    # Pending state cleared after commit.
                    assert deferred_inbox_app._pending_plan_approval is None
            finally:
                _S.approve = original_approve  # type: ignore[assignment]
        _run(body())


class TestUndoWindowConstant:
    def test_default_undo_window_is_ten_seconds(self) -> None:
        """The class-level default matches the issue's spec."""
        from pollypm.cockpit_ui import PollyInboxApp
        # Class-level default lives on the type so we can read it
        # without instantiating (which requires a config path).
        assert PollyInboxApp._approve_undo_window_seconds == 10.0


class TestPersonaLookup:
    def test_persona_resolves_from_project_config(self, tmp_path) -> None:
        """``_project_persona_name`` returns the configured persona."""
        from pollypm.cockpit_ui import PollyInboxApp
        project_path = tmp_path / "savethenovel"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        config_path.write_text(
            "[project]\n"
            f'tmux_session = "pollypm-test"\n'
            f'workspace_root = "{project_path.parent}"\n'
            "\n"
            f'[projects.savethenovel]\n'
            f'key = "savethenovel"\n'
            f'name = "Save The Novel"\n'
            f'path = "{project_path}"\n'
            f'persona_name = "Sage"\n'
        )
        app = PollyInboxApp(config_path)
        persona = app._project_persona_name("savethenovel")
        assert persona == "Sage"

    def test_unknown_project_returns_none(self, tmp_path) -> None:
        """Bogus project key falls through to None (toast picks Polly)."""
        from pollypm.cockpit_ui import PollyInboxApp
        config_path = tmp_path / "pollypm.toml"
        config_path.write_text(
            "[project]\n"
            f'tmux_session = "pollypm-test"\n'
            f'workspace_root = "{tmp_path}"\n'
        )
        app = PollyInboxApp(config_path)
        assert app._project_persona_name("nonexistent") is None
        assert app._project_persona_name("") is None


class TestSubtaskCount:
    def test_subtask_count_matches_children(
        self, plan_review_env, deferred_inbox_app,
    ) -> None:
        """The plan task's children list determines the toast copy."""
        from pollypm.work.sqlite_service import SQLiteWorkService
        plan_task_id = plan_review_env["plan_task_id"]
        # Stamp three children onto the plan task using add_context to
        # avoid running a full work-flow decomposition. We just need
        # the children list non-empty for the count helper to pick up.
        # The pure helper reads ``len(task.children)`` so we set the
        # attribute directly via a fake task in a stub svc.

        class _FakeSvc:
            def __init__(self, count: int) -> None:
                self._count = count

            def get(self, task_id):
                class _Task:
                    children = [("demo", i) for i in range(self._count)]

                return _Task()

            def close(self):
                pass

        # Monkeypatch the resolver to return the fake.
        def fake_resolve(item, plan_task_id):
            return _FakeSvc(7)

        deferred_inbox_app._resolve_inbox_svc = fake_resolve  # type: ignore[assignment]
        count = deferred_inbox_app._plan_task_subtask_count(None, plan_task_id)
        assert count == 7

    def test_subtask_count_handles_lookup_failure(
        self, deferred_inbox_app,
    ) -> None:
        """Missing svc / missing task → 0 (toast still renders)."""
        deferred_inbox_app._resolve_inbox_svc = (
            lambda item, plan_task_id: None
        )  # type: ignore[assignment]
        assert (
            deferred_inbox_app._plan_task_subtask_count(None, "demo/1") == 0
        )


class TestDrilldownApproveFlow:
    """Approve from project drilldown (#1402).

    The drilldown surface intercepts ``a`` when a plan_review action
    card is at the top of the dashboard data; otherwise it falls back
    to the legacy alerts view.
    """

    def _stub_dashboard(self, action_items: list[dict]):
        """Construct a bare ``PollyProjectDashboardApp`` for unit testing.

        We bypass ``__init__`` because the real one requires a config
        path and starts loading state. The methods under test only
        touch ``self.data.action_items`` and a handful of helpers we
        stub on the instance.
        """
        from pollypm.cockpit_ui import (
            PollyProjectDashboardApp,
            ProjectDashboardData,
        )
        app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
        app._pending_plan_approval = None
        app._pending_plan_approval_timer = None
        app._approve_undo_window_seconds = 0.0  # immediate commit
        app.config_path = None  # type: ignore[assignment]
        app.project_key = "demo"

        # Minimal ProjectDashboardData stand-in. The real dataclass
        # has many required fields so we use a SimpleNamespace.
        from types import SimpleNamespace
        app.data = SimpleNamespace(action_items=action_items)
        # Stub the helpers the method calls.
        app._drilldown_plan_task_subtask_count = lambda plan_task_id: 4
        app._drilldown_project_persona_name = lambda: "Sage"
        # Capture notify calls.
        app._notify_calls = []
        app.notify = lambda msg, **kw: app._notify_calls.append((str(msg), kw))
        # Capture record_action_response calls.
        app._record_calls = []

        def fake_record(index, response, *, approve_if_possible=False):
            app._record_calls.append((index, response, approve_if_possible))

        app._record_action_response = fake_record  # type: ignore[assignment]
        # action_view_alerts called when no plan_review present.
        app._alerts_called = False

        def fake_alerts():
            app._alerts_called = True

        app.action_view_alerts = fake_alerts  # type: ignore[assignment]
        return app

    def test_a_approves_first_plan_review_card(self) -> None:
        """``a`` on a drilldown with a plan_review card runs the celebration."""
        app = self._stub_dashboard([
            {
                "is_plan_review": True,
                "primary_ref": "demo/3",
                "primary_action": {"kind": "review_plan", "task_id": "demo/3"},
            },
        ])
        result = app._approve_first_plan_review_if_present()
        assert result is True
        # Toast captured.
        assert any(
            "Sage:" in msg and "tasks plated" in msg
            for msg, _ in app._notify_calls
        )
        # Immediate-commit path called record_action_response.
        assert app._record_calls == [
            (0, "Approved from project dashboard.", True),
        ]
        # No alerts fall-through.
        assert app._alerts_called is False

    def test_a_falls_through_to_alerts_when_no_plan_review(self) -> None:
        """No plan_review card → ``a`` opens alerts."""
        app = self._stub_dashboard([
            {"is_plan_review": False, "primary_ref": "demo/9"},
        ])
        app.action_approve_or_alerts()
        assert app._alerts_called is True
        assert app._record_calls == []

    def test_a_no_op_with_no_action_items(self) -> None:
        """Empty action_items still falls through to alerts."""
        app = self._stub_dashboard([])
        app.action_approve_or_alerts()
        assert app._alerts_called is True

    def test_undo_within_window_skips_approval(self) -> None:
        """``u`` after ``a`` cancels the deferred approval."""
        from pollypm.cockpit_ui import PollyProjectDashboardApp
        app = self._stub_dashboard([
            {
                "is_plan_review": True,
                "primary_ref": "demo/3",
                "primary_action": {"kind": "review_plan", "task_id": "demo/3"},
            },
        ])
        # Use a finite window so we can land an undo. ``set_timer``
        # isn't running (no event loop), so the except-fallback would
        # commit immediately. To exercise the undo path purely we set
        # the timer manually after ``approve``.
        app._approve_undo_window_seconds = 100.0  # too long to elapse

        # Stub set_timer to record without firing.
        scheduled = []

        def fake_set_timer(delay, fn):
            scheduled.append((delay, fn))

            class _T:
                stopped = False

                def stop(self_inner):
                    self_inner.stopped = True

            t = _T()
            return t

        app.set_timer = fake_set_timer  # type: ignore[assignment]
        result = app._approve_first_plan_review_if_present()
        assert result is True
        # Pending state recorded; commit not yet fired.
        assert app._pending_plan_approval is not None
        assert app._record_calls == []
        # Now press 'u' (undo).
        app.action_undo_plan_approval_or_refresh()
        # Pending state cleared; commit still not fired.
        assert app._pending_plan_approval is None
        assert app._record_calls == []
        # Timer was stopped.
        assert scheduled and scheduled[0][1] is not None
