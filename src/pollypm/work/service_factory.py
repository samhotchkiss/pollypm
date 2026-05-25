"""Small work-service construction helpers for non-CLI callers."""

from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def attach_session_manager(
    svc: Any,
    *,
    project_path: Path,
    config: Any = None,
) -> Any | None:
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

    Wire-up failures are still captured on
    ``svc._session_attach_error`` (#2064 round-10) so the API claim
    path can surface them as a ``TaskActionResult.warnings`` entry —
    debug-only logging was indistinguishable from a healthy
    DB-only-by-design claim. A non-git ``project_path`` is treated
    as a clean no-op (the API may legitimately serve non-git
    projects) and does NOT populate the error attribute.
    """
    try:
        from pollypm.session_services import create_tmux_client
        from pollypm.work.session_manager import SessionManager
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "attach_session_manager: tmux/session imports failed",
            exc_info=True,
        )
        _record_attach_error(
            svc, f"SessionManager imports failed: {exc}"
        )
        return None
    try:
        if not (project_path.exists() and (project_path / ".git").exists()):
            return None
    except OSError:
        return None

    # #2064 round-11 blocker #4: stamping ``_session_attach_error``
    # on SessionService construction failure was a false positive
    # whenever the subsequent SessionManager wire-up still succeeded
    # (SessionManager accepts ``session_service=None`` and falls
    # back to a degraded raw-tmux mode). Track the SessionService
    # failure locally and only stamp when the FINAL outcome is "no
    # SessionManager attached" — that is the condition the warning
    # actually describes ("no per-task tmux lane was provisioned"
    # at ``web_api/service.py``). A successful fallback attach
    # silently records nothing, matching the pre-round-10 behaviour
    # the operator saw before we added the surfacing.
    session_service = None
    session_service_error: str | None = None
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
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "attach_session_manager: SessionService construction failed; "
            "will attempt SessionManager fallback without a session service",
            exc_info=True,
        )
        session_service_error = f"SessionService construction failed: {exc}"
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
        # Success — even if SessionService construction failed, we
        # have a working SessionManager (the fallback raw-tmux
        # path). DO NOT stamp ``_session_attach_error``; the
        # warning at ``_collect_claim_warnings`` would otherwise
        # falsely claim "no per-task tmux lane was provisioned".
        return session_mgr
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "attach_session_manager: SessionManager wire-up failed",
            exc_info=True,
        )
        if session_service_error is not None:
            # Surface both failures so the operator sees the full
            # chain; the SessionService failure is the root cause.
            _record_attach_error(
                svc,
                f"SessionManager wire-up failed: {exc} "
                f"(after {session_service_error})",
            )
        else:
            _record_attach_error(
                svc, f"SessionManager wire-up failed: {exc}"
            )
        return None


def _record_attach_error(svc: Any, message: str) -> None:
    """Stamp ``svc._session_attach_error`` without clobbering prior ones.

    The first failure wins — later failures are typically downstream
    consequences (e.g. SessionManager wire-up after a swallowed
    SessionService construction failure). Keeping the first message
    gives the operator the root cause.
    """
    if getattr(svc, "_session_attach_error", None):
        return
    try:
        svc._session_attach_error = message
    except Exception:  # noqa: BLE001
        # If the service rejects attribute writes (unlikely — both
        # PgWorkService and the test fakes accept it) we have no
        # recovery, but we also must not crash the claim path.
        logger.debug(
            "attach_session_manager: could not stamp attach error on svc",
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

    Defensively wraps the :func:`attach_session_manager` call
    (#2064 round-10) so any unexpected exception escaping the
    best-effort helper still yields a usable ``svc`` — the work
    service falls back to a DB-only writer and the failure lands
    on ``svc._session_attach_error`` for the API claim path to
    surface as a warning.
    """
    from pollypm.work.factory import create_work_service

    svc = create_work_service(
        config=config,
        project_path=project_path,
        project_key=project_key,
        sync_manager=sync_manager,
    )
    try:
        attach_session_manager(
            svc, project_path=project_path, config=config
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "create_work_service_with_session: attach_session_manager "
            "raised unexpectedly",
            exc_info=True,
        )
        _record_attach_error(svc, f"SessionManager attach failed: {exc}")
    return svc


class _DeferredProvisionSessionManager:
    """SessionManager facade that defers slow worker provisioning.

    ``PgWorkService.claim`` uses the attached manager for two things:
    a cheap pre-claim cap probe and the expensive post-commit
    ``provision_worker`` side effect. The Web API needs the first part
    before returning but must not make the HTTP response wait for git
    worktree creation and tmux/provider launch. This facade forwards
    the cap probe to the real manager and schedules provisioning on
    the caller-owned executor.
    """

    def __init__(
        self,
        real_manager: Any,
        *,
        config: Any,
        project_key: str,
        project_path: Path,
        schedule: Callable[[Callable[[], None]], object],
    ) -> None:
        self._real_manager = real_manager
        self._config = config
        self._project_key = project_key
        self._project_path = project_path
        self._schedule = schedule

    def check_parallel_cap(self, project: str, task_id: str) -> None:
        check_cap = getattr(self._real_manager, "check_parallel_cap", None)
        if callable(check_cap):
            check_cap(project, task_id)

    def provision_worker(self, task_id: str, agent_name: str) -> None:
        def _run() -> None:
            provision_claimed_worker(
                config=self._config,
                project_key=self._project_key,
                project_path=self._project_path,
                task_id=task_id,
                agent_name=agent_name,
            )

        self._schedule(_run)


def create_work_service_with_deferred_session(
    *,
    config: Any,
    project_key: str,
    project_path: Path,
    schedule: Callable[[Callable[[], None]], object],
    sync_manager: Any = None,
) -> Any:
    """Construct a work service with post-claim provisioning deferred.

    This is the Web API variant of
    :func:`create_work_service_with_session`. It still wires a real
    ``SessionManager`` so ``claim()`` can run the same pre-claim
    parallel-cap check as the CLI, but replaces the post-commit
    provisioning call with an executor-scheduled background task.
    """
    from pollypm.work.factory import create_work_service

    svc = create_work_service(
        config=config,
        project_path=project_path,
        project_key=project_key,
        sync_manager=sync_manager,
    )
    try:
        session_mgr = attach_session_manager(
            svc, project_path=project_path, config=config
        )
        if session_mgr is not None:
            svc.set_session_manager(
                _DeferredProvisionSessionManager(
                    session_mgr,
                    config=config,
                    project_key=project_key,
                    project_path=project_path,
                    schedule=schedule,
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "create_work_service_with_deferred_session: "
            "attach_session_manager raised unexpectedly",
            exc_info=True,
        )
        _record_attach_error(svc, f"SessionManager attach failed: {exc}")
    return svc


def provision_claimed_worker(
    *,
    config: Any,
    project_key: str,
    project_path: Path,
    task_id: str,
    agent_name: str,
) -> None:
    """Provision the per-task worker for an already-claimed API task."""
    from pollypm.work.factory import create_work_service

    try:
        with create_work_service(
            config=config,
            project_path=project_path,
            project_key=project_key,
        ) as svc:
            session_mgr = attach_session_manager(
                svc, project_path=project_path, config=config
            )
            if session_mgr is None:
                logger.warning(
                    "deferred claim provision: no SessionManager for %s",
                    task_id,
                )
                return
            try:
                task = svc.get(task_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "deferred claim provision: task %s disappeared",
                    task_id,
                    exc_info=True,
                )
                return
            status = getattr(getattr(task, "work_status", None), "value", None)
            if status is None:
                status = getattr(task, "work_status", None)
            if status not in {"in_progress", "review"}:
                logger.info(
                    "deferred claim provision: skip %s in state %r",
                    task_id,
                    status,
                )
                return
            try:
                session_mgr.provision_worker(task_id, agent_name)
            except Exception as exc:  # noqa: BLE001
                _record_deferred_provision_failure(
                    svc, task, task_id, agent_name, exc
                )
                raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "deferred claim provision failed for %s: %s",
            task_id,
            exc,
            exc_info=True,
        )


def _record_deferred_provision_failure(
    svc: Any,
    task: Any,
    task_id: str,
    actor: str,
    exc: BaseException,
) -> None:
    """Audit deferred provisioning failure without undoing the claim.

    The HTTP claim response has already reported success by the time
    deferred provisioning runs. Rolling the task back in the background
    makes the 200 response false and hides the ownership boundary from
    operators. Keep the claim and record an explicit breadcrumb instead.
    """
    add_context = getattr(svc, "add_context", None)
    if not callable(add_context):
        return
    reason = (
        "Deferred worker provisioning failed after the task was claimed; "
        f"claim preserved for {actor}. Error: {exc}"
    )
    try:
        add_context(
            task_id,
            "system",
            reason,
            entry_type="worker_provision_failed",
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "deferred claim provision: failed to record context for %s",
            task_id,
            exc_info=True,
        )


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
    "create_work_service_with_deferred_session",
    "create_work_service_with_session",
    "open_project_work_service",
    "provision_claimed_worker",
]
