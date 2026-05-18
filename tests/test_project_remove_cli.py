"""Tests for ``pm project remove`` (#1561).

Narrow scope: surface the existing ``remove_project`` core function as a
``pm project remove <key>`` CLI command. Mirrors the ``pm project new``
pattern. The command refuses when the project has queued or in-flight
work-service tasks unless ``--force`` (or ``--yes``) is supplied.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from pollypm.plugins_builtin.project_planning.cli.project import project_app


runner = CliRunner()


def _write_config(
    config_path: Path,
    *,
    workspace_root: Path,
    project_path: Path,
    slug: str,
    extra_sessions: str = "",
) -> None:
    config_path.write_text(
        "[project]\n"
        'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{workspace_root}"\n'
        "\n"
        f'[projects.{slug}]\n'
        f'key = "{slug}"\n'
        'name = "Demo"\n'
        f'path = "{project_path}"\n'
        f"{extra_sessions}"
    )


@pytest.fixture
def env(tmp_path: Path) -> dict:
    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project_path = workspace_root / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_config(
        config_path,
        workspace_root=workspace_root,
        project_path=project_path,
        slug="demo",
    )
    return {
        "config_path": config_path,
        "project_path": project_path,
        "workspace_root": workspace_root,
    }


def _load_cfg(config_path: Path):
    from pollypm.config import load_config
    return load_config(config_path)


# --------------------------------------------------------------------------
# Happy path: no active tasks → removes without prompting.
# --------------------------------------------------------------------------


def test_cli_remove_happy_path(env) -> None:
    target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with patch(target, return_value=0):
        result = runner.invoke(
            project_app,
            ["remove", "demo", "--config", str(env["config_path"])],
        )
    assert result.exit_code == 0, result.output
    assert "Removed project 'demo'" in result.output

    config = _load_cfg(env["config_path"])
    assert "demo" not in config.projects


# --------------------------------------------------------------------------
# Unknown project → exit(1) with a clear error.
# --------------------------------------------------------------------------


def test_cli_remove_unknown_project_errors_cleanly(env) -> None:
    result = runner.invoke(
        project_app,
        ["remove", "does_not_exist", "--config", str(env["config_path"])],
    )
    assert result.exit_code == 1, result.output
    assert "Unknown project" in result.output


# --------------------------------------------------------------------------
# Active tasks: refuses without confirmation; --force bypasses.
# --------------------------------------------------------------------------


def test_cli_remove_aborts_with_active_tasks_and_no_force(env) -> None:
    target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with patch(target, return_value=3):
        # CliRunner sends an empty stdin → typer.confirm reads "" → False.
        result = runner.invoke(
            project_app,
            ["remove", "demo", "--config", str(env["config_path"])],
            input="n\n",
        )
    assert result.exit_code == 1, result.output
    assert "3 queued or in-flight tasks" in result.output
    assert "Aborted" in result.output

    # Config is untouched.
    config = _load_cfg(env["config_path"])
    assert "demo" in config.projects


def test_cli_remove_force_bypasses_active_task_prompt(env) -> None:
    target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with patch(target, return_value=2):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--force",
                "--config", str(env["config_path"]),
            ],
        )
    assert result.exit_code == 0, result.output
    assert "Removed project 'demo'" in result.output
    # The follow-up note about orphan rows should fire when active>0.
    assert "left in place" in result.output

    config = _load_cfg(env["config_path"])
    assert "demo" not in config.projects


def test_cli_remove_yes_accepts_active_task_prompt(env) -> None:
    target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with patch(target, return_value=1):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--yes",
                "--config", str(env["config_path"]),
            ],
        )
    assert result.exit_code == 0, result.output
    # Singular noun agreement.
    assert "1 queued or in-flight task" in result.output

    config = _load_cfg(env["config_path"])
    assert "demo" not in config.projects


# --------------------------------------------------------------------------
# Session-reference refusal propagates from the core function.
# --------------------------------------------------------------------------


def test_cli_remove_refuses_when_project_still_has_sessions(tmp_path: Path) -> None:
    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project_path = workspace_root / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_config(
        config_path,
        workspace_root=workspace_root,
        project_path=project_path,
        slug="demo",
        extra_sessions=(
            "[sessions.architect_demo]\n"
            'role = "architect"\n'
            'provider = "claude"\n'
            'account = "claude_main"\n'
            'cwd = "."\n'
            'project = "demo"\n'
            'window_name = "architect-demo"\n'
            "\n"
            "[accounts.claude_main]\n"
            'provider = "claude"\n'
            'home = "/tmp/claude_home"\n'
        ),
    )
    target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with patch(target, return_value=0):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--force",
                "--config", str(config_path),
            ],
        )
    assert result.exit_code == 1, result.output
    assert "still used by session" in result.output

    config = _load_cfg(config_path)
    assert "demo" in config.projects


# --------------------------------------------------------------------------
# --purge-sessions cascade: kill tmux + drop [sessions.*] entries so the
# core function's session-reference invariant no longer refuses removal.
# --------------------------------------------------------------------------


def _project_with_sessions_config(
    tmp_path: Path, *, sessions_block: str
) -> tuple[Path, Path]:
    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project_path = workspace_root / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_config(
        config_path,
        workspace_root=workspace_root,
        project_path=project_path,
        slug="demo",
        extra_sessions=(
            sessions_block
            + "\n[accounts.claude_main]\n"
            'provider = "claude"\n'
            'home = "/tmp/claude_home"\n'
        ),
    )
    return config_path, project_path


def test_cli_remove_purge_sessions_kills_tmux_and_drops_entries(
    tmp_path: Path,
) -> None:
    """--purge-sessions tears down tmux + config so removal succeeds."""
    config_path, _ = _project_with_sessions_config(
        tmp_path,
        sessions_block=(
            "[sessions.architect_demo]\n"
            'role = "architect"\n'
            'provider = "claude"\n'
            'account = "claude_main"\n'
            'cwd = "."\n'
            'project = "demo"\n'
            'window_name = "architect-demo"\n'
            "\n"
            "[sessions.reviewer_demo]\n"
            'role = "reviewer"\n'
            'provider = "claude"\n'
            'account = "claude_main"\n'
            'cwd = "."\n'
            'project = "demo"\n'
            'window_name = "reviewer-demo"\n'
        ),
    )

    fake_tmux = type(
        "FakeTmux", (), {
            "has_session": lambda self, name: True,
            "kill_session": lambda self, name: True,
        },
    )()
    count_target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    tmux_target = (
        "pollypm.plugins_builtin.project_planning.cli.project.create_tmux_client"
    )
    # ``create_tmux_client`` is imported lazily inside
    # ``_purge_project_sessions``; patch the module the lazy import resolves
    # against (the source module) since the local symbol isn't bound at
    # import time of project.py.
    with (
        patch(count_target, return_value=0),
        patch(
            "pollypm.session_services.create_tmux_client",
            return_value=fake_tmux,
        ),
    ):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--purge-sessions",
                "--config", str(config_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Killed tmux session architect_demo" in result.output
    assert "Killed tmux session reviewer_demo" in result.output
    assert "Removed project 'demo'" in result.output

    config = _load_cfg(config_path)
    assert "demo" not in config.projects
    assert "architect_demo" not in config.sessions
    assert "reviewer_demo" not in config.sessions

    # Patch reference to silence unused warning when local stub is unused.
    _ = tmux_target


def test_cli_remove_purge_sessions_handles_already_dead_tmux(
    tmp_path: Path,
) -> None:
    """--purge-sessions still drops config entries when tmux session is gone."""
    config_path, _ = _project_with_sessions_config(
        tmp_path,
        sessions_block=(
            "[sessions.architect_demo]\n"
            'role = "architect"\n'
            'provider = "claude"\n'
            'account = "claude_main"\n'
            'cwd = "."\n'
            'project = "demo"\n'
            'window_name = "architect-demo"\n'
        ),
    )

    fake_tmux = type(
        "FakeTmux", (), {
            "has_session": lambda self, name: False,
            "kill_session": lambda self, name: False,
        },
    )()
    count_target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with (
        patch(count_target, return_value=0),
        patch(
            "pollypm.session_services.create_tmux_client",
            return_value=fake_tmux,
        ),
    ):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--purge-sessions",
                "--config", str(config_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "tmux session was not running" in result.output
    assert "Removed project 'demo'" in result.output

    config = _load_cfg(config_path)
    assert "demo" not in config.projects
    assert "architect_demo" not in config.sessions


# --------------------------------------------------------------------------
# --dry-run: prints the teardown plan, mutates nothing.
# --------------------------------------------------------------------------


def test_cli_remove_dry_run_with_no_sessions(env) -> None:
    target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with patch(target, return_value=0):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--dry-run",
                "--config", str(env["config_path"]),
            ],
        )
    assert result.exit_code == 0, result.output
    assert "Dry run: would remove project 'demo'" in result.output
    assert "sessions: (none)" in result.output
    assert "Re-run without --dry-run to apply." in result.output

    # Nothing mutated.
    config = _load_cfg(env["config_path"])
    assert "demo" in config.projects


def test_cli_remove_dry_run_lists_sessions_without_killing(tmp_path: Path) -> None:
    config_path, _ = _project_with_sessions_config(
        tmp_path,
        sessions_block=(
            "[sessions.architect_demo]\n"
            'role = "architect"\n'
            'provider = "claude"\n'
            'account = "claude_main"\n'
            'cwd = "."\n'
            'project = "demo"\n'
            'window_name = "architect-demo"\n'
        ),
    )

    fake_tmux = type(
        "FakeTmux", (), {
            "has_session": lambda self, name: True,
            # If this ever fires the dry-run guard is broken.
            "kill_session": lambda self, name: (_ for _ in ()).throw(
                AssertionError("kill_session must not run in --dry-run")
            ),
        },
    )()
    count_target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with (
        patch(count_target, return_value=2),
        patch(
            "pollypm.session_services.create_tmux_client",
            return_value=fake_tmux,
        ),
    ):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--dry-run", "--purge-sessions",
                "--config", str(config_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Dry run: would remove project 'demo'" in result.output
    assert "sessions to purge:" in result.output
    assert "architect_demo (live)" in result.output
    assert "2 queued/in-flight" in result.output

    # Nothing mutated.
    config = _load_cfg(config_path)
    assert "demo" in config.projects
    assert "architect_demo" in config.sessions


def test_cli_remove_dry_run_warns_when_sessions_present_without_purge(
    tmp_path: Path,
) -> None:
    config_path, _ = _project_with_sessions_config(
        tmp_path,
        sessions_block=(
            "[sessions.architect_demo]\n"
            'role = "architect"\n'
            'provider = "claude"\n'
            'account = "claude_main"\n'
            'cwd = "."\n'
            'project = "demo"\n'
            'window_name = "architect-demo"\n'
        ),
    )

    fake_tmux = type(
        "FakeTmux", (), {
            "has_session": lambda self, name: False,
            "kill_session": lambda self, name: False,
        },
    )()
    count_target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with (
        patch(count_target, return_value=0),
        patch(
            "pollypm.session_services.create_tmux_client",
            return_value=fake_tmux,
        ),
    ):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--dry-run",
                "--config", str(config_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "use --purge-sessions to tear down" in result.output
    assert "remove_project will refuse" in result.output


# --------------------------------------------------------------------------
# #1645: _count_active_tasks must honor --config and not leak to the
# default-resolver workspace DB.
# --------------------------------------------------------------------------


def _seed_active_task(
    config_path: Path, *, project_key: str, count: int = 1
) -> Path:
    """Seed ``count`` non-terminal tasks in the work DB tied to ``config_path``.

    Returns the resolved work-service DB path so callers can assert on it.
    Uses the same resolver/factory the production code uses so the test
    proves the wiring end-to-end, not just an isolated layer.
    """
    from pollypm.config import load_config as _load
    from pollypm.work import create_work_service

    config = _load(config_path)
    db_paths: list[Path] = []
    with create_work_service(project_key=project_key, config=config) as svc:
        db_paths.append(Path(svc._db_path))
        for idx in range(count):
            svc.create(
                title=f"seed task {idx}",
                description="seeded by test",
                type="task",
                project=project_key,
                flow_template="standard",
                roles={"worker": "agent-1", "reviewer": "agent-2"},
                priority="normal",
                created_by="test",
            )
    return db_paths[0]


def test_count_active_tasks_honors_config_path(env, tmp_path: Path) -> None:
    """#1645: the active-task guard must read the work DB tied to --config.

    Seeds active work via the same factory ``_count_active_tasks`` uses,
    points it at the test's config, and asserts the count is non-zero —
    proving ``config_path`` is plumbed through. Before #1645's fix this
    test would observe 0 active tasks because the function ignored
    ``config_path`` and fell through to the default resolver.
    """
    from pollypm.plugins_builtin.project_planning.cli.project import (
        _count_active_tasks,
    )

    seeded_db = _seed_active_task(
        env["config_path"], project_key="demo", count=2,
    )
    # Sanity: the DB landed inside the test workspace, not the developer's
    # default workspace_root.
    assert seeded_db.is_relative_to(env["workspace_root"]), (
        f"seeded DB {seeded_db} is outside test workspace "
        f"{env['workspace_root']} — fixture is leaking"
    )

    active = _count_active_tasks("demo", env["config_path"])
    assert active == 2


def test_cli_remove_prompts_on_active_tasks_seeded_via_config(
    env, tmp_path: Path
) -> None:
    """End-to-end: ``pm project remove --config <path>`` sees active work.

    No ``_count_active_tasks`` monkeypatch — the prompt must fire purely
    because the seeded workspace DB resolved through ``--config`` reports
    non-zero non-terminal tasks. Reproduces the regression body for #1645.
    """
    _seed_active_task(env["config_path"], project_key="demo", count=1)

    result = runner.invoke(
        project_app,
        ["remove", "demo", "--config", str(env["config_path"])],
        input="n\n",
    )

    assert result.exit_code == 1, result.output
    assert "1 queued or in-flight task" in result.output
    assert "Aborted" in result.output

    # Config untouched — abort means no mutation.
    config = _load_cfg(env["config_path"])
    assert "demo" in config.projects


def test_count_active_tasks_does_not_leak_across_configs(
    env, tmp_path: Path
) -> None:
    """A second config with no seeded work must report 0 active tasks.

    Guards against the inverse regression: ``_count_active_tasks`` MUST
    NOT count tasks from any other workspace DB just because the default
    resolver would have landed there. Seeds work in config A, asks about
    config B → expect 0.
    """
    from pollypm.plugins_builtin.project_planning.cli.project import (
        _count_active_tasks,
    )

    _seed_active_task(env["config_path"], project_key="demo", count=3)

    # Build a second, independent config + workspace_root with no seeded
    # work. The two configs share no on-disk state.
    other_workspace = tmp_path / "other_dev"
    other_workspace.mkdir()
    other_project = other_workspace / "demo"
    other_project.mkdir()
    (other_project / ".git").mkdir()
    other_config = tmp_path / "other.toml"
    _write_config(
        other_config,
        workspace_root=other_workspace,
        project_path=other_project,
        slug="demo",
    )

    # The seeded config sees its 3 tasks.
    assert _count_active_tasks("demo", env["config_path"]) == 3
    # The clean config sees none — even though both use project key
    # "demo", the work DB lookup is keyed off the resolved workspace_root.
    assert _count_active_tasks("demo", other_config) == 0


# --------------------------------------------------------------------------
# --purge-state cascade: delete every project-scoped row from state.db
# (#1561 wedge #3).
# --------------------------------------------------------------------------


def _seed_state_db_rows(
    config_path: Path, project_key: str, *, task_count: int = 2
) -> Path:
    """Seed work_tasks, messages, and an audit-tail file for ``project_key``.

    Returns the workspace state.db path. Uses the same factory the
    production code uses so the test proves the wiring end-to-end. We
    seed across multiple project-scoped tables so the test confirms
    the sweep traverses more than just ``work_tasks``.
    """
    import sqlite3

    from pollypm.config import load_config as _load
    from pollypm.work import create_work_service

    config = _load(config_path)
    db_path_holder: list[Path] = []
    with create_work_service(project_key=project_key, config=config) as svc:
        db_path_holder.append(Path(svc._db_path))
        for idx in range(task_count):
            svc.create(
                title=f"seed task {idx}",
                description="seeded by test",
                type="task",
                project=project_key,
                flow_template="standard",
                roles={"worker": "agent-1", "reviewer": "agent-2"},
                priority="normal",
                created_by="test",
            )
    db_path = db_path_holder[0]

    # Bootstrap the SQLAlchemy-managed core schema (``messages``) AND
    # the legacy state-store schema (``worktrees``, ``architect_resume_tokens``,
    # …). The work-service factory only materializes ``work_*`` tables;
    # each of the other state.db tables has its own bootstrap path.
    from pollypm.store import SQLAlchemyStore
    from pollypm.storage.state import StateStore

    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        pass
    finally:
        store.close()
    legacy = StateStore(db_path)
    try:
        pass
    finally:
        legacy.close()

    # Seed messages + worktrees rows directly so the test exercises
    # the cross-table sweep, not just the work_* surface.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO messages "
            "(scope, type, tier, recipient, sender, state, "
            "subject, body, payload_json, labels, kind) "
            "VALUES (?, 'event', 'immediate', 'user', 'system', 'open', "
            "'seeded subj', 'seeded body', '{}', '[]', 'legacy')",
            (project_key,),
        )
        # worktrees table — separate state.db surface, project_key col.
        conn.execute(
            "INSERT INTO worktrees "
            "(project_key, lane_kind, lane_key, path, branch, status, "
            "created_at, updated_at) "
            "VALUES (?, 'feature', 'demo-lane', '/tmp/x', 'main', "
            "'active', '2026-01-01', '2026-01-01')",
            (project_key,),
        )
        conn.commit()
    finally:
        conn.close()

    return db_path


def _audit_tail_for(project_key: str, *, audit_home: Path) -> Path:
    """Return the central audit-tail path for ``project_key`` under ``audit_home``.

    Mirrors :func:`pollypm.audit.log.central_log_path` minus the env
    plumbing — tests redirect via ``POLLYPM_AUDIT_HOME`` so the
    production helper does the right thing.
    """
    return audit_home / f"{project_key}.jsonl"


def test_count_project_state_rows_reports_seeded_rows(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_count_project_state_rows`` returns per-table counts for seeded data."""
    from pollypm.plugins_builtin.project_planning.cli.project import (
        _count_project_state_rows,
    )

    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    _seed_state_db_rows(env["config_path"], "demo", task_count=3)
    tail_path = _audit_tail_for("demo", audit_home=audit_home)
    tail_path.write_text('{"event": "seeded"}\n')

    counts = _count_project_state_rows(env["config_path"], "demo")

    # work_tasks seeded above.
    assert counts["work_tasks"] == 3
    # messages seeded directly.
    assert counts["messages"] >= 1
    # worktrees seeded directly.
    assert counts["worktrees"] == 1
    # audit tail file is a single "row".
    assert counts["audit_tail"] == 1


