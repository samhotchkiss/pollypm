"""Targeted tests for the ``?`` keyboard-help overlay.

Drives :class:`pollypm.cockpit_ui.KeyboardHelpModal` plus the
``action_show_keyboard_help`` wiring on every cockpit App. The UI tests
mirror the palette / inbox suites — a single-project cockpit fixture,
Textual Pilot, and a helper that finds the modal on the screen stack.

Run targeted (do NOT run the whole suite)::

    HOME=/tmp/pytest-agent-helpoverlay uv run pytest \\
        tests/test_keyboard_help.py \\
        tests/test_command_palette.py \\
        tests/test_cockpit_inbox_ui.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fixtures — mirror test_command_palette so the helper keeps one shape.
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


def _seed_project_db(
    project_path: Path, *, labels: list[str] | None = None,
) -> str:
    """Seed one task with optional labels; returns its task_id."""
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
        task = svc.create(
            title="Help smoke",
            description="body",
            type="task",
            project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            priority="normal",
            created_by="polly",
            labels=labels or [],
        )
        return task.task_id


@pytest.fixture
def single_project_env(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_single_project_config(project_path, config_path)
    task_id = _seed_project_db(project_path)
    return {
        "config_path": config_path,
        "project_path": project_path,
        "task_id": task_id,
    }


@pytest.fixture
def plan_review_env(tmp_path: Path):
    """Single-project fixture whose lone task carries the plan_review label."""
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_single_project_config(project_path, config_path)
    task_id = _seed_project_db(project_path, labels=["plan_review"])
    return {
        "config_path": config_path,
        "project_path": project_path,
        "task_id": task_id,
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


def _find_help_modal(app):
    from pollypm.cockpit_ui import KeyboardHelpModal
    for screen in app.screen_stack:
        if isinstance(screen, KeyboardHelpModal):
            return screen
    return None


# ---------------------------------------------------------------------------
# 1. ``?`` opens KeyboardHelpModal from each cockpit App.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "app_factory_name",
    [
        "PollyInboxApp",
        "PollyCockpitApp",
        "PollyWorkerRosterApp",
        "PollyActivityFeedApp",
        "PollySettingsPaneApp",
    ],
)
def test_question_mark_opens_help_modal(
    single_project_env, app_factory_name,
) -> None:
    """``?`` opens :class:`KeyboardHelpModal` from every cockpit App."""
    if not _load_config_compatible(single_project_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm import cockpit_ui
    cls = getattr(cockpit_ui, app_factory_name)
    app = cls(single_project_env["config_path"])

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert _find_help_modal(app) is None
            await pilot.press("question_mark")
            await pilot.pause()
            modal = _find_help_modal(app)
            assert modal is not None, (
                f"expected KeyboardHelpModal on screen stack for {app_factory_name}"
            )
    _run(body())


def test_question_mark_opens_help_modal_project_dashboard(
    single_project_env,
) -> None:
    """Project dashboard also opens the help modal on ``?``.

    The dashboard needs a project key so it can't share the generic
    parametrised factory.
    """
    if not _load_config_compatible(single_project_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyProjectDashboardApp
    app = PollyProjectDashboardApp(
        single_project_env["config_path"], "demo",
    )

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert _find_help_modal(app) is None
            await pilot.press("question_mark")
            await pilot.pause()
            assert _find_help_modal(app) is not None
    _run(body())


# ---------------------------------------------------------------------------
# 2. Modal shows the screen's bindings categorised.
# ---------------------------------------------------------------------------


def test_modal_shows_screen_bindings_categorised(single_project_env) -> None:
    """The modal renders a ``This screen`` section listing the App's keys."""
    if not _load_config_compatible(single_project_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(single_project_env["config_path"])

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            modal = _find_help_modal(app)
            assert modal is not None
            # Inbox BINDINGS include ``r`` for reply and ``a`` for archive;
            # both should appear under the per-screen section.
            sections = {cat: rows for cat, rows in modal._sections}
            assert "This screen" in sections, (
                f"expected a This screen section; got {list(sections)}"
            )
            # Flatten key labels for easy substring assertion.
            screen_keys = [k for k, _ in sections["This screen"]]
            joined = " ".join(screen_keys)
            assert "r" in joined, f"missing 'r' reply binding; got {screen_keys}"
            assert "a" in joined, f"missing 'a' archive binding; got {screen_keys}"
    _run(body())


def test_modal_style_masks_background_and_keeps_nav_pair_visible() -> None:
    """Help should be opaque and tall enough for the j/k pair labels."""
    from pollypm.cockpit_ui import KeyboardHelpModal, PollyCockpitApp
    from pollypm.cockpit_palette import _collect_keybindings_for_screen

    css = KeyboardHelpModal.CSS
    assert "KeyboardHelpModal" in css
    assert "background: #0f1317" in css
    assert "max-height: 24" in css

    modal = KeyboardHelpModal(
        _collect_keybindings_for_screen(PollyCockpitApp(Path("/tmp/nope"))),
        screen_title="Cockpit",
    )
    lines = modal._render_body().splitlines()
    key_index = lines.index("  [b]k / \u2191[/b]")
    assert lines[key_index + 1] == "      [dim]Up[/dim]"


# ---------------------------------------------------------------------------
# 3. Modal shows global bindings.
# ---------------------------------------------------------------------------


def test_modal_shows_global_bindings(single_project_env) -> None:
    """The modal always includes a Global section with ``:`` and ``?``."""
    if not _load_config_compatible(single_project_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(single_project_env["config_path"])

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            modal = _find_help_modal(app)
            assert modal is not None
            sections = {cat: rows for cat, rows in modal._sections}
            global_section = None
            for name in sections:
                if name.lower().startswith("global"):
                    global_section = sections[name]
                    break
            assert global_section is not None, (
                f"expected a Global section; got {list(sections)}"
            )
            keys = [k for k, _ in global_section]
            assert ":" in keys, f"Global section missing ':' palette; got {keys}"
            assert "?" in keys, f"Global section missing '?' help; got {keys}"
    _run(body())


def test_modal_includes_cockpit_rail_glyph_legend() -> None:
    from pollypm.cockpit_palette import _collect_keybindings_for_screen
    from pollypm.cockpit_ui import PollyCockpitApp

    sections = dict(_collect_keybindings_for_screen(PollyCockpitApp(Path("/tmp/nope"))))
    glyph_rows = sections.get("Rail glyphs")

    assert glyph_rows is not None
    descriptions = " ".join(desc for _key, desc in glyph_rows)
    keys = [key for key, _desc in glyph_rows]
    assert "♥ / ♡" in keys
    assert "⚠" in keys
    assert "writing or working" in descriptions
    assert "current rail selection" in descriptions
    # #872: the project sparkline glyphs need an on-screen legend.
    assert any("sparkline" in desc.lower() for desc in
               (d for _, d in glyph_rows))


# ---------------------------------------------------------------------------
# 4. For inbox screen with selected plan_review item: shows plan_review keys.
# ---------------------------------------------------------------------------


def test_modal_surfaces_plan_review_keys_when_selected(plan_review_env) -> None:
    """With a plan_review task selected, the modal surfaces ``v`` / ``A``.

    The keys already live in ``PollyInboxApp.BINDINGS`` (they're always
    there); what this test really proves is that the modal adds a
    *dedicated* plan_review section alongside the screen section, so the
    user understands the context-gating.
    """
    if not _load_config_compatible(plan_review_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(plan_review_env["config_path"])

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Make sure the seeded task is selected so the label check hits.
            await pilot.press("j")
            await pilot.pause()
            # Force the selection since list navigation may no-op when the
            # cursor is already on the only row.
            app._selected_task_id = plan_review_env["task_id"]
            await pilot.press("question_mark")
            await pilot.pause()
            modal = _find_help_modal(app)
            assert modal is not None
            section_names = [cat for cat, _ in modal._sections]
            plan_review_section = None
            for cat, rows in modal._sections:
                if "plan_review" in cat:
                    plan_review_section = rows
                    break
            assert plan_review_section is not None, (
                f"expected a plan_review section; got {section_names}"
            )
            keys = [k for k, _ in plan_review_section]
            assert "v" in keys, f"missing v binding in plan_review section: {keys}"
            assert "A" in keys, f"missing A binding in plan_review section: {keys}"
    _run(body())


def test_modal_omits_plan_review_section_when_no_label(single_project_env) -> None:
    """With a plain task selected, the plan_review section is absent."""
    if not _load_config_compatible(single_project_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(single_project_env["config_path"])

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_task_id = single_project_env["task_id"]
            await pilot.press("question_mark")
            await pilot.pause()
            modal = _find_help_modal(app)
            assert modal is not None
            for cat, _ in modal._sections:
                assert "plan_review" not in cat, (
                    f"plan_review section leaked on non-plan_review task: {cat}"
                )
    _run(body())


# ---------------------------------------------------------------------------
# 5. Esc dismisses the modal.
# ---------------------------------------------------------------------------


def test_esc_dismisses_help_modal(single_project_env) -> None:
    """Esc closes the help overlay without side-effects."""
    if not _load_config_compatible(single_project_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp
    app = PollyInboxApp(single_project_env["config_path"])

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            assert _find_help_modal(app) is not None
            await pilot.press("escape")
            await pilot.pause()
            assert _find_help_modal(app) is None
    _run(body())


# ---------------------------------------------------------------------------
# 6. Command palette's "Show keyboard shortcuts" dispatches to the modal.
# ---------------------------------------------------------------------------


def test_palette_shortcuts_command_opens_help_modal(single_project_env) -> None:
    """Selecting ``session.shortcuts`` from the palette opens the help modal.

    Keeps ``_palette_last_shortcuts`` populated for backwards compat
    (the prior text-only payload) while *also* pushing the modal — the
    modal is the primary surface now.
    """
    if not _load_config_compatible(single_project_env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm.cockpit_ui import PollyInboxApp, _dispatch_palette_tag
    app = PollyInboxApp(single_project_env["config_path"])

    async def body() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Dispatch directly — mirrors what the palette does on dismiss.
            _dispatch_palette_tag(app, "session.shortcuts")
            await pilot.pause()
            assert _find_help_modal(app) is not None, (
                "expected session.shortcuts to open KeyboardHelpModal"
            )
            # Backwards-compat payload is still stashed for pre-modal callers.
            assert getattr(app, "_palette_last_shortcuts", "")
    _run(body())


# ---------------------------------------------------------------------------
# Bonus: every cockpit App class declares the ``?`` binding.
# ---------------------------------------------------------------------------


def test_question_mark_binding_registered_on_every_cockpit_app() -> None:
    """Class-level assertion — cheap, independent of Pilot startup."""
    from pollypm.cockpit_ui import (
        PollyActivityFeedApp,
        PollyCockpitApp,
        PollyInboxApp,
        PollyProjectDashboardApp,
        PollySettingsPaneApp,
        PollyWorkerRosterApp,
    )

    def _has_help_binding(cls) -> bool:
        for binding in getattr(cls, "BINDINGS", []):
            keys = getattr(binding, "key", "")
            if "question_mark" in keys:
                return True
        return False

    for cls in (
        PollyCockpitApp,
        PollyInboxApp,
        PollyProjectDashboardApp,
        PollyWorkerRosterApp,
        PollyActivityFeedApp,
        PollySettingsPaneApp,
    ):
        assert _has_help_binding(cls), (
            f"{cls.__name__} is missing the ``?`` help keybinding"
        )


def test_help_modal_binds_jk_for_scroll() -> None:
    """``j``/``k``/``Down``/``Up`` scroll the help modal (#859).

    Without these bindings, a user reading a tall help dialog who
    presses ``j`` to "go down" gets nothing — and the keystroke can
    leak through to the rail underneath (#861).
    """
    from pollypm.cockpit_ui import KeyboardHelpModal

    keys = {
        binding.key.split(",")[0]
        for binding in KeyboardHelpModal.BINDINGS
    }
    # Top-level keys, exact membership check.
    bound = {key for binding in KeyboardHelpModal.BINDINGS for key in binding.key.split(",")}
    for required in ("j", "k", "down", "up", "g", "G", "home", "end"):
        assert required in bound, f"help modal missing scroll binding for {required!r}"


def test_help_modal_bindings_are_priority_to_trap_rail_keys() -> None:
    """Modal bindings must run with priority so the rail underneath does
    not eat j/k/Esc before the modal sees them (#861)."""
    from pollypm.cockpit_ui import KeyboardHelpModal

    for binding in KeyboardHelpModal.BINDINGS:
        assert getattr(binding, "priority", False), (
            f"{binding.key!r} must be priority to trap rail bindings"
        )
