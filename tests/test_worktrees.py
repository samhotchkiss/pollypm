import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest
import typer

from pollypm.config import write_config
from pollypm.models import (
    AccountConfig,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PollyPMConfig,
    PollyPMSettings,
    ProviderKind,
)
from pollypm.storage.records import WorktreeRecord
from pollypm.worktrees import (
    ARCHITECT_WORKTREE_STATUS_PATH,
    cleanup_worktree,
    ensure_worktree,
    list_worktrees,
    refresh_architect_worktree,
)


def _git_project(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "sam@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Sam"], check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)
    return repo


def _config(tmp_path: Path, repo: Path, *, project_key: str = "demo") -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(root_dir=tmp_path, base_dir=tmp_path / ".pollypm", logs_dir=tmp_path / ".pollypm/logs", snapshots_dir=tmp_path / ".pollypm/snapshots", state_db=tmp_path / ".pollypm/state.db"),
        pollypm=PollyPMSettings(controller_account="codex_primary"),
        accounts={
            "codex_primary": AccountConfig(
                name="codex_primary",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                home=tmp_path / ".pollypm/homes/codex_primary",
            )
        },
        sessions={},
        projects={
            project_key: KnownProject(
                key=project_key,
                path=repo,
                name="Demo",
                kind=ProjectKind.GIT,
                tracked=True,
            )
        },
    )


def _install_memory_worktree_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> list[WorktreeRecord]:
    records: list[WorktreeRecord] = []

    def list_records(_config, project_key):
        if project_key is None:
            return list(records)
        return [record for record in records if record.project_key == project_key]

    def upsert_record(_config, **kwargs):
        for record in records:
            if (
                record.project_key == kwargs["project_key"]
                and record.lane_kind == kwargs["lane_kind"]
                and record.lane_key == kwargs["lane_key"]
                and record.status == kwargs["status"]
            ):
                record.session_name = kwargs["session_name"]
                record.issue_key = kwargs["issue_key"]
                record.path = kwargs["path"]
                record.branch = kwargs["branch"]
                record.updated_at = "now"
                return
        records.append(
            WorktreeRecord(
                project_key=kwargs["project_key"],
                lane_kind=kwargs["lane_kind"],
                lane_key=kwargs["lane_key"],
                session_name=kwargs["session_name"],
                issue_key=kwargs["issue_key"],
                path=kwargs["path"],
                branch=kwargs["branch"],
                status=kwargs["status"],
                created_at="now",
                updated_at="now",
            )
        )

    def update_status(_config, project_key, lane_kind, lane_key, status):
        for record in records:
            if (
                record.project_key == project_key
                and record.lane_kind == lane_kind
                and record.lane_key == lane_key
                and record.status == "active"
            ):
                record.status = status
                record.updated_at = "now"

    monkeypatch.setattr(
        "pollypm.worktrees._list_worktrees_for_backend",
        list_records,
    )
    monkeypatch.setattr(
        "pollypm.worktrees._upsert_worktree_for_backend",
        upsert_record,
    )
    monkeypatch.setattr(
        "pollypm.worktrees._update_worktree_status_for_backend",
        update_status,
    )
    return records


def test_worktree_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_project(tmp_path)
    config = _config(tmp_path, repo)
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    _install_memory_worktree_backend(monkeypatch)

    worktree = ensure_worktree(
        config_path,
        project_key="demo",
        lane_kind="pa",
        lane_key="worker_demo",
        session_name="worker_demo",
    )
    assert worktree is not None
    assert Path(worktree.path).exists()
    assert Path(worktree.path).parent.name == "worker_demo"
    assert (Path(worktree.path).parent / ".session.worker_demo.lock").exists()
    assert list_worktrees(config_path, "demo")

    removed = cleanup_worktree(config_path, project_key="demo", lane_kind="pa", lane_key="worker_demo", force=True)
    assert removed == Path(worktree.path)


