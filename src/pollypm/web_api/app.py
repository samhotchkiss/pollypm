"""FastAPI app factory for ``pm serve`` (Phase 1).

Composes the routers in :mod:`pollypm.web_api.routes`, wires the
bearer-auth dependency to every router except ``/health``, and
overrides the ``ConfigDep`` provider so endpoints get the operator's
``PollyPMConfig`` without each route loading it themselves.

The app is intentionally constructed once per call to
:func:`create_app` so tests can spin up isolated instances against
tmp config / tmp token paths.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import threading
import time
from concurrent.futures import thread as _futures_thread
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from pollypm.config import PollyPMConfig, load_config
from pollypm.web_api.auth import (
    SESSION_ISSUED_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_TTL_SECONDS,
    _extract_token,
    is_tailscale_ip,
    make_bearer_auth_dependency,
    make_sse_auth_dependency,
    mint_session_cookie,
)
from pollypm.web_api.dashboard_snapshot_cache import DashboardSnapshotCache
from pollypm.web_api.token import DEFAULT_TOKEN_PATH, load_token
from pollypm.web_api.errors import (
    APIError,
    handle_api_error,
    handle_not_found,
    handle_unhandled_exception,
    handle_validation_error,
)
from pollypm.web_api.routes import audit as audit_routes
from pollypm.web_api.routes import alerts as alerts_routes
from pollypm.web_api.routes import briefings as briefings_routes
from pollypm.web_api.routes import chat_messages as chat_messages_routes
from pollypm.web_api.routes import chat_send as chat_send_routes
from pollypm.web_api.routes import config as config_routes
from pollypm.web_api.routes import dashboard as dashboard_routes
from pollypm.web_api.routes import doctor as doctor_routes
from pollypm.web_api.routes import events as events_routes
from pollypm.web_api.routes import health as health_routes
from pollypm.web_api.routes import heartbeats as heartbeats_routes
from pollypm.web_api.routes import inbox as inbox_routes
from pollypm.web_api.routes import projects as projects_routes
from pollypm.web_api.routes import sessions_admin as sessions_admin_routes
from pollypm.web_api.routes import storage as storage_routes
from pollypm.web_api.routes import tasks as tasks_routes
from pollypm.web_api.routes._deps import _config_provider

logger = logging.getLogger(__name__)


API_V1_PREFIX = "/api/v1"
_UI_IMMUTABLE_ASSETS = frozenset({"app.js", "styles.css"})
_UI_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
_UI_INDEX_CACHE_CONTROL = "no-cache"


# Doctor run/fix work runs in a dedicated thread pool so a slow check
# (network probe, blocked filesystem, wedged subprocess) trips the
# route's wall-clock timeout without hanging the request thread. The
# pool + its single-flight / last-report locks live on ``app.state``
# and are owned by the lifespan context below: startup creates them,
# shutdown cancels in-flight work and waits briefly for the pool to
# drain. Previously these were module-level globals in
# ``routes/doctor.py``; that left timed-out worker threads alive past
# ``pm serve`` shutdown with no FastAPI hook to call
# ``shutdown(cancel_futures=True)`` (Codex round-5 on #2058).
_DOCTOR_MAX_WORKERS = 2
_DOCTOR_THREAD_PREFIX = "doctor"
# Bound on the post-shutdown wait for in-flight doctor workers. Doctor
# work is a mix of fast checks and bounded fixes; in practice the
# longest checks are bounded by ``DEFAULT_RUN_TIMEOUT_SECONDS`` (25s).
# We don't block app teardown for that long — a timed-out worker by
# definition has overrun its budget, so we cancel queued work, wait
# briefly for cooperative completion, and log a warning if anything is
# still alive after the grace period. The worker keeps running until it
# returns naturally (Python stdlib has no safe way to interrupt
# arbitrary blocking code) but it no longer holds the FastAPI app's
# shutdown hostage.
_DOCTOR_SHUTDOWN_GRACE_S = 5.0


class _DaemonThread(threading.Thread):
    """``threading.Thread`` subclass that defaults ``daemon=True``.

    Used as a drop-in replacement for ``threading.Thread`` inside
    :class:`DaemonThreadPoolExecutor._adjust_thread_count` so worker
    threads inherit ``daemon=True`` without us having to mirror the
    cpython ``_adjust_thread_count`` body (its argument shape differs
    between Python 3.13 and 3.14, and pyproject.toml currently allows
    both — see Codex round-6 on #2058).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        kwargs.setdefault("daemon", True)
        super().__init__(*args, **kwargs)


class DaemonThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """ThreadPoolExecutor whose worker threads are daemon threads.

    Required for executors that may have wedged workers at shutdown:
    a non-daemon ``ThreadPoolExecutor`` worker keeps the interpreter
    alive after ``executor.shutdown(wait=False)`` returns, so ``pm
    serve`` cannot exit while a doctor check is hung even though
    FastAPI lifespan teardown has already returned.

    Trade-off: daemon threads can be terminated mid-operation when the
    process exits. That is acceptable for doctor work because by the
    time the process is exiting the operator has already received a
    504 (``routes/doctor.py:_await_with_budget`` returns while
    abandoning the worker), and there is no recoverable in-process
    state — fixes that mutate disk/storage are bounded by their own
    cooperative checks, not by the worker thread's liveness.

    We override ``_adjust_thread_count`` by temporarily patching
    ``concurrent.futures.thread.threading.Thread`` (the symbol the
    stdlib body uses to construct workers) to a daemon-defaulting
    subclass, then delegating to ``super()``. This avoids mirroring
    the stdlib body — its ``threading.Thread(...)`` ``args=`` tuple
    shape changed between Python 3.13 (``(weakref, queue, init,
    initargs)``) and 3.14 (``(weakref, worker_context, queue)``), and
    pyproject.toml allows both interpreters. Patching the constructor
    is version-portable.
    """

    def _adjust_thread_count(self) -> None:  # type: ignore[override]
        original_thread_cls = _futures_thread.threading.Thread
        _futures_thread.threading.Thread = _DaemonThread  # type: ignore[attr-defined,misc]
        try:
            super()._adjust_thread_count()
        finally:
            _futures_thread.threading.Thread = original_thread_cls  # type: ignore[attr-defined,misc]


# Briefings regenerate uses a small dedicated thread pool so a slow
# provider (LLM stall, GitHub rate-limit) trips the route's wall-clock
# timeout without hanging the request thread. The pool + its in-flight
# bookkeeping live on ``app.state`` and are owned by the lifespan
# context below: startup creates them, shutdown cancels in-flight work
# and waits briefly for cooperative completion. Previously these were
# module-level globals in ``routes/briefings.py``; that left zombie
# threads alive after ``pm serve`` shutdown because there was no
# FastAPI hook to call ``shutdown(cancel_futures=True)`` (Codex
# round-4 on #2059). Workers are daemon threads + evicted from
# ``concurrent.futures._threads_queues`` on grace-period overrun so a
# wedged provider can't keep the interpreter alive past app teardown
# (Codex round-7 on #2059 — mirror the #2058 doctor-executor pattern).
_BRIEFING_REGEN_MAX_WORKERS = 2
_BRIEFING_REGEN_THREAD_PREFIX = "briefing-regen"
# Bound on the post-shutdown wait for in-flight briefing workers.
# Regenerate is bounded server-side by the route's wall-clock timeout
# (~30s default per spec §2.7); we don't block app teardown that long
# — a timed-out worker has already overrun its budget, so we cancel
# queued work, wait briefly for cooperative completion, then evict
# survivors so the interpreter can exit. Same shape as
# :data:`_DOCTOR_SHUTDOWN_GRACE_S`.
_BRIEFING_SHUTDOWN_GRACE_S = 5.0

# Claim worker provisioning is intentionally off the HTTP request path:
# git worktree creation and tmux/provider startup can take seconds, but
# the operator's click only needs the atomic DB claim to complete.
_CLAIM_PROVISION_MAX_WORKERS = 2
_CLAIM_PROVISION_THREAD_PREFIX = "claim-provision"
_CLAIM_PROVISION_SHUTDOWN_GRACE_S = 5.0


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan that owns the doctor + briefings executors.

    Startup attaches the following attributes to ``app.state`` so the
    routes resolve them via ``request.app.state`` rather than reaching
    into module-level globals:

    Doctor (Codex round-5 on #2058):

    - ``doctor_executor``: shared :class:`DaemonThreadPoolExecutor`
      for check / fix / verify work. ``max_workers=2`` matches the
      per-request ``--fix`` single-flight: one slot for fix, one for
      parallel read-only checks. Larger fan-out is gratuitous
      (doctor is an operator surface, not a hot path). Daemon
      workers are mandatory so a wedged check doesn't keep
      ``pm serve`` alive after shutdown — see Codex round-6 on
      #2058 and the class docstring for the trade-off.
    - ``doctor_fix_lock``: serializes ``fix=true`` runs across the
      app. Held by the request that wins, released by the worker's
      done-callback when the future truly terminates (not when the
      HTTP request returns — Codex round-4 on #2058).
    - ``doctor_last_report_lock``: guards the in-memory cache slot
      for ``GET /doctor/report``.
    - ``doctor_last_report``: the cache slot itself
      (``tuple[DoctorReport, float] | None``).

    Briefings (Codex round-4 + round-7 on #2059):

    - ``briefing_executor``: shared :class:`DaemonThreadPoolExecutor`
      for regen work. ``max_workers=2`` is intentionally tiny — regen
      is heavy (LLM + subprocess); combined with the per-scope
      in-flight guard, this caps fan-out at "one concurrent regen per
      (type, project)". Mirrors the doctor executor's daemon-thread
      + atexit-eviction discipline so a wedged provider can't keep
      ``pm serve`` alive after shutdown — Codex round-7 on #2059
      flagged that the round-6 daemon-only change is not
      process-safe without the eviction half from #2058.
    - ``briefing_inflight``: ``{(type, project_key): Future}`` so
      duplicate retries are rejected with 409 instead of stacking
      work on the executor.
    - ``briefing_inflight_lock``: guards the dict.

    Claims:

    - ``claim_provision_executor``: shared daemon executor for slow
      post-claim worker provisioning. The DB claim remains synchronous;
      worktree/tmux launch continues in this pool so ``POST
      /tasks/{p}/{n}/claim`` returns within the click budget.

    Shutdown cancels not-yet-started futures for each executor, then
    calls ``executor.shutdown(wait=False, cancel_futures=True)`` so
    the teardown doesn't block on a wedged worker (a running
    ``ThreadPoolExecutor`` worker cannot be cancelled — ``wait=True``
    would hang ``pm serve`` shutdown on a wedged provider; Codex
    round-5 on #2059). We then wait up to the configured grace for
    cooperative completion and evict any survivors from
    ``concurrent.futures._threads_queues`` so the stdlib's
    ``_python_exit`` atexit hook can't join them and hold the
    interpreter open (the v1 RC tradeoff: we can't kill the thread,
    but we can refuse to block on it).
    """
    # Daemon-thread executor so a wedged worker doesn't keep the
    # interpreter alive past app shutdown. ``executor.shutdown(wait=
    # False)`` releases FastAPI lifespan, but a non-daemon worker
    # would still block ``pm serve`` exit until the worker returns
    # naturally — see Codex round-6 on #2058. Daemon workers let the
    # interpreter exit; the trade-off (workers can be killed mid-op
    # at process exit) is acceptable because the operator has already
    # been told 504 by ``_await_with_budget`` and doctor fix
    # mutations are bounded by cooperative checks rather than worker
    # liveness.
    app.state.doctor_executor = DaemonThreadPoolExecutor(
        max_workers=_DOCTOR_MAX_WORKERS,
        thread_name_prefix=_DOCTOR_THREAD_PREFIX,
    )
    app.state.doctor_fix_lock = threading.Lock()
    app.state.doctor_last_report_lock = threading.Lock()
    app.state.doctor_last_report = None
    # Briefings executor reuses the same daemon-thread helper so a
    # wedged regen provider (LLM stall, GitHub rate-limit) can't keep
    # ``pm serve`` alive after shutdown. Codex round-7 on #2059 flagged
    # that the round-6 daemon-only change isn't process-safe without
    # mirroring the atexit eviction from #2058 — done at teardown
    # below.
    app.state.briefing_executor = DaemonThreadPoolExecutor(
        max_workers=_BRIEFING_REGEN_MAX_WORKERS,
        thread_name_prefix=_BRIEFING_REGEN_THREAD_PREFIX,
    )
    app.state.briefing_inflight: dict[tuple[str, str], concurrent.futures.Future[Any]] = {}
    app.state.briefing_inflight_lock = threading.Lock()
    app.state.claim_provision_executor = DaemonThreadPoolExecutor(
        max_workers=_CLAIM_PROVISION_MAX_WORKERS,
        thread_name_prefix=_CLAIM_PROVISION_THREAD_PREFIX,
    )
    try:
        yield
    finally:
        # Briefings teardown first: cancel in-flight registry entries,
        # then run the shared shutdown helper. Doctor uses the same
        # helper to inherit the cancel-futures + grace-wait + atexit
        # eviction discipline.
        try:
            with app.state.briefing_inflight_lock:
                for future in list(app.state.briefing_inflight.values()):
                    future.cancel()
                app.state.briefing_inflight.clear()
        except Exception:  # noqa: BLE001
            logger.warning(
                "lifespan: failed to clear briefing in-flight registry",
                exc_info=True,
            )
        _shutdown_daemon_executor(
            app.state.claim_provision_executor,
            label="claim provisioning executor",
            grace_s=_CLAIM_PROVISION_SHUTDOWN_GRACE_S,
        )
        _shutdown_daemon_executor(
            app.state.briefing_executor,
            label="briefing executor",
            grace_s=_BRIEFING_SHUTDOWN_GRACE_S,
        )
        _shutdown_daemon_executor(
            app.state.doctor_executor,
            label="doctor executor",
            grace_s=_DOCTOR_SHUTDOWN_GRACE_S,
        )


def _shutdown_daemon_executor(
    executor: concurrent.futures.ThreadPoolExecutor,
    *,
    label: str,
    grace_s: float,
) -> None:
    """Cancel + grace-wait + atexit-evict a daemon executor.

    Shared between the doctor and briefings executors so both inherit
    the same teardown discipline:

    1. ``shutdown(wait=False, cancel_futures=True)`` drops queued
       futures and releases lifespan immediately. A wedged in-flight
       worker can't block FastAPI teardown — a running
       ``ThreadPoolExecutor`` worker cannot be cancelled, so
       ``wait=True`` would block teardown forever on a wedged provider
       (Codex round-5 on #2059, mirroring the doctor pattern from
       #2058).
    2. Best-effort grace wait so cooperative workers can finish.
       ``_threads`` is a private but stable attribute on
       ``ThreadPoolExecutor`` (CPython 3.9+); used here purely as a
       liveness probe so we don't ``join`` (which would block).
    3. Survivors are popped from
       ``concurrent.futures._threads_queues`` so the stdlib's
       ``_python_exit`` atexit hook can't join them and keep
       ``pm serve`` alive past interpreter teardown. Combined with
       ``daemon=True`` workers (see :class:`DaemonThreadPoolExecutor`)
       the process can actually exit. Codex round-6 on #2058 +
       round-7 on #2059.
    """
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        logger.warning(
            "lifespan: %s shutdown raised", label, exc_info=True,
        )
    deadline = time.monotonic() + grace_s
    try:
        workers = list(getattr(executor, "_threads", []) or [])
    except Exception:  # noqa: BLE001
        workers = []
    while time.monotonic() < deadline and any(t.is_alive() for t in workers):
        time.sleep(0.05)
    still_running = [t for t in workers if t.is_alive()]
    if still_running:
        logger.warning(
            "lifespan: %s: %d worker(s) still running after %.1fs grace; "
            "leaking thread(s) past app teardown",
            label,
            len(still_running),
            grace_s,
        )
        try:
            for t in still_running:
                _futures_thread._threads_queues.pop(t, None)
        except Exception:  # noqa: BLE001
            logger.warning(
                "lifespan: failed to evict wedged %s worker(s) from "
                "concurrent.futures._threads_queues",
                label,
                exc_info=True,
            )


def create_app(
    *,
    config: PollyPMConfig,
    token_path: Path | None = None,
    tailnet_trust_enabled: bool = False,
) -> FastAPI:
    """Build a FastAPI app for ``pm serve``.

    Parameters
    ----------
    config:
        Pre-loaded :class:`PollyPMConfig` the endpoints read from.
    token_path:
        Override the bearer-token file location. Defaults to
        ``~/.pollypm/api-token`` per spec §3. Tests pass a tmp path
        so the user's real token never surfaces.
    tailnet_trust_enabled:
        When ``True``, the auth dependency and the ``/ui/`` cookie gate
        treat a request from the Tailscale CGNAT range
        (``100.64.0.0/10``) as authenticated even without a bearer or
        cookie. ``pm serve`` only enables this when it actually bound
        to a verified Tailscale IPv4 — an explicit
        ``--host 0.0.0.0 --allow-remote`` keeps the default ``False``,
        so a CGNAT-source peer on the public interface still needs a
        credential. Default ``False`` (closed by default; tests and
        legacy callers stay strict).
    """
    # FastAPI's default ``openapi_url`` is public, but the spec
    # (§3) lists only ``/health`` as auth-exempt. Disable the default
    # endpoint and serve our own auth-protected route below so the
    # generated contract isn't reachable without a bearer token.
    app = FastAPI(
        title="PollyPM Web API",
        version="0.1.0",
        docs_url=None,  # we serve OpenAPI via the spec endpoint
        redoc_url=None,
        openapi_url=None,
        # Lifespan owns the doctor + briefings ThreadPoolExecutors,
        # the doctor single-flight fix lock + last-report cache, and
        # the briefings in-flight registry — so they're cleanly torn
        # down on app exit (Codex round-5 on #2058 + round-4/round-7
        # on #2059). Routes read them off
        # ``request.app.state`` rather than module-level globals.
        lifespan=_lifespan,
    )

    # CORS for the separate-repo frontend. The spec (§10) shows the
    # browser sending ``Authorization: Bearer …`` to ``http://127.0.0.1:8765``;
    # without CORS, every preflight returns 405 and the frontend never
    # talks to the API. We allow loopback origins on any port (the
    # frontend's Vite dev server picks an arbitrary port) and require
    # credentials so the bearer header survives. This is intentionally
    # narrow — we do NOT mirror arbitrary ``Origin`` headers.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
        # ``X-PollyPM-Warning`` is set by ``POST /chat/{session}/send``
        # with ``safety=loose`` when the heartbeat suggests the agent
        # may still be streaming (see ``chat_send.py``). Without
        # exposing it here, browsers strip the header before the
        # frontend can read it, defeating the documented contract.
        expose_headers=["Last-Event-ID", "X-PollyPM-Warning"],
    )
    app.state.tailnet_trust_enabled = tailnet_trust_enabled
    app.state.dashboard_snapshot_cache = DashboardSnapshotCache()

    # Wire the config provider. When the loaded config carries the
    # on-disk ``config_path`` (always true for ``pm serve``), re-invoke
    # :func:`load_config` on each request so TOML edits propagate
    # without a server restart. ``load_config`` has an mtime-keyed
    # cache, so an unchanged file is a single ``stat`` call — no
    # re-parse cost (Codex round-2 P0 on PR #2056: the old
    # ``lambda: config`` returned a startup-frozen snapshot).
    #
    # The fallback (no ``config_path``) keeps the in-memory snapshot —
    # tests build :class:`PollyPMConfig` directly and never set
    # ``config_path``, and we don't want them to depend on a real
    # TOML on disk.
    config_path = getattr(config, "config_path", None)
    if config_path is not None:

        def _reload_config() -> PollyPMConfig:
            try:
                return load_config(config_path)
            except Exception:
                # Disk read failure (file deleted between requests,
                # transient permission glitch, malformed TOML mid-edit).
                # Fall back to the startup snapshot rather than 500ing
                # the endpoint — the operator can still see what
                # ``pm serve`` booted with.
                logger.warning(
                    "load_config(%s) failed; serving startup snapshot",
                    config_path,
                    exc_info=True,
                )
                return config

        app.dependency_overrides[_config_provider] = _reload_config
    else:
        app.dependency_overrides[_config_provider] = lambda: config

    # Exception handlers — turn all error paths into the spec's body
    # shape.
    app.add_exception_handler(APIError, handle_api_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(404, handle_not_found)
    app.add_exception_handler(Exception, handle_unhandled_exception)

    # Wire the briefings provider so /api/v1/briefings reports
    # ``morning.available=true`` and render/regenerate don't 503 in
    # normal ``pm serve`` use. The full plugin host isn't bootstrapped
    # in the API process (it owns roster + job scheduling + cockpit
    # rail registration — none of which the read/regenerate path
    # needs); the only piece the briefings routes consult is the
    # registry seam, which the ``morning_briefing`` plugin populates
    # via ``register_briefing_provider`` during plugin ``initialize``.
    # Mirror that single call here so the API has the same provider
    # the cockpit + CLI use. The import is local so a future build
    # that disables the built-in plugin doesn't take a hard import
    # on the plugin tree (Codex round-2 P0 on #2059). The helper
    # honors ``config.plugins.disabled`` before registering — same
    # contract the plugin host enforces (Codex round-3 on #2059).
    _wire_briefings_provider(config)

    # Health is exempt from auth per spec §3.
    app.include_router(health_routes.router, prefix=API_V1_PREFIX)

    auth_dependency = make_bearer_auth_dependency(
        token_path, tailnet_trust_enabled=tailnet_trust_enabled,
    )
    auth_deps = [Depends(auth_dependency)]
    # SSE has its own auth dependency that also accepts ``?token=``
    # (browser EventSource cannot set custom headers — see spec §4).
    sse_auth_dependency = make_sse_auth_dependency(
        token_path, tailnet_trust_enabled=tailnet_trust_enabled,
    )
    sse_auth_deps = [Depends(sse_auth_dependency)]

    app.include_router(projects_routes.router, prefix=API_V1_PREFIX, dependencies=auth_deps)
    app.include_router(tasks_routes.router, prefix=API_V1_PREFIX, dependencies=auth_deps)
    app.include_router(inbox_routes.router, prefix=API_V1_PREFIX, dependencies=auth_deps)
    app.include_router(alerts_routes.router, prefix=API_V1_PREFIX, dependencies=auth_deps)
    app.include_router(dashboard_routes.router, prefix=API_V1_PREFIX, dependencies=auth_deps)
    # Phase 2 surface §13 — read-only config endpoints. Mutation is
    # deferred to Phase 3 per the spec; config edits stay TOML-first.
    app.include_router(config_routes.router, prefix=API_V1_PREFIX, dependencies=auth_deps)
    # Phase 2 surface §12 — read-only storage report (no prune).
    app.include_router(storage_routes.router, prefix=API_V1_PREFIX, dependencies=auth_deps)
    # Phase 2 §7 doctor endpoints (list checks, last report, run + fix).
    # Sits under the same ``/api/v1`` prefix as the other surfaces; the
    # bearer-auth dependency is reused — there is no read/write split
    # because every doctor endpoint can side-effect (run + fix).
    app.include_router(doctor_routes.router, prefix=API_V1_PREFIX, dependencies=auth_deps)
    # Phase 2 surface §9 — briefings list/render/regenerate.
    app.include_router(
        briefings_routes.router, prefix=API_V1_PREFIX, dependencies=auth_deps,
    )
    app.include_router(events_routes.router, prefix=API_V1_PREFIX, dependencies=sse_auth_deps)
    # Phase 2 §11 — read-side heartbeats surface. SSE stream endpoint
    # (§11.1 row 4) ships as a separate follow-up; the GET endpoints
    # are independently mergeable because they go through the same
    # ``pg_heartbeats`` facade the cockpit + ``pm sessions`` already
    # consume.
    app.include_router(
        heartbeats_routes.router,
        prefix=API_V1_PREFIX,
        dependencies=auth_deps,
    )
    # Phase 2 surface #6 — historical audit query (grep + stats). The
    # streaming side already ships at ``/api/v1/events`` (Phase 1 SSE);
    # we deliberately do not re-route that path here.
    app.include_router(audit_routes.router, prefix=API_V1_PREFIX, dependencies=auth_deps)
    # P2 of the chat-endpoints spec — GET /api/v1/chat/sessions and
    # GET /api/v1/chat/{session_name}/messages. Sits under the same
    # ``/chat`` prefix the P3 send endpoint shares so all
    # chat-surface verbs are co-located in the routing table.
    app.include_router(
        chat_messages_routes.router,
        prefix=f"{API_V1_PREFIX}/chat",
        dependencies=auth_deps,
    )
    # P3 of the chat-endpoints spec — POST /api/v1/chat/{session}/send.
    # P1 (surface registry + transcript readers) and P2 (GET messages)
    # ship in separate PRs; the send endpoint is independently mergeable
    # because it depends only on the supervisor's storage-closet naming
    # convention + ``TmuxClient.send_keys`` + ``pg_heartbeats``.
    app.include_router(
        chat_send_routes.router,
        prefix=f"{API_V1_PREFIX}/chat",
        dependencies=auth_deps,
    )
    # Phase 2 surface #8 — sessions admin (§10 of the endpoint spec).
    # Mirrors ``pm sessions`` (#2039) plus restart / pause / resume.
    app.include_router(
        sessions_admin_routes.router,
        prefix=API_V1_PREFIX,
        dependencies=auth_deps,
    )

    # v0 web UI (Phase 7) — static SPA at ``/ui/`` with a cookie-bridge
    # entry point that mints a signed ``pollypm-session`` cookie. The
    # static files (app.js, styles.css) are served raw; the entry route
    # is custom so it can set the cookie + return the HTML in one
    # round-trip.
    _mount_web_ui(
        app,
        token_path=token_path,
        tailnet_trust_enabled=tailnet_trust_enabled,
    )

    # ``security: bearerAuth`` declared at the document level so
    # generated clients carry the correct Auth scheme.
    _attach_security_scheme(app)

    # Auth-gated OpenAPI route. Codegen tooling that targets this
    # endpoint must send the same ``Authorization: Bearer …`` header
    # used everywhere else.
    @app.get(
        f"{API_V1_PREFIX}/openapi.json",
        include_in_schema=False,
        dependencies=auth_deps,
    )
    def _openapi_endpoint() -> JSONResponse:
        return JSONResponse(app.openapi())

    return app


def _wire_briefings_provider(config: PollyPMConfig) -> None:
    """Bootstrap built-in briefing providers for the API.

    Delegates to :func:`pollypm.briefings_bootstrap.bootstrap_builtin_briefings`
    so the Web API never imports ``pollypm.plugins_builtin`` directly
    (Codex round-9 on #2059: the prior implementation imported
    ``plugins_builtin.morning_briefing.inbox`` from this module, which
    violated the documented core boundary — see
    ``briefings_registry.py`` module docstring + ``cli.py:201-207``).

    ``pm serve`` doesn't boot the plugin host (which owns roster + job
    scheduling — none of which the read/regenerate path needs); the
    bootstrap helper mirrors the single registry-wiring step the
    plugin's ``initialize`` hook performs so
    ``GET /api/v1/briefings`` reports ``morning.available=true`` in
    production. The bootstrap honors ``config.plugins.disabled``.

    Re-running this on every ``create_app`` call is a no-op when
    enabled (registry slots just rebind) and idempotent when disabled.
    """
    from pollypm.briefings_bootstrap import bootstrap_builtin_briefings

    bootstrap_builtin_briefings(config)


def _attach_security_scheme(app: FastAPI) -> None:
    """Inject the OpenAPI ``securitySchemes`` block.

    FastAPI's auto-generated OpenAPI doesn't add any auth schemes
    unless we wire them through ``OAuth2PasswordBearer`` or override
    ``openapi_schema``. The simplest path is to customize the
    schema once after first generation.

    The runtime accepts three credential modes (see
    ``docs/web-api-spec.md`` §3 and the static
    ``docs/api/openapi.yaml``):

    1. ``bearerAuth`` — ``Authorization: Bearer <token>`` header,
       token sourced from ``~/.pollypm/api-token``.
    2. ``cookieAuth`` — signed ``pollypm-session`` cookie minted by
       ``GET /ui/`` for loopback peers, verified tailnet peers, or
       callers presenting a valid bearer header. The raw bearer token
       is not stored in the cookie; token rotation invalidates the
       signature.
    3. Credential-free tailnet-peer mode (empty ``{}`` entry in the
       security list) — only honoured when the app was constructed
       with ``tailnet_trust_enabled=True``.

    The runtime ``/openapi.json`` must mirror that three-mode story
    so generated clients reading the live endpoint see the same auth
    contract as readers of the static YAML (#2065).
    """
    base_openapi = app.openapi

    def _custom_openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = base_openapi()
        schema.setdefault("components", {})
        # Replace any auto-detected scheme block wholesale: we want the
        # runtime doc to advertise exactly the same scheme set as the
        # static contract.
        schema["components"]["securitySchemes"] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "opaque",
                "description": (
                    "Bearer token from `~/.pollypm/api-token`. "
                    "Generated on first `pm serve` startup; rotated "
                    "via `pm api regen-token`. Always accepted. "
                    "See `docs/web-api-spec.md` §3."
                ),
            },
            "cookieAuth": {
                "type": "apiKey",
                "in": "cookie",
                "name": "pollypm-session",
                "description": (
                    "Session cookie minted by `GET /ui/` for loopback "
                    "peers, verified tailnet peers (when "
                    "`tailnet_trust_enabled=True`), or callers "
                    "presenting a valid bearer header. The cookie is "
                    "an opaque per-browser session value signed with "
                    "the current API token; the raw bearer token is "
                    "not stored in the cookie. LAN clients receive HTML "
                    "without `Set-Cookie` and 401 on the first API call. "
                    "See `docs/web-api-spec.md` §3."
                ),
            },
        }
        # Default security applies to every operation; ``/health``
        # opts out via the route definition (security: []).
        # The empty ``{}`` entry models the credential-free
        # tailnet-peer mode (OpenAPI's canonical way of saying
        # "no credentials required"); it does NOT mean every caller
        # is anonymous — the runtime still gates on
        # ``tailnet_trust_enabled`` + a verified peer IP.
        schema["security"] = [
            {"bearerAuth": []},
            {"cookieAuth": []},
            {},
        ]
        # Drop the security requirement from ``/health`` so the
        # generated doc matches the runtime behaviour.
        for path, ops in schema.get("paths", {}).items():
            if path.endswith("/health"):
                for op in ops.values():
                    if isinstance(op, dict):
                        op["security"] = []
        app.openapi_schema = schema
        return schema

    app.openapi = _custom_openapi  # type: ignore[assignment]


def _mount_web_ui(
    app: FastAPI,
    *,
    token_path: Path | None,
    tailnet_trust_enabled: bool = False,
) -> None:
    """Mount the v0 web UI static assets + cookie-bridge entry route.

    Layout:

    - ``GET /ui/`` returns ``index.html`` and sets a signed
      ``pollypm-session`` cookie, so subsequent ``fetch(...,
      {credentials: 'include'})`` calls authenticate without the user
      ever seeing the bearer token.
    - ``GET /ui/inbox`` (and the dashboard-adjacent ``/ui/tasks`` /
      ``/ui/alerts`` aliases) serve the same SPA shell so bookmarked
      operator surfaces don't fall through to the static-file 404.
    - ``GET /ui/{path}`` (e.g. ``app.js``, ``styles.css``) is served by
      :class:`StaticFiles` from the ``ui/`` directory next to this
      module. No auth on the static assets themselves — the bytes are
      not sensitive and gating them behind cookie auth would break the
      ``GET /ui/`` boot (browser fetches ``app.js`` before the cookie
      round-trips on slow links). The API endpoints those assets call
      remain auth-gated.
    - ``GET /ui`` (no trailing slash) → 307 to ``/ui/`` so the cookie
      gets set even if the operator types the short form.
    """
    ui_dir = Path(__file__).parent / "ui"
    if not ui_dir.exists():
        # Defensive: an installed wheel without the ui/ data directory
        # would 404 on /ui/, which is fine but logging makes the cause
        # discoverable. The app still boots and the JSON API works.
        logger.warning(
            "web UI directory missing at %s; /ui/ will 404", ui_dir,
        )
        return

    index_path = ui_dir / "index.html"

    def _file_fingerprint(path: Path) -> str:
        try:
            stat = path.stat()
        except OSError:
            return "missing"
        return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"

    def _index_etag() -> str:
        parts = [
            f"index:{_file_fingerprint(index_path)}",
            *(f"{name}:{_file_fingerprint(ui_dir / name)}" for name in sorted(_UI_IMMUTABLE_ASSETS)),
        ]
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
        return f'"ui-index-{digest}"'

    def _client_has_etag(request: Request, etag: str) -> bool:
        header = request.headers.get("if-none-match")
        if not header:
            return False
        return any(candidate.strip() == etag for candidate in header.split(","))

    def _versioned_index_html() -> str:
        html = index_path.read_text(encoding="utf-8")
        for asset_name in sorted(_UI_IMMUTABLE_ASSETS):
            asset_version = _file_fingerprint(ui_dir / asset_name)
            html = html.replace(
                f"/ui/{asset_name}",
                f"/ui/{asset_name}?v={asset_version}",
            )
        return html

    def _ui_index_response(request: Request) -> Response:
        etag = _index_etag()
        headers = {
            "Cache-Control": _UI_INDEX_CACHE_CONTROL,
            "ETag": etag,
        }
        if _client_has_etag(request, etag):
            return Response(status_code=304, headers=headers)
        return Response(
            _versioned_index_html(),
            media_type="text/html",
            headers=headers,
        )

    @app.get("/ui", include_in_schema=False)
    def _ui_root_redirect() -> Response:
        # 307 preserves method + the spec for "the trailing slash form
        # is canonical" without altering verbs.
        return Response(status_code=307, headers={"Location": "/ui/"})

    @app.get("/ui/alerts/", include_in_schema=False)
    @app.get("/ui/alerts", include_in_schema=False)
    @app.get("/ui/tasks/", include_in_schema=False)
    @app.get("/ui/tasks", include_in_schema=False)
    @app.get("/ui/inbox/", include_in_schema=False)
    @app.get("/ui/inbox", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    def _ui_index(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Response:
        # Load the token at request time (not app-build time) so a
        # ``pm api regen-token`` rotation flows into the cookie signature
        # on the next page load without an app restart.
        resolved_token_path = token_path or DEFAULT_TOKEN_PATH
        token_value = load_token(resolved_token_path)
        response = _ui_index_response(request)

        # Cookie issuance is credential issuance — gate it behind a
        # proven local-operator path so a LAN device (or anyone who
        # can reach this port from a network we don't trust) can't
        # GET /ui/ and walk away with a browser credential via
        # Set-Cookie. Three acceptable signals:
        #
        # 1. Authorization: Bearer <token> matches the on-disk token
        #    (operator pasted it via curl / scripted boot).
        # 2. Loopback client (127.0.0.1, ::1) — local operator on
        #    the Mac itself.
        # 3. Tailscale CGNAT peer — operator hitting /ui/ from a
        #    tailnet device, but **only when ``tailnet_trust_enabled``
        #    is True** (i.e. ``pm serve`` actually bound to a verified
        #    Tailscale interface). On an explicit
        #    ``--host 0.0.0.0 --allow-remote`` deploy the flag is
        #    False, so a CGNAT-source peer no longer earns a cookie:
        #    RFC 6598 shared address space is also used by some ISP
        #    CGNAT setups and must not be trusted on the public
        #    interface.
        #
        # Everything else (LAN device, public-facing deploy by
        # accident, spoofed source IP through a misconfigured proxy)
        # gets the HTML but NO cookie. The SPA surfaces a 401 on its
        # first /api/ call so the operator knows to access via
        # loopback or tailnet. See spec doc:
        # docs/web-ui-2065-security-spec.md (decision d-ii).
        client_host = request.client.host if request.client else None
        is_loopback = client_host in ("127.0.0.1", "::1")
        is_tailnet = tailnet_trust_enabled and is_tailscale_ip(client_host)
        bearer_token = _extract_token(authorization)
        valid_bearer = False
        if bearer_token is not None and token_value is not None:
            from secrets import compare_digest

            valid_bearer = compare_digest(bearer_token, token_value)

        if token_value and (is_loopback or is_tailnet or valid_bearer):
            # ``HttpOnly`` so JS can't read the token; ``SameSite=Lax``
            # so cross-tab navigation still carries it; ``Secure=False``
            # because the v0 deploy is loopback/Tailscale HTTP. When
            # the operator puts this behind TLS they should set
            # ``Secure=True`` via a reverse proxy.
            issued_at = int(time.time())
            response.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=mint_session_cookie(token_value, issued_at=issued_at),
                httponly=True,
                samesite="lax",
                secure=False,
                path="/",
                max_age=SESSION_COOKIE_TTL_SECONDS,
            )
            response.set_cookie(
                key=SESSION_ISSUED_COOKIE_NAME,
                value=str(issued_at),
                httponly=False,
                samesite="lax",
                secure=False,
                path="/",
                max_age=SESSION_COOKIE_TTL_SECONDS,
            )
        # If the token file doesn't exist yet, or the caller isn't on
        # a trusted path, we still serve the HTML — the SPA will
        # surface a 401 the first time it hits /api/ and the operator
        # will know to access from loopback / Tailscale (or to run
        # `pm api regen-token`).
        return response

    ui_root = ui_dir.resolve()
    spa_deep_links = {"", "inbox", "tasks", "alerts"}

    @app.get("/ui/{asset_path:path}", include_in_schema=False)
    def _ui_static_asset(
        asset_path: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Response:
        normalized = asset_path.strip("/")
        if normalized in spa_deep_links:
            return _ui_index(request, authorization)

        candidate = (ui_dir / asset_path).resolve()
        try:
            candidate.relative_to(ui_root)
        except ValueError:
            return Response("Not Found", status_code=404)
        if not candidate.is_file():
            return Response("Not Found", status_code=404)
        response = FileResponse(candidate)
        if candidate.name in _UI_IMMUTABLE_ASSETS:
            response.headers["Cache-Control"] = _UI_ASSET_CACHE_CONTROL
        return response


__all__ = ["API_V1_PREFIX", "create_app"]
