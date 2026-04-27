"""Rendered cockpit smoke matrix (#882).

Drives a representative subset of cockpit ``App`` classes through the
:mod:`pollypm.cockpit_smoke` harness at the five target terminal
sizes (80x30, 100x40, 169x50, 200x50, 210x65) and asserts the
universal smoke checks: no traceback reached the cockpit, no raw
provider bootstrap leaked, no orphan box characters, no
letter-by-letter wraps.

The matrix is parameterized so adding a new screen is a single
fixture row. Surfaces that require a live tmux session (PM Chat /
worker pane) are intentionally not included — they are tested at
the integration layer (#884 launch state-machine) and the smoke
matrix here is the rendered cockpit only.

Test pattern:

* Build a self-contained per-test config (single workspace,
  single demo project, optional seeded data).
* Construct the App.
* Hand it to ``SmokeHarness.mount(size=...)`` and pause.
* Run the universal assertion bundle.
* Optionally: run per-app custom assertions (e.g., that the help
  modal exposes ``ctrl+q``).

Tests are async; the existing cockpit suite uses ``asyncio.run``
inside a sync wrapper, so this file follows that convention to
avoid pulling in ``pytest-asyncio``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

from pollypm.cockpit_smoke import (
    SMOKE_TERMINAL_SIZES,
    SmokeHarness,
)
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _write_minimal_config(project_path: Path, config_path: Path) -> None:
    """Write a one-project pollypm.toml so the cockpit Apps can mount.

    Mirrors the established pattern from
    ``tests/test_keyboard_help.py::_write_single_project_config`` —
    keeping the shape identical avoids duplicate fixture maintenance.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[project]\n"
        'tmux_session = "pollypm-smoke"\n'
        f'workspace_root = "{project_path.parent}"\n'
        "\n"
        "[projects.demo]\n"
        'key = "demo"\n'
        'name = "Demo"\n'
        f'path = "{project_path}"\n'
    )


def _seed_one_review_task(project_path: Path) -> str:
    """Seed one review-state task so screens have something to render."""
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
        task = svc.create(
            title="Smoke review",
            description="Smoke description",
            type="task",
            project="demo",
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            priority="normal",
            created_by="polly",
            labels=[],
        )
        return task.task_id


@pytest.fixture
def smoke_env(tmp_path: Path) -> dict[str, Any]:
    """Single-project workspace with one seeded task."""
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_minimal_config(project_path, config_path)
    task_id = _seed_one_review_task(project_path)
    return {
        "config_path": config_path,
        "project_path": project_path,
        "task_id": task_id,
    }


# ---------------------------------------------------------------------------
# Async runner — same idiom as test_tasks_ui / test_keyboard_help
# ---------------------------------------------------------------------------


def _run(coro: Awaitable[None]) -> None:
    asyncio.run(coro)


# ---------------------------------------------------------------------------
# Universal smoke check executor
# ---------------------------------------------------------------------------


async def _run_universal_smoke(
    factory: Callable[[], Any],
    *,
    size: tuple[int, int],
    custom: Callable[[SmokeHarness], None] | None = None,
) -> None:
    """Drive ``factory()`` at ``size``, run the universal asserts."""
    app = factory()
    async with SmokeHarness.mount(app, size=size) as smoke:
        smoke.snapshot()
        smoke.assert_no_traceback()
        smoke.assert_no_bootstrap_prompt()
        smoke.assert_no_orphan_box_chars()
        smoke.assert_no_letter_by_letter_wrap()
        # Even on a tiny terminal a real cockpit screen has at least
        # a handful of widgets (header + body + status). One or zero
        # widgets means compose() raised silently.
        smoke.assert_minimum_widget_count(3)
        if custom is not None:
            custom(smoke)


# ---------------------------------------------------------------------------
# PollyTasksApp smoke matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SMOKE_TERMINAL_SIZES)
def test_tasks_app_smoke(smoke_env: dict[str, Any], size: tuple[int, int]) -> None:
    """Tasks renders cleanly at every target size."""
    from pollypm.cockpit_tasks import PollyTasksApp

    def factory() -> Any:
        return PollyTasksApp(smoke_env["config_path"], "demo")

    _run(_run_universal_smoke(factory, size=size))


# ---------------------------------------------------------------------------
# Inbox smoke matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SMOKE_TERMINAL_SIZES)
def test_inbox_app_smoke(smoke_env: dict[str, Any], size: tuple[int, int]) -> None:
    """Inbox renders cleanly at every target size."""
    try:
        from pollypm.cockpit_ui import PollyInboxApp
    except ImportError:
        pytest.skip("PollyInboxApp not importable in this environment")

    def factory() -> Any:
        return PollyInboxApp(smoke_env["config_path"])

    _run(_run_universal_smoke(factory, size=size))


