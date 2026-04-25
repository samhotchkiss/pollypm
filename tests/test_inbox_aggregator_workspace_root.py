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


def _seed_message(
    db_path: Path,
    *,
    scope: str = "demo",
    type_: str = "notify",
    state: str = "open",
    subject: str = "[Action] notify title",
    payload: dict | None = None,
    labels: list[str] | None = None,
) -> int:
    """Seed one user-recipient message directly into the unified store."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    from pollypm.store import SQLAlchemyStore
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        return store.enqueue_message(
            type=type_,
            tier="immediate",
            scope=scope,
            sender="polly",
            recipient="user",
            subject=subject,
            body="notify body",
            state=state,
            payload=payload,
            labels=labels,
        )
    finally:
        store.close()


def test_aggregator_counts_actionable_messages_not_just_tasks(env) -> None:
    """The rail badge counts actionable notify/inbox_task/alert messages.

    FYI activity stays discoverable in the inbox all-messages lens, but
    it must not inflate the rail badge.
    """
    # One work-service task so the task-only path is still exercised.
    _seed(env["project_db"], env["project_path"],
          project="demo", title="real chat task")

    # Three notify messages directly in the unified store (same shape
    # that ``pm notify`` / loop-test harnesses produce). All must
    # contribute to the aggregate count.
    for _ in range(3):
        _seed_message(env["workspace_db"], scope="inbox")

    count = _count_inbox_tasks_for_label(_load_cfg(env["config_path"]))
    assert count == 4, f"expected 1 task + 3 notifies, got {count}"


def test_aggregator_skips_fyi_messages(env) -> None:
    """Completion/FYI notifications should not inflate the rail badge."""
    _seed_message(env["workspace_db"], scope="inbox", subject="Demo shipped cleanly")

    count = _count_inbox_tasks_for_label(_load_cfg(env["config_path"]))
    assert count == 0, f"expected no actionable messages, got {count}"


def test_aggregator_skips_non_open_messages(env) -> None:
    """Only state=open messages contribute. Archived / resolved don't."""
    # Seed one open + one archived so we can verify filter.
    _seed_message(env["workspace_db"], scope="inbox")
    _seed_message(env["workspace_db"], scope="inbox", subject="archived", state="archived")

    count = _count_inbox_tasks_for_label(_load_cfg(env["config_path"]))
    # Only the open notify counts; archived one is excluded.
    assert count == 1, f"expected 1 open notify, got {count}"


def test_aggregator_skips_deleted_project_workspace_messages(env) -> None:
    """Workspace-root messages for untracked projects stay out of the badge."""
    _seed_message(env["workspace_db"], scope="demo", subject="[Action] live project")
    _seed_message(
        env["workspace_db"],
        scope="ghost_proj",
        subject="[Action] deleted project",
    )

    count = _count_inbox_tasks_for_label(_load_cfg(env["config_path"]))
    assert count == 1, (
        f"expected only tracked-project messages to count, got {count}"
    )


def test_aggregator_skips_deleted_project_payload_messages(env) -> None:
    """Workspace-root inbox-scope messages can still point at deleted projects."""
    _seed_message(
        env["workspace_db"],
        scope="inbox",
        subject="[Action] live project payload",
        payload={"project": "demo"},
    )
    _seed_message(
        env["workspace_db"],
        scope="inbox",
        subject="[Action] deleted project payload",
        payload={"project": "ghost_proj"},
    )

    count = _count_inbox_tasks_for_label(_load_cfg(env["config_path"]))
    assert count == 1, (
        f"expected only tracked-project payload messages to count, got {count}"
    )


def test_archive_deleted_project_messages_dry_run_does_not_close(
    env, monkeypatch,
) -> None:
    """Cleanup preview reports stale project refs without mutating rows."""
    _pin_load_config(monkeypatch, env["config_path"])
    _seed_message(env["workspace_db"], scope="demo", subject="live project")
    _seed_message(env["workspace_db"], scope="ghost_proj", subject="deleted scope")
    _seed_message(
        env["workspace_db"],
        scope="inbox",
        subject="deleted payload",
        payload={"project": "ghost_payload"},
    )

    result = runner.invoke(
        inbox_app,
        [
            "archive",
            "--deleted-projects",
            "--dry-run",
            "--db",
            str(env["workspace_db"]),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Would archive 2 deleted-project messages" in result.output
    from pollypm.store import SQLAlchemyStore

    store = SQLAlchemyStore(f"sqlite:///{env['workspace_db']}")
    try:
        assert len(store.query_messages(recipient="user", state="open")) == 3
    finally:
        store.close()


def test_archive_deleted_project_messages_closes_only_stale_refs(
    env, monkeypatch,
) -> None:
    """Stale raw-store inbox rows get closed, tracked-project rows remain open."""
    _pin_load_config(monkeypatch, env["config_path"])
    _seed_message(env["workspace_db"], scope="demo", subject="live project")
    _seed_message(env["workspace_db"], scope="ghost_proj", subject="deleted scope")
    _seed_message(
        env["workspace_db"],
        scope="inbox",
        subject="deleted label",
        labels=["project:ghost_label"],
    )

    result = runner.invoke(
        inbox_app,
        ["archive", "--deleted-projects", "--db", str(env["workspace_db"])],
    )

    assert result.exit_code == 0, result.output
    assert "Archived 2 deleted-project messages." in result.output
    from pollypm.store import SQLAlchemyStore

    store = SQLAlchemyStore(f"sqlite:///{env['workspace_db']}")
    try:
        open_subjects = {
            row["subject"]
            for row in store.query_messages(recipient="user", state="open")
        }
        closed_subjects = {
            row["subject"]
            for row in store.query_messages(recipient="user", state="closed")
        }
    finally:
        store.close()
    assert len(open_subjects) == 1
    assert next(iter(open_subjects)).endswith("live project")
    assert {subject.rsplit("] ", 1)[-1] for subject in closed_subjects} == {
        "deleted scope",
        "deleted label",
    }


def test_aggregator_skips_dev_channel_messages(env) -> None:
    """#754: messages tagged with ``channel:dev`` are test/debug
    traffic and must NOT count against the user-facing rail badge."""
    from pollypm.store import SQLAlchemyStore

    env["workspace_db"].parent.mkdir(parents=True, exist_ok=True)
    store = SQLAlchemyStore(f"sqlite:///{env['workspace_db']}")
    try:
        # One inbox-channel (implicit, no label).
        store.enqueue_message(
            type="notify", tier="immediate", scope="inbox",
            sender="polly", recipient="user",
            subject="[Action] real action", body="body",
        )
        # Two dev-channel test notifications.
        for i in range(2):
            store.enqueue_message(
                type="notify", tier="immediate", scope="inbox",
                sender="polly", recipient="user",
                subject=f"dev-test-{i}", body="body",
                labels=["channel:dev"],
            )
    finally:
        store.close()

    count = _count_inbox_tasks_for_label(_load_cfg(env["config_path"]))
    # Only the one inbox-channel message counts.
    assert count == 1, (
        f"expected 1 user-facing notify, got {count} — dev-channel "
        "messages must be hidden from the rail badge"
    )
