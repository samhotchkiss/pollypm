"""Backup + restore of the PollyPM state database.

This module is the implementation behind ``pm backup`` / ``pm restore``. The
CLI layer in :mod:`pollypm.cli` is kept thin — all of the IO, sanity
checks, and retention logic live here so they are testable without going
through Typer.

Design notes:

* The DB snapshot uses SQLite's online backup API (``sqlite3.Connection.backup``),
  NOT ``shutil.copy``. That is the only safe way to copy a live WAL-mode
  database while the heartbeat / cockpit may be writing to it.
* Snapshots are gzipped on disk. The backup API needs a plain sqlite
  file to write into, so we back up to a temporary uncompressed file
  first and then gzip it.
* ``--full`` tar.gz archives include the snapshot DB plus config /
  logs / snapshots / agent homes. They are not touched by retention —
  operators use them for point-in-time rescue, not routine cleanup.
* Restores always write a ``.before-restore`` sibling copy of the live
  DB before replacing it. This is the safety net; it's non-negotiable
  and the CLI layer cannot skip it.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Retention only applies to plain DB snapshots (``state-db-*.db.gz``).
# ``--full`` tar.gz archives are left alone because they are larger and
# more precious — an operator who ran ``pm backup --full`` almost
# always did so intentionally before a risky change.
_DB_SNAPSHOT_PREFIX = "state-db-"
_DB_SNAPSHOT_SUFFIX = ".db.gz"
_FULL_SNAPSHOT_PREFIX = "full-"
_FULL_SNAPSHOT_SUFFIX = ".tar.gz"

# Default retention for plain DB snapshots; keep the last N.
DEFAULT_KEEP = 7


# --------------------------------------------------------------------- #
# Result dataclasses
# --------------------------------------------------------------------- #


@dataclass(slots=True)
class BackupResult:
    path: Path
    db_size_before: int
    archive_size: int
    pruned: list[Path]
    full: bool


@dataclass(slots=True)
class RestorePlan:
    snapshot_path: Path
    live_db_path: Path
    safety_path: Path
    is_tar: bool


@dataclass(slots=True)
class RestoreResult:
    snapshot_path: Path
    live_db_path: Path
    safety_path: Path
    is_tar: bool


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _timestamp() -> str:
    """Return a filename-safe local timestamp."""
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")


def _default_backup_dir(base_dir: Path) -> Path:
    """Return ``~/.pollypm/backups`` (or the configured ``base_dir``)."""
    return base_dir / "backups"


def _size_or_zero(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _is_valid_sqlite_file(path: Path) -> bool:
    """Return True if ``path`` looks like a readable SQLite DB.

    We do a cheap header check (``SQLite format 3\\0``) plus a
    ``PRAGMA schema_version`` query. We don't validate the schema —
    the caller may be restoring from a snapshot that predates a
    migration, and the cockpit will handle that on next startup.
    """
    try:
        with path.open("rb") as fh:
            header = fh.read(16)
    except OSError:
        return False
    if not header.startswith(b"SQLite format 3\x00"):
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA schema_version").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False
    return True


def _is_valid_tar_gz(path: Path) -> bool:
    try:
        with tarfile.open(path, mode="r:gz") as _:
            return True
    except (tarfile.TarError, OSError):
        return False


def _classify_snapshot(path: Path) -> str:
    """Return ``"db"``, ``"tar"``, or raise ``ValueError``."""
    name = path.name
    if name.endswith(_DB_SNAPSHOT_SUFFIX) or name.endswith(".db") or name.endswith(".sqlite"):
        # Allow both the canonical gzipped form and raw .db files
        # (handy for quick dev snapshots and for verifying a file
        # that was decompressed manually).
        return "db"
    if name.endswith(_FULL_SNAPSHOT_SUFFIX) or name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "tar"
    # Fall back to magic-byte sniffing so operators can rename files.
    try:
        with path.open("rb") as fh:
            magic = fh.read(4)
    except OSError as exc:
        raise ValueError(f"cannot read snapshot at {path}: {exc}") from exc
    if magic.startswith(b"SQLite"):
        return "db"
    if magic[:2] == b"\x1f\x8b":
        # Could be a raw gzipped DB or a tar.gz. Peek inside.
        try:
            with tarfile.open(path, mode="r:gz"):
                return "tar"
        except tarfile.TarError:
            return "db"
    raise ValueError(f"unrecognized snapshot format: {path}")


def _online_backup_to_plain_file(source_db: Path, dest: Path) -> None:
    """Use SQLite's online backup API to copy ``source_db`` -> ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _gzip_file(src: Path, dest_gz: Path) -> None:
    dest_gz.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as fin, gzip.open(dest_gz, "wb") as fout:
        shutil.copyfileobj(fin, fout)


