"""Backup + restore of the PollyPM state database.

This module is the implementation behind ``pm backup`` / ``pm restore``
and ``pm storage backup`` / ``pm storage restore``. The CLI layer in
:mod:`pollypm.cli` is kept thin — all of the IO, sanity checks, and
retention logic live here so they are testable without going through
Typer.

Design notes:

* The SQLite DB snapshot uses SQLite's online backup API
  (``sqlite3.Connection.backup``), NOT ``shutil.copy``. That is the
  only safe way to copy a live WAL-mode database while the heartbeat
  / cockpit may be writing to it.
* SQLite snapshots are gzipped on disk. The backup API needs a plain
  sqlite file to write into, so we back up to a temporary uncompressed
  file first and then gzip it.
* The Postgres branch (#1737, Slice G) shells out to ``pg_dump
  --format=custom`` and ``pg_restore --clean --if-exists --no-owner``.
  Custom format is already compressed; we do not gzip on top.
* ``--full`` tar.gz archives include the SQLite snapshot DB plus
  config / logs / snapshots / agent homes. They are not touched by
  retention — operators use them for point-in-time rescue, not routine
  cleanup.
* Restores always write a ``.before-restore`` sibling copy of the live
  DB before replacing it. This is the safety net; it's non-negotiable
  and the CLI layer cannot skip it. For the pg branch the safety copy
  is itself a ``pg_dump`` to ``<dsn>.before-restore-<ts>.pgdump``.
* Backend dispatch reads ``config.storage.backend``: ``"sqlite"``
  preserves the legacy path verbatim (the migration rollback safety
  net), ``"postgres"`` runs the pg_dump / pg_restore branch added in
  Slice G.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pollypm.storage.sqlite_pragmas import readonly_uri

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

# Retention only applies to plain DB snapshots (``state-db-*.db.gz``).
# ``--full`` tar.gz archives are left alone because they are larger and
# more precious — an operator who ran ``pm backup --full`` almost
# always did so intentionally before a risky change.
_DB_SNAPSHOT_PREFIX = "state-db-"
_DB_SNAPSHOT_SUFFIX = ".db.gz"
_FULL_SNAPSHOT_PREFIX = "full-"
_FULL_SNAPSHOT_SUFFIX = ".tar.gz"

# Postgres pg_dump custom-format snapshot naming. Filename shape mirrors
# the sqlite path so an operator scanning ``~/.pollypm/backups/`` can
# tell which backend a snapshot came from at a glance.
_PG_SNAPSHOT_PREFIX = "pg-"
_PG_SNAPSHOT_SUFFIX = ".pgdump"
_PG_AUDIT_INDEX_FILENAME = "index.jsonl"

# Default retention for plain DB snapshots; keep the last N. Applies to
# both the sqlite ``.db.gz`` path and the pg ``.pgdump`` path so an
# operator's mental model carries cleanly across the cutover.
DEFAULT_KEEP = 7
BACKUP_LOCK_RETRY_MAX_SECONDS = 3.0
BACKUP_LOCK_RETRY_INITIAL_SECONDS = 0.1


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


class BackupLockedError(RuntimeError):
    """Raised when the live SQLite DB stays locked across backup retries."""


class PgBackupError(RuntimeError):
    """Raised when ``pg_dump`` / ``pg_restore`` fails or is missing."""


@dataclass(slots=True)
class PgBackupResult:
    """Outcome of a successful ``pg_dump`` snapshot.

    Mirrors :class:`BackupResult` for the sqlite path but is its own
    type so the CLI layer can dispatch on backend without a ``full``
    flag in the way.
    """

    path: Path
    archive_size: int
    pruned: list[Path]
    dsn: str


@dataclass(slots=True)
class PgRestorePlan:
    """Description of what ``pg_restore`` would do against ``dsn``."""

    snapshot_path: Path
    dsn: str
    safety_path: Path


@dataclass(slots=True)
class PgRestoreResult:
    snapshot_path: Path
    dsn: str
    safety_path: Path | None


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
        # sqlite-ripout: sanctioned - migration/backup only
        conn = sqlite3.connect(readonly_uri(path), uri=True)
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


def _is_locked_sqlite_error(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower()


def _online_backup_to_plain_file(source_db: Path, dest: Path) -> None:
    """Use SQLite's online backup API to copy ``source_db`` -> ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    delay = BACKUP_LOCK_RETRY_INITIAL_SECONDS
    deadline = time.monotonic() + BACKUP_LOCK_RETRY_MAX_SECONDS
    while True:
        try:
            # sqlite-ripout: sanctioned - migration/backup only
            src = sqlite3.connect(readonly_uri(source_db), uri=True)
            try:
                # sqlite-ripout: sanctioned - migration/backup only
                dst = sqlite3.connect(dest)
                try:
                    src.backup(dst)
                    return
                finally:
                    dst.close()
            finally:
                src.close()
        except sqlite3.OperationalError as exc:
            if not _is_locked_sqlite_error(exc):
                raise
            if time.monotonic() >= deadline:
                raise BackupLockedError(
                    "state.db is locked by an active writer. Wait for the cockpit or heartbeat "
                    "to quiesce, then retry the backup."
                ) from exc
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def _gzip_file(src: Path, dest_gz: Path) -> None:
    dest_gz.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as fin, gzip.open(dest_gz, "wb") as fout:
        shutil.copyfileobj(fin, fout)


