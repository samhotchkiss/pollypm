"""Cleanup helper for the legacy ``~/.pollypm/.pollypm/`` doubled-path artifact.

Background
----------
Pre-#1972 config renders wrote sibling paths as ``.pollypm/...`` relative
to ``~/.pollypm`` itself, which reloads as a bogus nested
``~/.pollypm/.pollypm/`` directory. #1996/#2002/#2003/#2004 stopped NEW
writes from going there, but operators upgrading from older builds still
carry a stranded tree of dead files. ``pm doctor`` warns about it via
:func:`pollypm.doctor.check_doubled_pollypm_path`; this module is the
fixer that ``--fix`` invokes for that specific check.

Contract
--------
* Always MOVE, never delete. The user can ``rm -rf`` the backup once
  they've eyeballed it.
* Refuse if the doubled tree's contents look anomalous — specifically
  if a real ``~/.pollypm`` subdir name (``audit``, ``agent_homes``,
  ``plugins``, ``state.db``, …) appears as a direct child. That would
  suggest the user's real config dir got nested somehow and ``mv``-ing
  it sideways would lose live state.
* Refuse if the target doesn't exist (nothing to clean).
* Never follow symlinks during the safety scan.
* Dry-run mode never touches the filesystem.

The helper is intentionally a thin, pure function so the doctor
``--fix`` hook and the unit tests share the same code path.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# Direct children of the *real* ``~/.pollypm/`` that, if seen as direct
# children of the doubled tree, mean we're looking at something more
# than a stray leftover and must refuse to move it. Keep this list
# conservative — anything here is "if I move this dir, the user loses
# live state". Don't include scratch-y names that legitimately also
# appear under the doubled tree (logs/, errors.log, the config TOML
# itself — those CAN show up under the doubled path from the original
# bad renders and are safe to relocate as part of the move).
ANOMALOUS_DIRECT_CHILDREN: frozenset[str] = frozenset(
    {
        "audit",
        "agent_homes",
        "plugins",
        "state.db",
        "state.db-wal",
        "state.db-shm",
        "pollypm.toml",
    }
)


@dataclass(slots=True)
class CleanupPlan:
    """Outcome of inspecting (and optionally moving) the doubled tree.

    ``moved`` is True only after a successful real-mode move; dry-run
    leaves it False.
    """

    target: Path
    backup_path: Path
    file_count: int = 0
    total_bytes: int = 0
    moved: bool = False
    dry_run: bool = False
    refused_reason: str | None = None
    anomalies: list[str] = field(default_factory=list)


def _scan(target: Path) -> tuple[int, int]:
    """Return ``(file_count, total_bytes)`` for a non-symlink walk."""
    file_count = 0
    total_bytes = 0
    # Walk WITHOUT following symlinks. ``os.walk`` defaults to
    # ``followlinks=False`` and ``Path.rglob`` itself doesn't follow
    # symlinked directories, but we explicitly guard each entry too.
    for entry in target.rglob("*"):
        try:
            if entry.is_symlink():
                # Count symlinks themselves as zero-byte entries — they
                # move along with the tree but we don't dereference.
                file_count += 1
                continue
            if entry.is_file():
                stat = entry.stat()
                total_bytes += stat.st_size
                file_count += 1
        except OSError:
            # Permission-denied or vanished mid-walk: skip silently. The
            # safety check above already caught the kind of anomaly we
            # actually care about (suspicious top-level names).
            continue
    return file_count, total_bytes


def _detect_anomalies(target: Path) -> list[str]:
    """Return names under ``target`` that look like real config state.

    Only inspects DIRECT children. Symlinks are listed by name (we don't
    follow them) so they still trigger the safety guard if a symlinked
    ``audit`` somehow pointed at the user's real one.
    """
    anomalies: list[str] = []
    try:
        children = sorted(target.iterdir())
    except OSError:
        return anomalies
    for child in children:
        if child.name in ANOMALOUS_DIRECT_CHILDREN:
            anomalies.append(child.name)
    return anomalies


def _backup_path_for(target: Path, *, now: datetime | None = None) -> Path:
    """Return ``<sibling>/.pollypm.bak-YYYYMMDD-HHMMSS`` next to target.

    Uses local time — operators reading the dir listing think in local
    time, and the suffix is purely cosmetic / sort-order useful.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    # ``target`` is ``~/.pollypm/.pollypm`` so we want the sibling
    # ``~/.pollypm/.pollypm.bak-...`` — same parent, suffixed name.
    return target.parent / f"{target.name}.bak-{stamp}"


