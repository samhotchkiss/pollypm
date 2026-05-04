"""Runtime service loader shared by cadence/plugin entry points."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _RuntimeServices:
    """Container for session, store, work-service, and config handles."""

    session_service: Any | None
    state_store: Any | None
    work_service: Any | None
    project_root: Path
    msg_store: Any | None = None
    config: Any | None = None
    storage_closet_name: str = "pollypm-storage-closet"
    known_projects: tuple[Any, ...] = field(default_factory=tuple)
    enforce_plan: bool = True
    plan_dir: str = "docs/plan"
    auto_claim: bool = True
    max_concurrent_per_project: int = 2
    _owns_connections: bool = False

    def close(self) -> None:
        """Release every owned sqlite connection."""
        if not self._owns_connections:
            return
        store = self.state_store
        if store is not None:
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    logger.debug("runtime_services: state_store close raised", exc_info=True)
        work = self.work_service
        if work is not None:
            close = getattr(work, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    logger.debug("runtime_services: work_service close raised", exc_info=True)


def load_runtime_services(
    *,
    config_path: Path | None = None,
) -> _RuntimeServices:
    """Resolve session service, state store, work service, and config."""
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path

    resolved_path = config_path or resolve_config_path(DEFAULT_CONFIG_PATH)
    if not resolved_path or not resolved_path.exists():
        return _RuntimeServices(
            session_service=None,
            state_store=None,
            work_service=None,
            project_root=Path.cwd(),
            known_projects=(),
            msg_store=None,
        )
    config = load_config(resolved_path)

    from pollypm.storage.state import StateStore

    store = StateStore(config.project.state_db)

    msg_store: Any | None
    try:
        from pollypm.store.registry import get_store

        msg_store = get_store(config)
    except Exception:  # noqa: BLE001
        logger.debug("runtime_services: unified Store unavailable", exc_info=True)
        msg_store = None

    session_service: Any | None
    try:
        from pollypm.session_services.tmux import TmuxSessionService

        session_service = TmuxSessionService(config=config, store=store)
    except Exception:  # noqa: BLE001
        logger.debug("runtime_services: session service unavailable", exc_info=True)
        session_service = None

    project_root = Path(
        getattr(config.project, "workspace_root", None) or config.project.root_dir
    )
    work_service: Any | None
    try:
        from pollypm.work.sqlite_service import SQLiteWorkService

        db_path = project_root / ".pollypm" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        work_service = SQLiteWorkService(db_path=db_path, project_path=project_root)
    except Exception:  # noqa: BLE001
        logger.debug("runtime_services: work service unavailable", exc_info=True)
        work_service = None

    known_projects = tuple(config.projects.values())

    return _RuntimeServices(
        session_service=session_service,
        state_store=store,
        work_service=work_service,
        project_root=project_root,
        config=config,
        storage_closet_name=f"{config.project.tmux_session}-storage-closet",
        known_projects=known_projects,
        enforce_plan=config.planner.enforce_plan,
        plan_dir=config.planner.plan_dir,
        msg_store=msg_store,
        auto_claim=config.planner.auto_claim,
        max_concurrent_per_project=config.planner.max_concurrent_per_project,
        _owns_connections=True,
    )


__all__ = ["_RuntimeServices", "load_runtime_services"]
