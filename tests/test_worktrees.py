import subprocess
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
from pollypm.storage.state import StateStore
from pollypm.worktrees import cleanup_worktree, ensure_worktree, list_worktrees


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


def test_worktree_lifecycle(tmp_path: Path) -> None:
    repo = _git_project(tmp_path)
    config = _config(tmp_path, repo)
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

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


def test_ensure_worktree_rejects_invalid_project_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_project(tmp_path)
    config = _config(tmp_path, repo, project_key="demo/evil")
    monkeypatch.setattr("pollypm.worktrees.load_config", lambda _: config)
    monkeypatch.setattr(
        "pollypm.worktrees.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("git should not be called")),
    )

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

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        Path(cmd[8]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("pollypm.worktrees.load_config", lambda _: config)
    monkeypatch.setattr("pollypm.worktrees.subprocess.run", fake_run)

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
    store = StateStore(config.project.state_db)
    store.upsert_worktree(
        project_key="demo",
        lane_kind="pa",
        lane_key="worker_demo",
        session_name="worker_demo",
        issue_key=None,
        path=str(worktree_path),
        branch="pollypm/demo/pa/worker_demo",
        status="active",
    )
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("pollypm.worktrees.load_config", lambda _: config)
    monkeypatch.setattr("pollypm.worktrees.subprocess.run", fake_run)

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
