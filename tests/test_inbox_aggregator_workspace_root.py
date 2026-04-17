"""Cockpit inbox aggregator + CLI default DB — workspace-root resolution (#271).

``pm notify`` (with defaults) writes to ``<workspace_root>/.pollypm/state.db``.
The cockpit's inbox count scans per-project DBs *and* the workspace-root DB
so notifications are never invisible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.cli import app as root_app
from pollypm.cockpit import _count_inbox_tasks_for_label
from pollypm.work.inbox_cli import inbox_app
from pollypm.work.sqlite_service import SQLiteWorkService


runner = CliRunner()


def _write_config(
    workspace_root: Path, project_path: Path, config_path: Path,
) -> None:
    config_path.write_text(
        "[project]\n"
        'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{workspace_root}"\n'
        "\n"
        "[projects.demo]\n"
        'key = "demo"\n'
        'name = "Demo"\n'
        f'path = "{project_path}"\n'
    )


def _seed(db_path: Path, project_path: Path, *, project: str, title: str) -> str:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task = svc.create(
            title=title, description=f"body for {title}", type="task",
            project=project, flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            priority="normal", created_by="polly",
        )
        return task.task_id
    finally:
        svc.close()


@pytest.fixture
def env(tmp_path: Path):
    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project_path = workspace_root / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_config(workspace_root, project_path, config_path)
    return {
        "workspace_root": workspace_root,
        "project_path": project_path,
        "config_path": config_path,
        "project_db": project_path / ".pollypm" / "state.db",
        "workspace_db": workspace_root / ".pollypm" / "state.db",
    }


def _pin_load_config(monkeypatch, config_path: Path) -> None:
    """Redirect no-arg ``load_config()`` to the test workspace config."""
    from pollypm import config as config_module
    real_load = config_module.load_config
    monkeypatch.setattr(
        config_module, "load_config",
        lambda path=None: real_load(config_path if path is None else path),
    )


def _load_cfg(config_path: Path):
    from pollypm.config import load_config
    return load_config(config_path)


def test_notify_default_db_writes_to_workspace_root(
    env, tmp_path: Path, monkeypatch,
) -> None:
    """``pm notify`` without ``--db`` lands in ``<workspace_root>/.pollypm/state.db``."""
    _pin_load_config(monkeypatch, env["config_path"])
    cwd = tmp_path / "somewhere_else"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    result = runner.invoke(
        root_app, ["notify", "Deploy blocked", "Needs verification"],
    )
    assert result.exit_code == 0, result.output

    assert env["workspace_db"].exists()
    # The cwd-relative DB must NOT have been created.
    assert not (cwd / ".pollypm" / "state.db").exists()


def test_inbox_default_db_reads_from_workspace_root(
    env, tmp_path: Path, monkeypatch,
) -> None:
    """``pm inbox`` without ``--db`` reads from the workspace-root DB."""
    task_id = _seed(
        env["workspace_db"], env["workspace_root"],
        project="inbox", title="only workspace item",
    )
    _pin_load_config(monkeypatch, env["config_path"])
    cwd = tmp_path / "somewhere_else"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    result = runner.invoke(inbox_app, ["--json"])
    assert result.exit_code == 0, result.output
    returned = [t["task_id"] for t in json.loads(result.output)["tasks"]]
    assert task_id in returned


def test_aggregator_counts_both_project_and_workspace_root(env) -> None:
    """Cockpit aggregator sums per-project + workspace-root inbox items."""
    _seed(env["project_db"], env["project_path"],
          project="demo", title="project-local item")
    _seed(env["workspace_db"], env["workspace_root"],
          project="inbox", title="workspace-root item")
    assert _count_inbox_tasks_for_label(_load_cfg(env["config_path"])) == 2


def test_aggregator_dedupes_same_task_id_across_sources(env) -> None:
    """If the same task_id appears in two DBs, it's counted once."""
    _seed(env["project_db"], env["project_path"],
          project="demo", title="first copy")
    _seed(env["workspace_db"], env["workspace_root"],
          project="demo", title="second copy")
    # Both DBs numbered their tasks ``demo/1`` — dedupe on task_id.
    assert _count_inbox_tasks_for_label(_load_cfg(env["config_path"])) == 1


def test_aggregator_skips_missing_workspace_root_db(env) -> None:
    """Aggregator must not raise when the workspace-root DB is absent."""
    _seed(env["project_db"], env["project_path"],
          project="demo", title="only project")
    assert not env["workspace_db"].exists()
    assert _count_inbox_tasks_for_label(_load_cfg(env["config_path"])) == 1