def _gunzip_file(src_gz: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(src_gz, "rb") as fin, dest.open("wb") as fout:
        shutil.copyfileobj(fin, fout)


def _sqlite_sidecars(db_path: Path) -> tuple[Path, Path]:
    return (
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    )


def _remove_sqlite_sidecars(db_path: Path) -> None:
    """Remove stale WAL/SHM sidecars before a restore swap."""
    for sidecar in _sqlite_sidecars(db_path):
        if not sidecar.exists():
            continue
        try:
            sidecar.unlink()
        except OSError as exc:
            raise RuntimeError(
                f"Could not remove SQLite sidecar {sidecar}: {exc}. Restore aborted."
            ) from exc


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _add_path_to_archive(
    tar: tarfile.TarFile,
    source: Path,
    *,
    arcname: str,
    allowed_root: Path,
) -> None:
    """Add ``source`` to ``tar`` while refusing symlinks and special files."""
    if source.is_symlink():
        raise ValueError(f"refusing to include symlink in full backup: {source}")

    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise ValueError(
            f"refusing to include unreadable path in full backup: {source}: {exc}"
        ) from exc

    if not _path_is_within(resolved, allowed_root):
        raise ValueError(
            f"refusing to include path outside archive root {allowed_root}: {source}"
        )

    if source.is_dir():
        tar.add(source, arcname=arcname, recursive=False)
        for child in sorted(source.iterdir()):
            _add_path_to_archive(
                tar,
                child,
                arcname=f"{arcname}/{child.name}",
                allowed_root=allowed_root,
            )
        return

    if source.is_file():
        tar.add(source, arcname=arcname, recursive=False)
        return

    raise ValueError(f"refusing to include non-file path in full backup: {source}")


def _archive_member_kind(member: tarfile.TarInfo) -> str:
    if member.isfile():
        return "regular file"
    if member.isdir():
        return "directory"
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hard link"
    if member.ischr():
        return "character device"
    if member.isblk():
        return "block device"
    if member.isfifo():
        return "fifo"
    return f"type {member.type!r}"


def _require_regular_state_db_member(
    tar: tarfile.TarFile, snapshot_path: Path
) -> tarfile.TarInfo:
    matches = [member for member in tar.getmembers() if member.name == "state.db"]
    if not matches:
        raise ValueError(
            f"full backup is missing state.db at the archive root: {snapshot_path}"
        )
    if len(matches) != 1:
        raise ValueError(
            f"full backup has duplicate state.db entries at the archive root: {snapshot_path}"
        )

    member = matches[0]
    if not member.isfile():
        kind = _archive_member_kind(member)
        raise ValueError(
            f"full backup state.db must be a regular file, found {kind}: {snapshot_path}"
        )
    return member


def _extract_valid_state_db_from_archive(snapshot_path: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(snapshot_path, mode="r:gz") as tar:
            member = _require_regular_state_db_member(tar, snapshot_path)
            extracted = tar.extractfile(member)
            if extracted is None:
                raise ValueError(
                    f"full backup could not open state.db payload: {snapshot_path}"
                )
            try:
                with dest.open("wb") as fout:
                    shutil.copyfileobj(extracted, fout)
            finally:
                extracted.close()
    except (tarfile.TarError, OSError, KeyError) as exc:
        raise ValueError(
            f"failed to read state.db from full backup: {snapshot_path}: {exc}"
        ) from exc

    if not _is_valid_sqlite_file(dest):
        raise ValueError(
            f"full backup state.db is not a valid SQLite database: {snapshot_path}"
        )


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
# Postgres branch — pg_dump / pg_restore (issue #1737, Slice G)
# --------------------------------------------------------------------- #


def _resolve_pg_binary(name: str) -> str:
    """Return an absolute path to ``pg_dump``/``pg_restore`` or raise.

    The pg client tools are usually on ``$PATH`` on a dev box but may
    live under ``/opt/homebrew/opt/postgresql@17/bin`` on macOS. We
    consult ``$PATH`` first (operators who put pg on PATH should keep
    that override) and fall back to ``pg_config --bindir`` so the
    Homebrew layout works without extra config.
    """
    found = shutil.which(name)
    if found:
        return found
    pg_config = shutil.which("pg_config")
    if pg_config:
        try:
            result = subprocess.run(
                [pg_config, "--bindir"],
                check=True,
                text=True,
                capture_output=True,
                timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            result = None
        if result is not None:
            bindir = Path(result.stdout.strip())
            candidate = bindir / name
            if candidate.exists():
                return str(candidate)
    raise PgBackupError(
        f"could not locate {name}. Install the Postgres client tools "
        "(e.g. `brew install postgresql@17`) or put pg_dump on PATH."
    )


def _record_pg_backup_index(
    backup_dir: Path,
    *,
    snapshot_path: Path,
    dsn: str,
    archive_size: int,
    duration_seconds: float,
) -> None:
    """Append an audit row to ``~/.pollypm/backups/index.jsonl``.

    The index gives operators a single ledger of every pg_dump that
    ran without having to ``stat`` every file in the directory. Append-
    only JSONL keeps the format trivial to grep and survives DB
    rebuilds — the same reasoning behind the audit JSONL the migration
    spec calls out as non-negotiable.

    Errors writing the index are logged via stderr but do NOT fail the
    backup — losing the audit row should not cost the operator a
    snapshot they just paid for.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": "pg_backup",
        "path": str(snapshot_path),
        "dsn": _redact_dsn(dsn),
        "archive_size": archive_size,
        "duration_seconds": round(duration_seconds, 3),
    }
    index_path = backup_dir / _PG_AUDIT_INDEX_FILENAME
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        with index_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        # Best-effort: the snapshot itself succeeded; losing the audit
        # row would be embarrassing but not catastrophic.
        return


def _redact_dsn(dsn: str) -> str:
    """Best-effort password redaction for log / audit output."""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(dsn)
        if not parts.password:
            return dsn
        userinfo, _, hostinfo = parts.netloc.rpartition("@")
        user = userinfo.partition(":")[0]
        new_netloc = f"{user}:***@{hostinfo}" if user else f":***@{hostinfo}"
        return urlunsplit(parts._replace(netloc=new_netloc))
    except Exception:  # noqa: BLE001 — never break a log line
        return "***"


def _list_pg_snapshots(backup_dir: Path) -> list[Path]:
    if not backup_dir.exists():
        return []
    out: list[Path] = []
    for child in backup_dir.iterdir():
        if not child.is_file():
            continue
        if child.name.startswith(_PG_SNAPSHOT_PREFIX) and child.name.endswith(
            _PG_SNAPSHOT_SUFFIX
        ):
            out.append(child)
    out.sort(key=lambda p: p.stat().st_mtime)
    return out


def _prune_pg_snapshots(backup_dir: Path, keep: int) -> list[Path]:
    if keep < 0:
        raise ValueError("keep must be >= 0")
    snapshots = _list_pg_snapshots(backup_dir)
    if len(snapshots) <= keep:
        return []
    to_delete = snapshots[: len(snapshots) - keep]
    deleted: list[Path] = []
    for path in to_delete:
        try:
            path.unlink()
            deleted.append(path)
        except OSError:
            continue
    return deleted


def _run_pg_dump(
    dsn: str,
    dest: Path,
    *,
    pg_dump_binary: str | None = None,
) -> None:
    """Run ``pg_dump --format=custom`` against ``dsn`` into ``dest``.

    Uses ``--no-owner`` and ``--no-privileges`` so the dump replays
    cleanly into a fresh database owned by a different role (matches
    the migration spec — operators can move PollyPM between machines
    without dragging the original superuser ACL along).
    """
    binary = pg_dump_binary or _resolve_pg_binary("pg_dump")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        binary,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        f"--dbname={dsn}",
        f"--file={dest}",
    ]
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        raise PgBackupError(f"pg_dump invocation failed: {exc}") from exc
    if completed.returncode != 0:
        # Clean up partial output so retention math doesn't trip later.
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        raise PgBackupError(
            f"pg_dump exited with code {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )


def _run_pg_restore(
    dsn: str,
    source: Path,
    *,
    pg_restore_binary: str | None = None,
) -> None:
    """Run ``pg_restore --clean --if-exists --no-owner`` against ``dsn``.

    ``--clean --if-exists`` drops + recreates objects so the restore is
    idempotent against an already-populated database. ``--no-owner``
    matches the dump-side flag so the restore doesn't try to chown
    objects to the original role.
    """
    binary = pg_restore_binary or _resolve_pg_binary("pg_restore")
    if not source.exists():
        raise FileNotFoundError(f"snapshot not found: {source}")
    cmd = [
        binary,
        "--clean",
        "--if-exists",
        "--no-owner",
        f"--dbname={dsn}",
        str(source),
    ]
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        raise PgBackupError(f"pg_restore invocation failed: {exc}") from exc
    # ``pg_restore`` exits non-zero even on benign "table did not exist
    # to drop" warnings. The ``--exit-on-error`` flag would help but
    # also rejects warnings we explicitly tolerate. We require returncode
    # 0 OR 1 (1 = warnings only) and surface anything higher.
    if completed.returncode not in (0, 1):
        raise PgBackupError(
            f"pg_restore exited with code {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )


def _resolve_pg_dsn(config: "PollyPMConfig | None") -> str:
    """Return the pg DSN to use for backup / restore.

    Delegates to :mod:`pollypm.storage.pg_pool` so the priority order
    (env override → ``[storage] url`` → default) stays in one place.
    """
    from pollypm.storage.pg_pool import resolve_dsn

    return resolve_dsn(config)


def backup_pg_dsn(
    *,
    base_dir: Path,
    config: "PollyPMConfig | None" = None,
    output: Path | None = None,
    keep: int = DEFAULT_KEEP,
    pg_dump_binary: str | None = None,
) -> PgBackupResult:
    """Snapshot the configured pg DSN to ``backup_dir`` via ``pg_dump``.

    Parameters
    ----------
    base_dir:
        ``config.project.base_dir`` — used to locate the default
        ``backups/`` directory.
    config:
        Optional :class:`PollyPMConfig` so DSN resolution honours the
        ``[storage] url`` knob. Pass ``None`` to fall back to the env
        override + built-in default (matches the doctor probe).
    output:
        Optional custom destination path. Directories are created.
    keep:
        Retention count for pg snapshots.
    pg_dump_binary:
        Override the ``pg_dump`` binary path. Test seam only.
    """
    backup_dir = _default_backup_dir(base_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = _timestamp()
    if output is not None:
        snapshot_path = output
    else:
        snapshot_path = backup_dir / f"{_PG_SNAPSHOT_PREFIX}{timestamp}{_PG_SNAPSHOT_SUFFIX}"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)

    dsn = _resolve_pg_dsn(config)
    start = time.monotonic()
    _run_pg_dump(dsn, snapshot_path, pg_dump_binary=pg_dump_binary)
    duration = time.monotonic() - start

    archive_size = _size_or_zero(snapshot_path)
    _record_pg_backup_index(
        backup_dir,
        snapshot_path=snapshot_path,
        dsn=dsn,
        archive_size=archive_size,
        duration_seconds=duration,
    )

    pruned: list[Path] = []
    if output is None:
        pruned = _prune_pg_snapshots(backup_dir, keep)

    return PgBackupResult(
        path=snapshot_path,
        archive_size=archive_size,
        pruned=pruned,
        dsn=dsn,
    )


def plan_pg_restore(
    snapshot_path: Path,
    *,
    config: "PollyPMConfig | None" = None,
) -> PgRestorePlan:
    """Validate ``snapshot_path`` and describe the planned restore.

    Raises ``FileNotFoundError`` / ``ValueError`` on invalid input.
    Does NOT touch the database.
    """
    if not snapshot_path.exists():
        raise FileNotFoundError(f"snapshot not found: {snapshot_path}")
    # The custom format starts with the 5-byte magic "PGDMP". We don't
    # parse the header further — pg_restore itself is the canonical
    # validator.
    try:
        with snapshot_path.open("rb") as fh:
            magic = fh.read(5)
    except OSError as exc:
        raise ValueError(f"cannot read snapshot at {snapshot_path}: {exc}") from exc
    if magic != b"PGDMP":
        raise ValueError(
            f"snapshot is not a pg_dump custom-format archive: {snapshot_path}"
        )
    dsn = _resolve_pg_dsn(config)
    safety_path = snapshot_path.with_name(
        f"{snapshot_path.stem}.before-restore-{_timestamp()}{_PG_SNAPSHOT_SUFFIX}"
    )
    return PgRestorePlan(snapshot_path=snapshot_path, dsn=dsn, safety_path=safety_path)


def execute_pg_restore(
    plan: PgRestorePlan,
    *,
    pg_dump_binary: str | None = None,
    pg_restore_binary: str | None = None,
) -> PgRestoreResult:
    """Apply ``plan``: safety-snapshot the live pg, then run pg_restore.

    The safety snapshot is best-effort — if the live pg is unreachable
    (e.g. fresh install, never written to) we proceed without one so
    operators can still bootstrap from a snapshot. That mirrors how the
    sqlite branch tolerates a missing live DB.
    """
    safety_written: Path | None = None
    try:
        _run_pg_dump(
            plan.dsn, plan.safety_path, pg_dump_binary=pg_dump_binary
        )
        safety_written = plan.safety_path
    except PgBackupError:
        # Best-effort safety dump. The restore is still useful — we
        # surface the missing safety net to the operator via the
        # returned result so the CLI can warn.
        safety_written = None

    _run_pg_restore(
        plan.dsn, plan.snapshot_path, pg_restore_binary=pg_restore_binary
    )

    return PgRestoreResult(
        snapshot_path=plan.snapshot_path,
        dsn=plan.dsn,
        safety_path=safety_written,
    )


def latest_pg_backup_age_seconds(base_dir: Path) -> float | None:
    """Return the age in seconds of the most recent pg snapshot, or None.

    Used by ``pm doctor`` (#1737, Slice G) to surface "last backup is
    > N days old" as an actionable warning. Returns ``None`` when no
    snapshot exists — the doctor renders that as a separate failure
    mode ("no backups exist yet").
    """
    backup_dir = _default_backup_dir(base_dir)
    snapshots = _list_pg_snapshots(backup_dir)
    if not snapshots:
        return None
    newest = snapshots[-1]
    try:
        mtime = newest.stat().st_mtime
    except OSError:
        return None
    return max(0.0, time.time() - mtime)


# --------------------------------------------------------------------- #
# Public API — backup
# --------------------------------------------------------------------- #


def backup_via_config(
    config: "PollyPMConfig",
    *,
    output: Path | None = None,
    full: bool = False,
    keep: int = DEFAULT_KEEP,
    extra_roots: list[Path] | None = None,
) -> BackupResult | PgBackupResult:
    """Dispatch to the sqlite or pg backup path based on ``config``.

    Single entry point for the CLI layer so the dispatch lives in one
    place and the tests can drive it without touching Typer. The
    sqlite branch preserves :func:`backup_state_db` verbatim (still
    the rollback safety net during the transition); the pg branch
    delegates to :func:`backup_pg_dsn`.

    ``full`` is sqlite-only — running ``--full`` against a pg backend
    surfaces a clear error rather than silently dropping the flag.
    """
    backend = config.storage.backend
    if backend == "postgres":
        if full:
            raise PgBackupError(
                "--full archives are not supported on the postgres "
                "backend. Run `pm storage backup` without --full, then "
                "use your filesystem snapshot tool for ~/.pollypm/."
            )
        return backup_pg_dsn(
            base_dir=config.project.base_dir,
            config=config,
            output=output,
            keep=keep,
        )
    return backup_state_db(
        config.project.state_db,
        base_dir=config.project.base_dir,
        output=output,
        full=full,
        keep=keep,
        extra_roots=extra_roots,
    )


def restore_via_config(
    config: "PollyPMConfig",
    snapshot_path: Path,
) -> tuple[RestorePlan | PgRestorePlan, str]:
    """Build a restore plan dispatched on ``config.storage.backend``.

    Returns ``(plan, backend)`` so the CLI can render the appropriate
    confirmation banner before calling :func:`execute_restore` /
    :func:`execute_pg_restore`. Raises the same ``FileNotFoundError``
    / ``ValueError`` shapes the underlying planners do.
    """
    backend = config.storage.backend
    if backend == "postgres":
        plan = plan_pg_restore(snapshot_path, config=config)
        return plan, "postgres"
    plan = plan_restore(snapshot_path, config.project.state_db)
    return plan, "sqlite"


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
            base_root = base_dir.resolve()

            try:
                with tarfile.open(archive_path, mode="w:gz") as tar:
                    tar.add(staging_db, arcname="state.db", recursive=False)
                    # Bundle the base_dir tree — but skip the ``backups/``
                    # subdir so archives don't grow recursively each run.
                    if base_dir.exists():
                        for child in sorted(base_dir.iterdir()):
                            if child == backup_dir:
                                continue
                            if child == state_db:
                                # Already captured as the online backup
                                continue
                            _add_path_to_archive(
                                tar,
                                child,
                                arcname=f"base/{child.name}",
                                allowed_root=base_root,
                            )
                    for extra in extra_roots or []:
                        if not extra.exists():
                            continue
                        _add_path_to_archive(
                            tar,
                            extra,
                            arcname=f"extra/{extra.name}",
                            allowed_root=extra.resolve(),
                        )
            except Exception:
                try:
                    if archive_path.exists():
                        archive_path.unlink()
                except OSError:
                    pass
                raise

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
        with tempfile.TemporaryDirectory() as staging:
            extracted = Path(staging) / "probe.db"
            _extract_valid_state_db_from_archive(snapshot_path, extracted)

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
    with tempfile.TemporaryDirectory(dir=live_db.parent, prefix=".restore-") as staging:
        staged_db = Path(staging) / "restored.db"

        if plan.is_tar:
            _extract_valid_state_db_from_archive(snapshot, staged_db)
        elif snapshot.suffix == ".gz":
            _gunzip_file(snapshot, staged_db)
        else:
            shutil.copy2(snapshot, staged_db)

        # 3. Remove stale WAL/SHM sidecars from the old DB before we
        #    atomically swap in the restored snapshot. If cleanup fails,
        #    abort loudly instead of risking a mixed restore + stale WAL.
        _remove_sqlite_sidecars(live_db)

        # 4. Atomic replace on the same filesystem.
        os.replace(staged_db, live_db)

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
    "BackupLockedError",
    "BackupResult",
    "RestorePlan",
    "RestoreResult",
    "PgBackupError",
    "PgBackupResult",
    "PgRestorePlan",
    "PgRestoreResult",
    "DEFAULT_KEEP",
    "backup_state_db",
    "plan_restore",
    "execute_restore",
    "backup_pg_dsn",
    "plan_pg_restore",
    "execute_pg_restore",
    "backup_via_config",
    "restore_via_config",
    "latest_pg_backup_age_seconds",
    "humanize_bytes",
]
