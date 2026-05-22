"""Canonical ``~/.pollypm/`` storage scanner — neutral helper module.

Owns the *read-only* scanner that powers both ``pm storage report``
(CLI surface in :mod:`pollypm.cli_features.storage`) and
``GET /api/v1/storage`` (web surface in
:mod:`pollypm.web_api.routes.storage`). Extracted out of
``cli_features/storage.py`` (Codex review on PR #2054) so the web
route doesn't import a Typer/migration/prune-laden CLI module just
to read the disk-usage report.

What lives here
---------------

* The canonical subdir list (:data:`HOME_SUBDIRS`) — the source of
  truth for "which subdirs of ``~/.pollypm/`` does the report cover."
* The wire-shaped dataclasses :class:`DirScan` / :class:`HomeReport`.
* The pure-function scanner :func:`scan_pollypm_home` plus the
  internal helpers it depends on (subdir walk, config-file rollup,
  live-agent / orphan-worktree detection, audit-rotation flag).

What does NOT live here
-----------------------

The renderers (text / JSON tables), prune candidate iterators,
typer wiring, ``bootstrap-pg`` / ``migrate-to-pg`` orchestration —
those stay in ``cli_features/storage.py`` (CLI-only concerns) or
``storage/pg_migration_tool.py``.

Backwards-compat
----------------

``cli_features/storage.py`` re-exports every public name in this
module under its original ``cli_features.storage.<name>`` path, so
in-tree callers (and the existing test suite) keep working without
edits.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


__all__ = [
    "HOME_SUBDIRS",
    "SHALLOW_ONLY_DIRS",
    "SCAN_FILE_CAP",
    "ORPHAN_WORKTREE_STALE_DAYS",
    "DirScan",
    "HomeReport",
    "scan_pollypm_home",
    # Internal-but-shared helpers (CLI prune paths consume these).
    "count_live_agent_worktrees",
    "count_orphan_worktrees",
    "iter_orphan_worktree_paths",
    "has_gz_descendant",
    "scan_subdir",
    "scan_config_files",
]


# ---------------------------------------------------------------------------
# Canonical constants
# ---------------------------------------------------------------------------

# The order here is the natural reading order of the report
# (snapshots first because it's the unbounded one); the actual
# render-time sort is configurable via the CLI's ``--sort``.
HOME_SUBDIRS: tuple[str, ...] = (
    "snapshots",
    "transcripts",
    "homes",
    "agent_homes",
    "worktrees",
    "audit",
    "artifacts",
    "checkpoints",
    "dossier",
    "logs",
)

# Subdirs where recursion is capped because production has been
# observed to grow them without bound. Cap-hit fires a NOTES flag.
SHALLOW_ONLY_DIRS: frozenset[str] = frozenset({"snapshots"})

# How many entries we'll walk in a capped subdir before bailing.
# 50k is large enough to give exact counts on a healthy install
# (snapshots/ holds ~few-thousand files on a normal week) while
# short enough to bound the report at ~1s even when the dir is
# already broken. The number is intentionally NOT user-tunable —
# this is a visibility tool, not a forensics tool; if the operator
# needs an exact count of a 1M-file dir they can use ``find | wc``.
SCAN_FILE_CAP: int = 50_000

# How many days of stillness make an unattended worktree dir an
# orphan candidate. Picked deliberately conservative: a live agent
# is expected to touch its worktree at least once per heartbeat
# (default 30s), so 1 day is ~2880 heartbeats of silence — well
# past any "agent is doing slow work" plateau.
ORPHAN_WORKTREE_STALE_DAYS: int = 1


# ---------------------------------------------------------------------------
# Wire-shaped dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DirScan:
    """One row of the storage report — totals for a single subdir.

    Attributes
    ----------
    name :
        Subdir name relative to ``~/.pollypm/`` (e.g. ``"snapshots"``).
    files :
        Total file count. When ``cap_hit`` is True this is the cap
        value, not the true count.
    bytes :
        Sum of ``stat().st_size`` over every file walked.
    oldest_mtime / newest_mtime :
        Earliest / latest mtime seen (epoch seconds). ``None`` when
        the dir is empty or unreadable.
    cap_hit :
        True when the scan stopped at ``SCAN_FILE_CAP`` rather than
        completing — the report flags this in NOTES so the operator
        knows the count is a lower bound.
    note :
        Free-form NOTES column text (unbounded-growth flag, orphan
        count for worktrees, rotation flag for audit, etc.).
    """

    name: str
    files: int = 0
    bytes: int = 0
    oldest_mtime: float | None = None
    newest_mtime: float | None = None
    cap_hit: bool = False
    note: str = ""


@dataclass(slots=True)
class HomeReport:
    """Whole-home summary returned by ``scan_pollypm_home``.

    Holds the per-subdir rows plus aggregate totals so the renderer
    doesn't have to walk the list twice. ``config_files_*`` counts
    top-level ``*.toml`` / ``*.json`` / ``*.pid`` artifacts directly
    under ``~/.pollypm/`` (separated from subdirs so they don't
    distort sorting).
    """

    home: Path
    rows: list[DirScan] = field(default_factory=list)
    config_files: int = 0
    config_bytes: int = 0
    config_newest_mtime: float | None = None

    @property
    def total_files(self) -> int:
        return self.config_files + sum(row.files for row in self.rows)

    @property
    def total_bytes(self) -> int:
        return self.config_bytes + sum(row.bytes for row in self.rows)


# ---------------------------------------------------------------------------
# Filesystem walkers (private to the scanner, exposed for CLI prune reuse)
# ---------------------------------------------------------------------------


def scan_subdir(
    root: Path,
    name: str,
    *,
    cap: int | None,
) -> DirScan:
    """Walk ``root/name`` with ``os.scandir`` and return a ``DirScan``.

    When ``cap`` is set and we exceed it, ``cap_hit=True`` is recorded
    and the walk stops early — the caller treats those counts as a
    lower bound. A missing / unreadable subdir returns an empty
    ``DirScan`` rather than raising; the report should still render
    even when one subtree has wedged permissions.

    Uses ``os.scandir`` because it returns file metadata (``stat``,
    ``is_file``) from the directory entry without an extra syscall —
    ``Path.rglob`` round-trips through ``PosixPath`` construction and
    is measurably slower at the file counts we care about. We also
    catch ``OSError`` per-entry so one bad symlink doesn't abort the
    whole subdir scan (#1810's doubled-path scaffold leaves a few
    broken links on long-lived dev machines).
    """
    scan = DirScan(name=name)
    target = root / name
    if not target.is_dir():
        return scan

    stack: list[str] = [str(target)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        # Don't follow symlinks — they'd let an
                        # attacker / misconfigured plugin send the
                        # scan out of ~/.pollypm/. ``follow_symlinks
                        # =False`` on ``is_dir`` avoids that.
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    scan.files += 1
                    scan.bytes += st.st_size
                    mtime = st.st_mtime
                    if scan.oldest_mtime is None or mtime < scan.oldest_mtime:
                        scan.oldest_mtime = mtime
                    if scan.newest_mtime is None or mtime > scan.newest_mtime:
                        scan.newest_mtime = mtime
                    if cap is not None and scan.files >= cap:
                        scan.cap_hit = True
                        return scan
        except OSError:
            continue
    return scan


def scan_config_files(root: Path) -> tuple[int, int, float | None]:
    """Return ``(count, bytes, newest_mtime)`` for top-level config artifacts.

    Counts every file directly under ``~/.pollypm/`` (NOT in a
    subdir): pollypm.toml, state.db / state.db-wal / -shm,
    rail_daemon.pid, errors.log, etc. Separated from the subdir scan
    so the report can summarise "config files" as its own row
    without distorting the sort-by-bytes ordering.
    """
    count = 0
    total = 0
    newest: float | None = None
    if not root.is_dir():
        return (0, 0, None)
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                count += 1
                total += st.st_size
                if newest is None or st.st_mtime > newest:
                    newest = st.st_mtime
    except OSError:
        return (0, 0, None)
    return (count, total, newest)


def count_live_agent_worktrees(home: Path) -> set[str] | None:
    """Return the set of worktree dir names that have a live agent task.

    Best-effort: reads ``~/.pollypm/worktrees/`` and matches each
    directory name against in-progress work-service tasks.

    Return contract (issue: Codex blocker on PR #2040 — distinguishing
    "live set unknown" from "known empty live set" is a SAFETY
    requirement, because the prune paths must REFUSE to delete when the
    live set is unknown):

    * ``None`` — live-agent detection FAILED (config missing, work
      service module unimportable, listing raised, etc.). Callers that
      delete dirs MUST refuse to prune in this case — otherwise an
      unreachable work service would cause every stale ``agent-*`` dir
      to be treated as orphaned and deleted.
    * ``set()`` — work service reachable but reports no in-progress
      agent tasks. Safe to proceed with orphan calculation: any stale
      dir really is orphaned.
    * ``{agent_id, ...}`` — populated live set. Standard orphan
      calculation applies.

    The visibility-only callers (``scan_pollypm_home`` /
    ``count_orphan_worktrees``) MAY treat ``None`` as ``set()`` since
    they only render advisory NOTES, never mutate the filesystem.
    """
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, resolve_config_path, load_config
        from pollypm.work.service_factory import get_default_work_service
    except Exception:  # noqa: BLE001 — module missing == live set unknown
        return None
    try:
        cfg_path = resolve_config_path(DEFAULT_CONFIG_PATH)
        if not cfg_path.exists():
            return None
        cfg = load_config(cfg_path)
        service = get_default_work_service(cfg)
    except Exception:  # noqa: BLE001 — service-init failure == unknown
        return None

    live: set[str] = set()
    try:
        # The work service exposes tasks via ``list_tasks`` /
        # ``get_active_tasks`` depending on backend; we try the
        # broader one and fall back. Names are best-effort: an
        # agent task may be tagged ``agent-<id>`` or just ``<id>``.
        list_tasks = getattr(service, "list_tasks", None)
        if list_tasks is None:
            return None
        for task in list_tasks():
            status = getattr(task, "status", None)
            # Normalize ``WorkStatus`` enum -> its ``.value`` string so
            # active tasks represented as ``WorkStatus.IN_PROGRESS``
            # are recognised; without this they'd fall through the
            # filter and their worktrees would be eligible for prune
            # (Codex PR #2040 blocker).
            status_value = getattr(status, "value", status)
            if status_value not in ("in_progress", "claimed", "assigned"):
                continue
            agent_id = (
                getattr(task, "worktree_name", None)
                or getattr(task, "agent_id", None)
                or getattr(task, "id", None)
            )
            if agent_id:
                live.add(str(agent_id))
                live.add(f"agent-{agent_id}")
    except Exception:  # noqa: BLE001 — listing failed == unknown
        return None
    return live


def count_orphan_worktrees(
    home: Path,
    *,
    live_names: set[str],
    stale_after_seconds: float,
) -> int:
    """Return the count of worktree subdirs with no matching live agent.

    Orphan definition: dir whose mtime is older than
    ``stale_after_seconds`` AND whose basename is not in
    ``live_names``. The mtime check protects against flagging a
    just-spawned worktree before the agent has registered itself.
    """
    target = home / "worktrees"
    if not target.is_dir():
        return 0
    now = time.time()
    orphans = 0
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if entry.name in live_names:
                    continue
                if now - st.st_mtime < stale_after_seconds:
                    continue
                orphans += 1
    except OSError:
        return 0
    return orphans


def iter_orphan_worktree_paths(
    home: Path,
    *,
    live_names: set[str],
    stale_after_seconds: float,
) -> Iterable[Path]:
    """Yield orphan worktree paths (same definition as ``count_orphan_worktrees``).

    Split out from the counter so prune can iterate without a second
    scandir pass — counter wraps this for backwards-compat.
    """
    target = home / "worktrees"
    if not target.is_dir():
        return
    now = time.time()
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if entry.name in live_names:
                    continue
                if now - st.st_mtime < stale_after_seconds:
                    continue
                yield Path(entry.path)
    except OSError:
        return


def has_gz_descendant(root: Path) -> bool:
    """Best-effort scandir check: True if any ``.gz`` file lives under ``root``.

    Cheap walk with an early-exit on the first hit. Used to flag
    audit rotation in NOTES without a full second scan of the audit
    tree (which is small anyway, but the early-exit keeps the cost
    predictable on every dir size).
    """
    stack: list[str] = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            if entry.name.endswith(".gz"):
                                return True
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def scan_pollypm_home(home: Path | None = None) -> HomeReport:
    """Scan ``home`` (default ``~/.pollypm``) and return a ``HomeReport``.

    Public entry point — both ``pm storage report`` and
    ``GET /api/v1/storage`` route through here. Pass a ``tmp_path``
    in tests to scan a synthetic tree without going through the
    Typer surface.

    When ``home`` does not exist on disk (fresh install, alternate
    ``base_dir`` not yet provisioned), returns an empty
    :class:`HomeReport` (no rows, zero totals) — the renderers and
    the web route both treat that as a normal operating mode rather
    than an error. Callers wanting a single-subdir slice in the
    missing-home case should synthesize an empty :class:`DirScan`
    themselves; we don't pre-populate rows because the existing
    ``pm storage report --json`` shape assumes ``rows == []`` means
    "no data" (see PR #2054 round 1 + the route's compensating
    branch).
    """
    if home is None:
        home = Path.home() / ".pollypm"
    report = HomeReport(home=home)
    if not home.is_dir():
        return report

    # Visibility-only path: treat "unknown" (None) as "no known live"
    # for the orphan-NOTES badge. The report never deletes; rendering
    # NOTES against an empty live set just over-flags rather than
    # under-flags, which is the safe direction here. The prune paths
    # below have a separate refuse-on-unknown gate.
    live_names = count_live_agent_worktrees(home) or set()
    stale_seconds = ORPHAN_WORKTREE_STALE_DAYS * 86400.0

    for name in HOME_SUBDIRS:
        cap = SCAN_FILE_CAP if name in SHALLOW_ONLY_DIRS else None
        row = scan_subdir(home, name, cap=cap)
        # NOTES heuristics. Order matters: cap-hit dominates because
        # any other note would understate the situation.
        notes: list[str] = []
        if row.cap_hit:
            notes.append(
                f"⚠ unbounded growth (>{SCAN_FILE_CAP:,} files; scan capped)"
            )
        if name == "worktrees" and row.files > 0:
            orphans = count_orphan_worktrees(
                home,
                live_names=live_names,
                stale_after_seconds=stale_seconds,
            )
            if orphans > 0:
                notes.append(f"{orphans} orphaned (no live agent)")
        if name == "audit" and row.files > 0:
            # Rotation lands a ``.jsonl`` plus ``.gz`` siblings. If
            # we see any ``.gz`` in the tree, rotation is active.
            if has_gz_descendant(home / name):
                notes.append("rotation active")
        row.note = " · ".join(notes)
        report.rows.append(row)

    cfg_count, cfg_bytes, cfg_newest = scan_config_files(home)
    report.config_files = cfg_count
    report.config_bytes = cfg_bytes
    report.config_newest_mtime = cfg_newest

    return report