def test_purge_project_state_deletes_rows_and_audit_tail(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_purge_project_state`` wipes every seeded row + the audit tail."""
    from pollypm.plugins_builtin.project_planning.cli.project import (
        _count_project_state_rows,
        _purge_project_state,
    )

    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    _seed_state_db_rows(env["config_path"], "demo", task_count=2)
    tail_path = _audit_tail_for("demo", audit_home=audit_home)
    tail_path.write_text('{"event": "seeded"}\n')
    assert tail_path.exists()

    removed = _purge_project_state(env["config_path"], "demo")

    assert removed["work_tasks"] == 2
    assert removed["audit_tail"] == 1

    # Post-purge counts are all zero.
    after = _count_project_state_rows(env["config_path"], "demo")
    assert after["work_tasks"] == 0
    assert after["messages"] == 0
    assert after["worktrees"] == 0
    assert after["audit_tail"] == 0
    assert not tail_path.exists()


def test_purge_project_state_dry_run_is_noop(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In dry-run mode counts mirror what WOULD be deleted; nothing mutated."""
    from pollypm.plugins_builtin.project_planning.cli.project import (
        _count_project_state_rows,
        _purge_project_state,
    )

    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    _seed_state_db_rows(env["config_path"], "demo", task_count=2)
    tail_path = _audit_tail_for("demo", audit_home=audit_home)
    tail_path.write_text('{"event": "seeded"}\n')

    preview = _purge_project_state(env["config_path"], "demo", dry_run=True)
    assert preview["work_tasks"] == 2
    assert preview["audit_tail"] == 1

    # Nothing actually deleted.
    after = _count_project_state_rows(env["config_path"], "demo")
    assert after["work_tasks"] == 2
    assert after["audit_tail"] == 1
    assert tail_path.exists()


def test_purge_project_state_does_not_touch_other_projects(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A purge of project A leaves project B's rows intact (FK isolation)."""
    from pollypm.plugins_builtin.project_planning.cli.project import (
        _count_project_state_rows,
        _purge_project_state,
    )

    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    # Add a second project to the same workspace so both seed into the
    # same state.db. The purge must scope by project key — no spillover.
    other_path = env["workspace_root"] / "other_proj"
    other_path.mkdir()
    (other_path / ".git").mkdir()
    config_text = env["config_path"].read_text()
    env["config_path"].write_text(
        config_text
        + f"\n[projects.other_proj]\n"
        f'key = "other_proj"\n'
        'name = "Other"\n'
        f'path = "{other_path}"\n'
    )

    _seed_state_db_rows(env["config_path"], "demo", task_count=2)
    _seed_state_db_rows(env["config_path"], "other_proj", task_count=3)

    _purge_project_state(env["config_path"], "demo")

    # demo gone.
    demo_after = _count_project_state_rows(env["config_path"], "demo")
    assert demo_after["work_tasks"] == 0
    assert demo_after["messages"] == 0

    # other_proj intact.
    other_after = _count_project_state_rows(env["config_path"], "other_proj")
    assert other_after["work_tasks"] == 3
    assert other_after["messages"] >= 1


def test_cli_remove_purge_state_full_cascade(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: ``pm project remove --purge-state --yes`` clears rows + removes the project."""
    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    _seed_state_db_rows(env["config_path"], "demo", task_count=2)
    tail_path = _audit_tail_for("demo", audit_home=audit_home)
    tail_path.write_text('{"event": "seeded"}\n')

    result = runner.invoke(
        project_app,
        [
            "remove", "demo", "--purge-state", "--yes",
            "--config", str(env["config_path"]),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output and "state.db row" in result.output
    assert "Removed central audit-tail JSONL" in result.output
    assert "Removed project 'demo'" in result.output

    config = _load_cfg(env["config_path"])
    assert "demo" not in config.projects
    assert not tail_path.exists()


def test_cli_remove_purge_state_prompts_without_force(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --yes / --force, --purge-state prompts before destructive action."""
    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    _seed_state_db_rows(env["config_path"], "demo", task_count=2)

    # Decline the destructive prompt. The active-task prompt also fires
    # first because we seeded queued tasks — we accept that with "y"
    # and decline the destructive purge prompt with "n".
    result = runner.invoke(
        project_app,
        [
            "remove", "demo", "--purge-state",
            "--config", str(env["config_path"]),
        ],
        input="y\nn\n",
    )

    assert result.exit_code == 1, result.output
    assert "Permanently delete state.db rows" in result.output
    assert "Aborted" in result.output

    # Nothing mutated.
    config = _load_cfg(env["config_path"])
    assert "demo" in config.projects


def test_cli_remove_dry_run_lists_state_rows_without_deleting(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` shows the row counts but never mutates state.db."""
    from pollypm.plugins_builtin.project_planning.cli.project import (
        _count_project_state_rows,
    )

    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    _seed_state_db_rows(env["config_path"], "demo", task_count=2)
    tail_path = _audit_tail_for("demo", audit_home=audit_home)
    tail_path.write_text('{"event": "seeded"}\n')

    result = runner.invoke(
        project_app,
        [
            "remove", "demo", "--dry-run", "--purge-state",
            "--config", str(env["config_path"]),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "state.db rows to purge:" in result.output
    assert "work_tasks: 2 rows" in result.output
    assert "audit_tail: 1 file" in result.output

    # Nothing actually deleted.
    after = _count_project_state_rows(env["config_path"], "demo")
    assert after["work_tasks"] == 2
    assert tail_path.exists()
    config = _load_cfg(env["config_path"])
    assert "demo" in config.projects


def test_cli_remove_dry_run_warns_when_state_rows_present_without_purge(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` without ``--purge-state`` flags the orphan-row footprint."""
    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    _seed_state_db_rows(env["config_path"], "demo", task_count=2)

    result = runner.invoke(
        project_app,
        [
            "remove", "demo", "--dry-run",
            "--config", str(env["config_path"]),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "use --purge-state to delete" in result.output
    assert "NOT touched without --purge-state" in result.output


# --------------------------------------------------------------------------
# Storage-facade unit tests (#1676): the bulk DELETE lives in
# ``pollypm.storage.project_state_purge`` so the plugin CLI no longer
# imports ``sqlite3`` directly. These exercises pin the facade contract.
# --------------------------------------------------------------------------


def test_storage_facade_count_rows_excludes_audit_tail(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facade returns DB-only keys; ``audit_tail`` is a CLI-layer concern."""
    from pollypm.plugins_builtin.project_planning.cli.project import (
        _workspace_db_path,
    )
    from pollypm.storage.project_state_purge import (
        count_project_state_rows,
    )

    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    _seed_state_db_rows(env["config_path"], "demo", task_count=2)
    # Audit tail file present, but the facade must not surface it.
    _audit_tail_for("demo", audit_home=audit_home).write_text("{}\n")

    counts = count_project_state_rows(
        _workspace_db_path(env["config_path"]), "demo",
    )

    # DB tables surface real counts.
    assert counts["work_tasks"] == 2
    assert counts["messages"] >= 1
    assert counts["worktrees"] == 1
    # Facade contract: no audit_tail key (that's the CLI shim's job).
    assert "audit_tail" not in counts


def test_storage_facade_purge_rows_is_transactional(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facade bulk DELETE wipes every seeded row in one pass."""
    from pollypm.plugins_builtin.project_planning.cli.project import (
        _workspace_db_path,
    )
    from pollypm.storage.project_state_purge import (
        count_project_state_rows,
        purge_project_state_rows,
    )

    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    _seed_state_db_rows(env["config_path"], "demo", task_count=2)
    db_path = _workspace_db_path(env["config_path"])

    removed = purge_project_state_rows(db_path, "demo")
    assert removed["work_tasks"] == 2

    # Post-purge: zero rows everywhere.
    after = count_project_state_rows(db_path, "demo")
    assert all(n == 0 for n in after.values())


def test_storage_facade_purge_rows_dry_run_is_noop(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dry_run=True`` returns would-delete counts without mutating."""
    from pollypm.plugins_builtin.project_planning.cli.project import (
        _workspace_db_path,
    )
    from pollypm.storage.project_state_purge import (
        count_project_state_rows,
        purge_project_state_rows,
    )

    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    _seed_state_db_rows(env["config_path"], "demo", task_count=3)
    db_path = _workspace_db_path(env["config_path"])

    preview = purge_project_state_rows(db_path, "demo", dry_run=True)
    assert preview["work_tasks"] == 3

    # Nothing actually deleted.
    after = count_project_state_rows(db_path, "demo")
    assert after["work_tasks"] == 3


def test_plugin_cli_does_not_import_sqlite3() -> None:
    """Regression for #1676: the plugin CLI module must not touch ``sqlite3``.

    Mirrors ``test_work_task_query_callers_do_not_open_sqlite_directly``
    but lives next to the implementation so the wedge it protects is
    obvious in the diff history.
    """
    from pollypm.plugins_builtin.project_planning.cli import project as cli_mod

    text = Path(cli_mod.__file__).read_text(encoding="utf-8")
    assert "import sqlite3" not in text
    assert "sqlite3.connect" not in text


# --------------------------------------------------------------------------
# Regression: #1673 — DB purge failure must NOT strip the TOML project.
# Before the fix, ``_purge_project_state`` caught ``sqlite3.Error`` and
# returned the pre-purge counts, so ``remove_cmd`` happily printed
# "Deleted N rows" and called ``remove_project`` — leaving rows in the
# DB and the project gone from pollypm.toml. The fix raises a
# ``_PurgeStateError`` (translated from the facade's
# ``ProjectStatePurgeError``) on hard DB failures so the command aborts
# before touching the config.
# --------------------------------------------------------------------------


def test_purge_project_state_raises_on_db_open_failure(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection failure surfaces as ``_PurgeStateError`` (#1673)."""
    import sqlite3

    from pollypm.plugins_builtin.project_planning.cli import (
        project as project_mod,
    )

    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    _seed_state_db_rows(env["config_path"], "demo", task_count=2)

    real_connect = sqlite3.connect

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    # Patch the sqlite3 module the storage facade imports.
    monkeypatch.setattr(sqlite3, "connect", boom)
    try:
        with pytest.raises(project_mod._PurgeStateError):
            project_mod._purge_project_state(env["config_path"], "demo")
    finally:
        monkeypatch.setattr(sqlite3, "connect", real_connect)


def test_purge_project_state_raises_on_commit_failure(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-transaction failure rolls back and raises (#1673)."""
    import sqlite3

    from pollypm.plugins_builtin.project_planning.cli import (
        project as project_mod,
    )

    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    _seed_state_db_rows(env["config_path"], "demo", task_count=2)

    real_connect = sqlite3.connect

    class _BoomConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            if sql.strip().upper().startswith("BEGIN"):
                raise sqlite3.OperationalError("database is locked")
            return self._inner.execute(sql, *args, **kwargs)

        def commit(self):
            return self._inner.commit()

        def rollback(self):
            return self._inner.rollback()

        def close(self):
            return self._inner.close()

    def wrap(*args, **kwargs):
        return _BoomConn(real_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", wrap)
    try:
        with pytest.raises(project_mod._PurgeStateError):
            project_mod._purge_project_state(env["config_path"], "demo")
    finally:
        monkeypatch.setattr(sqlite3, "connect", real_connect)

    # Rows still present — purge never committed.
    after = project_mod._count_project_state_rows(env["config_path"], "demo")
    assert after["work_tasks"] == 2


def test_cli_remove_purge_state_aborts_when_db_purge_fails(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pm project remove --purge-state`` does NOT strip TOML on DB failure (#1673)."""
    from pollypm.plugins_builtin.project_planning.cli import (
        project as project_mod,
    )

    audit_home = tmp_path / "audit"
    audit_home.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    _seed_state_db_rows(env["config_path"], "demo", task_count=2)
    tail_path = _audit_tail_for("demo", audit_home=audit_home)
    tail_path.write_text('{"event": "seeded"}\n')

    def boom(*_args, **_kwargs):
        raise project_mod._PurgeStateError("simulated locked DB")

    monkeypatch.setattr(project_mod, "_purge_project_state", boom)

    result = runner.invoke(
        project_app,
        [
            "remove", "demo", "--purge-state", "--yes",
            "--config", str(env["config_path"]),
        ],
    )

    # Command must exit non-zero with an explicit error message.
    assert result.exit_code == 1, result.output
    assert "state.db purge failed" in result.output
    assert "Aborted" in result.output

    # CRITICAL: project entry still in pollypm.toml.
    config = _load_cfg(env["config_path"])
    assert "demo" in config.projects

    # CRITICAL: no "Removed project" line emitted.
    assert "Removed project 'demo'" not in result.output

    # Audit tail untouched (purge bailed before audit cleanup too).
    assert tail_path.exists()