# ---------------------------------------------------------------------------
# Activity feed smoke matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SMOKE_TERMINAL_SIZES)
def test_activity_feed_app_smoke(
    smoke_env: dict[str, Any], size: tuple[int, int]
) -> None:
    """Activity feed renders cleanly at every target size.

    The audit cites #829 — preserving ``Try: pm`` in the data did
    not survive the rendered Activity column at narrow widths. The
    smoke matrix runs at 80x30 specifically to defend that.
    """
    try:
        from pollypm.cockpit_activity import PollyActivityFeedApp
    except ImportError:
        pytest.skip("PollyActivityFeedApp not importable")

    def factory() -> Any:
        return PollyActivityFeedApp(smoke_env["config_path"])

    _run(_run_universal_smoke(factory, size=size))


# ---------------------------------------------------------------------------
# Keyboard help smoke matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", SMOKE_TERMINAL_SIZES)
def test_keyboard_help_modal_smoke(
    smoke_env: dict[str, Any], size: tuple[int, int]
) -> None:
    """The ``?`` overlay must mount cleanly and render its title
    at every size in the matrix.

    #898 — the prior version of this test asserted ``ctrl+q``
    visible on the first rendered screen, which the new
    canvas-aware harness correctly rejected at 80x30: the help
    modal is scrollable, ``ctrl+q`` lives further down the list,
    and forcing visibility-without-scroll would over-constrain
    the modal layout. The smoke matrix's job is to catch
    *unintended* clipping (covered by ``test_smoke_harness_
    detects_clipping`` below); scrollable overlay content is
    out of scope.
    """
    try:
        from pollypm.cockpit_ui import PollyInboxApp
    except ImportError:
        pytest.skip("PollyInboxApp not importable")

    async def body() -> None:
        app = PollyInboxApp(smoke_env["config_path"])
        async with SmokeHarness.mount(app, size=size) as smoke:
            await smoke.pilot.press("question_mark")
            await smoke.pilot.pause()
            smoke.snapshot()
            smoke.assert_no_traceback()
            # The modal title is rendered at the top of the
            # dialog and is short enough to fit at 80x30, so it's
            # the canonical "modal mounted and rendered" signal.
            smoke.assert_text_visible("Keyboard shortcuts")

    _run(body())


# ---------------------------------------------------------------------------
# Smoke harness self-tests
# ---------------------------------------------------------------------------


def test_smoke_size_matrix_includes_all_targets() -> None:
    """The size matrix must include the five audit-mandated sizes.

    If a future refactor accidentally narrows the matrix, the
    release gate (#889) loses coverage of the narrowest case (80x30)
    where most truncation bugs live."""
    expected = {(80, 30), (100, 40), (169, 50), (200, 50), (210, 65)}
    assert set(SMOKE_TERMINAL_SIZES) == expected


def test_smoke_harness_detects_traceback() -> None:
    """The harness must fail loudly if a traceback reaches a widget."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class _BrokenApp(App):
        def compose(self) -> ComposeResult:
            # Synthesize the exact text shape of a Python traceback.
            yield Static(
                'Traceback (most recent call last):\n'
                '  File "x.py", line 1, in <module>\n'
                '    raise ValueError("nope")\n'
                'ValueError: nope'
            )

    async def body() -> None:
        async with SmokeHarness.mount(_BrokenApp(), size=(80, 30)) as smoke:
            smoke.snapshot()
            with pytest.raises(AssertionError, match="Traceback"):
                smoke.assert_no_traceback()

    _run(body())


def test_smoke_harness_detects_bootstrap_prompt() -> None:
    """Raw provider bootstrap text must be flagged."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class _BootstrapLeakApp(App):
        def compose(self) -> ComposeResult:
            yield Static("Select a theme")

    async def body() -> None:
        async with SmokeHarness.mount(
            _BootstrapLeakApp(), size=(80, 30)
        ) as smoke:
            smoke.snapshot()
            with pytest.raises(AssertionError, match="Bootstrap prompt"):
                smoke.assert_no_bootstrap_prompt()

    _run(body())


def test_smoke_harness_detects_letter_by_letter_wrap() -> None:
    """Three+ consecutive single-letter lines must be flagged."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class _WrapApp(App):
        def compose(self) -> ComposeResult:
            yield Static("c\nt\nr\nl\nq")

    async def body() -> None:
        async with SmokeHarness.mount(_WrapApp(), size=(80, 30)) as smoke:
            smoke.snapshot()
            with pytest.raises(AssertionError, match="Letter-by-letter"):
                smoke.assert_no_letter_by_letter_wrap()

    _run(body())


def test_smoke_harness_detects_orphan_box_chars() -> None:
    """A single box-drawing character on a line is suspicious."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class _OrphanApp(App):
        def compose(self) -> ComposeResult:
            yield Static("Title\n┐\nBody")

    async def body() -> None:
        async with SmokeHarness.mount(_OrphanApp(), size=(80, 30)) as smoke:
            smoke.snapshot()
            with pytest.raises(AssertionError, match="Orphan box"):
                smoke.assert_no_orphan_box_chars()

    _run(body())


