"""Regression coverage for #1496 — pm task approve recurrent .gitignore conflicts.

Two layers of coverage:

1. Unit tests for ``_parse_overwrite_files_from_stderr`` against canned
   git stderr shapes (clean parse, multi-file, edge cases).
2. Integration test exercising ``_auto_merge_approved_task_branch``
   end-to-end on a real fixture repo where ``main`` and the task branch
   each added different lines to ``.gitignore`` — the historical failure
   mode that left coffeeboardnm/1 stuck.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from pollypm.work.models import Task, TaskType
from pollypm.work.sqlite_service import (
    SQLiteWorkService,
    _parse_overwrite_files_from_stderr,
)


# ---------------------------------------------------------------------------
# _parse_overwrite_files_from_stderr — unit
# ---------------------------------------------------------------------------


def test_parse_overwrite_files_single_file() -> None:
    stderr = (
        "error: Your local changes to the following files would be overwritten by merge:\n"
        "\t.gitignore\n"
        "Please commit your changes or stash them before you merge.\n"
        "Aborting\n"
    )
    assert _parse_overwrite_files_from_stderr(stderr) == [".gitignore"]


def test_parse_overwrite_files_multiple_files() -> None:
    stderr = (
        "error: Your local changes to the following files would be overwritten by merge:\n"
        "\t.gitignore\n"
        "\t.dockerignore\n"
        "\tdocs/CHANGELOG.md\n"
        "Please commit your changes or stash them before you merge.\n"
        "Aborting\n"
    )
    assert _parse_overwrite_files_from_stderr(stderr) == [
        ".gitignore",
        ".dockerignore",
        "docs/CHANGELOG.md",
    ]


def test_parse_overwrite_files_checkout_variant() -> None:
    """``git checkout`` uses the same shape with "by checkout" instead."""
    stderr = (
        "error: Your local changes to the following files would be overwritten by checkout:\n"
        "\t.gitignore\n"
        "Aborting\n"
    )
    assert _parse_overwrite_files_from_stderr(stderr) == [".gitignore"]


def test_parse_overwrite_files_no_match_returns_empty() -> None:
    """Genuine merge conflicts (not pre-merge dirty refusal) don't match."""
    stderr = (
        "Auto-merging .gitignore\n"
        "CONFLICT (add/add): Merge conflict in .gitignore\n"
        "Automatic merge failed; fix conflicts and then commit the result.\n"
    )
    assert _parse_overwrite_files_from_stderr(stderr) == []


def test_parse_overwrite_files_empty_stderr() -> None:
    assert _parse_overwrite_files_from_stderr("") == []
    assert _parse_overwrite_files_from_stderr("   \n  ") == []


# ---------------------------------------------------------------------------
# _auto_merge_approved_task_branch — integration on a real fixture repo
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``cwd`` and return the CompletedProcess."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _make_fixture_repo(
    tmp_path: Path,
    *,
    main_gitignore: str,
    task_gitignore: str,
    base_gitignore: str = "# base\n",
) -> Path:
    """Initialize a fixture project repo with diverging ``.gitignore``.

    History: ``base_gitignore`` committed on main (the merge base), then
    ``main`` adds its own ignore line and ``task/proj-1`` adds its own
    on top of base. Closely mirrors the coffeeboardnm/1 shape from #1496.
    """
    project = tmp_path / "fixture-proj"
    project.mkdir()
    _git("init", "-q", "-b", "main", cwd=project)
    _git("config", "user.email", "test@example.com", cwd=project)
    _git("config", "user.name", "Test", cwd=project)
    _git("config", "commit.gpgsign", "false", cwd=project)

    (project / ".gitignore").write_text(base_gitignore)
    (project / "README.md").write_text("# fixture\n")
    _git("add", ".", cwd=project)
    _git("commit", "-q", "-m", "base", cwd=project)

    # Task branch adds task-specific ignore.
    _git("checkout", "-q", "-b", "task/proj-1", cwd=project)
    (project / ".gitignore").write_text(task_gitignore)
    _git("add", ".gitignore", cwd=project)
    _git("commit", "-q", "-m", "task: ignore task-only path", cwd=project)

    # Main advances independently with its own ignore line.
    _git("checkout", "-q", "main", cwd=project)
    (project / ".gitignore").write_text(main_gitignore)
    _git("add", ".gitignore", cwd=project)
    _git("commit", "-q", "-m", "main: ignore main-only path", cwd=project)

    return project


class _FixtureService(SQLiteWorkService):
    """Subclass that pins the project path resolver to a known dir."""

    def __init__(self, project_path: Path) -> None:
        self._fixture_project_path = project_path

    def _resolve_project_path(self, _project: str) -> Path | None:  # type: ignore[override]
        return self._fixture_project_path


def _make_task(project_slug: str = "proj", number: int = 1) -> Task:
    return Task(
        project=project_slug,
        task_number=number,
        title="fixture",
        type=TaskType.TASK,
    )


