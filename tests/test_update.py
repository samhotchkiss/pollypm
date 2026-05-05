"""Tests for ``pm update`` (#1079).

Exercises the in_progress refusal, check-only reporting, fetch / reset
/ install orchestration, and CLI surface. Every shell-out is stubbed
via the ``fetcher`` / ``resolver`` / ``runner`` / ``reset_runner``
seams so no real ``git`` or ``uv`` fires from this suite.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from pollypm import update as update_mod
from pollypm.cli import app as cli_app


runner = CliRunner()


def _ok_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess for the ``runner`` seam."""
    return subprocess.CompletedProcess(
        args=["uv"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def _commits(*pairs: tuple[str, str]) -> list[update_mod.CommitInfo]:
    return [update_mod.CommitInfo(sha=sha, subject=subject) for sha, subject in pairs]


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #

def test_pendingcommits_up_to_date_when_empty() -> None:
    p = update_mod.PendingCommits(head_sha="abc", target_sha="abc")
    assert p.up_to_date is True
    assert p.count == 0


def test_pendingcommits_count_matches_commits_list() -> None:
    p = update_mod.PendingCommits(
        head_sha="a", target_sha="b",
        commits=_commits(("a1", "first"), ("a2", "second")),
    )
    assert p.count == 2
    assert p.up_to_date is False


# --------------------------------------------------------------------------- #
# update() — refusal paths
# --------------------------------------------------------------------------- #

def test_update_refuses_when_in_progress_tasks_exist() -> None:
    result = update_mod.update(in_progress_count=2)
    assert result.refused is True
    assert result.ok is False
    assert "in_progress" in result.message
    assert "2" in result.message


def test_update_refuses_when_repo_not_a_git_checkout(tmp_path: Path) -> None:
    # tmp_path has no .git/ — should bail before fetch.
    result = update_mod.update(
        repo_root=tmp_path, in_progress_count=0,
    )
    assert result.ok is False
    assert result.refused is False
    assert "not a git checkout" in result.message


def test_update_refuses_claude_agent_worktree_before_side_effects(tmp_path: Path) -> None:
    repo = tmp_path / "pollypm" / ".claude" / "worktrees" / "agent-abc123"
    repo.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /tmp/gitdir\n")
    calls: list[str] = []

    def _fetcher(repo_root: Path) -> tuple[bool, str]:
        calls.append("fetch")
        return (True, "")

    def _resolver(repo_root: Path) -> update_mod.PendingCommits:
        calls.append("resolve")
        return update_mod.PendingCommits(head_sha="aaaa", target_sha="bbbb")

    def _runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append("install")
        return _ok_run()

    def _reset(repo_root: Path, target: str) -> tuple[bool, str]:
        calls.append("reset")
        return (True, "")

    result = update_mod.update(
        repo_root=repo,
        in_progress_count=0,
        fetcher=_fetcher,
        resolver=_resolver,
        runner=_runner,
        reset_runner=_reset,
    )

    assert result.ok is False
    assert result.refused is True
    assert "agent worktree" in result.message
    assert "canonical install" in result.message
    assert calls == []


# --------------------------------------------------------------------------- #
# update() — fetch + check-only
# --------------------------------------------------------------------------- #

def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # only existence is checked
    return repo


def test_update_check_only_reports_pending_commits(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    pending = update_mod.PendingCommits(
        head_sha="aaaaaaaaaaaa", target_sha="bbbbbbbbbbbb",
        commits=_commits(("c1", "Fix #1066"), ("c2", "Fix #1078")),
    )
    result = update_mod.update(
        check_only=True,
        repo_root=repo,
        in_progress_count=0,
        fetcher=lambda r: (True, ""),
        resolver=lambda r: pending,
    )
    assert result.ok is True
    assert result.check_only is True
    assert result.count == 2
    assert "check-only" in result.message
    assert result.commits[0].subject == "Fix #1066"


def test_update_check_only_reports_already_up_to_date(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    same = "abcdef0abcdef"
    pending = update_mod.PendingCommits(head_sha=same, target_sha=same)
    result = update_mod.update(
        check_only=True,
        repo_root=repo,
        in_progress_count=0,
        fetcher=lambda r: (True, ""),
        resolver=lambda r: pending,
    )
    assert result.ok is True
    assert result.count == 0
    assert "already up to date" in result.message


def test_update_aborts_on_fetch_failure(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = update_mod.update(
        repo_root=repo,
        in_progress_count=0,
        fetcher=lambda r: (False, "fatal: could not read from remote"),
    )
    assert result.ok is False
    assert "git fetch failed" in result.message
    assert "fatal: could not read from remote" in result.stderr


def test_update_aborts_when_origin_main_unresolved(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    pending = update_mod.PendingCommits(head_sha="abc", target_sha="")
    result = update_mod.update(
        repo_root=repo,
        in_progress_count=0,
        fetcher=lambda r: (True, ""),
        resolver=lambda r: pending,
    )
    assert result.ok is False
    assert "origin/main" in result.message


# --------------------------------------------------------------------------- #
# update() — full flow (reset + reinstall)
# --------------------------------------------------------------------------- #

def test_update_full_flow_runs_reset_then_install(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    pending = update_mod.PendingCommits(
        head_sha="aaaaaaaaaaaa", target_sha="bbbbbbbbbbbb",
        commits=_commits(("c1", "Fix #1066")),
    )
    runs: list[list[str]] = []
    resets: list[tuple[Path, str]] = []

    def _runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        runs.append(argv)
        return _ok_run()

    def _reset(r: Path, target: str) -> tuple[bool, str]:
        resets.append((r, target))
        return (True, "")

    result = update_mod.update(
        repo_root=repo,
        in_progress_count=0,
        fetcher=lambda r: (True, ""),
        resolver=lambda r: pending,
        runner=_runner,
        reset_runner=_reset,
    )
    assert result.ok is True
    assert result.refused is False
    assert resets == [(repo.resolve(), "origin/main")]
    assert len(runs) == 1
    cmd = runs[0]
    assert cmd[:4] == ["uv", "tool", "install", "--reinstall"]
    assert Path(cmd[4]) == repo.resolve()
    assert "updated" in result.message
    assert "1 commit" in result.message


def test_update_aborts_when_reset_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    pending = update_mod.PendingCommits(
        head_sha="aaaa", target_sha="bbbb",
        commits=_commits(("c1", "Fix #1066")),
    )
    runs: list[list[str]] = []

    def _runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        runs.append(argv)
        return _ok_run()

    result = update_mod.update(
        repo_root=repo,
        in_progress_count=0,
        fetcher=lambda r: (True, ""),
        resolver=lambda r: pending,
        runner=_runner,
        reset_runner=lambda r, target: (False, "would overwrite local changes"),
    )
    assert result.ok is False
    assert "git reset --hard failed" in result.message
    assert runs == []  # uv install never ran


def test_update_surfaces_install_failure(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    pending = update_mod.PendingCommits(
        head_sha="aaaa", target_sha="bbbb",
        commits=_commits(("c1", "Fix #1066")),
    )
    result = update_mod.update(
        repo_root=repo,
        in_progress_count=0,
        fetcher=lambda r: (True, ""),
        resolver=lambda r: pending,
        runner=lambda argv: _ok_run(returncode=2, stderr="boom"),
        reset_runner=lambda r, target: (True, ""),
    )
    assert result.ok is False
    assert "uv tool install failed" in result.message
    assert "boom" in result.stderr


def test_update_handles_uv_not_on_path(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    pending = update_mod.PendingCommits(
        head_sha="aaaa", target_sha="bbbb",
        commits=_commits(("c1", "Fix #1066")),
    )

    def _missing_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("uv")

    result = update_mod.update(
        repo_root=repo,
        in_progress_count=0,
        fetcher=lambda r: (True, ""),
        resolver=lambda r: pending,
        runner=_missing_runner,
        reset_runner=lambda r, target: (True, ""),
    )
    assert result.ok is False
    assert "uv binary not on PATH" in result.message


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #

def test_cli_update_check_only_renders_summary(monkeypatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    pending = update_mod.PendingCommits(
        head_sha="aaaaaaaaaaaa", target_sha="bbbbbbbbbbbb",
        commits=_commits(
            ("c1c1c1c1c1c1", "Fix #1066 stale cockpit"),
            ("c2c2c2c2c2c2", "Fix #1078 task cancellation"),
        ),
    )
    monkeypatch.setattr(update_mod, "_repo_root", lambda: repo)
    monkeypatch.setattr(update_mod, "count_in_progress_tasks", lambda: 0)
    monkeypatch.setattr(update_mod, "_git_fetch", lambda r: (True, ""))
    monkeypatch.setattr(update_mod, "pending_commits", lambda r: pending)

    result = runner.invoke(cli_app, ["update", "--check-only"])
    assert result.exit_code == 0, result.output
    assert "check-only" in result.output
    assert "Fix #1066" in result.output
    assert "Fix #1078" in result.output
    assert "aaaaaaaaaaaa" in result.output
    assert "bbbbbbbbbbbb" in result.output


def test_cli_update_refuses_with_in_progress(monkeypatch) -> None:
    monkeypatch.setattr(update_mod, "count_in_progress_tasks", lambda: 3)
    result = runner.invoke(cli_app, ["update"])
    assert result.exit_code == 1
    assert "refusing to update" in result.output
    assert "3 task" in result.output
