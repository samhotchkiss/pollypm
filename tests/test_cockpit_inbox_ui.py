"""Textual UI tests for the cockpit inbox screen.

Drives :class:`pollypm.cockpit_ui.PollyInboxApp` via ``Pilot`` to assert
the full interactive loop: navigate with arrows, open a message, reply,
archive, and verify the underlying work-service state after each action.

Skipped when the minimum-viable config layer can't stub a single-project
cockpit (e.g. platform-specific config bootstrap failures) so a CI
environment without tmux doesn't block on unrelated infra.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Config fixture — a minimal "one project" cockpit config with a real DB
# ---------------------------------------------------------------------------


def _write_minimal_config(project_path: Path, config_path: Path) -> None:
    """Emit a pollypm.toml pointing at a single-project workspace.

    Must mirror the layout the cockpit loader expects: a workspace root
    with a ``[pollypm.projects.<key>]`` block pointing at a folder that
    has a ``.pollypm/state.db`` file we just seeded with inbox rows.
    """
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


def _seed_project(project_path: Path) -> list[str]:
    """Create a few inbox tasks in a project-root state.db. Returns task_ids."""
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        ids: list[str] = []
        for title, body in [
            ("Smoke subject", "Smoke body"),
            ("Deploy blocked", "Verify email click."),
            ("Homepage rewrite", "Review please."),
        ]:
            t = svc.create(
                title=title,
                description=body,
                type="task",
                project="demo",
                flow_template="chat",
                roles={"requester": "user", "operator": "polly"},
                priority="normal",
                created_by="polly",
            )
            ids.append(t.task_id)
        return ids
    finally:
        svc.close()


@pytest.fixture
def inbox_env(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()  # pretend git repo
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    ids = _seed_project(project_path)
    return {
        "config_path": config_path,
        "project_path": project_path,
        "task_ids": ids,
    }


def _load_config_compatible(config_path: Path) -> bool:
    """Skip the suite if config loader rejects our minimal TOML shape."""
    try:
        from pollypm.config import load_config
        cfg = load_config(config_path)
        return "demo" in getattr(cfg, "projects", {})
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def inbox_app(inbox_env):
    if not _load_config_compatible(inbox_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    return PollyInboxApp(inbox_env["config_path"])


# ---------------------------------------------------------------------------
# Pilot tests
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async test body under asyncio, matching onboarding tests."""
    asyncio.run(coro)


def test_inbox_lists_seeded_messages(inbox_env, inbox_app) -> None:
    """On mount, every seeded inbox task shows up in the left list."""
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert len(inbox_app._tasks) == len(inbox_env["task_ids"])
            # All three are unread on first load — nothing has a read marker.
            assert len(inbox_app._unread_ids) == len(inbox_env["task_ids"])
    _run(body())


def test_list_row_renders_title_on_line1_and_project_age_on_line2(
    inbox_env, inbox_app,
) -> None:
    """Each row is two lines: bold title, then dim ``project · age``.

    The sender (always "polly") must NOT appear in the row; project key
    is more useful across a multi-project workspace.
    """
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from pollypm.cockpit_ui import _InboxListItem, _format_inbox_row
            rows = [
                child for child in inbox_app.list_view.children
                if isinstance(child, _InboxListItem)
            ]
            assert rows, "expected at least one inbox row"
            first = rows[0]
            # Re-derive the Rich Text from the public helper so we're not
            # probing Textual's private Static internals.
            rendered = _format_inbox_row(first.task_ref, is_unread=first.is_unread)
            plain = rendered.plain
            # Two lines — title then metadata.
            lines = plain.split("\n")
            assert len(lines) == 2, f"expected 2 lines, got: {lines!r}"
            # Line 1 holds the subject (one of the seeded titles).
            assert any(
                s in lines[0] for s in ("Smoke", "Deploy", "Homepage")
            ), f"expected a seeded title on line 1, got: {lines[0]!r}"
            # Line 2 has the project key; never the sender "polly".
            assert "demo" in lines[1]
            assert "polly" not in lines[1].lower()
            # Age is relative — "just now" is fine for a freshly-seeded row.
            assert "\u00b7" in lines[1] or "ago" in lines[1] or "now" in lines[1]
    _run(body())


