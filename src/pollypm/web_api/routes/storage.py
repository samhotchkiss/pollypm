"""``GET /api/v1/storage`` endpoints — Phase 2 storage report.

Mirrors the JSON shape that ``pm storage report --json`` (PR #2040)
emits, without re-deriving the scan. The CLI's
:func:`pollypm.cli_features.storage.scan_pollypm_home` is the single
source of truth: it knows which subdirs of ``~/.pollypm/`` to walk,
which to cap (``snapshots/`` is unbounded), and how to annotate the
NOTES column (cap-hit, orphan worktrees, audit rotation).

This router is a thin adapter:

* ``GET /api/v1/storage`` → whole-home report (every subdir + config
  files + totals).
* ``GET /api/v1/storage/{subdir}`` → single-subdir slice. Returns the
  same ``StorageEntry`` shape an entry in ``subdirs[]`` carries on the
  top-level report; ``404 not_found`` when the subdir name isn't in
  the canonical list. We do NOT walk arbitrary user-supplied paths —
  that's a future ``/storage/breakdown`` design (spec §12.1).

Per the Phase 2 spec §12.2, the API never deletes. ``pm storage
prune`` is CLI-only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from pollypm.cli_features.storage import (
    _HOME_SUBDIRS,
    DirScan,
    HomeReport,
    scan_pollypm_home,
)
from pollypm.web_api.errors import not_found, service_unavailable
from pollypm.web_api.models import (
    StorageConfigFiles,
    StorageEntry,
    StorageReport,
)
from pollypm.web_api.routes._deps import ConfigDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Storage"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(ts: float | None) -> datetime | None:
    """Convert epoch seconds to a tz-aware ``datetime`` (``None`` on falsy)."""
    if ts is None or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _entry_from_scan(row: DirScan) -> StorageEntry:
    """Project a ``DirScan`` into the wire :class:`StorageEntry`."""
    return StorageEntry(
        name=row.name,
        files=row.files,
        bytes=row.bytes,
        oldest_mtime=_iso(row.oldest_mtime),
        newest_mtime=_iso(row.newest_mtime),
        cap_hit=row.cap_hit,
        note=row.note or "",
    )


def _resolve_home(config: Any) -> Path:
    """Pick the ``~/.pollypm/`` directory to scan.

    The CLI's :func:`scan_pollypm_home` accepts ``None`` and defaults
    to ``~/.pollypm``. The Web API mirrors that default but lets the
    operator's configured ``base_dir`` override it when present —
    tests + non-default installs end up scanning the right tree
    without the route having to know about XDG/etc.
    """
    base_dir = getattr(getattr(config, "project", None), "base_dir", None)
    if isinstance(base_dir, Path) and base_dir:
        return base_dir
    return Path.home() / ".pollypm"


def _report_to_wire(report: HomeReport) -> StorageReport:
    """Project the dataclass ``HomeReport`` into the Pydantic wire shape."""
    return StorageReport(
        home=str(report.home),
        generated_at=datetime.now(tz=timezone.utc),
        total_files=report.total_files,
        total_bytes=report.total_bytes,
        subdirs=[_entry_from_scan(row) for row in report.rows],
        config_files=StorageConfigFiles(
            files=report.config_files,
            bytes=report.config_bytes,
            newest_mtime=_iso(report.config_newest_mtime),
        ),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/storage",
    response_model=StorageReport,
    summary="~/.pollypm/ disk-usage report",
    operation_id="getStorageReport",
)
def get_storage_report(config: ConfigDep) -> StorageReport:
    """Return the whole-home storage report (mirrors ``pm storage report --json``).

    Edge cases:

    - **Missing home dir.** ``scan_pollypm_home`` returns an empty
      :class:`HomeReport` (no rows, zero totals) rather than raising —
      we surface that as ``200`` with an empty payload. Phase 2 spec §0
      explicitly: a non-existent home is a normal operating mode
      (fresh install, alternate ``base_dir``), not ``500``.
    - **Scan failure.** Unexpected ``OSError`` walking the tree is
      already swallowed per-entry inside ``scan_pollypm_home``; any
      programming-error exception bubbles up to the global 500 handler
      via :class:`pollypm.web_api.errors.APIError` machinery.
    """
    home = _resolve_home(config)
    try:
        report = scan_pollypm_home(home)
    except OSError as exc:
        # Defensive — scan_pollypm_home already swallows per-entry
        # OSErrors. A top-level OSError here means the home path
        # itself became unreachable mid-request (volume unmounted,
        # permission flipped). Surface as 503 rather than 500 so the
        # client retries.
        raise service_unavailable(
            f"could not scan storage tree at {home}: {exc}",
            hint="Check filesystem permissions on ~/.pollypm/ and retry.",
        ) from exc
    return _report_to_wire(report)


@router.get(
    "/storage/{subdir}",
    response_model=StorageEntry,
    summary="Single ~/.pollypm/ subdir slice",
    operation_id="getStorageSubdir",
)
def get_storage_subdir(subdir: str, config: ConfigDep) -> StorageEntry:
    """Return one row of the storage report by subdir name.

    Restricted to the canonical subdir list defined in
    ``cli_features/storage.py::_HOME_SUBDIRS``. Arbitrary user paths
    are intentionally not accepted — the report is for visibility,
    not arbitrary tree walks (spec §12.3 keeps that under a future
    ``/storage/breakdown?path=…`` design that's out of scope here).
    """
    if subdir not in _HOME_SUBDIRS:
        raise not_found(
            f"Unknown storage subdir: {subdir!r}",
            hint=(
                "Valid subdirs: " + ", ".join(_HOME_SUBDIRS) + ". "
                "Use GET /api/v1/storage for the whole-home report."
            ),
        )
    home = _resolve_home(config)
    try:
        report = scan_pollypm_home(home)
    except OSError as exc:
        raise service_unavailable(
            f"could not scan storage tree at {home}: {exc}",
            hint="Check filesystem permissions on ~/.pollypm/ and retry.",
        ) from exc
    for row in report.rows:
        if row.name == subdir:
            return _entry_from_scan(row)
    # ``scan_pollypm_home`` always emits a row per ``_HOME_SUBDIRS``
    # entry (empty DirScan when the dir doesn't exist on disk), so
    # this branch is only reached if the canonical list and the scan
    # diverge — that's a programming error worth surfacing.
    raise not_found(
        f"subdir {subdir!r} not present in scan output",
        hint="This indicates a bug in scan_pollypm_home; file an issue.",
    )


__all__ = ["router"]
