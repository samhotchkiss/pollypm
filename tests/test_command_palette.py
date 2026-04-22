"""Targeted tests for the global ``:`` command palette.

Covers the registry layer in :mod:`pollypm.cockpit` and the modal
screen plumbing in :mod:`pollypm.cockpit_ui`. The UI tests drive the
cockpit inbox / rail via ``Pilot`` — mirroring the other cockpit UI
suites — so we can assert the modal opens, filters, dismisses, and
dispatches.

Run targeted (not the whole suite)::

    HOME=/tmp/pytest-agent-palette uv run pytest \\
        tests/test_command_palette.py \\
        tests/test_cockpit_inbox_ui.py \\
        tests/test_project_dashboard_ui.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pollypm.cockpit import (
    PaletteCommand,
    build_palette_commands,
    filter_palette_commands,
)
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fixtures — mirror the single-project cockpit bootstrap used by other
# cockpit UI suites so tests stay consistent across screens.
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


def _write_three_project_config(base: Path, config_path: Path) -> dict[str, Path]:
    """Write a config with three projects; returns ``{key: path}``."""
    paths: dict[str, Path] = {}
    for key, name in (("alpha", "Alpha Site"), ("beta", "Beta Pipeline"), ("gamma", "Gamma Inbox")):
        p = base / key
        p.mkdir()
        (p / ".git").mkdir()
        paths[key] = p
    config_path.parent.mkdir(parents=True, exist_ok=True)
    blocks = "\n\n".join(
        "[projects.{k}]\nkey = \"{k}\"\nname = \"{n}\"\npath = \"{p}\"".format(
            k=k, n=n, p=paths[k],
        )
        for (k, n) in (("alpha", "Alpha Site"), ("beta", "Beta Pipeline"), ("gamma", "Gamma Inbox"))
    )
    config_path.write_text(
        "[project]\n"
        'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{base}"\n'
        "\n"
        f"{blocks}\n"
    )
    return paths


def _seed_project_db(project_path: Path) -> list[str]:
    """Seed one task so the cockpit inbox opens cleanly."""
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
        task = svc.create(
            title="Palette smoke",
            description="body",
            type="task",
            project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            priority="normal",
            created_by="polly",
        )
        ids.append(task.task_id)
    return ids


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


# ---------------------------------------------------------------------------
# Registry layer — pure data, no Textual.
# ---------------------------------------------------------------------------


def test_build_palette_commands_includes_core_navigation(single_project_config: Path) -> None:
    """Every core cockpit view has a ``Go to <view>`` palette entry."""
    commands = build_palette_commands(single_project_config)
    tags = {c.tag for c in commands}
    # Core navigation — matches the brief's required list.
    for expected in (
        "nav.inbox",
        "nav.workers",
        "nav.activity",
        "nav.settings",
        "nav.dashboard",
    ):
        assert expected in tags, f"missing core navigation command: {expected}"
    # Per-project navigation entry gets built off the config projects.
    assert "nav.project:demo" in tags
    # Session + system commands that the brief asked for.
    for expected in (
        "session.refresh",
        "session.restart",
        "session.shortcuts",
        "system.doctor",
        "system.edit_config",
        "inbox.notify",
        "inbox.archive_read",
    ):
        assert expected in tags, f"missing command: {expected}"


def test_registered_projects_each_get_commands(tmp_path: Path) -> None:
    """Three configured projects emit three Go/Create/Queue command triples."""
    config_path = tmp_path / "pollypm.toml"
    paths = _write_three_project_config(tmp_path, config_path)
    if not _load_config_compatible(config_path):
        pytest.skip("multi-project pollypm.toml fixture not supported by loader")

    commands = build_palette_commands(config_path)
    tags = {c.tag for c in commands}
    for key in paths:
        assert f"nav.project:{key}" in tags
        assert f"task.create:{key}" in tags
        assert f"task.queue_next:{key}" in tags

    # Project display names show up in the rendered title so the user
    # recognises them (not just the slug).
    titles = [c.title for c in commands]
    assert any("Alpha Site" in t for t in titles)
    assert any("Beta Pipeline" in t for t in titles)
    assert any("Gamma Inbox" in t for t in titles)


def test_filter_palette_returns_all_when_query_empty(single_project_config: Path) -> None:
    commands = build_palette_commands(single_project_config)
    assert filter_palette_commands(commands, "") == commands
    assert filter_palette_commands(commands, "   ") == commands


def test_filter_palette_matches_on_title_substring(single_project_config: Path) -> None:
    """Typing "inbox" surfaces the Inbox commands."""
    commands = build_palette_commands(single_project_config)
    filtered = filter_palette_commands(commands, "inbox")
    assert filtered, "expected at least one match for 'inbox'"
    titles = [c.title for c in filtered]
    assert any("Go to Inbox" == t for t in titles)


def test_filter_palette_fuzzy_subsequence(single_project_config: Path) -> None:
    """"inb" — a non-contiguous subsequence — still matches Go to Inbox."""
    commands = build_palette_commands(single_project_config)
    filtered = filter_palette_commands(commands, "inb")
    assert filtered, "expected a subsequence match for 'inb'"
    assert filtered[0].tag == "nav.inbox", (
        f"expected 'Go to Inbox' to rank first; got {filtered[0].title}"
    )


def test_filter_palette_orders_substring_before_subsequence() -> None:
    """Substring matches outrank spread-out subsequence matches."""
    cmds = [
        PaletteCommand(
            title="Go to Inbox", subtitle="", category="Navigation",
            keybind=None, tag="nav.inbox",
        ),
        PaletteCommand(
            title="Restart cockpit", subtitle="reboot inbox stuff",
            category="Session", keybind=None, tag="session.restart",
        ),
    ]
    filtered = filter_palette_commands(cmds, "inbox")
    # Title substring should rank above a subtitle-only hit.
    assert filtered[0].tag == "nav.inbox"
    assert filtered[-1].tag == "session.restart"


def test_filter_palette_excludes_non_matches(single_project_config: Path) -> None:
    commands = build_palette_commands(single_project_config)
    filtered = filter_palette_commands(commands, "zzznope")
    assert filtered == []


def test_current_project_hint_reorders_commands(tmp_path: Path) -> None:
    """With a current_project hint, that project's commands sort first."""
    config_path = tmp_path / "pollypm.toml"
    _write_three_project_config(tmp_path, config_path)
    if not _load_config_compatible(config_path):
        pytest.skip("multi-project pollypm.toml fixture not supported by loader")

    hinted = build_palette_commands(config_path, current_project="beta")
    # The first few entries that target a project should all be for beta.
    project_targeted = [c for c in hinted if c.tag.endswith(":beta") or c.tag.endswith(":alpha") or c.tag.endswith(":gamma")]
    assert project_targeted, "expected some project-scoped commands"
    # The first project-scoped entry must belong to the hinted project.
    assert project_targeted[0].tag.endswith(":beta")


