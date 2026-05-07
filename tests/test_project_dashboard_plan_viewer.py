"""Inline-plan-viewer tests for :class:`PollyProjectDashboardApp`.

Extends the project-dashboard suite to cover the new inline plan-viewer
surface: rendered plan.md body, TOC, aux-file listing, staleness
indicator, and the ``p``/``v``/``o`` keybindings. The UI changes live
entirely in ``cockpit_ui.PollyProjectDashboardApp`` — no new modules.

All tests are targeted. Run with::

    HOME=/tmp/pytest-agent-planviewer uv run pytest \\
        tests/test_project_dashboard_plan_viewer.py \\
        tests/test_project_dashboard_ui.py -q
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Minimal config + fixtures — mirror test_project_dashboard_ui.py so the
# loader shape stays in sync with that file.
# ---------------------------------------------------------------------------


def _write_config(project_path: Path, config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[project]\n"
        f'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{project_path.parent}"\n'
        "\n"
        "[projects.demo]\n"
        'key = "demo"\n'
        'name = "Demo"\n'
        f'path = "{project_path}"\n'
    )


def _load_config_compatible(config_path: Path) -> bool:
    try:
        from pollypm.config import load_config
        cfg = load_config(config_path)
        return "demo" in getattr(cfg, "projects", {})
    except Exception:  # noqa: BLE001
        return False


def _seed_minimal_state_db(project_path: Path) -> None:
    """Create an empty state.db so the work-service path exists.

    The plan viewer tests don't need seeded tasks in most cases, but
    ``_dashboard_gather_tasks`` still expects the DB file to be present
    so it can read counts. An empty DB returns empty counts — clean.
    """
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(
        db_path=db_path, project_path=project_path,
    ):
        pass


@pytest.fixture
def env(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_config(project_path, config_path)
    _seed_minimal_state_db(project_path)
    return {
        "config_path": config_path,
        "project_path": project_path,
    }


@pytest.fixture
def app(env):
    if not _load_config_compatible(env["config_path"]):
        pytest.skip("minimal pollypm.toml fixture not supported by loader")
    from pollypm import cockpit_ui as _cockpit_ui
    _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()
    from pollypm.cockpit_ui import PollyProjectDashboardApp
    return PollyProjectDashboardApp(env["config_path"], "demo")


def _run(coro) -> None:
    asyncio.run(coro)


def _write_plan(project_path: Path, body: str) -> Path:
    plan_path = project_path / "docs" / "plan" / "plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(body)
    return plan_path


# ---------------------------------------------------------------------------
# 1. Plan section renders plan.md content when present
# ---------------------------------------------------------------------------


def test_plan_renders_markdown_body_inline(env, app) -> None:
    """The full plan.md body renders inline via ``_md_to_rich``."""
    async def body() -> None:
        _write_plan(
            env["project_path"],
            "# My Plan\n"
            "\n"
            "## Phase 1\n"
            "\n"
            "Do the **important** thing first.\n"
            "\n"
            "- step one\n"
            "- step two\n"
            "\n"
            "## Phase 2\n"
            "\n"
            "Then the other thing.\n",
        )
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            assert app.data is not None
            # Plan text loaded into the snapshot.
            assert app.data.plan_text is not None
            assert "Do the **important** thing" in app.data.plan_text
            # Rendered inline (Rich markup conversion happened).
            rendered = str(app.plan_content.render())
            # Key body content appears.
            assert "Do the" in rendered
            assert "important" in rendered
            assert "step one" in rendered
            assert "Then the other thing" in rendered
    _run(body())


# ---------------------------------------------------------------------------
# 2. Plan section shows empty state when missing
# ---------------------------------------------------------------------------


def test_plan_content_empty_when_no_file(env, app) -> None:
    """With no plan.md on disk the plan-content widget stays blank."""
    async def body() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert app.data is not None
            assert app.data.plan_path is None
            assert app.data.plan_text is None
            # Content widget renders nothing extra (empty-state lives
            # in ``plan_body`` already — covered by the existing suite).
            assert str(app.plan_content.render()).strip() == ""
    _run(body())


def test_md_to_rich_preserves_inline_markdown_helpers() -> None:
    """The markdown-to-rich helper should keep its inline formatting."""
    from pollypm.cockpit_ui import _md_to_rich

    rendered = _md_to_rich("**bold** *italic* `code`\n1. item\n- bullet")

    assert "[b]bold[/b]" in rendered
    assert "[i]italic[/i]" in rendered
    assert "[dim]code[/dim]" in rendered
    assert "1. item" in rendered
    assert "• bullet" in rendered


# ---------------------------------------------------------------------------
# 3. TOC lists H2 sections
# ---------------------------------------------------------------------------


def test_toc_lists_h2_headers(env, app) -> None:
    """The H2 section list (TOC) is surfaced in the plan_body widget."""
    async def body() -> None:
        _write_plan(
            env["project_path"],
            "# Plan\n\n## Alpha\n\n## Beta\n\n### Not a TOC entry\n\n## Gamma\n",
        )
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert app.data is not None
            assert app.data.plan_sections == ["Alpha", "Beta", "Gamma"]
            rendered = str(app.plan_body.render())
            assert "Alpha" in rendered
            assert "Beta" in rendered
            assert "Gamma" in rendered
            # H3s must not leak into the TOC.
            assert "Not a TOC entry" not in rendered
    _run(body())


# ---------------------------------------------------------------------------
# 4. Auxiliary files under docs/plan/ appear as jump hints
# ---------------------------------------------------------------------------


def test_aux_files_listed_in_plan_body(env, app) -> None:
    """``architecture.md``, ``risks.md``, and milestones appear as jumps."""
    async def body() -> None:
        project = env["project_path"]
        _write_plan(project, "# Plan\n\n## Only section\n")
        (project / "docs" / "plan" / "architecture.md").write_text("# Arch\n")
        (project / "docs" / "plan" / "risks.md").write_text("# Risks\n")
        milestones = project / "docs" / "plan" / "milestones"
        milestones.mkdir()
        (milestones / "m1.md").write_text("m1")
        (milestones / "m2.md").write_text("m2")

        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            assert app.data is not None
            aux_names = [p.name for p in app.data.plan_aux_files]
            assert "architecture.md" in aux_names
            assert "risks.md" in aux_names
            assert "m1.md" in aux_names
            assert "m2.md" in aux_names
            rendered = str(app.plan_body.render())
            assert "architecture.md" in rendered
            assert "risks.md" in rendered
    _run(body())


# ---------------------------------------------------------------------------
# 5a. Staleness indicator fires when plan_approved_at < latest backlog
# ---------------------------------------------------------------------------


def test_staleness_from_backlog_timestamp(env, app, monkeypatch) -> None:
    """When the staleness helper flags the plan, the UI shows the warning."""
    async def body() -> None:
        _write_plan(env["project_path"], "# Plan\n\n## Section\n")

        # Monkeypatch the staleness helper to return a reason — this
        # decouples the UI test from the plan_presence + SQLite walk.
        from pollypm import cockpit_ui as _cockpit_ui

        def _fake_stale(plan_path, plan_mtime, project_path, project_key):
            return "plan approved before latest backlog task"

        monkeypatch.setattr(
            _cockpit_ui, "_dashboard_plan_staleness", _fake_stale,
        )
        _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()

        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            assert app.data is not None
            assert app.data.plan_stale_reason is not None
            rendered = str(app.plan_stale.render())
            assert "stale" in rendered.lower()
            assert "before latest backlog" in rendered
    _run(body())


# ---------------------------------------------------------------------------
# 5b. Staleness indicator fires when file mtime > 30 days old
# ---------------------------------------------------------------------------


def test_staleness_from_mtime(env, app) -> None:
    """An old plan.md (mtime > 30 days) shows the stale warning."""
    async def body() -> None:
        plan_path = _write_plan(env["project_path"], "# Plan\n\n## One\n")
        # Backdate the plan file by 45 days.
        old = time.time() - 45 * 86400
        os.utime(plan_path, (old, old))
        from pollypm import cockpit_ui as _cockpit_ui
        _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()

        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert app.data is not None
            assert app.data.plan_stale_reason is not None
            rendered = str(app.plan_stale.render())
            assert "stale" in rendered.lower()
    _run(body())


def test_no_staleness_when_fresh(env, app) -> None:
    """Fresh plan with no backlog → no stale warning rendered."""
    async def body() -> None:
        _write_plan(env["project_path"], "# Plan\n\n## One\n")
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert app.data is not None
            assert app.data.plan_stale_reason is None
            assert str(app.plan_stale.render()).strip() == ""
    _run(body())


# ---------------------------------------------------------------------------
# 6. ``v`` keybinding was removed in #1405 (broken visual explainer).
# Pressing ``v`` is now a no-op at the dashboard layer regardless of
# whether the explainer artifact exists on disk.
# ---------------------------------------------------------------------------


def test_v_is_unbound_after_1405(env, app, monkeypatch) -> None:
    """``v`` no longer triggers ``_open_external`` (binding removed)."""
    async def body() -> None:
        _write_plan(env["project_path"], "# Plan\n\n## One\n")
        reports = env["project_path"] / "reports"
        reports.mkdir()
        explainer = reports / "plan-review.html"
        explainer.write_text("<html>explain</html>")

        from pollypm import cockpit_ui as _cockpit_ui
        _cockpit_ui._PROJECT_DASHBOARD_TASK_CACHE.clear()

        opened: list[Path] = []

        def fake_open(self, path: Path) -> None:
            opened.append(path)

        from pollypm.cockpit_ui import PollyProjectDashboardApp
        monkeypatch.setattr(
            PollyProjectDashboardApp, "_open_external", fake_open,
        )

        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            assert app.data is not None
            await pilot.press("v")
            await pilot.pause()
            assert opened == [], "v must be unbound after #1405"
    _run(body())


# ---------------------------------------------------------------------------
# 7. `p` toggles plan-focus mode (hides other sections) and back
# ---------------------------------------------------------------------------


def test_p_toggles_plan_view_mode(env, app) -> None:
    """Pressing ``p`` twice enters then exits plan-focus mode."""
    async def body() -> None:
        _write_plan(env["project_path"], "# Plan\n\n## One\n")
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            # Start: not in plan view.
            assert app._plan_view_mode is False
            await pilot.press("p")
            await pilot.pause()
            assert app._plan_view_mode is True
            # The non-plan sections should have the -hidden class.
            now_section = app.query_one("#proj-now-section")
            assert now_section.has_class("-hidden")
            # Hint line flips to plan-view hints.
            hint_text = str(app.hint.render())
            assert "j/k" in hint_text
            # Toggle back.
            await pilot.press("p")
            await pilot.pause()
            assert app._plan_view_mode is False
            assert not now_section.has_class("-hidden")
    _run(body())


# ---------------------------------------------------------------------------
# 8. `o` opens plan.md in system editor (seam-mocked)
# ---------------------------------------------------------------------------


def test_o_opens_plan_in_editor(env, app, monkeypatch) -> None:
    """``o`` dispatches the plan path to ``_open_external``."""
    async def body() -> None:
        plan_path = _write_plan(env["project_path"], "# Plan\n\n## One\n")

        opened: list[Path] = []

        def fake_open(self, path: Path) -> None:
            opened.append(path)

        from pollypm.cockpit_ui import PollyProjectDashboardApp
        monkeypatch.setattr(
            PollyProjectDashboardApp, "_open_external", fake_open,
        )

        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            assert opened, "expected _open_external to be called for plan.md"
            assert opened[-1] == plan_path
    _run(body())