def test_selecting_a_row_renders_detail_and_clears_unread(inbox_env, inbox_app) -> None:
    """Keyboard navigation opens the message and records a read marker."""
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Land on the first row and trigger an open.
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            selected = inbox_app._selected_task_id
            assert selected is not None
            # Detail renders the subject (plain text, markup-stripped).
            detail_text = str(inbox_app.detail.render())
            assert any(s in detail_text for s in ("Smoke subject", "Deploy", "Homepage"))
            # The row is no longer unread locally…
            assert selected not in inbox_app._unread_ids
            # …and the work-service has a read marker for that task.
            svc = inbox_app._svc_for_task(selected)
            try:
                reads = svc.get_context(selected, entry_type="read")
            finally:
                svc.close()
            assert len(reads) == 1
    _run(body())


def test_reply_input_is_always_present_on_detail_open(inbox_env, inbox_app) -> None:
    """Reply Input is visible from mount — not gated by pressing ``r``.

    The list keeps focus on mount so j/k still works; the user must
    explicitly press ``r`` (or Tab/click) to land in the reply box.
    """
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Reply Input exists in the widget tree from first paint.
            assert inbox_app.reply_input.is_mounted
            # It is visually shown (always-visible design — no toggle class).
            assert inbox_app.reply_input.display is not False
            # The list, not the reply box, has focus on mount.
            assert inbox_app.list_view.has_focus
            assert not inbox_app.reply_input.has_focus
    _run(body())


def test_r_shortcut_focuses_reply_without_toggling_visibility(
    inbox_env, inbox_app,
) -> None:
    """Pressing ``r`` focuses the already-visible reply box."""
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            assert not inbox_app.reply_input.has_focus
            await pilot.press("r")
            await pilot.pause()
            assert inbox_app.reply_input.has_focus
    _run(body())


def test_esc_from_reply_returns_focus_to_list(inbox_env, inbox_app) -> None:
    """Esc inside the reply box hands focus back to the list (no exit)."""
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            assert inbox_app.reply_input.has_focus
            await pilot.press("escape")
            await pilot.pause()
            # Focus is back on the list and the app is still running.
            assert inbox_app.list_view.has_focus
            assert not inbox_app.reply_input.has_focus
    _run(body())


def test_reply_flow_persists_and_appears_in_thread(inbox_env, inbox_app) -> None:
    """Typing in the always-visible reply + Enter posts and clears the input."""
    async def body() -> None:
        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            task_id = inbox_app._selected_task_id
            assert task_id is not None

            # Focus the reply box via the keyboard shortcut.
            await pilot.press("r")
            await pilot.pause()
            assert inbox_app.reply_input.has_focus

            # Type a reply and submit.
            inbox_app.reply_input.value = "Got it, thanks"
            await pilot.press("enter")
            await pilot.pause()

            # Input is cleared and focus returns to the list.
            assert inbox_app.reply_input.value == ""
            assert inbox_app.list_view.has_focus

            # The reply is persisted as a reply context row.
            svc = inbox_app._svc_for_task(task_id)
            try:
                replies = svc.list_replies(task_id)
            finally:
                svc.close()
            assert [e.text for e in replies] == ["Got it, thanks"]
            assert replies[0].actor == "user"
            assert replies[0].entry_type == "reply"

            # Detail pane re-renders with the reply visible in-thread.
            rendered = str(inbox_app.detail.render())
            assert "Got it, thanks" in rendered
    _run(body())


def test_archive_removes_row_and_flips_status(inbox_env, inbox_app) -> None:
    """a archives the selected message; it disappears from the list."""
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            initial_total = len(inbox_app._tasks)
            assert initial_total >= 1

            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            target = inbox_app._selected_task_id
            assert target is not None

            await pilot.press("a")
            await pilot.pause()

            # List shrinks by one and the archived task is gone.
            assert len(inbox_app._tasks) == initial_total - 1
            assert all(t.task_id != target for t in inbox_app._tasks)

            # Work status is DONE in the underlying DB.
            svc = inbox_app._svc_for_task(target)
            try:
                task = svc.get(target)
            finally:
                svc.close()
            assert task.work_status.value == "done"
    _run(body())


def test_empty_state_message_when_no_inbox(tmp_path: Path) -> None:
    """An inbox with zero messages shows the friendly empty-state copy."""
    async def body() -> None:
        project_path = tmp_path / "empty"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        _write_minimal_config(project_path, config_path)
        # Create the DB but don't seed any tasks.
        db_path = project_path / ".pollypm" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
        svc.close()

        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        from pollypm.cockpit_ui import PollyInboxApp
        app = PollyInboxApp(config_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._tasks == []
            detail_text = str(app.detail.render())
            assert "No messages" in detail_text
            assert "Polly" in detail_text
    _run(body())
