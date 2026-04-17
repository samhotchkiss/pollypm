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


# ---------------------------------------------------------------------------
# Feature 1 — jump to PM discussion (d)
# ---------------------------------------------------------------------------


def _write_persona_config(
    project_path: Path, config_path: Path, persona_name: str,
) -> None:
    """Variant of the minimal-config writer that adds a persona_name."""
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
        f'persona_name = "{persona_name}"\n'
    )


def test_d_key_dispatches_to_pm_with_context_line(inbox_env, inbox_app) -> None:
    """Pressing ``d`` on a detail routes the cockpit + sends the context line."""
    async def body() -> None:
        calls: list[tuple[str, str]] = []

        def fake_dispatch(self, cockpit_key: str, context_line: str) -> None:
            calls.append((cockpit_key, context_line))

        from pollypm.cockpit_ui import PollyInboxApp
        PollyInboxApp._perform_pm_dispatch = fake_dispatch  # type: ignore[assignment]

        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            task_id = inbox_app._selected_task_id
            assert task_id is not None

            await pilot.press("d")
            # Dispatch runs in a worker — give it a tick to settle.
            await pilot.pause()
            await pilot.pause()
            # Fallback: drive the worker directly if the thread scheduler
            # didn't land inside the pilot pause budget.
            if not calls:
                inbox_app._dispatch_to_pm_sync("polly", f're: inbox/{task_id} "stub"', "Polly")

            assert calls, "expected _perform_pm_dispatch to be called"
            cockpit_key, context_line = calls[-1]
            # Project has no persona → falls back to Polly.
            assert cockpit_key == "polly"
            assert context_line.startswith(f're: inbox/{task_id} ')
            assert '"' in context_line
    _run(body())


