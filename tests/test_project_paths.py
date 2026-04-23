"""Tests for the project_paths helper + materialization (#763).

Covers:
- role_guide_path returns an in-project absolute path when a fork
  exists
- role_guide_path triggers on-demand materialization when no fork
  exists yet
- role_guide_path for operator-pm points at the shipped Polly guide
  (global role, never per-project)
- materialize_role_guides writes all three project-scoped role guides
- materialize_role_guides is idempotent (skips existing files)
- materialize_role_guides with force=True overwrites existing files
- ensure_project_scaffold materializes guides as part of project
  creation
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create a bare project directory with .git so it registers as git."""
    root = tmp_path / "my_project"
    root.mkdir()
    (root / ".git").mkdir()
    return root


def _guide_path(project_path: Path, role: str) -> Path:
    return project_path / ".pollypm" / "project-guides" / f"{role}.md"


# ---------------------------------------------------------------------------
# materialize_role_guides
# ---------------------------------------------------------------------------


def test_materialize_writes_all_three_project_scoped_roles(project: Path) -> None:
    from pollypm.project_paths import materialize_role_guides

    written = materialize_role_guides(project)
    paths = {p.name for p in written}
    assert "architect.md" in paths
    assert "worker.md" in paths
    assert "reviewer.md" in paths

    for role in ("architect", "worker", "reviewer"):
        target = _guide_path(project, role)
        assert target.exists(), f"{role}.md should be materialized"
        body = target.read_text()
        assert body.startswith("---\nforked_from:"), (
            f"{role}.md should carry a forked_from header: {body[:80]!r}"
        )


def test_materialize_is_idempotent_by_default(project: Path) -> None:
    from pollypm.project_paths import materialize_role_guides

    first = materialize_role_guides(project)
    assert first, "first materialization should write all three roles"

    second = materialize_role_guides(project)
    assert second == [], (
        "second materialization should skip existing files (got "
        f"{second!r})"
    )


def test_materialize_force_overwrites_existing_files(project: Path) -> None:
    from pollypm.project_paths import materialize_role_guides

    materialize_role_guides(project)
    # Mutate the worker file so we can tell if force overwrote it.
    worker_target = _guide_path(project, "worker")
    worker_target.write_text("---\nforked_from: mine\n---\n\nHAND-EDITED")
    assert "HAND-EDITED" in worker_target.read_text()

    written = materialize_role_guides(project, force=True)
    names_written = {p.name for p in written}
    assert "worker.md" in names_written

    body = worker_target.read_text()
    assert "HAND-EDITED" not in body
    assert body.startswith("---\nforked_from:")


def test_materialize_skips_when_project_dir_missing(tmp_path: Path) -> None:
    """Materialize on a non-existent project path must not create the
    project and must not crash — scaffold is the only thing that
    should ever create the project directory."""
    from pollypm.project_paths import materialize_role_guides

    ghost = tmp_path / "ghost"
    assert not ghost.exists()
    result = materialize_role_guides(ghost)
    assert result == []
    assert not ghost.exists()


# ---------------------------------------------------------------------------
# role_guide_path
# ---------------------------------------------------------------------------


def test_role_guide_path_returns_in_project_absolute_path(project: Path) -> None:
    from pollypm.project_paths import materialize_role_guides, role_guide_path

    materialize_role_guides(project)
    path = role_guide_path(project, "architect")
    assert path.is_absolute()
    assert path.is_file()
    # Must be inside the project, not in PollyPM's install tree.
    assert str(project.resolve()) in str(path.resolve())


def test_role_guide_path_materializes_on_demand_when_missing(project: Path) -> None:
    """When a fork doesn't exist yet, role_guide_path should
    materialize it and then return the in-project path."""
    from pollypm.project_paths import role_guide_path

    target = _guide_path(project, "reviewer")
    assert not target.exists()

    path = role_guide_path(project, "reviewer")
    assert path.is_absolute()
    # Materialization happened as a side effect.
    assert target.exists()
    assert path.resolve() == target.resolve()


def test_role_guide_path_operator_pm_is_global(project: Path) -> None:
    """Operator-PM (Polly) is global — every project shares one. The
    helper must return the shipped built-in path, not a per-project
    one."""
    from pollypm.project_paths import role_guide_path

    path = role_guide_path(project, "operator-pm")
    assert path.is_absolute()
    assert path.name == "polly-operator-guide.md"
    assert "plugins_builtin/core_agent_profiles/profiles" in str(path)
    # Must NOT be inside the user's project.
    assert str(project.resolve()) not in str(path.resolve())


def test_role_guide_path_unknown_role_falls_back_to_worker(project: Path) -> None:
    from pollypm.project_paths import role_guide_path

    path = role_guide_path(project, "polyglot")
    assert path.name == "worker.md"


def test_role_guide_path_never_returns_src_relative(project: Path) -> None:
    """Regression for #762/#763: the resolved path must never start
    with a repo-relative 'src/' prefix. Absolute paths only."""
    from pollypm.project_paths import role_guide_path

    for role in ("architect", "worker", "reviewer", "operator-pm"):
        path = role_guide_path(project, role)
        assert not str(path).startswith("src/"), (
            f"{role} resolved to repo-relative path {path!r}"
        )
        assert path.is_absolute(), (
            f"{role} path must be absolute, got {path!r}"
        )


# ---------------------------------------------------------------------------
# ensure_project_scaffold integration
# ---------------------------------------------------------------------------


def test_ensure_project_scaffold_materializes_role_guides(project: Path) -> None:
    """Creating a project via the scaffold helper should produce an
    in-project copy of every role guide — no separate init step
    needed."""
    from pollypm.projects import ensure_project_scaffold

    ensure_project_scaffold(project)

    for role in ("architect", "worker", "reviewer"):
        target = _guide_path(project, role)
        assert target.exists(), (
            f"ensure_project_scaffold did not materialize {role}.md "
            f"(expected at {target})"
        )
        body = target.read_text()
        assert body.startswith("---\nforked_from:")


def test_ensure_project_scaffold_preserves_existing_forks(project: Path) -> None:
    """If the user has hand-forked a guide (or init-guide did), the
    scaffold helper must NOT overwrite it — idempotent materialization
    respects existing content."""
    from pollypm.projects import ensure_project_scaffold

    # Create a forked guide BEFORE scaffold.
    custom = _guide_path(project, "architect")
    custom.parent.mkdir(parents=True, exist_ok=True)
    custom.write_text("---\nforked_from: mine\n---\n\nHAND-EDITED ARCHITECT")

    ensure_project_scaffold(project)

    # Content preserved.
    body = custom.read_text()
    assert "HAND-EDITED ARCHITECT" in body