def cleanup_doubled_path(
    target: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> CleanupPlan:
    """Inspect (and optionally move) the doubled-path artifact.

    Args:
        target: Absolute path to the doubled tree, typically
            ``~/.pollypm/.pollypm``. Symlinks at this path are refused.
        dry_run: When True, scan + safety-check only; never move.
        now: Override clock for deterministic backup naming in tests.

    Returns:
        A :class:`CleanupPlan` describing what was found and (in
        non-dry-run mode) what was moved. Inspect ``refused_reason``
        first: a non-None value means the helper did NOT move anything,
        and ``moved`` stays False.
    """
    plan = CleanupPlan(
        target=target,
        backup_path=_backup_path_for(target, now=now),
        dry_run=dry_run,
    )

    # Refuse: target missing.
    if not target.exists():
        plan.refused_reason = f"target does not exist: {target}"
        return plan

    # Refuse: target is a symlink. Moving a symlink would either
    # relocate the link itself (confusing) or — if we ever changed to
    # follow it — torch the user's real ~/.pollypm/. Just refuse.
    if target.is_symlink():
        plan.refused_reason = f"target is a symlink: {target}"
        return plan

    if not target.is_dir():
        plan.refused_reason = f"target is not a directory: {target}"
        return plan

    # Refuse: anomalous direct children (looks like a real config root).
    anomalies = _detect_anomalies(target)
    plan.anomalies = anomalies
    if anomalies:
        plan.refused_reason = (
            f"anomalous direct children under {target}: "
            f"{', '.join(anomalies)} — refusing to move because this "
            f"looks like a real config root, not a stray doubled-path "
            f"artifact"
        )
        return plan

    # Safe to count + (optionally) move.
    plan.file_count, plan.total_bytes = _scan(target)

    if dry_run:
        return plan

    # Refuse to clobber an existing backup. Operators may have already
    # run the cleanup once today and we don't want to merge two backup
    # trees silently.
    if plan.backup_path.exists():
        plan.refused_reason = (
            f"backup path already exists: {plan.backup_path}"
        )
        return plan

    try:
        # ``shutil.move`` falls back to copy+delete across filesystems;
        # within ``~/.pollypm/`` it'll just rename, which is what we
        # want — atomic and instant.
        shutil.move(str(target), str(plan.backup_path))
    except OSError as exc:
        plan.refused_reason = f"move failed: {exc}"
        return plan

    plan.moved = True
    return plan


def emit_cleanup_audit_event(plan: CleanupPlan) -> None:
    """Emit ``cockpit.doubled_path_artifacts_cleaned`` for a moved plan.

    No-op for dry-run or refused plans. Best-effort: audit failures
    never propagate (we already moved the files; losing one event is
    strictly better than implying the move didn't happen).
    """
    if not plan.moved:
        return
    try:
        from pollypm.audit import emit as _audit_emit

        _audit_emit(
            event="cockpit.doubled_path_artifacts_cleaned",
            project="_workspace",
            subject=str(plan.target),
            actor="pm doctor --fix",
            status="ok",
            metadata={
                "moved_file_count": plan.file_count,
                "moved_size_bytes": plan.total_bytes,
                "backup_path": str(plan.backup_path),
            },
        )
    except Exception:  # noqa: BLE001 — audit must never block
        logger.warning(
            "Failed to emit cockpit.doubled_path_artifacts_cleaned audit event",
            exc_info=True,
        )


def format_summary(plan: CleanupPlan) -> str:
    """Render a one-screen human summary of a CleanupPlan."""
    size_kib = plan.total_bytes / 1024.0
    word = "file" if plan.file_count == 1 else "files"
    if plan.refused_reason is not None:
        return f"refused: {plan.refused_reason}"
    if plan.dry_run:
        return (
            f"dry-run: would move {plan.file_count} {word} "
            f"({size_kib:.1f} KiB) from {plan.target} "
            f"to {plan.backup_path}"
        )
    if plan.moved:
        return (
            f"moved {plan.file_count} {word} ({size_kib:.1f} KiB) "
            f"from {plan.target} to {plan.backup_path}"
        )
    return f"no-op: nothing moved at {plan.target}"


__all__ = [
    "ANOMALOUS_DIRECT_CHILDREN",
    "CleanupPlan",
    "cleanup_doubled_path",
    "emit_cleanup_audit_event",
    "format_summary",
]
