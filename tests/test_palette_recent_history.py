"""Targeted tests for the command-palette recent-history UX.

The rest of the palette surface lives in
``tests/test_command_palette.py`` — this file sticks to the empty-query
"Recent" section and its supporting history plumbing so each case stays
fast and isolated.

Run targeted (not the whole suite)::

    HOME=/tmp/pytest-agent-palhistory uv run pytest \\
        tests/test_palette_recent_history.py \\
        tests/test_command_palette.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pollypm.cockpit import PaletteCommand
from pollypm.cockpit_ui import (
    _PaletteListItem,
    _PaletteSectionHeader,
    _palette_history,
    _record_palette_command,
    _resolve_recent_commands,
)
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fixtures — mirror the lightweight single-project setup used by the
# existing command-palette tests so the two files share a mental model.
# ---------------------------------------------------------------------------


def _write_single_project_config(project_path: Path, config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[project]\n"
        'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{project_path.parent}"\n'
        "\n"
        "[projects.demo]\n"
        'key = "demo"\n'
        'name = "Demo"\n'
        f'path = "{project_path}"\n'
    )


def _seed_project_db(project_path: Path) -> None:
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
        svc.create(
            title="Palette history smoke",
            description="body",
            type="task",
            project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            priority="normal",
            created_by="polly",
        )


@pytest.fixture
def single_project_config(tmp_path: Path) -> Path:
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_single_project_config(project_path, config_path)
    _seed_project_db(project_path)
    return config_path


def _load_config_compatible(config_path: Path) -> bool:
    try:
        from pollypm.config import load_config
        cfg = load_config(config_path)
        return len(getattr(cfg, "projects", {})) > 0
    except Exception:  # noqa: BLE001
        return False


def _run(coro) -> None:
    asyncio.run(coro)


def _palette_cmd(tag: str, title: str = "") -> PaletteCommand:
    """Construct a throwaway PaletteCommand for the unit-level tests."""
    return PaletteCommand(
        title=title or tag,
        subtitle="",
        category="Navigation",
        keybind=None,
        tag=tag,
    )


# ---------------------------------------------------------------------------
# Helper-layer tests — no Textual, run in microseconds.
# ---------------------------------------------------------------------------


def test_resolve_recent_commands_drops_unknown_tags() -> None:
    """Tags that aren't in the current palette scope are silently dropped."""
    commands = [_palette_cmd("nav.inbox"), _palette_cmd("nav.workers")]
    resolved = _resolve_recent_commands(
        commands, ["nav.inbox", "nav.missing", "nav.workers"], limit=5,
    )
    assert [c.tag for c in resolved] == ["nav.inbox", "nav.workers"]


def test_resolve_recent_commands_respects_limit() -> None:
    """Limit caps the resolved list before the full history is walked."""
    commands = [_palette_cmd(f"nav.{i}") for i in range(10)]
    recent_tags = [f"nav.{i}" for i in range(10)]
    resolved = _resolve_recent_commands(commands, recent_tags, limit=3)
    assert [c.tag for c in resolved] == ["nav.0", "nav.1", "nav.2"]


def test_record_palette_command_dedupes_preserving_most_recent() -> None:
    """Running the same tag twice keeps it once, at the most-recent slot."""
    class _Stub:
        pass
    app = _Stub()
    _record_palette_command(app, "nav.inbox")
    _record_palette_command(app, "nav.workers")
    _record_palette_command(app, "nav.inbox")  # re-run
    history = _palette_history(app)
    assert list(history) == ["nav.workers", "nav.inbox"]


def test_record_palette_command_caps_history_at_ten() -> None:
    """More than 10 distinct runs drops the oldest (deque maxlen)."""
    class _Stub:
        pass
    app = _Stub()
    for i in range(15):
        _record_palette_command(app, f"nav.cmd-{i}")
    history = _palette_history(app)
    assert len(history) == 10
    # Oldest five were evicted; the 10 most-recent remain in order.
    assert list(history) == [f"nav.cmd-{i}" for i in range(5, 15)]


def test_record_palette_command_ignores_empty_tag() -> None:
    """None/empty dispatches must not inflate the history."""
    class _Stub:
        pass
    app = _Stub()
    _record_palette_command(app, "")
    assert not _palette_history(app)


# ---------------------------------------------------------------------------
# Modal-layer tests — drive the modal directly so we don't need the full
# cockpit rail. These bypass ``run_test`` since we only need the pure
# rendering contract (``_visible`` / ``_row_kinds``); the UI plumbing is
# already covered by ``tests/test_command_palette.py``.
# ---------------------------------------------------------------------------