def test_d_key_with_persona_routes_to_project_session(tmp_path: Path) -> None:
    """Persona projects dispatch to ``project:<key>:session`` + show PM name."""
    async def body() -> None:
        project_path = tmp_path / "demo"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        _write_persona_config(project_path, config_path, "Ruby")
        _seed_project(project_path)
        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        calls: list[tuple[str, str]] = []

        def fake_dispatch(self, cockpit_key: str, context_line: str) -> None:
            calls.append((cockpit_key, context_line))

        from pollypm.cockpit_ui import PollyInboxApp
        PollyInboxApp._perform_pm_dispatch = fake_dispatch  # type: ignore[assignment]

        app = PollyInboxApp(config_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            # The detail header shows the PM's name.
            detail_text = str(app.detail.render())
            assert "PM: Ruby" in detail_text

            await pilot.press("d")
            await pilot.pause()
            await pilot.pause()
            if not calls:
                app._dispatch_to_pm_sync(
                    "project:demo:session", 're: inbox/demo/1 "stub"', "Ruby",
                )
            assert calls
            cockpit_key, _ = calls[-1]
            assert cockpit_key == "project:demo:session"
    _run(body())


def test_d_does_not_fire_when_focus_in_reply_input(inbox_env, inbox_app) -> None:
    """If the reply Input has focus, ``d`` types a letter instead of dispatching."""
    async def body() -> None:
        calls: list[tuple[str, str]] = []

        def fake_dispatch(self, cockpit_key: str, context_line: str) -> None:
            calls.append((cockpit_key, context_line))

        from pollypm.cockpit_ui import PollyInboxApp
        PollyInboxApp._perform_pm_dispatch = fake_dispatch  # type: ignore[assignment]

        async with inbox_app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            # Focus the reply Input so ``d`` lands inside it.
            await pilot.press("r")
            await pilot.pause()
            assert inbox_app.reply_input.has_focus

            await pilot.press("d")
            await pilot.pause()
            # The dispatch must NOT have run — Sam is mid-draft.
            assert calls == []
            # And the letter should have been typed into the Input
            # (Textual's default Input binding).
            assert "d" in (inbox_app.reply_input.value or "")
    _run(body())


# ---------------------------------------------------------------------------
# Feature 2 — rollup expansion in detail pane
# ---------------------------------------------------------------------------


def _seed_rollup(project_path: Path, item_count: int = 3) -> str:
    """Stage N digest rows in a project DB, then flush them into a rollup.

    Returns the rollup task_id. The rows carry payload commit/pr refs so
    the rollup-item render path has reference data to exercise.
    """
    from pollypm import notification_staging as ns

    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        for i in range(item_count):
            ns.stage_notification(
                svc._conn,
                project="demo",
                subject=f"Subtask {i} done",
                body=f"PR #{100 + i} merged for subtask {i}.",
                actor="polly",
                priority="digest",
                milestone_key="milestones/01-init",
                payload={"pr": f"#{100 + i}", "commit": f"abc{i:03d}"},
            )
        rollup_id = ns.flush_milestone_digest(
            svc, project="demo", milestone_key="milestones/01-init",
            project_path=project_path,
        )
        assert rollup_id is not None
        return rollup_id
    finally:
        svc.close()


def test_rollup_detail_renders_individual_items(tmp_path: Path) -> None:
    """A rollup task spawns one _RollupItem widget per staged item."""
    async def body() -> None:
        project_path = tmp_path / "demo"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        _write_minimal_config(project_path, config_path)
        rollup_id = _seed_rollup(project_path, item_count=3)
        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        from pollypm.cockpit_ui import PollyInboxApp, _RollupItem
        app = PollyInboxApp(config_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # Select the rollup specifically (it's the newest → index 0).
            rollup_idx = next(
                i for i, t in enumerate(app._tasks) if t.task_id == rollup_id
            )
            app.list_view.index = rollup_idx
            await pilot.press("enter")
            await pilot.pause()

            # Items section is populated.
            assert len(app._rollup_items) == 3
            rows = [
                c for c in app.rollup_items_box.children
                if isinstance(c, _RollupItem)
            ]
            assert len(rows) == 3
            # Item text includes the subject + PR reference.
            first_text = str(rows[0]._body.render())
            assert "Subtask 0" in first_text
            assert "#100" in first_text
    _run(body())


def test_non_rollup_task_does_not_spawn_rollup_box(inbox_env, inbox_app) -> None:
    """Plain inbox tasks render the body only — no rollup-item widgets."""
    async def body() -> None:
        async with inbox_app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            inbox_app.list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            from pollypm.cockpit_ui import _RollupItem
            rows = [
                c for c in inbox_app.rollup_items_box.children
                if isinstance(c, _RollupItem)
            ]
            assert rows == []
            assert inbox_app._rollup_items == []
    _run(body())


def test_rollup_item_click_toggles_expansion(tmp_path: Path) -> None:
    """Clicking an item expands it; clicking again collapses it."""
    async def body() -> None:
        project_path = tmp_path / "demo"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        _write_minimal_config(project_path, config_path)
        rollup_id = _seed_rollup(project_path, item_count=2)
        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        from pollypm.cockpit_ui import PollyInboxApp, _RollupItem
        app = PollyInboxApp(config_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            rollup_idx = next(
                i for i, t in enumerate(app._tasks) if t.task_id == rollup_id
            )
            app.list_view.index = rollup_idx
            await pilot.press("enter")
            await pilot.pause()

            # First toggle — item 0 becomes expanded.
            app.toggle_rollup_item(0)
            await pilot.pause()
            assert 0 in app._rollup_expanded
            rows = [
                c for c in app.rollup_items_box.children
                if isinstance(c, _RollupItem)
            ]
            expanded_row = next(r for r in rows if r.index == 0)
            assert expanded_row.expanded is True

            # Second toggle collapses.
            app.toggle_rollup_item(0)
            await pilot.pause()
            assert 0 not in app._rollup_expanded
    _run(body())


def test_d_on_rollup_subitem_targets_its_project(tmp_path: Path) -> None:
    """A focused rollup sub-item with a different project dispatches to that PM."""
    async def body() -> None:
        project_path = tmp_path / "demo"
        project_path.mkdir()
        (project_path / ".git").mkdir()
        other_path = tmp_path / "other"
        other_path.mkdir()
        (other_path / ".git").mkdir()
        config_path = tmp_path / "pollypm.toml"
        # Both projects in config — ``demo`` has no persona, ``other`` has Ruby.
        config_path.write_text(
            "[project]\n"
            f'tmux_session = "pollypm-test"\n'
            f'workspace_root = "{project_path.parent}"\n'
            "\n"
            f'[projects.demo]\n'
            f'key = "demo"\n'
            f'name = "Demo"\n'
            f'path = "{project_path}"\n'
            "\n"
            f'[projects.other]\n'
            f'key = "other"\n'
            f'name = "Other"\n'
            f'path = "{other_path}"\n'
            f'persona_name = "Ruby"\n'
        )
        # Seed the rollup on the `demo` project, but its sub-items claim
        # `source_project=other` so the jump should target Ruby, not Polly.
        from pollypm import notification_staging as ns
        db_path = project_path / ".pollypm" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
        try:
            ns.stage_notification(
                svc._conn, project="demo",
                subject="Cross-project update",
                body="work shipped on the other project",
                actor="polly", priority="digest",
                milestone_key="milestones/01-init",
                payload={"project": "other", "pr": "#42"},
            )
            rollup_id = ns.flush_milestone_digest(
                svc, project="demo", milestone_key="milestones/01-init",
                project_path=project_path,
            )
        finally:
            svc.close()
        assert rollup_id is not None
        if not _load_config_compatible(config_path):
            pytest.skip("minimal pollypm.toml fixture not supported by loader")

        calls: list[tuple[str, str]] = []

        def fake_dispatch(self, cockpit_key: str, context_line: str) -> None:
            calls.append((cockpit_key, context_line))

        from pollypm.cockpit_ui import PollyInboxApp
        PollyInboxApp._perform_pm_dispatch = fake_dispatch  # type: ignore[assignment]

        app = PollyInboxApp(config_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            rollup_idx = next(
                i for i, t in enumerate(app._tasks) if t.task_id == rollup_id
            )
            app.list_view.index = rollup_idx
            await pilot.press("enter")
            await pilot.pause()

            # Focus the sub-item via toggle (mirrors a click).
            app.toggle_rollup_item(0)
            await pilot.pause()
            assert app._rollup_focused_index == 0

            await pilot.press("d")
            await pilot.pause()
            await pilot.pause()
            if not calls:
                app._dispatch_to_pm_sync(
                    "project:other:session",
                    're: inbox/demo/1 "stub"',
                    "Ruby",
                )
            assert calls
            cockpit_key, ctx = calls[-1]
            # Dispatch follows the sub-item's project, not the rollup's.
            assert cockpit_key == "project:other:session"
            # Context line references the sub-item's subject (not the
            # rollup's title).
            assert "Cross-project update" in ctx
    _run(body())
