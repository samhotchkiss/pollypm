"""Tests for the cockpit inbox filter + search bar (#NEW).

Drives :class:`pollypm.cockpit_ui.PollyInboxApp` via ``Pilot`` and
asserts the filter overlay's behaviour: `/` mounts a fuzzy text Input,
chip-toggle keys narrow by unread / project / recent / type, multiple
chips AND-combine, ``c`` clears everything, and the friendly
empty-match copy renders when nothing survives. Filters are
session-scoped — a remount returns to the full list.

Mirrors ``tests/test_cockpit_inbox_ui.py`` for fixture shape so the two
suites can share monkeypatches if needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fixtures — single-project minimal cockpit + a multi-project variant
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


def _write_multi_config(
    project_paths: list[tuple[str, Path]], config_path: Path,
) -> None:
    """Emit a config with multiple `[projects.<key>]` blocks."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_root = project_paths[0][1].parent
    lines = [
        "[project]\n",
        f'tmux_session = "pollypm-test"\n',
        f'workspace_root = "{workspace_root}"\n',
        "\n",
    ]
    for key, path in project_paths:
        lines.append(f'[projects.{key}]\n')
        lines.append(f'key = "{key}"\n')
        lines.append(f'name = "{key.title()}"\n')
        lines.append(f'path = "{path}"\n')
        lines.append("\n")
    config_path.write_text("".join(lines))


def _seed_project(
    project_path: Path,
    *,
    project_key: str = "demo",
    items: list[tuple[str, str, list[str] | None]] | None = None,
) -> list[str]:
    """Seed a project's state.db with inbox tasks.

    ``items`` is a list of ``(title, body, labels)``. When unset, three
    plain tasks are seeded (matching the broader inbox suite).
    """
    if items is None:
        items = [
            ("Smoke subject", "Smoke body", None),
            ("Deploy blocked", "Verify email click.", None),
            ("Homepage rewrite", "Review please.", None),
        ]
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        ids: list[str] = []
        for title, body, labels in items:
            t = svc.create(
                title=title,
                description=body,
                type="task",
                project=project_key,
                flow_template="chat",
                roles={"requester": "user", "operator": "polly"},
                priority="normal",
                created_by="polly",
                labels=labels,
            )
            ids.append(t.task_id)
        return ids
    finally:
        svc.close()