def test_empty_history_empty_query_shows_only_full_list(
    single_project_config: Path,
) -> None:
    """No Recent section renders when the history is empty."""
    if not _load_config_compatible(single_project_config):
        pytest.skip("config fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(single_project_config)

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("colon")
            await pilot.pause()
            modal = None
            from pollypm.cockpit_ui import CommandPaletteModal as _M
            for screen in app.screen_stack:
                if isinstance(screen, _M):
                    modal = screen
                    break
            assert modal is not None
            # Recent resolved list is empty → no header rows rendered.
            assert modal._recent_commands == []
            assert all(kind == "item" for kind in modal._row_kinds)
            # _visible exactly matches the full command set (no
            # duplicated Recent entries).
            assert len(modal._visible) == len(modal._all_commands)
    _run(body())


def test_three_distinct_commands_reopen_shows_recent_section(
    single_project_config: Path,
) -> None:
    """Running 3 distinct commands then reopening renders them in reverse order."""
    if not _load_config_compatible(single_project_config):
        pytest.skip("config fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(single_project_config)

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Seed the App's history in the same order the user would:
            # inbox → workers → activity. Most-recent-first is
            # activity, workers, inbox.
            _record_palette_command(app, "nav.inbox")
            _record_palette_command(app, "nav.workers")
            _record_palette_command(app, "nav.activity")

            await pilot.press("colon")
            await pilot.pause()
            from pollypm.cockpit_ui import CommandPaletteModal as _M
            modal = next(
                (s for s in app.screen_stack if isinstance(s, _M)), None,
            )
            assert modal is not None
            # Recent section rendered, in reverse-chronological order.
            tags = [c.tag for c in modal._recent_commands]
            assert tags == ["nav.activity", "nav.workers", "nav.inbox"]
            # Two headers rendered (Recent + All commands).
            assert modal._row_kinds.count("header") == 2
            # First rendered item is the most recent command — Enter
            # would re-run it.
            first_item_idx = modal._row_kinds.index("item")
            assert modal.list_view.index == first_item_idx
            assert modal._visible[0].tag == "nav.activity"
    _run(body())


def test_running_same_command_twice_dedupes_in_recent(
    single_project_config: Path,
) -> None:
    """Recent shows a repeated command once, not twice."""
    if not _load_config_compatible(single_project_config):
        pytest.skip("config fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(single_project_config)

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _record_palette_command(app, "nav.inbox")
            _record_palette_command(app, "nav.workers")
            _record_palette_command(app, "nav.inbox")  # repeat

            await pilot.press("colon")
            await pilot.pause()
            from pollypm.cockpit_ui import CommandPaletteModal as _M
            modal = next(
                (s for s in app.screen_stack if isinstance(s, _M)), None,
            )
            assert modal is not None
            tags = [c.tag for c in modal._recent_commands]
            assert tags == ["nav.inbox", "nav.workers"], (
                f"expected dedup with inbox moved to most-recent; got {tags}"
            )
    _run(body())


def test_recent_section_caps_at_five_even_when_history_is_ten(
    single_project_config: Path,
) -> None:
    """History stores up to 10, but the Recent section only renders 5."""
    if not _load_config_compatible(single_project_config):
        pytest.skip("config fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(single_project_config)

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # 12 distinct commands → history keeps the last 10; Recent
            # still only surfaces 5 in the modal.
            tags = [
                "nav.inbox", "nav.workers", "nav.activity", "nav.metrics",
                "nav.settings", "nav.dashboard", "session.refresh",
                "session.restart", "session.shortcuts", "system.doctor",
                "system.edit_config", "inbox.notify",
            ]
            for tag in tags:
                _record_palette_command(app, tag)
            assert len(_palette_history(app)) == 10

            await pilot.press("colon")
            await pilot.pause()
            from pollypm.cockpit_ui import CommandPaletteModal as _M
            modal = next(
                (s for s in app.screen_stack if isinstance(s, _M)), None,
            )
            assert modal is not None
            assert len(modal._recent_commands) == 5
            # Most-recent-first — the last tag pushed is the first row.
            assert modal._recent_commands[0].tag == "inbox.notify"
    _run(body())


def test_non_empty_query_hides_recent_section(
    single_project_config: Path,
) -> None:
    """Typing any query collapses the palette back to a single section."""
    if not _load_config_compatible(single_project_config):
        pytest.skip("config fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(single_project_config)

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _record_palette_command(app, "nav.inbox")
            _record_palette_command(app, "nav.workers")

            await pilot.press("colon")
            await pilot.pause()
            from pollypm.cockpit_ui import CommandPaletteModal as _M
            modal = next(
                (s for s in app.screen_stack if isinstance(s, _M)), None,
            )
            assert modal is not None
            # Sanity: empty-query open rendered Recent.
            assert modal._row_kinds.count("header") == 2
            # Now type — recent section must disappear.
            modal._filter("inbox")
            await pilot.pause()
            assert modal._row_kinds.count("header") == 0, (
                f"expected no headers while filtering; "
                f"kinds={modal._row_kinds}"
            )
            # Every _visible entry is a fresh filter match against the
            # full list — not pulled from Recent.
            for cmd in modal._visible:
                assert "inbox" in cmd.haystack()
            # Clearing the query brings Recent back.
            modal._filter("")
            await pilot.pause()
            assert modal._row_kinds.count("header") == 2
    _run(body())


def test_history_is_session_scoped_new_app_starts_empty(
    single_project_config: Path,
) -> None:
    """A fresh PollyCockpitApp has no recent history (no persistence)."""
    if not _load_config_compatible(single_project_config):
        pytest.skip("config fixture not supported by loader")
    from pollypm.cockpit_ui import PollyCockpitApp, PollyInboxApp

    # First App instance: record something.
    first = PollyInboxApp(single_project_config)
    _record_palette_command(first, "nav.inbox")
    _record_palette_command(first, "nav.workers")
    assert len(_palette_history(first)) == 2

    # Second instance: history starts empty because it's stored on the
    # instance, not persisted. Covers the session-scoping promise in the
    # brief.
    second = PollyInboxApp(single_project_config)
    assert len(_palette_history(second)) == 0

    # Extra coverage: the cockpit rail App shares the same plumbing.
    rail = PollyCockpitApp(single_project_config)
    assert len(_palette_history(rail)) == 0


def test_section_header_is_disabled_listitem() -> None:
    """Header rows are marked disabled so arrow keys skip them."""
    header = _PaletteSectionHeader("Recent")
    assert header.disabled is True
    # It's a ListItem but NOT a _PaletteListItem — this is how the
    # click-dispatch handler ignores it.
    from textual.widgets import ListItem
    assert isinstance(header, ListItem)
    assert not isinstance(header, _PaletteListItem)