def test_auto_merge_succeeds_for_additive_gitignore_divergence(tmp_path: Path) -> None:
    """The historical coffeeboardnm/1 shape: both sides added different lines.

    Should auto-resolve via the existing union-safe logic — this test pins
    that the happy path keeps working alongside the new pre-merge recovery.
    """
    project_path = _make_fixture_repo(
        tmp_path,
        base_gitignore="# base\n",
        main_gitignore="# base\nmain-only-path\n",
        task_gitignore="# base\ntask-only-path\n",
    )
    svc = _FixtureService(project_path)
    svc._auto_merge_approved_task_branch(_make_task())

    merged = (project_path / ".gitignore").read_text()
    assert "main-only-path" in merged
    assert "task-only-path" in merged

    # No leftover stash, no merge state.
    assert not (project_path / ".git" / "MERGE_HEAD").exists()
    stash = subprocess.run(
        ["git", "-C", str(project_path), "stash", "list"],
        check=True, capture_output=True, text=True,
    )
    assert stash.stdout.strip() == ""


def test_auto_merge_recovers_from_pre_merge_overwrite_via_monkeypatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct exercise of the new pre-merge "would be overwritten" recovery
    path. We monkeypatch the merge subprocess to mimic git's pre-merge
    dirty-tree refusal on the first attempt, then return success on retry —
    asserts the recovery resets the offending safe file and re-attempts.
    """
    project_path = _make_fixture_repo(
        tmp_path,
        base_gitignore="# base\n",
        main_gitignore="# base\nmain-only-path\n",
        task_gitignore="# base\ntask-only-path\n",
    )

    svc = _FixtureService(project_path)
    real_git_run = SQLiteWorkService._git_run.__get__(svc)
    merge_attempts: list[list[str]] = []
    checkout_seen: list[list[str]] = []

    fake_overwrite_stderr = (
        "error: Your local changes to the following files would be"
        " overwritten by merge:\n"
        "\t.gitignore\n"
        "Please commit your changes or stash them before you merge.\n"
        "Aborting\n"
    )

    def faux_git_run(self, project_path_arg: Path, *args: str):
        # First non-ff merge attempt → fake the overwrite refusal.
        # All other commands (status, ff_only, checkout, retry merge) → real.
        is_no_ff_merge = (
            "merge" in args
            and "--no-ff" in args
            and "--no-edit" in args
        )
        if is_no_ff_merge:
            merge_attempts.append(list(args))
            if len(merge_attempts) == 1:
                return subprocess.CompletedProcess(
                    args=["git"] + list(args),
                    returncode=1,
                    stdout="",
                    stderr=fake_overwrite_stderr,
                )
        if "checkout" in args and "HEAD" in args:
            checkout_seen.append(list(args))
        return real_git_run(project_path_arg, *args)

    monkeypatch.setattr(_FixtureService, "_git_run", faux_git_run)

    svc._auto_merge_approved_task_branch(_make_task())

    # Recovery ran: the checkout reset the refused safe file.
    assert any(
        "checkout" in c and "HEAD" in c and ".gitignore" in c
        for c in checkout_seen
    ), f"expected ``git checkout HEAD -- .gitignore`` reset, saw {checkout_seen}"
    # Two no-ff merge attempts: original (faked failure) + retry.
    assert len(merge_attempts) == 2, (
        f"expected one retry after recovery, got {len(merge_attempts)}"
    )
    # Final state: both ignore lines present.
    merged = (project_path / ".gitignore").read_text()
    assert "main-only-path" in merged
    assert "task-only-path" in merged


def test_auto_merge_preserves_ignored_scratch_files(
    tmp_path: Path,
) -> None:
    """Operator's ignored scratch files survive an approve merge —
    porcelain skips them, the merge's working-tree side never sees them."""
    project_path = _make_fixture_repo(
        tmp_path,
        base_gitignore="# base\n",
        main_gitignore="# base\nmain-only-path\n",
        task_gitignore="# base\ntask-only-path\n",
    )

    # The dirty-tree gate requires all dirty entries to be PollyPM scaffold.
    # Untracked files outside the allowlist trigger ValidationError, so we
    # only seed an ignored scratch file (porcelain skips ignored entries).
    (project_path / ".gitignore").write_text(
        "# base\nmain-only-path\nscratch/\n"
    )
    _git("add", ".gitignore", cwd=project_path)
    _git("commit", "-q", "-m", "ignore scratch dir", cwd=project_path)

    scratch = project_path / "scratch"
    scratch.mkdir()
    (scratch / "notes.txt").write_text("operator notes — DO NOT TOUCH\n")

    svc = _FixtureService(project_path)
    svc._auto_merge_approved_task_branch(_make_task())

    # Operator's scratch file untouched.
    assert (scratch / "notes.txt").read_text() == (
        "operator notes — DO NOT TOUCH\n"
    )

    merged = (project_path / ".gitignore").read_text()
    assert "main-only-path" in merged
    assert "task-only-path" in merged
    assert "scratch/" in merged