def _gunzip_file(src_gz: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(src_gz, "rb") as fin, dest.open("wb") as fout:
        shutil.copyfileobj(fin, fout)


# --------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------- #


def _list_db_snapshots(backup_dir: Path) -> list[Path]:
    if not backup_dir.exists():
        return []
    out: list[Path] = []
    for child in backup_dir.iterdir():
        if not child.is_file():
            continue
        if child.name.startswith(_DB_SNAPSHOT_PREFIX) and child.name.endswith(_DB_SNAPSHOT_SUFFIX):
            out.append(child)
    # Oldest first so retention pruning is a simple slice.
    out.sort(key=lambda p: p.stat().st_mtime)
    return out


def _prune_db_snapshots(backup_dir: Path, keep: int) -> list[Path]:
    """Delete db snapshots past ``keep``. Returns the deleted paths."""
    if keep < 0:
        raise ValueError("keep must be >= 0")
    snapshots = _list_db_snapshots(backup_dir)
    if len(snapshots) <= keep:
        return []
    to_delete = snapshots[: len(snapshots) - keep]
    deleted: list[Path] = []
    for path in to_delete:
        try:
            path.unlink()
            deleted.append(path)
        except OSError:
            # Best-effort — leave stragglers in place rather than
            # failing the whole backup.
            continue
    return deleted


# --------------------------------------------------------------------- #
# Public API — backup
# --------------------------------------------------------------------- #


def backup_state_db(
    state_db: Path,
    *,
    base_dir: Path,
    output: Path | None = None,
    full: bool = False,
    keep: int = DEFAULT_KEEP,
    extra_roots: list[Path] | None = None,
) -> BackupResult:
    """Snapshot ``state_db`` to the backup directory (or ``output``).

    Parameters
    ----------
    state_db:
        Path to the live SQLite DB (typically ``~/.pollypm/state.db``).
    base_dir:
        The ``config.project.base_dir`` — used to locate the default
        backup directory and, for ``--full``, to pick up logs /
        snapshots / agent homes that live under it.
    output:
        Optional custom destination path. Directories are created.
        When ``full`` is True the output is interpreted as a
        ``.tar.gz`` path; otherwise as a ``.db.gz`` path.
    full:
        If True, create a tar.gz that bundles the DB snapshot plus the
        contents of ``base_dir`` and any ``extra_roots``.
    keep:
        Retention count for plain DB snapshots. Ignored when ``full``.
    extra_roots:
        Additional paths to include in a ``--full`` archive. Missing
        paths are silently skipped.
    """
    if not state_db.exists():
        raise FileNotFoundError(f"state.db not found at {state_db}")

    db_size_before = _size_or_zero(state_db)
    backup_dir = _default_backup_dir(base_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = _timestamp()

    if full:
        # Stage the DB snapshot into a temp file, then pack everything
        # into a tar.gz. We keep the DB inside the archive under a
        # stable path (``state.db``) so ``pm restore`` can find it
        # without knowing the original hostname / timestamp.
        if output is not None:
            archive_path = output
        else:
            archive_path = backup_dir / f"{_FULL_SNAPSHOT_PREFIX}{timestamp}{_FULL_SNAPSHOT_SUFFIX}"
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as staging:
            staging_db = Path(staging) / "state.db"
            _online_backup_to_plain_file(state_db, staging_db)

            with tarfile.open(archive_path, mode="w:gz") as tar:
                tar.add(staging_db, arcname="state.db")
                # Bundle the base_dir tree — but skip the ``backups/``
                # subdir so archives don't grow recursively each run.
                if base_dir.exists():
                    for child in sorted(base_dir.iterdir()):
                        if child.resolve() == backup_dir.resolve():
                            continue
                        if child.resolve() == state_db.resolve():
                            # Already captured as the online backup
                            continue
                        tar.add(child, arcname=f"base/{child.name}")
                for extra in extra_roots or []:
                    if not extra.exists():
                        continue
                    tar.add(extra, arcname=f"extra/{extra.name}")

        return BackupResult(
            path=archive_path,
            db_size_before=db_size_before,
            archive_size=_size_or_zero(archive_path),
            pruned=[],
            full=True,
        )

    # Plain DB snapshot path.
    if output is not None:
        snapshot_path = output
    else:
        snapshot_path = backup_dir / f"{_DB_SNAPSHOT_PREFIX}{timestamp}{_DB_SNAPSHOT_SUFFIX}"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as staging:
        staging_db = Path(staging) / "state.db"
        _online_backup_to_plain_file(state_db, staging_db)
        _gzip_file(staging_db, snapshot_path)

    pruned: list[Path] = []
    # Only prune the default backup dir. If the user wrote a snapshot
    # to a custom ``--output`` we leave their file layout alone.
    if output is None:
        pruned = _prune_db_snapshots(backup_dir, keep)

    return BackupResult(
        path=snapshot_path,
        db_size_before=db_size_before,
        archive_size=_size_or_zero(snapshot_path),
        pruned=pruned,
        full=False,
    )


# --------------------------------------------------------------------- #
# Public API — restore
# --------------------------------------------------------------------- #


def plan_restore(snapshot_path: Path, live_db: Path) -> RestorePlan:
    """Validate the snapshot and describe what would happen.

    Raises ``FileNotFoundError`` / ``ValueError`` on invalid input.
    Does NOT touch the filesystem.
    """
    if not snapshot_path.exists():
        raise FileNotFoundError(f"snapshot not found: {snapshot_path}")

    kind = _classify_snapshot(snapshot_path)

    # Verify the snapshot is actually well-formed. For gzipped DBs we
    # have to decompress into a temp file to run the sqlite header +
    # schema_version probe.
    if kind == "db":
        if snapshot_path.suffix == ".gz":
            with tempfile.TemporaryDirectory() as staging:
                decompressed = Path(staging) / "probe.db"
                try:
                    _gunzip_file(snapshot_path, decompressed)
                except OSError as exc:
                    raise ValueError(f"failed to decompress snapshot: {exc}") from exc
                if not _is_valid_sqlite_file(decompressed):
                    raise ValueError(
                        f"snapshot is not a valid SQLite database: {snapshot_path}"
                    )
        else:
            if not _is_valid_sqlite_file(snapshot_path):
                raise ValueError(
                    f"snapshot is not a valid SQLite database: {snapshot_path}"
                )
    else:  # tar
        if not _is_valid_tar_gz(snapshot_path):
            raise ValueError(f"snapshot is not a valid tar.gz: {snapshot_path}")
        # Ensure it contains a state.db member.
        with tarfile.open(snapshot_path, mode="r:gz") as tar:
            names = tar.getnames()
        if "state.db" not in names:
            raise ValueError(
                f"full backup is missing state.db at the archive root: {snapshot_path}"
            )

    safety_path = live_db.with_name(f"{live_db.name}.before-restore-{_timestamp()}")
    return RestorePlan(
        snapshot_path=snapshot_path,
        live_db_path=live_db,
        safety_path=safety_path,
        is_tar=(kind == "tar"),
    )


def execute_restore(plan: RestorePlan) -> RestoreResult:
    """Apply ``plan``: safety-snapshot the live DB, then replace it.

    Caller is responsible for having stopped the cockpit first. This
    function does NOT attempt to stop anything.
    """
    live_db = plan.live_db_path
    snapshot = plan.snapshot_path
    safety = plan.safety_path

    # 1. Safety snapshot of the live DB (if present). This runs BEFORE
    #    we touch anything, so an operator who aborts mid-restore
    #    still has the pre-restore state.
    if live_db.exists():
        safety.parent.mkdir(parents=True, exist_ok=True)
        # Use the online backup API when possible so we don't race
        # with any lingering writers. Fall back to copy2 if it isn't
        # a valid SQLite DB anymore (e.g. it's already truncated).
        try:
            _online_backup_to_plain_file(live_db, safety)
        except sqlite3.DatabaseError:
            shutil.copy2(live_db, safety)

    # 2. Materialize the snapshot into a plain file we can move into
    #    place.
    live_db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as staging:
        staged_db = Path(staging) / "restored.db"

        if plan.is_tar:
            with tarfile.open(snapshot, mode="r:gz") as tar:
                member = tar.getmember("state.db")
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise RuntimeError("tarfile could not open state.db entry")
                with staged_db.open("wb") as fout:
                    shutil.copyfileobj(extracted, fout)
        elif snapshot.suffix == ".gz":
            _gunzip_file(snapshot, staged_db)
        else:
            shutil.copy2(snapshot, staged_db)

        # 3. Atomic-ish replace. On POSIX ``os.replace`` is atomic on
        #    the same filesystem; the tempdir is in /tmp which may
        #    differ, so we copy into place instead.
        shutil.copy2(staged_db, live_db)

    # 4. Clean up WAL / SHM sidecars from the old DB — they belong to
    #    the previous transaction log and would confuse SQLite after
    #    we've swapped in a cold snapshot. Best-effort.
    for sidecar in (
        live_db.with_name(live_db.name + "-wal"),
        live_db.with_name(live_db.name + "-shm"),
    ):
        try:
            if sidecar.exists():
                sidecar.unlink()
        except OSError:
            pass

    return RestoreResult(
        snapshot_path=snapshot,
        live_db_path=live_db,
        safety_path=safety,
        is_tar=plan.is_tar,
    )


# --------------------------------------------------------------------- #
# Utility used by ``pm backup`` summary text
# --------------------------------------------------------------------- #


def humanize_bytes(n: int) -> str:
    """Return a compact human-readable size for CLI output."""
    if n < 1024:
        return f"{n} B"
    size = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024.0 or unit == "TB":
            return f"{size:.2f} {unit}"
    return f"{n} B"


__all__ = [
    "BackupResult",
    "RestorePlan",
    "RestoreResult",
    "DEFAULT_KEEP",
    "backup_state_db",
    "plan_restore",
    "execute_restore",
    "humanize_bytes",
]
