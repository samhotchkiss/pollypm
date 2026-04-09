import subprocess
from pathlib import Path

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


def test_worktree_lifecycle(tmp_path: Path) -> None:
    repo = _git_project(tmp_path)
    config = PollyPMConfig(
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
            "demo": KnownProject(key="demo", path=repo, name="Demo", kind=ProjectKind.GIT, tracked=True)
        },
    )
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
    assert list_worktrees(config_path, "demo")

    removed = cleanup_worktree(config_path, project_key="demo", lane_kind="pa", lane_key="worker_demo", force=True)
    assert removed == Path(worktree.path)
