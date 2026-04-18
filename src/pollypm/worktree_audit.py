"""Worktree state classification for the heartbeat audit handler (#251).

This module houses the pure, subprocess-only classifier used by the
``worktree.state_audit`` recurring handler. It deliberately does NOT
touch SQLite, config, or the plugin host — the handler in
``plugins_builtin/core_recurring/plugin.py`` is the only caller and
owns the side-effect story (alerts / inbox / state store writes).

States surfaced:

* ``clean`` — no uncommitted changes, HEAD on a named branch, no lock
  file. The expected steady-state for an in-progress worker.
* ``dirty_expected`` — uncommitted working-tree changes. Fine during
  active development; the handler separately checks the worktree's
  mtime-derived staleness and raises a low-severity alert if the
  directory hasn't been touched in >60 min.
* ``merge_conflict`` — ``git status --porcelain`` reports one of the
  unmerged-path codes (``UU``, ``AA``, ``DD``, ``AU``, ``UA``, ``UD``,
  ``DU``). Blocks progress — worker can't commit.
* ``detached_head`` — HEAD is not on a branch (e.g. after a
  ``git checkout <sha>``). A worker session on detached HEAD cannot
  push its work.
* ``orphan_branch`` — HEAD is on a local branch but that branch has no
  upstream AND no commits in the last 7 days. Indicates an abandoned
  worktree whose work is at risk of being GC'd by the prune handler.
* ``lock_file`` — ``.git/index.lock`` exists. We surface this so a
  stuck git process (or a crashed one that left a lock behind) is
  visible at the 10-minute cadence. Lock-file age is returned in
  metadata so the handler can escalate severity at >5min.

The classifier returns an enum + metadata dict. The metadata carries
whatever downstream alerting needs (lock age, stale days, conflict
file list) so the handler doesn't re-run git.
"""

from __future__ import annotations

import enum
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class WorktreeState(str, enum.Enum):
    """Classification output for a single worktree."""

    CLEAN = "clean"
    DIRTY_EXPECTED = "dirty_expected"
    MERGE_CONFLICT = "merge_conflict"
    DETACHED_HEAD = "detached_head"
    ORPHAN_BRANCH = "orphan_branch"
    LOCK_FILE = "lock_file"
    # Surfaces when the path doesn't exist or isn't a git directory.
    # Treated as non-actionable by the handler — the prune pass owns
    # those cases.
    MISSING = "missing"


# Unmerged-path prefixes per ``git status --porcelain`` section "Porcelain
# Format". We treat any presence as a merge conflict signal.
_CONFLICT_CODES: frozenset[str] = frozenset({
    "UU", "AA", "DD", "AU", "UA", "UD", "DU",
})