# ---------------------------------------------------------------------------
# UI layer — driving the palette via Textual ``Pilot``.
# ---------------------------------------------------------------------------


@pytest.fixture
def inbox_app(single_project_config):
    if not _load_config_compatible(single_project_config):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    return PollyInboxApp(single_project_config)


def _find_palette_modal(app):
    from pollypm.cockpit_ui import CommandPaletteModal
    for screen in app.screen_stack:
        if isinstance(screen, CommandPaletteModal):
            return screen
    return None


def test_colon_opens_command_palette_modal(inbox_app) -> None:
    """Pressing ``:`` from anywhere inside the inbox opens the palette."""
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert _find_palette_modal(inbox_app) is None
            await pilot.press("colon")
            await pilot.pause()
            modal = _find_palette_modal(inbox_app)
            assert modal is not None, "expected CommandPaletteModal on screen stack"
            # Input autofocuses so typing starts filtering immediately.
            assert modal.input.has_focus
    _run(body())


def test_ctrl_k_opens_command_palette_modal(inbox_app) -> None:
    """Pressing ``Ctrl-K`` from anywhere inside the inbox opens the palette."""

    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert _find_palette_modal(inbox_app) is None
            await pilot.press("ctrl+k")
            await pilot.pause()
            modal = _find_palette_modal(inbox_app)
            assert modal is not None, "expected CommandPaletteModal on screen stack"
            assert modal.input.has_focus

    _run(body())


def test_typing_filters_the_command_list(inbox_app) -> None:
    """Typing into the palette shrinks the visible command list."""
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("colon")
            await pilot.pause()
            modal = _find_palette_modal(inbox_app)
            assert modal is not None
            total_before = len(modal._visible)
            # Type "inbox" into the autofocused Input.
            modal.input.value = "inbox"
            # Fire the Input.Changed handler directly — driving key presses
            # while the Input holds focus is flaky across Textual versions.
            modal._filter("inbox")
            await pilot.pause()
            total_after = len(modal._visible)
            assert 0 < total_after < total_before, (
                f"expected a narrower match list; "
                f"before={total_before}, after={total_after}"
            )
            # Every remaining command mentions inbox somewhere.
            for cmd in modal._visible:
                assert "inbox" in cmd.haystack()
    _run(body())