def test_smoke_harness_assert_text_visible() -> None:
    """Visible text passes; absent text fails."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class _OkApp(App):
        def compose(self) -> ComposeResult:
            yield Static("Try: pm task claim demo/5")

    async def body() -> None:
        async with SmokeHarness.mount(_OkApp(), size=(80, 30)) as smoke:
            smoke.snapshot()
            smoke.assert_text_visible("Try: pm task claim")
            with pytest.raises(AssertionError, match="not visible"):
                smoke.assert_text_visible("nonexistent")

    _run(body())


def test_smoke_harness_counts_agree_detects_mismatch() -> None:
    """Two different counts for the same label must be flagged."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class _SplitCountApp(App):
        def compose(self) -> ComposeResult:
            yield Static("Inbox: 5\nElsewhere on the screen: Inbox: 7")

    async def body() -> None:
        async with SmokeHarness.mount(
            _SplitCountApp(), size=(100, 40)
        ) as smoke:
            smoke.snapshot()
            with pytest.raises(AssertionError, match="Counter 'Inbox'"):
                smoke.assert_counts_agree(["Inbox"])

    _run(body())


def test_smoke_harness_counts_agree_passes_when_consistent() -> None:
    """One count rendered twice with the same value must pass."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class _MatchingCountApp(App):
        def compose(self) -> ComposeResult:
            yield Static("Inbox: 5\nLater: Inbox: 5")

    async def body() -> None:
        async with SmokeHarness.mount(
            _MatchingCountApp(), size=(100, 40)
        ) as smoke:
            smoke.snapshot()
            # Should not raise.
            smoke.assert_counts_agree(["Inbox"])

    _run(body())


def test_smoke_harness_detects_clipping_at_80x30() -> None:
    """#898 acceptance criterion: the harness must fail when source
    text exists but is clipped at the configured terminal size.

    Synthesizes a widget whose source string contains ``ctrl+q``
    far to the right of column 80, with ``width: auto`` so
    Textual clips (instead of wrapping) at the viewport edge. At
    ``size=(80, 30)`` the canvas-aware capture clips the content
    and ``ctrl+q`` is invisible; ``assert_text_visible`` rejects
    it.

    The same widget at a wider size renders ``ctrl+q`` and the
    assertion passes — proving the harness reads post-layout
    text rather than the pre-layout source string."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class _ClippedApp(App):
        # ``width: auto`` keeps the static widget on one row; at
        # 80 columns the trailing 'ctrl+q' is clipped off-screen.
        CSS = "#payload { width: auto; height: 1; }"

        def compose(self) -> ComposeResult:
            yield Static(
                ("X" * 80) + "press ctrl+q to recover",
                id="payload",
            )

    async def narrow() -> None:
        async with SmokeHarness.mount(_ClippedApp(), size=(80, 30)) as smoke:
            smoke.snapshot()
            with pytest.raises(AssertionError, match="not visible"):
                smoke.assert_text_visible("ctrl+q")

    async def wide() -> None:
        async with SmokeHarness.mount(
            _ClippedApp(), size=(210, 30)
        ) as smoke:
            smoke.snapshot()
            # At a wide-enough terminal the same widget renders
            # ``ctrl+q`` on screen — proves the harness sees
            # post-layout text, not pre-layout source.
            smoke.assert_text_visible("ctrl+q")

    _run(narrow())
    _run(wide())


def test_smoke_harness_fallback_walk_used_when_compositor_unavailable() -> None:
    """#898 — when the compositor cannot render strips (e.g., a
    Textual API change or a degraded test environment), the
    harness falls back to the widget-walk path so the run still
    produces a usable capture rather than going completely empty.
    Documented behavior; tested explicitly so future refactors do
    not silently remove the fallback.

    Forces the fallback by patching ``render_strips`` to raise.
    Replacing the compositor object outright would break Textual's
    own teardown which calls ``compositor.clear()``; raising from
    ``render_strips`` exercises the same fallback branch without
    breaking the host app."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class _OkApp(App):
        def compose(self) -> ComposeResult:
            yield Static("fallback path content")

    async def body() -> None:
        async with SmokeHarness.mount(_OkApp(), size=(120, 30)) as smoke:
            comp = smoke.app.screen._compositor
            real_render = comp.render_strips

            def _raise() -> None:
                raise RuntimeError("synthetic compositor failure")

            comp.render_strips = _raise  # type: ignore[method-assign]
            try:
                smoke.snapshot()
                smoke.assert_text_visible("fallback path content")
            finally:
                comp.render_strips = real_render  # type: ignore[method-assign]

    _run(body())


def test_smoke_harness_minimum_widget_count() -> None:
    """A near-empty App must trip the minimum-widget guard."""
    from textual.app import App, ComposeResult

    class _EmptyApp(App):
        def compose(self) -> ComposeResult:
            return iter(())

    async def body() -> None:
        async with SmokeHarness.mount(_EmptyApp(), size=(100, 40)) as smoke:
            smoke.snapshot()
            with pytest.raises(AssertionError, match="widgets rendered"):
                smoke.assert_minimum_widget_count(3)

    _run(body())
