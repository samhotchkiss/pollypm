"""Resolve the work-service SQLite database path for a project.

Public, non-CLI module so callers outside the Typer CLI (notably
``supervisor_alerts``) can find a project's work DB without importing
``pollypm.work.cli`` and reaching into ``_resolve_db_path`` (#804).

Layout (post-#339, finalized in #1004):

* ``<workspace_root>/.pollypm/state.db`` is the canonical work DB.
  ``pm task list / get / next / queue / claim / done``, ``pm notify``,
  ``pm inbox``, the cockpit, and the supervisor all read and write
  here. Project isolation is row-level via the ``work_tasks.project``
  column.
* ``<project>/.pollypm/state.db`` is **deprecated**. The post-#339
  audit (#1004) found that an empty per-project DB created by ad-hoc
  scaffolding code would short-circuit reads here while writers
  silently kept landing in the workspace DB — `pm task list` and
  `pm task get` would disagree about the same task. The resolver no
  longer routes to the per-project file. Existing per-project DBs
  are migrated by ``pollypm.storage.legacy_per_project_db.migrate_legacy_per_project_dbs``.

The function never raises — config-load failures fall through to the
cwd-relative default. Callers that genuinely care about a missing DB
should check ``Path.exists()`` on the returned path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pollypm.config import PollyPMConfig

WORKSPACE_DEFAULT_DB_PATH = ".pollypm/state.db"

logger = logging.getLogger(__name__)


def resolve_work_db_path(
    db: str = WORKSPACE_DEFAULT_DB_PATH,
    project: str | None = None,
    *,
    config: "PollyPMConfig | None" = None,
) -> Path:
    """Resolve the work-service DB path for ``db`` / optional ``project``.

    Behaviour (collapsed layout, #1004):

    1. If ``db`` is anything other than the canonical default
       (``.pollypm/state.db``), it's an explicit override — return it
       verbatim. Tests and CI rely on this escape hatch.
    2. Otherwise return ``<workspace_root>/.pollypm/state.db`` from the
       loaded config, regardless of whether ``project`` is set.
       Project isolation is row-level via ``work_tasks.project``.
    3. As a last resort (no config), return the cwd-relative default.

    The ``project`` argument is accepted for API compatibility — the
    canonical reader is the same workspace-scope DB regardless. A
    pre-#1004 build of this resolver would route to a per-project
    state.db file when one existed; that branch caused the reader/
    writer split that #1004 fixes. Per-project DB files left over
    from previous installs are migrated by
    ``migrate_legacy_per_project_dbs`` and then deprecated.

    ``config`` (#928): callers that already hold a bound ``PollyPMConfig``
    (notably the supervisor) can pass it in to avoid a hidden
    ``load_config()`` call. This keeps test-isolated configs from
    leaking the developer's real workspace DB into nudge resolution.
    """
    is_default = db == WORKSPACE_DEFAULT_DB_PATH

    # Explicit override wins.
    if not is_default:
        db_path = Path(db)
        if db_path.exists():
            return db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return db_path

    def _load() -> "PollyPMConfig | None":
        if config is not None:
            return config
        try:
            from pollypm.config import load_config

            return load_config()
        except Exception:  # noqa: BLE001
            return None

    resolved_config = _load()

    # #1004: detect (but do not route to) a stale per-project DB so we
    # can warn loudly. The legacy resolver short-circuited to this file
    # whenever it existed on disk, even when empty — producing the
    # reader/writer split symptom on #1003 and the ``pm task next``/
    # ``pm task get`` mismatch logged on the day before. Reads now
    # always go to workspace; the warning lets the operator (or the
    # one-shot migration helper) clean up.
    if project and resolved_config is not None:
        try:
            known = getattr(resolved_config, "projects", {}) or {}
            project_cfg = known.get(project)
            if project_cfg is not None:
                project_db = Path(project_cfg.path) / ".pollypm" / "state.db"
                if project_db.exists():
                    logger.debug(
                        "db_resolver: ignoring legacy per-project DB at %s "
                        "(post-#1004 layout collapses to workspace-root). "
                        "Run pollypm.storage.legacy_per_project_db."
                        "migrate_legacy_per_project_dbs to import any "
                        "leftover rows and archive the file.",
                        project_db,
                    )
        except Exception:  # noqa: BLE001
            pass

    # Workspace-root default — every ``pm task`` / ``pm notify`` /
    # ``pm inbox`` call without an explicit ``--db`` lands here so
    # items stay visible regardless of cwd.
    if resolved_config is not None:
        try:
            workspace_root = getattr(resolved_config.project, "workspace_root", None)
            if workspace_root is not None:
                candidate = Path(workspace_root) / ".pollypm" / "state.db"
                candidate.parent.mkdir(parents=True, exist_ok=True)
                return candidate
        except Exception:  # noqa: BLE001
            pass

    # Fallback: cwd-relative default when no config is loadable.
    db_path = Path(db)
    if db_path.exists():
        return db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


__all__ = ["WORKSPACE_DEFAULT_DB_PATH", "resolve_work_db_path"]
