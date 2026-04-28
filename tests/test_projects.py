import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.config import write_config
from pollypm.models import KnownProject, ProjectKind, ProjectSettings, PollyPMConfig, PollyPMSettings
import pytest
from pollypm.task_backends.github import GitHubTaskBackendValidation

from pollypm.projects import (
    DEFAULT_GITIGNORE_LINES,
    SCAFFOLD_PATHS,
    commit_initial_scaffold,
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


# ---------------------------------------------------------------------------
# #926 — commit initial PollyPM scaffolding so the project root is clean
# ---------------------------------------------------------------------------


def _git_init_repo(repo: Path) -> None:
    """Initialize a fresh git repo with a stable identity for tests."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test User"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "commit.gpgsign", "false"],
        check=True,
    )


def _git_status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout


def _list_tree(repo: Path, ref: str = "HEAD") -> set[str]:
    """List files at ``ref`` (defaults to HEAD)."""
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", ref],
        check=True, capture_output=True, text=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def test_default_gitignore_template_keeps_pollypm_first_and_covers_common_noise() -> None:
    """The seeded ``.gitignore`` template must keep ``.pollypm/`` first
    (#926) and include the common per-language noise patterns (#925)
    so workers don't redundantly add their own."""
    assert DEFAULT_GITIGNORE_LINES[0] == ".pollypm/"
    expected = {
        "node_modules/", "dist/", "dist-ssr/", "*.local",
        ".DS_Store", ".vscode/*", "!.vscode/extensions.json", ".idea/",
        "__pycache__/", "*.pyc", ".pytest_cache/", ".env", ".venv/",
        "tsconfig.tsbuildinfo", "*.tsbuildinfo",
    }
    missing = expected - set(DEFAULT_GITIGNORE_LINES)
    assert not missing, f"missing default gitignore entries: {missing}"


def test_ensure_project_scaffold_seeds_full_gitignore_template_on_fresh_repo(
    tmp_path: Path,
) -> None:
    """The first call to ``ensure_project_scaffold`` writes the full
    default template, not just ``.pollypm/`` (#925/#926)."""
    project_path = tmp_path / "fresh"
    project_path.mkdir()
    ensure_project_scaffold(project_path)
    text = (project_path / ".gitignore").read_text()
    assert text.splitlines()[0] == ".pollypm/"
    assert "node_modules/" in text
    assert "__pycache__/" in text


def test_ensure_project_scaffold_preserves_existing_gitignore(
    tmp_path: Path,
) -> None:
    """If the user already has a ``.gitignore``, the scaffold must
    only append the missing ``.pollypm/`` entry — never reorder or
    rewrite existing lines."""
    project_path = tmp_path / "with-gitignore"
    project_path.mkdir()
    (project_path / ".gitignore").write_text("# user header\nbuild/\n")
    ensure_project_scaffold(project_path)
    text = (project_path / ".gitignore").read_text()
    assert text.startswith("# user header\nbuild/\n")
    assert ".pollypm/" in text
    # User-edited template should NOT be replaced with the seeded full
    # default — node_modules wasn't there before scaffold.
    assert "node_modules/" not in text


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_commit_initial_scaffold_leaves_clean_status_on_empty_repo(
    tmp_path: Path,
) -> None:
    """#926: after ``pm add-project`` on a fresh empty repo, the
    project root must be clean. ``commit_initial_scaffold`` runs
    after ``ensure_project_scaffold`` and must turn an untracked
    .gitignore + (later) docs/ + issues/ into one committed snapshot
    so the user's first ``pm task approve`` doesn't bounce on
    "uncommitted changes"."""
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    _git_init_repo(repo)
    ensure_project_scaffold(repo)
    # Add some scaffolded files that history-import would write so the
    # commit has interesting content beyond the .gitignore.
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "architecture.md").write_text("# Architecture\n")
    (repo / "docs" / "project-overview.md").write_text("# Overview\n")
    (repo / "issues").mkdir(exist_ok=True)
    for state in ("00-not-ready", "01-ready", "02-in-progress", "03-needs-review", "05-completed"):
        (repo / "issues" / state).mkdir(exist_ok=True)
        (repo / "issues" / state / ".gitkeep").write_text("")

    assert commit_initial_scaffold(repo) is True
    assert _git_status(repo) == "", f"expected clean status, got: {_git_status(repo)!r}"
    tree = _list_tree(repo)
    assert ".gitignore" in tree
    assert "docs/architecture.md" in tree
    assert "docs/project-overview.md" in tree
    # All five phase folders present (via .gitkeep).
    for state in ("00-not-ready", "01-ready", "02-in-progress", "03-needs-review", "05-completed"):
        assert f"issues/{state}/.gitkeep" in tree


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_commit_initial_scaffold_does_not_sweep_dirty_user_files(
    tmp_path: Path,
) -> None:
    """#926: ``commit_initial_scaffold`` must commit only the explicit
    PollyPM-scaffold paths, not other dirty user files in the working
    tree (e.g. an in-progress edit the user already had). The user's
    untracked ``WIP.md`` must remain untracked after the commit."""
    repo = tmp_path / "dirty"
    repo.mkdir()
    _git_init_repo(repo)
    # Seed a real first commit so HEAD exists, and add a tracked file
    # the user is mid-editing. Plus an untracked WIP.md.
    (repo / "src.py").write_text("print('v1')\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "src.py"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True,
    )
    # User dirties a tracked file and creates an untracked one.
    (repo / "src.py").write_text("print('v2-WIP')\n")
    (repo / "WIP.md").write_text("ongoing notes\n")
    # PollyPM scaffolds.
    ensure_project_scaffold(repo)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "architecture.md").write_text("# Architecture\n")

    assert commit_initial_scaffold(repo) is True
    # The user's mid-edit src.py and untracked WIP.md must still be
    # uncommitted.
    status = _git_status(repo)
    assert " M src.py" in status
    assert "?? WIP.md" in status
    # And the scaffold commit must NOT include them.
    tree = _list_tree(repo)
    assert "WIP.md" not in tree
    # src.py exists in HEAD but its committed content is the old one.
    head_src = subprocess.run(
        ["git", "-C", str(repo), "show", "HEAD:src.py"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert head_src == "print('v1')\n"
    # The scaffold commit's message follows the issue's suggestion.
    head_msg = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%s"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert "PollyPM" in head_msg


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_commit_initial_scaffold_is_idempotent(tmp_path: Path) -> None:
    """Re-running ``commit_initial_scaffold`` after the scaffold has
    already been committed must be a no-op (returns False), so the
    onboarding flows are safe to retry."""
    repo = tmp_path / "idempotent"
    repo.mkdir()
    _git_init_repo(repo)
    ensure_project_scaffold(repo)
    assert commit_initial_scaffold(repo) is True
    assert commit_initial_scaffold(repo) is False


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_commit_initial_scaffold_skips_non_git_directories(tmp_path: Path) -> None:
    """Folder-mode projects (no ``.git``) must not crash — the helper
    just returns False so registration succeeds for non-repo paths."""
    folder = tmp_path / "folder-only"
    folder.mkdir()
    ensure_project_scaffold(folder)
    assert commit_initial_scaffold(folder) is False


def test_scaffold_paths_covers_user_visible_artifacts() -> None:
    """The allowlist of paths ``commit_initial_scaffold`` will stage
    must contain the three user-visible scaffold roots called out in
    issue #926: ``.gitignore``, ``docs``, ``issues``."""
    assert ".gitignore" in SCAFFOLD_PATHS
    assert "docs" in SCAFFOLD_PATHS
    assert "issues" in SCAFFOLD_PATHS


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_pm_add_project_leaves_git_status_clean_on_fresh_repo(tmp_path: Path) -> None:
    """End-to-end #926: ``pm add-project --skip-import`` on a real
    fresh git repo must leave the project root with a clean
    ``git status``. Previously the CLI dropped untracked
    ``.gitignore``/``docs/``/``issues/`` artifacts and never committed
    them, which then bounced the user's first ``pm task approve`` on
    "uncommitted changes"."""
    from typer.testing import CliRunner
    from pollypm.cli import app as root_app

    repo = tmp_path / "fresh-repo"
    repo.mkdir()
    _git_init_repo(repo)

    config_path = tmp_path / "pollypm.toml"
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            workspace_root=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account=""),
        accounts={},
        sessions={},
        projects={},
    )
    write_config(config, config_path, force=True)

    runner = CliRunner()
    result = runner.invoke(
        root_app,
        [
            "add-project", str(repo), "--skip-import", "--skip-plan",
            "--config", str(config_path), "--name", "fresh-repo",
        ],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")

    # Working tree must be clean — no untracked .gitignore/docs/issues
    # left behind for the user to clean up before their first approve.
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == "", f"expected clean status after add-project, got: {status!r}"

    # The first commit on main must contain the seeded .gitignore.
    tree = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert ".gitignore" in tree
