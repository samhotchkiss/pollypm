"""Default-lens + lens-switching tests for the cockpit Inbox (#1573).

Drives :class:`pollypm.cockpit_ui.PollyInboxApp` via ``Pilot`` to assert
the curated awaits-you default lens, lens cycling via ``L``, direct
selection via ``1``..``6``, per-lens empty-state copy, focus
preservation across lens swaps, and the load-bearing regression
invariant that pins the badge-count == default-view-count ==
awaits_user-count equality.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pollypm.cockpit_inbox import (
    _count_inbox_tasks_for_label,
    pm_inbox_awaits_user_list,
    pm_inbox_filtered_list,
)
from pollypm.inbox.kind import InboxItemKind
from pollypm.store import SQLAlchemyStore
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fixtures — one project, mixed-kind seed
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


def _seed_awaits_user_task(project_path: Path, *, title: str = "Plan review") -> str:
    """Seed a plan_review_pending task — awaits_user=True."""
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task = svc.create(
            title=title,
            description="Plan ready for your review.",
            type="task",
            project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            priority="normal",
            created_by="polly",
            kind=InboxItemKind.PLAN_REVIEW_PENDING.value,
        )
        return task.task_id
    finally:
        svc.close()


def _seed_kind_message(
    workspace_root: Path,
    *,
    subject: str,
    kind: str,
    scope: str = "demo",
) -> int:
    """Insert a message row with an explicit ``kind`` value."""
    db_path = workspace_root / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        return store.enqueue_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="polly",
            subject=subject,
            body=f"{kind} body",
            scope=scope,
            kind=kind,
        )
    finally:
        store.close()


@pytest.fixture
def mixed_kinds_env(tmp_path: Path):
    """Build a workspace with one row per non-awaits-user kind +
    several awaits-user rows so every lens has something to surface.
    """
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    awaits_id = _seed_awaits_user_task(
        project_path, title="Plan review for demo",
    )
    workspace_root = tmp_path
    completion_id = _seed_kind_message(
        workspace_root,
        subject="Done: feature shipped",
        kind="completion_fyi",
    )
    activity_id = _seed_kind_message(
        workspace_root,
        subject="worker started on demo/3",
        kind="activity_event",
    )
    bug_id = _seed_kind_message(
        workspace_root,
        subject="Self-bug: heartbeat hiccup",
        kind="self_bug_report",
    )
    legacy_id = _seed_kind_message(
        workspace_root,
        subject="Legacy: pre-migration row",
        kind="legacy",
    )
    return {
        "config_path": config_path,
        "project_path": project_path,
        "awaits_task_id": awaits_id,
        "completion_msg_id": completion_id,
        "activity_msg_id": activity_id,
        "bug_msg_id": bug_id,
        "legacy_msg_id": legacy_id,
    }


def _load_config_compatible(config_path: Path) -> bool:
    try:
        from pollypm.config import load_config
        cfg = load_config(config_path)
        return "demo" in getattr(cfg, "projects", {})
    except Exception:  # noqa: BLE001
        return False


def _run(coro) -> None:
    asyncio.run(coro)


def _visible_titles(app) -> list[str]:
    from pollypm.cockpit_ui import _InboxListItem
    return [
        c.task_ref.title
        for c in app.list_view.children
        if isinstance(c, _InboxListItem)
    ]


# ---------------------------------------------------------------------------
# 1. Default lens IS awaits-you
# ---------------------------------------------------------------------------


def test_inbox_opens_to_awaits_you_lens(mixed_kinds_env) -> None:
    if not _load_config_compatible(mixed_kinds_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp

    app = PollyInboxApp(mixed_kinds_env["config_path"])

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app._active_lens == "awaits-you"
            titles = _visible_titles(app)
            # Awaits-you: the plan_review task + the legacy message
            # (LEGACY is fail-open in the predicate).
            assert any("Plan review for demo" in t for t in titles)
            assert any("Legacy: pre-migration row" in t for t in titles)
            # Non-awaits-user kinds are filtered out.
            assert not any("Done: feature shipped" in t for t in titles)
            assert not any("worker started on demo/3" in t for t in titles)
            assert not any("Self-bug: heartbeat hiccup" in t for t in titles)

    _run(body())


# ---------------------------------------------------------------------------
# 2. Regression invariant: badge == awaits_user list == default-view count
# ---------------------------------------------------------------------------


def test_lens_default_matches_rail_badge_and_predicate_count(
    mixed_kinds_env,
) -> None:
    """#1573 load-bearing equality.

    The badge, the dashboard "Waiting on you" section, and the inbox
    default lens MUST agree on every config. Splitting these three
    surfaces is the exact failure mode #1564 was created to retire.
    """
    if not _load_config_compatible(mixed_kinds_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.config import load_config
    from pollypm.cockpit_ui import PollyInboxApp

    config = load_config(mixed_kinds_env["config_path"])
    badge_count = _count_inbox_tasks_for_label(config)
    predicate_count = len(pm_inbox_awaits_user_list(config))
    assert badge_count == predicate_count

    app = PollyInboxApp(mixed_kinds_env["config_path"])

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app._active_lens == "awaits-you"
            visible = _visible_titles(app)
            assert len(visible) == badge_count
            assert len(visible) == predicate_count

    _run(body())


# ---------------------------------------------------------------------------
# 3. Lens cycling via ``L``
# ---------------------------------------------------------------------------


def test_l_capital_cycles_through_lenses(mixed_kinds_env) -> None:
    if not _load_config_compatible(mixed_kinds_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp, _INBOX_LENSES

    app = PollyInboxApp(mixed_kinds_env["config_path"])
    expected_slugs = [spec[0] for spec in _INBOX_LENSES]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app._active_lens == expected_slugs[0]
            for next_slug in expected_slugs[1:]:
                await pilot.press("L")
                await pilot.pause()
                assert app._active_lens == next_slug
            # Wrap back to the default.
            await pilot.press("L")
            await pilot.pause()
            assert app._active_lens == expected_slugs[0]

    _run(body())


# ---------------------------------------------------------------------------
# 4. Direct selection via 1..6 jumps to the right lens
# ---------------------------------------------------------------------------


def test_number_keys_select_lens_directly(mixed_kinds_env) -> None:
    if not _load_config_compatible(mixed_kinds_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp, _INBOX_LENSES

    app = PollyInboxApp(mixed_kinds_env["config_path"])
    expected_slugs = [spec[0] for spec in _INBOX_LENSES]

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            for index, slug in enumerate(expected_slugs, start=1):
                await pilot.press(str(index))
                await pilot.pause()
                assert app._active_lens == slug

    _run(body())


# ---------------------------------------------------------------------------
# 5. Each lens shows the right subset
# ---------------------------------------------------------------------------


def test_each_lens_shows_expected_subset(mixed_kinds_env) -> None:
    if not _load_config_compatible(mixed_kinds_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp

    app = PollyInboxApp(mixed_kinds_env["config_path"])

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()

            # ``all`` — every loaded row visible.
            await pilot.press("2")
            await pilot.pause()
            assert app._active_lens == "all"
            titles = _visible_titles(app)
            assert any("Plan review for demo" in t for t in titles)
            assert any("Done: feature shipped" in t for t in titles)
            assert any("worker started on demo/3" in t for t in titles)
            assert any("Self-bug: heartbeat hiccup" in t for t in titles)
            assert any("Legacy: pre-migration row" in t for t in titles)

            # ``completion-fyi`` — only the shipped FYI.
            await pilot.press("3")
            await pilot.pause()
            assert app._active_lens == "completion-fyi"
            titles = _visible_titles(app)
            assert len(titles) == 1
            assert any("Done: feature shipped" in t for t in titles)

            # ``activity-events`` — only the worker-started row.
            await pilot.press("4")
            await pilot.pause()
            assert app._active_lens == "activity-events"
            titles = _visible_titles(app)
            assert len(titles) == 1
            assert any("worker started on demo/3" in t for t in titles)

            # ``self-bug-reports`` — only the heartbeat hiccup.
            await pilot.press("5")
            await pilot.pause()
            assert app._active_lens == "self-bug-reports"
            titles = _visible_titles(app)
            assert len(titles) == 1
            assert any("Self-bug: heartbeat hiccup" in t for t in titles)

            # ``legacy`` — only the legacy pre-migration row.
            await pilot.press("6")
            await pilot.pause()
            assert app._active_lens == "legacy"
            titles = _visible_titles(app)
            assert any("Legacy: pre-migration row" in t for t in titles)
            assert not any("Done: feature shipped" in t for t in titles)

    _run(body())


# ---------------------------------------------------------------------------
# 6. Per-lens empty-state copy
# ---------------------------------------------------------------------------


def test_empty_lens_renders_lens_specific_copy(tmp_path: Path) -> None:
    """Each lens's empty state surfaces the issue-spec copy."""
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    # Seed only a single completion_fyi message so most lenses are empty.
    _seed_kind_message(
        tmp_path,
        subject="Done: only fyi",
        kind="completion_fyi",
    )

    if not _load_config_compatible(config_path):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")

    from pollypm.cockpit_ui import PollyInboxApp, _INBOX_LENSES

    app = PollyInboxApp(config_path)
    copy_by_slug = {spec[0]: spec[2] for spec in _INBOX_LENSES}

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()

            # awaits-you: empty (the only row is completion-fyi kind)
            assert app._active_lens == "awaits-you"
            assert _visible_titles(app) == []
            detail = str(app.detail.render())
            assert "Nothing awaiting your action" in detail

            # all: has the one row → not empty
            await pilot.press("2")
            await pilot.pause()
            assert len(_visible_titles(app)) == 1

            # activity-events: empty
            await pilot.press("4")
            await pilot.pause()
            assert _visible_titles(app) == []
            assert copy_by_slug["activity-events"] in str(app.detail.render())

            # self-bug-reports: empty
            await pilot.press("5")
            await pilot.pause()
            assert _visible_titles(app) == []
            assert "No self-reported bugs" in str(app.detail.render())
            assert "pm bug-report" in str(app.detail.render())

            # legacy: empty
            await pilot.press("6")
            await pilot.pause()
            assert _visible_titles(app) == []
            assert "All legacy rows have been classified" in str(app.detail.render())

    _run(body())


