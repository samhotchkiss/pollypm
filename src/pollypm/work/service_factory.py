"""Small work-service construction helpers for non-CLI callers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def attach_session_manager(
    svc: Any,
    *,
    project_path: Path,
    config: Any = None,
) -> None:
    """Wire a :class:`SessionManager` onto an already-constructed work service.

    Mirrors the per-task worker lifecycle wiring that ``pm`` CLI calls
    do via :func:`pollypm.work.cli._svc`. Without this attachment a
    ``svc.claim(...)`` call marks the row ``in_progress`` and sets
    ``assignee`` but never provisions the worker session, never
    applies the parallel-cap check, and never surfaces
    ``last_provision_error`` — the API ``POST /tasks/{p}/{n}/claim``
    handler initially shipped without this wiring (#2064 round-9
    blocker #2) and silently created claimed tasks with no worker
    lane.

    Best-effort: SessionManager wiring is optional. We refuse to
    raise from this helper so a missing tmux client / configless
    environment doesn't break the claim path entirely; the work
    service still functions as a DB-only writer in that case.
    Requires a real git checkout under ``project_path`` (otherwise
    there is no worktree for a per-task session to inhabit).
    """
    try:
        from pollypm.session_services import create_tmux_client
        from pollypm.work.session_manager import SessionManager
    except Exception:  # noqa: BLE001
        logger.debug(
            "attach_session_manager: tmux/session imports failed",
            exc_info=True,
        )
        return
    try:
        if not (project_path.exists() and (project_path / ".git").exists()):
            return
    except OSError:
        return

    session_service = None
    storage_closet_name = "pollypm-storage-closet"
    try:
        from pollypm.session_services.tmux import TmuxSessionService
        from pollypm.storage.state import StateStore

        if config is None:
            from pollypm.config import load_config

            config = load_config()
        storage_closet_name = (
            f"{config.project.tmux_session}-storage-closet"
        )
        store = StateStore(config.project.state_db)
        session_service = TmuxSessionService(config=config, store=store)
    except Exception:  # noqa: BLE001
        logger.debug(
            "attach_session_manager: SessionService construction failed",
            exc_info=True,
        )
    try:
        session_mgr = SessionManager(
            tmux_client=create_tmux_client(),
            work_service=svc,
            project_path=project_path,
            config=config,
            session_service=session_service,
            storage_closet_name=storage_closet_name,
        )
        svc.set_session_manager(session_mgr)
    except Exception:  # noqa: BLE001
        logger.debug(
            "attach_session_manager: SessionManager wire-up failed",
            exc_info=True,
        )


def create_work_service_with_session(
    *,
    config: Any,
    project_key: str,
    project_path: Path,
    sync_manager: Any = None,
) -> Any:
    """Construct a work service AND wire a SessionManager onto it.

    Single source of truth for "give me a work service that behaves
    the way ``pm task claim`` expects". Both the CLI (``_svc()`` in
    ``pollypm.work.cli``) and the Web API claim endpoint
    (``POST /tasks/{p}/{n}/claim``) call through this helper so they
    share the same per-task worker lifecycle wiring (#2064 round-9
    blocker #2). Previously the API helper called
    ``create_work_service(...)`` directly and skipped session
    provisioning, which meant API-claimed tasks could end up
    ``in_progress`` with no worker lane and no
    ``last_provision_error`` surface.

    ``sync_manager`` is plumbed through for callers that want the
    pre-built file-sync adapter; pg ignores it but the legacy
    keyword keeps source-compat with existing call sites.
    """
    from pollypm.work.factory import create_work_service

    svc = create_work_service(
        config=config,
        project_path=project_path,
        project_key=project_key,
        sync_manager=sync_manager,
    )
    attach_session_manager(svc, project_path=project_path, config=config)
    return svc


def open_project_work_service(project: Any, *, config: Any = None) -> Any | None:
    """Open a per-project work service, returning None on failure.

    Routes through :func:`pollypm.work.factory.create_work_service` so the
    operator's ``[storage] backend`` toml setting is honoured (#1369,
    #1737). ``db_path`` is sqlite-specific and ignored on the pg backend.
    ``config`` is optional; when omitted it is loaded on demand so the
    pg dispatch path still fires for callers that haven't been threaded
    yet (the canonical pattern is to pass the already-loaded config in).
    """
    project_path = getattr(project, "path", None)
    if project_path is None:
        return None
    # Typed helper routes through the doubled-pollypm-path guard (#1972).
    from pollypm.projects import project_state_db_path

    db_path = project_state_db_path(Path(project_path))
    try:
        if not db_path.exists():
            return None
    except OSError:
        return None
    if config is None:
        try:
            from pollypm.config import load_config

            config = load_config()
        except Exception:  # noqa: BLE001
            config = None
    try:
        from pollypm.work.factory import create_work_service

        return create_work_service(
            config=config,
            db_path=db_path,
            project_path=Path(project_path),
            project_key=getattr(project, "key", None),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "work.service_factory: open work service failed for %s",
            getattr(project, "key", "?"),
            exc_info=True,
        )
        return None


__all__ = [
    "attach_session_manager",
    "create_work_service_with_session",
    "open_project_work_service",
]
