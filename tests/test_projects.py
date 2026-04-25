from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.config import write_config
from pollypm.models import KnownProject, ProjectKind, ProjectSettings, PollyPMConfig, PollyPMSettings
import pytest
from pollypm.task_backends.github import GitHubTaskBackendValidation

from pollypm.projects import (
    default_persona_name,
    detect_project_kind,
    discover_git_repositories,
    discover_recent_git_repositories,
    enable_tracked_project,
    ensure_project_scaffold,
    ensure_session_lock,
    make_project_key,
    normalize_project_path,
    release_session_lock,
    register_project,
    scaffold_issue_tracker,
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
    assert default_persona_name("pollypm") == "Pete"
    assert default_persona_name("news") == "Nora"


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
    assert project.persona_name == "Pete"
    assert (project_path / ".pollypm").exists()
    assert (project_path / ".pollypm" / "config" / "project.toml").exists()
    assert 'persona_name = "Pete"' in (project_path / ".pollypm" / "config" / "project.toml").read_text()

    tracked = enable_tracked_project(config_path, project.key)
    assert tracked.tracked is True
    assert (project_path / "issues" / "03-needs-review").exists()
    assert (project_path / "issues" / ".latest_issue_number").exists()


def test_register_project_accepts_explicit_slug_override(tmp_path: Path) -> None:
    """#766: callers (CLI --slug, cockpit slug picker) can pin the
    project key instead of relying on auto-derivation."""
    project_path = tmp_path / "weird-directory-name"
    project_path.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path, base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account=""),
        accounts={},
        sessions={},
        projects={},
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

    project = register_project(config_path, project_path, slug="widget_shop")
    assert project.key == "widget_shop"


def test_register_project_rejects_non_canonical_slug(tmp_path: Path) -> None:
    """Uppercase / hyphens / punctuation in --slug must be rejected
    with a hint showing the canonical form."""
    import typer
    project_path = tmp_path / "whatever"
    project_path.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path, base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account=""),
        accounts={},
        sessions={},
        projects={},
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

    with pytest.raises(typer.BadParameter):
        register_project(config_path, project_path, slug="Widget-Shop")


def test_register_project_rejects_duplicate_slug(tmp_path: Path) -> None:
    """If the requested --slug is already taken, reject with a clean
    error rather than silently overwriting."""
    import typer
    existing = tmp_path / "existing"
    existing.mkdir()
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path, base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account=""),
        accounts={},
        sessions={},
        projects={
            "my_slug": KnownProject(
                key="my_slug", path=existing, name="Existing",
                kind=ProjectKind.FOLDER,
            ),
        },
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

    with pytest.raises(typer.BadParameter):
        register_project(config_path, incoming, slug="my_slug")


def test_ensure_project_scaffold_copies_project_instructions(tmp_path: Path) -> None:
    project_path = tmp_path / "sample-project"
    project_path.mkdir()

    ensure_project_scaffold(project_path)

    instructions_path = project_path / ".pollypm" / "INSTRUCT.md"
    assert instructions_path.exists()
    assert "Test and operate PollyPM through Polly chat" in instructions_path.read_text()


def test_scaffold_issue_tracker_for_github_backend_does_not_create_local_issue_tracker(monkeypatch, tmp_path: Path) -> None:
    project_path = tmp_path / "sample-project"
    project_path.mkdir()
    config_dir = project_path / ".pollypm" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "project.toml").write_text(
        """
[plugins]
issue_backend = "github"

[plugins.github_issues]
repo = "acme/widgets"
"""
    )

    monkeypatch.setattr(
        "pollypm.task_backends.github.GitHubTaskBackend.validate",
        lambda self: GitHubTaskBackendValidation(passed=True, checks=["repo_accessible"], errors=[]),
    )

    issues_root = scaffold_issue_tracker(project_path)

    assert issues_root == project_path
    assert not (project_path / "issues").exists()
    gitignore_text = (project_path / ".gitignore").read_text() if (project_path / ".gitignore").exists() else ""
    assert "issues/" not in gitignore_text


def test_scaffold_issue_tracker_validates_github_backend_on_activation(monkeypatch, tmp_path: Path) -> None:
    project_path = tmp_path / "sample-project"
    project_path.mkdir()
    config_dir = project_path / ".pollypm" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "project.toml").write_text(
        """
[plugins]
issue_backend = "github"

[plugins.github_issues]
repo = "acme/widgets"
"""
    )

    monkeypatch.setattr(
        "pollypm.task_backends.github.GitHubTaskBackend.validate",
        lambda self: GitHubTaskBackendValidation(passed=True, checks=["repo_accessible"], errors=[]),
    )

    issues_root = scaffold_issue_tracker(project_path)

    assert issues_root == project_path


def test_scaffold_issue_tracker_raises_when_github_backend_validation_fails(monkeypatch, tmp_path: Path) -> None:
    project_path = tmp_path / "sample-project"
    project_path.mkdir()
    config_dir = project_path / ".pollypm" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "project.toml").write_text(
        """
[plugins]
issue_backend = "github"

[plugins.github_issues]
repo = "acme/widgets"
"""
    )

    monkeypatch.setattr(
        "pollypm.task_backends.github.GitHubTaskBackend.validate",
        lambda self: GitHubTaskBackendValidation(passed=False, checks=["repo_accessible"], errors=["auth failed"]),
    )

    with pytest.raises(RuntimeError, match="Task backend validation failed: auth failed"):
        scaffold_issue_tracker(project_path)