@dataclass(slots=True)
class WorktreeClassification:
    """Classifier output — enum + metadata the handler consumes.

    ``metadata`` intentionally stays a plain dict (not a typed
    sub-record) so the handler can stash whatever context it found
    without this module growing a state-specific schema per case.
    """

    state: WorktreeState
    branch: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _git(cwd: Path, *args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    """Run ``git -C <cwd> <args>`` with captured output and ``check=False``.

    Mirrors ``plugins_builtin/core_recurring/plugin.py::_git`` used by
    the prune handler so behaviour (timeouts, text-mode, non-raising)
    is consistent across the two worktree handlers. We keep our own
    copy so this module stays import-independent.
    """
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def classify_worktree_state(path: Path) -> WorktreeClassification:
    """Classify ``path`` into one of the ``WorktreeState`` values.

    The classifier is intentionally best-effort: each sub-check is
    wrapped so a single failing git call (e.g. a corrupt worktree)
    falls through to ``MISSING`` rather than crashing the whole sweep.

    Order of checks matters:

    1. Path existence + ``.git`` presence → ``MISSING`` when absent.
    2. ``.git/index.lock`` → ``LOCK_FILE`` (win-LOSES against everything
       else because a held lock means subsequent git calls will stall
       or fail).
    3. ``git symbolic-ref HEAD`` → ``DETACHED_HEAD`` on failure.
    4. ``git status --porcelain`` → unmerged codes → ``MERGE_CONFLICT``.
    5. Any dirty lines → ``DIRTY_EXPECTED`` (after the staleness check
       the handler does on its side).
    6. No upstream + last commit >7d → ``ORPHAN_BRANCH``.
    7. Otherwise → ``CLEAN``.
    """
    if not path.exists() or not path.is_dir():
        return WorktreeClassification(
            state=WorktreeState.MISSING,
            metadata={"reason": "path_missing"},
        )

    git_dir = path / ".git"
    # ``.git`` may be a file (worktree checkout) or a directory (main
    # clone). Either is acceptable — we only need the lock-file probe
    # to resolve the actual gitdir.
    if not git_dir.exists():
        return WorktreeClassification(
            state=WorktreeState.MISSING,
            metadata={"reason": "not_a_git_worktree"},
        )

    # Resolve the real gitdir so ``index.lock`` is found on worktrees
    # where ``.git`` is a gitlink file (``gitdir: ...``).
    resolved_gitdir = _resolve_gitdir(git_dir)
    lock_path = resolved_gitdir / "index.lock" if resolved_gitdir else git_dir / "index.lock"
    if lock_path.exists():
        try:
            lock_age_s = time.time() - lock_path.stat().st_mtime
        except OSError:
            lock_age_s = 0.0
        return WorktreeClassification(
            state=WorktreeState.LOCK_FILE,
            metadata={
                "lock_path": str(lock_path),
                "lock_age_seconds": float(lock_age_s),
            },
        )

    # HEAD status — detached heads can't push, so flag them early.
    head_proc = _git(path, "symbolic-ref", "--quiet", "HEAD")
    if head_proc.returncode != 0:
        # symbolic-ref returns non-zero on detached HEAD.
        rev = _git(path, "rev-parse", "--short", "HEAD")
        sha = rev.stdout.strip() if rev.returncode == 0 else ""
        return WorktreeClassification(
            state=WorktreeState.DETACHED_HEAD,
            branch=None,
            metadata={"head_sha": sha},
        )
    ref = head_proc.stdout.strip()
    # ``refs/heads/foo`` → ``foo``.
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref

    # Porcelain status gives us both conflict codes and dirty indicator
    # in a single call.
    status_proc = _git(path, "status", "--porcelain")
    if status_proc.returncode != 0:
        # Degrade gracefully — if status fails we can't classify further.
        return WorktreeClassification(
            state=WorktreeState.MISSING,
            branch=branch,
            metadata={
                "reason": "status_failed",
                "stderr": status_proc.stderr.strip(),
            },
        )

    dirty_lines = [
        line for line in status_proc.stdout.splitlines() if line.strip()
    ]
    conflict_files: list[str] = []
    for line in dirty_lines:
        # Porcelain v1 format: first 2 chars = XY code, then space, then path.
        if len(line) < 3:
            continue
        code = line[:2]
        if code in _CONFLICT_CODES:
            conflict_files.append(line[3:].strip())

    if conflict_files:
        return WorktreeClassification(
            state=WorktreeState.MERGE_CONFLICT,
            branch=branch,
            metadata={"conflict_files": conflict_files},
        )

    if dirty_lines:
        return WorktreeClassification(
            state=WorktreeState.DIRTY_EXPECTED,
            branch=branch,
            metadata={"dirty_line_count": len(dirty_lines)},
        )

    # Orphan branch — local branch, no upstream, no commit in last 7d.
    upstream_proc = _git(
        path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}",
    )
    has_upstream = upstream_proc.returncode == 0 and bool(upstream_proc.stdout.strip())
    if not has_upstream:
        # Get the commit epoch of HEAD.
        commit_proc = _git(path, "log", "-1", "--format=%ct", "HEAD")
        if commit_proc.returncode == 0 and commit_proc.stdout.strip():
            try:
                commit_ts = int(commit_proc.stdout.strip())
            except ValueError:
                commit_ts = int(time.time())
            age_days = (time.time() - commit_ts) / 86400.0
            if age_days > 7.0:
                return WorktreeClassification(
                    state=WorktreeState.ORPHAN_BRANCH,
                    branch=branch,
                    metadata={
                        "age_days": float(age_days),
                        "has_upstream": False,
                    },
                )

    return WorktreeClassification(
        state=WorktreeState.CLEAN,
        branch=branch,
        metadata={},
    )


def _resolve_gitdir(git_entry: Path) -> Path | None:
    """Return the real gitdir for a worktree.

    For a worktree checkout, ``.git`` is a file whose contents look
    like ``gitdir: /abs/path/to/.git/worktrees/<name>``. Resolving the
    real gitdir lets us check ``index.lock`` on worktrees, not just on
    the main clone.
    """
    if git_entry.is_dir():
        return git_entry
    try:
        text = git_entry.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not text.startswith(prefix):
        return None
    raw = text[len(prefix):].strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (git_entry.parent / candidate).resolve()
    if candidate.exists():
        return candidate
    return None