# ---------------------------------------------------------------------------
# 7. Focus preservation across lens swaps
# ---------------------------------------------------------------------------


def test_focused_row_stays_visible_when_present_in_new_lens(
    mixed_kinds_env,
) -> None:
    """The selected row stays focused after a lens swap when it
    survives the new lens; otherwise focus jumps to the top.
    """
    if not _load_config_compatible(mixed_kinds_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp

    app = PollyInboxApp(mixed_kinds_env["config_path"])

    async def body() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Switch to ``all`` so every kind is visible; focus on the
            # plan_review task row.
            await pilot.press("2")
            await pilot.pause()
            titles = _visible_titles(app)
            target_idx = next(
                i for i, t in enumerate(titles) if "Plan review for demo" in t
            )
            app.list_view.index = target_idx
            app._sync_selection_from_list(defer_detail=True, mark_read=False)
            await pilot.pause()
            selected_task_id = app._selected_task_id
            assert selected_task_id is not None

            # Switch to ``awaits-you``. The plan_review task survives
            # the lens (PLAN_REVIEW_PENDING is in the awaits-user set),
            # so the focused task stays focused.
            await pilot.press("1")
            await pilot.pause()
            assert app._active_lens == "awaits-you"
            assert app._selected_task_id == selected_task_id

            # Switch to ``completion-fyi``. The plan_review task is
            # NOT in that lens; focus drops to the top of the new
            # lens (the lone completion_fyi message).
            await pilot.press("3")
            await pilot.pause()
            assert app._active_lens == "completion-fyi"
            assert app._selected_task_id != selected_task_id

    _run(body())


# ---------------------------------------------------------------------------
# 8. pm_inbox_filtered_list helper (data-layer sibling of awaits-user list)
# ---------------------------------------------------------------------------


def test_pm_inbox_filtered_list_returns_kind_scoped_subset(
    mixed_kinds_env,
) -> None:
    from pollypm.config import load_config

    config = load_config(mixed_kinds_env["config_path"])

    # No filter → returns everything (awaits-you tasks + every kind).
    all_items = pm_inbox_filtered_list(config)
    titles_all = [getattr(item, "title", "") for item in all_items]
    assert any("Plan review for demo" in t for t in titles_all)
    assert any("Done: feature shipped" in t for t in titles_all)
    assert any("worker started on demo/3" in t for t in titles_all)
    assert any("Self-bug: heartbeat hiccup" in t for t in titles_all)

    # Per-kind filter narrows correctly.
    completion = pm_inbox_filtered_list(
        config, kind_filter=InboxItemKind.COMPLETION_FYI,
    )
    titles_c = [getattr(item, "title", "") for item in completion]
    assert any("Done: feature shipped" in t for t in titles_c)
    assert not any("Plan review for demo" in t for t in titles_c)

    activity = pm_inbox_filtered_list(
        config, kind_filter=InboxItemKind.ACTIVITY_EVENT,
    )
    titles_a = [getattr(item, "title", "") for item in activity]
    assert any("worker started on demo/3" in t for t in titles_a)
    assert len(titles_a) == 1

    bugs = pm_inbox_filtered_list(
        config, kind_filter=InboxItemKind.SELF_BUG_REPORT,
    )
    titles_b = [getattr(item, "title", "") for item in bugs]
    assert any("Self-bug: heartbeat hiccup" in t for t in titles_b)
    assert len(titles_b) == 1
