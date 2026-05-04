"""Compatibility wrapper for legacy per-project DB migration."""

from __future__ import annotations

from pollypm.storage.legacy_per_project_db import (
    LEGACY_DB_SUFFIX,
    PerProjectMigrationReport,
    migrate_legacy_per_project_dbs,
    migrate_one,
)

__all__ = [
    "LEGACY_DB_SUFFIX",
    "PerProjectMigrationReport",
    "migrate_legacy_per_project_dbs",
    "migrate_one",
]
