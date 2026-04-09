from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.config import write_config
from pollypm.models import ProjectSettings, PollyPMConfig, PollyPMSettings

from pollypm.projects import (
    detect_project_kind,
    discover_git_repositories,
    discover_recent_git_repositories,
    enable_tracked_project,
    make_project_key,
    normalize_project_path,
    register_project,
)


def test_discover_git_repositories_finds_nested_repos(tmp_path: Path) -> None:
    repo_one = tmp_path / "dev" / "wire"
    repo_two = tmp_path / "clients" / "acme"
    (repo_one / ".git").mkdir(parents=True)
    (repo_two / ".git").mkdir(parents=True)
    (tmp_path / ".cache" / "ignored" / ".git").mkdir(parents=True)

    found = discover_git_repositories(tmp_path)

    assert found == [repo_two.resolve(), repo_one.resolve()]


def test_discover_git_repositories_skips_known_paths(tmp_path: Path) -> None:
    repo = tmp_path / "dev" / "wire"
    (repo / ".git").mkdir(parents=True)

    found = discover_git_repositories(tmp_path, known_paths={repo})

    assert found == []


def test_discover_recent_git_repositories_filters_by_recent_commit(monkeypatch, tmp_path: Path) -> None:
    recent_repo = tmp_path / "dev" / "recent"
    stale_repo = tmp_path / "dev" / "stale"
    (recent_repo / ".git").mkdir(parents=True)
    (stale_repo / ".git").mkdir(parents=True)

    recent_cutoff = datetime.now(UTC)

    def fake_last_commit(path: Path):
        if path == recent_repo.resolve():
            return recent_cutoff - timedelta(days=2)
        if path == stale_repo.resolve():
            return recent_cutoff - timedelta(days=30)
        return None

    monkeypatch.setattr("pollypm.projects.repository_last_local_commit_at", fake_last_commit)

    found = discover_recent_git_repositories(tmp_path, recent_days=14)

    assert found == [recent_repo.resolve()]


def test_discover_recent_git_repositories_skips_repos_without_local_commits(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "dev" / "foreign"
    (repo / ".git").mkdir(parents=True)

    monkeypatch.setattr("pollypm.projects.repository_last_local_commit_at", lambda _path: None)

    assert discover_recent_git_repositories(tmp_path, recent_days=14) == []


def test_make_project_key_adds_suffix_for_duplicates() -> None:
    assert make_project_key(Path("/Users/sam/dev/wire"), {"wire"}) == "wire_2"
    assert normalize_project_path(Path("~/dev")).is_absolute()


def test_register_project_accepts_plain_folder_and_can_enable_tracker(tmp_path: Path) -> None:
    project_path = tmp_path / "plain-project"
    project_path.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(root_dir=tmp_path, base_dir=tmp_path / ".pollypm", logs_dir=tmp_path / ".pollypm/logs", snapshots_dir=tmp_path / ".pollypm/snapshots", state_db=tmp_path / ".pollypm/state.db"),
        pollypm=PollyPMSettings(controller_account=""),
        accounts={},
        sessions={},
        projects={},
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

    project = register_project(config_path, project_path, name="Plain")
    assert detect_project_kind(project.path).value == "folder"
    assert (project_path / ".pollypm").exists()

    tracked = enable_tracked_project(config_path, project.key)
    assert tracked.tracked is True
    assert (project_path / "issues" / "03-needs-review").exists()
    assert (project_path / "issues" / ".latest_issue_number").exists()