def test_enable_tracked_project_supports_file_and_github_backends_side_by_side(monkeypatch, tmp_path: Path) -> None:
    file_project = tmp_path / "file-project"
    github_project = tmp_path / "github-project"
    file_project.mkdir()
    github_project.mkdir()

    config_dir = github_project / ".pollypm" / "config"
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account=""),
        accounts={},
        sessions={},
        projects={
            "file_demo": KnownProject(
                key="file_demo",
                path=file_project,
                name="File Demo",
                kind=ProjectKind.FOLDER,
                tracked=False,
            ),
            "github_demo": KnownProject(
                key="github_demo",
                path=github_project,
                name="GitHub Demo",
                kind=ProjectKind.FOLDER,
                tracked=False,
            ),
        },
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "project.toml").write_text(
        """
[plugins]
issue_backend = "github"

[plugins.github_issues]
repo = "acme/widgets"
"""
    )

    monkeypatch.setattr(
        "pollypm.task_backends.github.GitHubTaskBackend.validate",
        lambda self: GitHubTaskBackendValidation(passed=True, checks=["repo_accessible"], errors=[]),
    )

    enable_tracked_project(config_path, "file_demo")
    (config_dir / "project.toml").write_text(
        """
[plugins]
issue_backend = "github"

[plugins.github_issues]
repo = "acme/widgets"
"""
    )
    enable_tracked_project(config_path, "github_demo")

    assert (file_project / "issues" / "01-ready").exists()
    assert (file_project / "issues" / ".latest_issue_number").exists()
    assert not (github_project / "issues").exists()
    gitignore_text = (github_project / ".gitignore").read_text() if (github_project / ".gitignore").exists() else ""
    assert "issues/" not in gitignore_text


def test_session_lock_is_atomic_idempotent_and_releasable(tmp_path: Path) -> None:
    lock_root = tmp_path / "locks" / "worker"

    first = ensure_session_lock(lock_root, "worker")
    second = ensure_session_lock(lock_root, "worker")
    other = ensure_session_lock(lock_root, "other")

    assert first == second
    assert other.exists()

    release_session_lock(lock_root, "worker")
    assert not first.exists()
    assert other.exists()


def test_session_lock_is_scoped_to_session_id(tmp_path: Path) -> None:
    lock_root = tmp_path / "locks" / "worker"

    worker_lock = ensure_session_lock(lock_root, "worker")
    other_lock = ensure_session_lock(lock_root, "other")

    assert worker_lock.name == ".session.worker.lock"
    assert other_lock.name == ".session.other.lock"
    assert worker_lock.exists()
    assert other_lock.exists()


def test_session_lock_stale_unlink_race_surfaces_new_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    lock_root = tmp_path / "locks" / "worker"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / ".session.worker.lock"
    stale_created_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    lock_path.write_text(
        f'{{"session_id": "stale-owner", "created_at": "{stale_created_at}"}}\n'
    )

    real_open = __import__("os").open
    calls = {"count": 0}

    def fake_open(path, flags, mode=0o777):
        if Path(path) != lock_path:
            return real_open(path, flags, mode)
        calls["count"] += 1
        if calls["count"] == 1:
            raise FileExistsError
        if calls["count"] == 2:
            lock_path.write_text(
                f'{{"session_id": "fresh-owner", "created_at": "{datetime.now(UTC).isoformat()}"}}\n'
            )
            raise FileExistsError
        return real_open(path, flags, mode)

    monkeypatch.setattr("pollypm.projects.os.open", fake_open)

    with pytest.raises(RuntimeError, match="owned by fresh-owner"):
        ensure_session_lock(lock_root, "worker")


def test_session_lock_surfaces_infrastructure_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    lock_root = tmp_path / "locks" / "worker"

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pollypm.projects.os.open", boom)

    with pytest.raises(RuntimeError, match="Could not create session lock"):
        ensure_session_lock(lock_root, "worker")


def test_release_session_lock_handles_corrupt_non_dict_lock(
    tmp_path: Path,
) -> None:
    """Cycle 99: a corrupted lock file (parses to a non-dict shape)
    must not AttributeError out of ``release_session_lock``. The
    cleanup loop should still remove the file rather than aborting
    midway through a multi-lock release.
    """
    from pollypm.projects import release_session_lock, session_lock_path

    lock_root = tmp_path / "locks" / "worker"
    lock_root.mkdir(parents=True)
    # Seed a corrupted lock and an OK lock side by side.
    bad_lock = session_lock_path(lock_root, "bad")
    bad_lock.write_text("[1, 2, 3]")
    good_lock = session_lock_path(lock_root, "good")
    good_lock.write_text('{"session_id": "good", "created_at": "2026-04-25T00:00:00+00:00"}')

    # Sweep all locks (session_id=None). Must not raise on the corrupted
    # one and must still unlink both.
    release_session_lock(lock_root, None)
    assert not bad_lock.exists()
    assert not good_lock.exists()