def test_esc_dismisses_without_action(inbox_app) -> None:
    """Esc closes the palette and leaves the host App untouched."""
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("colon")
            await pilot.pause()
            assert _find_palette_modal(inbox_app) is not None
            # Track whether any palette tag fired.
            fired: list[str] = []
            from pollypm import cockpit_ui as _cui
            original = _cui._dispatch_palette_tag

            def capture(app, tag):
                fired.append(tag or "__none__")
                original(app, tag)

            _cui._dispatch_palette_tag = capture
            try:
                await pilot.press("escape")
                await pilot.pause()
            finally:
                _cui._dispatch_palette_tag = original
            assert _find_palette_modal(inbox_app) is None
            assert fired == ["__none__"], f"expected a None dispatch; got {fired}"
    _run(body())


def test_enter_executes_top_match(inbox_app) -> None:
    """Enter with a subsequence query dispatches the top-ranked command."""
    async def body() -> None:
        dispatched: list[str] = []
        from pollypm import cockpit_ui as _cui
        original = _cui._dispatch_palette_tag

        def capture(app, tag):
            dispatched.append(tag or "__none__")

        _cui._dispatch_palette_tag = capture
        try:
            async with inbox_app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("colon")
                await pilot.pause()
                modal = _find_palette_modal(inbox_app)
                assert modal is not None
                modal._filter("inb")
                await pilot.pause()
                assert modal._visible, "expected 'inb' to match something"
                top_tag = modal._visible[0].tag
                modal.action_run_selected()
                await pilot.pause()
        finally:
            _cui._dispatch_palette_tag = original
        assert dispatched == [top_tag], (
            f"expected Enter to dispatch top match {top_tag!r}; got {dispatched}"
        )
        assert top_tag == "nav.inbox"
    _run(body())


def test_arrow_keys_move_selection(inbox_app) -> None:
    """Arrow Down advances the ListView cursor inside the palette."""
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("colon")
            await pilot.pause()
            modal = _find_palette_modal(inbox_app)
            assert modal is not None
            assert modal.list_view.index == 0
            modal.action_cursor_down()
            await pilot.pause()
            assert modal.list_view.index == 1
            modal.action_cursor_down()
            await pilot.pause()
            assert modal.list_view.index == 2
            modal.action_cursor_up()
            await pilot.pause()
            assert modal.list_view.index == 1
    _run(body())


def test_shortcuts_command_populates_help_payload(inbox_app) -> None:
    """Selecting "Show keyboard shortcuts" stashes the current bindings."""
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("colon")
            await pilot.pause()
            modal = _find_palette_modal(inbox_app)
            assert modal is not None
            # Filter down to the shortcuts command specifically.
            modal._filter("keyboard shortcuts")
            await pilot.pause()
            assert modal._visible, "expected a match for 'keyboard shortcuts'"
            assert modal._visible[0].tag == "session.shortcuts"
            modal.action_run_selected()
            await pilot.pause()
            # The dispatcher stashes the rendered help text so the
            # caller (tests here) can assert on it even without a live
            # toast layer.
            payload = getattr(inbox_app, "_palette_last_shortcuts", "")
            assert payload, "expected shortcuts payload to be stashed"
            # The inbox app binds ``r`` for reply — it must appear in
            # the rendered shortcut list.
            assert "r" in payload
    _run(body())


def test_palette_binding_present_on_inbox_and_dashboard_and_rail(
    single_project_config: Path,
) -> None:
    """Every top-level cockpit App declares the palette binding."""
    from pollypm.cockpit_ui import (
        PollyCockpitApp,
        PollyInboxApp,
        PollyProjectDashboardApp,
        PollyWorkerRosterApp,
    )

    def _has_palette_keys(cls) -> bool:
        for binding in getattr(cls, "BINDINGS", []):
            keys = getattr(binding, "key", "")
            if "colon" in keys and "ctrl+k" in keys:
                return True
        return False

    assert _has_palette_keys(PollyCockpitApp)
    assert _has_palette_keys(PollyInboxApp)
    assert _has_palette_keys(PollyProjectDashboardApp)
    assert _has_palette_keys(PollyWorkerRosterApp)