def test_refresh_architect_worktree_fast_forwards_clean_checkout(
    tmp_path: Path,
) -> None:
    repo = _git_project(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
    worktree_path = tmp_path / "architect_demo"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-B",
            "pollypm/demo/architect/architect_demo",
            "--",
            str(worktree_path),
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    docs_dir = repo / "docs" / "test-plan"
    docs_dir.mkdir(parents=True)
    (docs_dir / "README.md").write_text("current plan\n")
    subprocess.run(["git", "-C", str(repo), "add", "docs/test-plan/README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "add test plan"],
        check=True,
        capture_output=True,
        text=True,
    )

    result = refresh_architect_worktree(repo, worktree_path)

    assert result.status == "updated"
    assert (worktree_path / "docs" / "test-plan" / "README.md").read_text() == "current plan\n"
    assert not (worktree_path / ARCHITECT_WORKTREE_STATUS_PATH).exists()


def test_refresh_architect_worktree_surfaces_dirty_staleness(
    tmp_path: Path,
) -> None:
    repo = _git_project(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
    worktree_path = tmp_path / "architect_demo"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-B",
            "pollypm/demo/architect/architect_demo",
            "--",
            str(worktree_path),
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    (worktree_path / "local-notes.md").write_text("operator notes\n")

    docs_dir = repo / "docs" / "test-plan"
    docs_dir.mkdir(parents=True)
    (docs_dir / "README.md").write_text("current plan\n")
    subprocess.run(["git", "-C", str(repo), "add", "docs/test-plan/README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "add test plan"],
        check=True,
        capture_output=True,
        text=True,
    )

    result = refresh_architect_worktree(repo, worktree_path)

    assert result.status == "stale"
    assert result.reason == "worktree has uncommitted changes"
    assert not (worktree_path / "docs" / "test-plan" / "README.md").exists()
    assert (worktree_path / "local-notes.md").read_text() == "operator notes\n"
    marker = worktree_path / ARCHITECT_WORKTREE_STATUS_PATH
    assert marker.exists()
    marker_text = marker.read_text(encoding="utf-8")
    assert "worktree has uncommitted changes" in marker_text
    assert "Do not reset, delete, or discard" in marker_text


def test_ensure_worktree_refreshes_existing_architect_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _git_project(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
    config = _config(tmp_path, repo)
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    _install_memory_worktree_backend(monkeypatch)

    worktree = ensure_worktree(
        config_path,
        project_key="demo",
        lane_kind="architect",
        lane_key="architect_demo",
        session_name="architect_demo",
    )
    assert worktree is not None
    worktree_path = Path(worktree.path)

    docs_dir = repo / "docs" / "test-plan"
    docs_dir.mkdir(parents=True)
    (docs_dir / "README.md").write_text("current plan\n")
    subprocess.run(["git", "-C", str(repo), "add", "docs/test-plan/README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "add test plan"],
        check=True,
        capture_output=True,
        text=True,
    )

    reused = ensure_worktree(
        config_path,
        project_key="demo",
        lane_kind="architect",
        lane_key="architect_demo",
        session_name="architect_demo",
    )

    assert reused is not None
    assert Path(reused.path) == worktree_path
    assert (worktree_path / "docs" / "test-plan" / "README.md").read_text() == "current plan\n"
    assert not (worktree_path / ARCHITECT_WORKTREE_STATUS_PATH).exists()


def test_ensure_worktree_rejects_invalid_project_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_project(tmp_path)
    config = _config(tmp_path, repo, project_key="demo/evil")
    monkeypatch.setattr("pollypm.worktrees.load_config", lambda _: config)

    with pytest.raises(typer.BadParameter, match="project_key contains invalid characters"):
        ensure_worktree(
            tmp_path / "pollypm.toml",
            project_key="demo/evil",
            lane_kind="pa",
            lane_key="worker_demo",
            session_name="worker_demo",
        )


def test_ensure_worktree_adds_separator_before_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _git_project(tmp_path)
    config = _config(tmp_path, repo)
    commands: list[list[str]] = []
    _install_memory_worktree_backend(monkeypatch)

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["git", "-C", str(repo)] and cmd[3:5] == [
            "worktree",
            "add",
        ]:
            commands.append(cmd)
            Path(cmd[8]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("pollypm.worktrees.load_config", lambda _: config)
    monkeypatch.setattr(
        "pollypm.worktrees.subprocess",
        SimpleNamespace(run=fake_run, CompletedProcess=subprocess.CompletedProcess),
    )

    worktree = ensure_worktree(
        tmp_path / "pollypm.toml",
        project_key="demo",
        lane_kind="pa",
        lane_key="worker_demo",
        session_name="worker_demo",
    )

    assert worktree is not None
    assert commands == [[
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        "-B",
        "pollypm/demo/pa/worker_demo",
        "--",
        worktree.path,
        "HEAD",
    ]]


def test_cleanup_worktree_adds_separator_before_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _git_project(tmp_path)
    config = _config(tmp_path, repo)
    config_path = tmp_path / "pollypm.toml"
    worktree_path = tmp_path / "worker_demo" / "demo-pa-worker_demo"
    records = _install_memory_worktree_backend(monkeypatch)
    records.append(
        WorktreeRecord(
            project_key="demo",
            lane_kind="pa",
            lane_key="worker_demo",
            session_name="worker_demo",
            issue_key=None,
            path=str(worktree_path),
            branch="pollypm/demo/pa/worker_demo",
            status="active",
            created_at="now",
            updated_at="now",
        )
    )
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("pollypm.worktrees.load_config", lambda _: config)
    monkeypatch.setattr(
        "pollypm.worktrees.subprocess",
        SimpleNamespace(run=fake_run, CompletedProcess=subprocess.CompletedProcess),
    )

    removed = cleanup_worktree(
        config_path,
        project_key="demo",
        lane_kind="pa",
        lane_key="worker_demo",
        force=True,
    )

    assert removed == worktree_path
    assert commands == [
        ["git", "-C", str(repo), "worktree", "remove", "--force", "--", str(worktree_path)],
        ["git", "-C", str(repo), "worktree", "prune"],
    ]