@pytest.fixture
def filter_env(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    # Seed a richer mix so the filter tests have something to narrow.
    ids = _seed_project(
        project_path,
        items=[
            ("shipped: cookie banner", "shipped to prod", ["shipped"]),
            ("shipped: rollup of merges", "merge digest", ["rollup"]),
            ("Deploy blocked on staging", "blocked", None),
            ("Plan review request", "review the plan", ["plan_review"]),
            ("Worker stuck on auth", "blocking question", ["blocking_question"]),
        ],
    )
    return {
        "config_path": config_path,
        "project_path": project_path,
        "task_ids": ids,
    }


def _load_config_compatible(config_path: Path) -> bool:
    try:
        from pollypm.config import load_config
        cfg = load_config(config_path)
        return bool(getattr(cfg, "projects", {}))
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def filter_app(filter_env):
    if not _load_config_compatible(filter_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    return PollyInboxApp(filter_env["config_path"])


def _run(coro) -> None:
    asyncio.run(coro)


def _visible_titles(app) -> list[str]:
    """Return the titles currently rendered in the list view.

    Walks the ``ListView`` children rather than ``app._tasks`` so we
    assert what the user actually sees, not just the unfiltered backing
    store.
    """
    from pollypm.cockpit_ui import _InboxListItem
    return [
        c.task_ref.title
        for c in app.list_view.children
        if isinstance(c, _InboxListItem)
    ]


# ---------------------------------------------------------------------------
# 1. `/` mounts the filter Input and typing live-filters the list
# ---------------------------------------------------------------------------


def test_slash_opens_filter_input_and_typing_filters_list(
    filter_env, filter_app,
) -> None:
    async def body() -> None:
        async with filter_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            initial = len(_visible_titles(filter_app))
            assert initial == 5

            await pilot.press("slash")
            await pilot.pause()
            # Filter Input is now shown + focused.
            assert filter_app.filter_input.display is True
            assert filter_app.filter_input.has_focus
            assert filter_app._filter_bar_visible is True

            # Type a query. Use the Changed event path so we don't
            # depend on the textual key dispatcher routing each char.
            filter_app.filter_input.value = "shipped"
            await pilot.pause()

            # Two seeded titles begin with "shipped".
            visible = _visible_titles(filter_app)
            assert len(visible) == 2
            assert all("shipped" in t.lower() for t in visible)
    _run(body())


# ---------------------------------------------------------------------------
# 2. Fuzzy match: 'shp' matches 'shipped' titles
# ---------------------------------------------------------------------------


def test_fuzzy_subsequence_match(filter_env, filter_app) -> None:
    async def body() -> None:
        async with filter_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            filter_app.filter_input.value = "shp"
            await pilot.pause()

            # 'shp' is a subsequence of 'shipped' → both shipped titles
            # must be in the visible set. Other rows may also match
            # because the haystack folds in project + sender (e.g. a row
            # whose body has s/h and the sender 'polly' supplies the p)
            # — that's working-as-intended for fuzzy search; we just
            # require the obvious hits to land.
            visible = _visible_titles(filter_app)
            assert visible, "fuzzy match should keep at least one row"
            assert any("cookie banner" in t.lower() for t in visible)
            assert any("rollup of merges" in t.lower() for t in visible)

            # And a query that demands letters that don't appear in any
            # row at all returns the empty list (sanity).
            filter_app.filter_input.value = "xyz_no_such_seq"
            await pilot.pause()
            assert _visible_titles(filter_app) == []
    _run(body())


# ---------------------------------------------------------------------------
# 3. Unread-only chip narrows the list
# ---------------------------------------------------------------------------


def test_unread_only_filter_chip(filter_env, filter_app) -> None:
    async def body() -> None:
        async with filter_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Open the first row → marks it read so the unread-only chip
            # actually narrows.
            filter_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            initial_unread = len(filter_app._unread_ids)
            assert initial_unread == 4  # 5 seeded, one read

            await pilot.press("u")
            await pilot.pause()

            assert filter_app._filter_unread_only is True
            visible = _visible_titles(filter_app)
            # Visible count == unread count.
            assert len(visible) == initial_unread
            # The chip strip shows 'unread'.
            assert "unread" in str(filter_app.filter_chips.render()).lower()
    _run(body())


# ---------------------------------------------------------------------------
# 4. Project picker filter narrows correctly (multi-project workspace)
# ---------------------------------------------------------------------------


def test_project_picker_filter_narrows_list(tmp_path: Path) -> None:
    async def body() -> None:
        demo = tmp_path / "demo"
        demo.mkdir()
        (demo / ".git").mkdir()
        other = tmp_path / "other"
        other.mkdir()
        (other / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        _write_multi_config([("demo", demo), ("other", other)], config_path)
        _seed_project(
            demo, project_key="demo",
            items=[("demo task A", "x", None), ("demo task B", "y", None)],
        )
        _seed_project(
            other, project_key="other",
            items=[("other task 1", "z", None)],
        )
        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        from pollypm.cockpit_ui import PollyInboxApp
        app = PollyInboxApp(config_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert len(_visible_titles(app)) == 3

            # Set the project filter directly — the picker modal would
            # do the same via dismiss, and we don't need to drive the
            # modal UI to validate the narrowing logic.
            app._filter_project = "demo"
            app._render_list(select_first=True)
            await pilot.pause()

            visible = _visible_titles(app)
            assert len(visible) == 2
            assert all("demo task" in t for t in visible)

            # Chip strip mentions the selected project.
            assert "demo" in str(app.filter_chips.render()).lower()
    _run(body())


# ---------------------------------------------------------------------------
# 5. Multiple chips AND-combine
# ---------------------------------------------------------------------------


def test_multiple_chips_and_combine(filter_env, filter_app) -> None:
    async def body() -> None:
        async with filter_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Toggle plan_review chip — only the plan_review row remains.
            await pilot.press("l")
            await pilot.pause()
            visible = _visible_titles(filter_app)
            assert visible == ["Plan review request"]

            # Layer in unread-only — plan_review row is still unread, so
            # the result is unchanged but both chips are active.
            await pilot.press("u")
            await pilot.pause()
            assert filter_app._filter_plan_review is True
            assert filter_app._filter_unread_only is True
            visible = _visible_titles(filter_app)
            assert visible == ["Plan review request"]

            # Add the blocking_question chip — AND-combined with
            # plan_review there are zero matching rows.
            await pilot.press("b")
            await pilot.pause()
            assert filter_app._filter_blocking is True
            assert _visible_titles(filter_app) == []
    _run(body())


# ---------------------------------------------------------------------------
# 6. `c` clears every chip + the typed text
# ---------------------------------------------------------------------------


def test_c_clears_all_filters(filter_env, filter_app) -> None:
    async def body() -> None:
        async with filter_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            filter_app.filter_input.value = "shipped"
            await pilot.pause()
            assert filter_app._has_active_filters() is True

            # Esc the input first so `c` lands as an action (not a
            # character into the Input).
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            assert filter_app._has_active_filters() is False
            assert filter_app._filter_unread_only is False
            assert filter_app._filter_plan_review is False
            assert filter_app._filter_text == ""
            # Full list is visible again.
            assert len(_visible_titles(filter_app)) == 5
    _run(body())


# ---------------------------------------------------------------------------
# 7. Empty-match state renders the friendly hint
# ---------------------------------------------------------------------------


def test_empty_match_state_shows_friendly_message(
    filter_env, filter_app,
) -> None:
    async def body() -> None:
        async with filter_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            filter_app.filter_input.value = "zzz_definitely_no_hit_zzz"
            await pilot.pause()

            assert _visible_titles(filter_app) == []
            detail_text = str(filter_app.detail.render())
            assert "No matches" in detail_text
            # Hint mentions ``c`` so the user knows the way out.
            assert "c" in detail_text
    _run(body())


# ---------------------------------------------------------------------------
# 8. Filters do NOT persist across PollyInboxApp remounts (session-scoped)
# ---------------------------------------------------------------------------


def test_filters_session_scoped_across_remounts(filter_env) -> None:
    async def body() -> None:
        from pollypm.cockpit_ui import PollyInboxApp
        # First mount — set a couple of filters.
        app1 = PollyInboxApp(filter_env["config_path"])
        async with app1.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert app1._filter_unread_only is True
            assert app1._filter_plan_review is True

        # Second mount — fresh instance must start with everything clear.
        app2 = PollyInboxApp(filter_env["config_path"])
        async with app2.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert app2._filter_unread_only is False
            assert app2._filter_plan_review is False
            assert app2._filter_text == ""
            assert app2._filter_project is None
            assert app2._has_active_filters() is False
            # Full list is visible — nothing has been narrowed.
            assert len(_visible_titles(app2)) == 5
    _run(body())
