"""Tests for the hourly ``agent_worktree.prune`` recurring handler.

The handler targets Claude Code harness worktrees under
``<repo_root>/.claude/worktrees/agent-*`` — the ones that accumulate from
background ``Agent()`` calls with ``isolation: "worktree"`` and are NOT
the PollyPM task worktrees under ``<project>/.pollypm/worktrees/...``.

Behaviour under test:

* A merged agent worktree is removed from disk, its local branch is
  deleted, and the ``pruned`` counter reflects the action.
* A worktree whose mtime is under 1 hour old is left alone ("still in
  use"), counted as ``skipped_active``.
* An unmerged worktree older than 7 days is warned about but NOT deleted
  — the branch may hold uncommitted in-progress work.
* An unmerged worktree ≤ 7 days old is left in place with no warning.
* The result dict always includes the four counters.
* Running the handler twice on a clean tree is a no-op the second pass.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from pollypm.plugins_builtin.core_recurring.plugin import (
    agent_worktree_prune_handler,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a git/shell command with captured output + check=True."""
    return subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo(root: Path) -> Path:
    """Initialize a real git repo with one commit on ``main``."""
    root.mkdir(parents=True, exist_ok=True)
    _run("git", "init", "-b", "main", str(root))
    _run("git", "-C", str(root), "config", "user.email", "prune@test")
    _run("git", "-C", str(root), "config", "user.name", "Prune Test")
    (root / "README.md").write_text("seed\n")
    _run("git", "-C", str(root), "add", "README.md")
    _run("git", "-C", str(root), "commit", "-m", "init")
    return root


def _make_agent_worktree(
    repo: Path, slug: str, *, merge_to_main: bool, age_seconds: float | None = None,
) -> Path:
    """Add a worktree under ``<repo>/.claude/worktrees/agent-<slug>``.

    If ``merge_to_main`` is True, the worktree's branch is merged into
    main before returning (leaves the worktree + its branch behind so the
    prune handler sees a merged state).

    If ``age_seconds`` is provided, the worktree directory's mtime is
    back-dated that many seconds so the handler's age checks fire.
    """
    worktrees_dir = repo / ".claude" / "worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    wt_path = worktrees_dir / f"agent-{slug}"
    branch = f"worktree-agent-{slug}"

    _run("git", "-C", str(repo), "worktree", "add", "-b", branch, str(wt_path))
    # Add a commit on the worktree's branch so there's something to merge.
    (wt_path / "work.txt").write_text(f"{slug} work\n")
    _run("git", "-C", str(wt_path), "add", "work.txt")
    _run("git", "-C", str(wt_path), "commit", "-m", f"{slug} commit")

    if merge_to_main:
        _run("git", "-C", str(repo), "merge", "--no-ff", "-m", f"merge {slug}", branch)

    if age_seconds is not None:
        target = time.time() - age_seconds
        os.utime(wt_path, (target, target))

    return wt_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pruning_merged_worktree_removes_dir_and_branch(tmp_path: Path) -> None:
    """A merged worktree is removed from disk and its local branch is gone."""
    repo = _make_repo(tmp_path / "repo")
    wt = _make_agent_worktree(
        repo, "merged-one", merge_to_main=True, age_seconds=2 * 3600,
    )
    assert wt.exists()

    result = agent_worktree_prune_handler({"project_root": str(repo)})

    assert result["pruned"] == 1
    assert result["skipped_active"] == 0
    assert result["warned_stale"] == 0
    assert result["errors"] == 0
    assert not wt.exists(), "merged worktree directory should be gone"

    # Local branch should be deleted.
    branches = _run("git", "-C", str(repo), "branch").stdout
    assert "worktree-agent-merged-one" not in branches


def test_pruning_skips_active_worktree_under_one_hour(tmp_path: Path) -> None:
    """A worktree with mtime < 1h is left alone, counted as active."""
    repo = _make_repo(tmp_path / "repo")
    # Freshly created — mtime is "now", well under an hour — and merged,
    # so the only reason to skip is the age guard.
    wt = _make_agent_worktree(repo, "fresh", merge_to_main=True)
    assert wt.exists()

    result = agent_worktree_prune_handler({"project_root": str(repo)})

    assert result["pruned"] == 0
    assert result["skipped_active"] == 1
    assert result["warned_stale"] == 0
    assert result["errors"] == 0
    assert wt.exists(), "fresh worktree must be preserved"


def test_pruning_warns_on_stale_unmerged_worktree(tmp_path: Path) -> None:
    """Unmerged + >7d old → warn counter increments; worktree survives."""
    repo = _make_repo(tmp_path / "repo")
    wt = _make_agent_worktree(
        repo, "stale-unmerged", merge_to_main=False, age_seconds=10 * 86400,
    )
    assert wt.exists()

    result = agent_worktree_prune_handler({"project_root": str(repo)})

    assert result["pruned"] == 0
    assert result["warned_stale"] == 1
    assert result["errors"] == 0
    assert wt.exists(), "unmerged worktree must not be deleted"
    branches = _run("git", "-C", str(repo), "branch").stdout
    assert "worktree-agent-stale-unmerged" in branches


def test_pruning_skips_unmerged_recent_worktree_silently(tmp_path: Path) -> None:
    """Unmerged + ≤7d old → no warn, no prune, silent skip."""
    repo = _make_repo(tmp_path / "repo")
    # 3 days old: over the 1-hour "active" bar, well under the 7-day "stale"
    # bar. Handler should not touch it and should not warn either.
    wt = _make_agent_worktree(
        repo, "recent-unmerged", merge_to_main=False, age_seconds=3 * 86400,
    )

    result = agent_worktree_prune_handler({"project_root": str(repo)})

    assert result["pruned"] == 0
    assert result["warned_stale"] == 0
    assert result["skipped_active"] == 0
    assert result["errors"] == 0
    assert wt.exists()


def test_result_dict_has_all_counters_when_no_worktrees_dir(tmp_path: Path) -> None:
    """Missing ``.claude/worktrees`` → zeros for every counter."""
    repo = _make_repo(tmp_path / "repo")
    result = agent_worktree_prune_handler({"project_root": str(repo)})
    assert result == {
        "pruned": 0,
        "skipped_active": 0,
        "warned_stale": 0,
        "errors": 0,
    }


def test_idempotent_on_clean_tree(tmp_path: Path) -> None:
    """Second pass after a full prune is a no-op."""
    repo = _make_repo(tmp_path / "repo")
    _make_agent_worktree(
        repo, "clean-me", merge_to_main=True, age_seconds=2 * 3600,
    )
    first = agent_worktree_prune_handler({"project_root": str(repo)})
    assert first["pruned"] == 1

    second = agent_worktree_prune_handler({"project_root": str(repo)})
    assert second == {
        "pruned": 0,
        "skipped_active": 0,
        "warned_stale": 0,
        "errors": 0,
    }
