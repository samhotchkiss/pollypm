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
from fastapi.staticfiles import StaticFiles

from pollypm.config import PollyPMConfig, load_config
from pollypm.web_api.auth import (
    SESSION_COOKIE_NAME,
    _extract_token,
    is_tailscale_ip,
    make_bearer_auth_dependency,
    make_sse_auth_dependency,
)
from pollypm.web_api.token import DEFAULT_TOKEN_PATH, load_token
from pollypm.web_api.errors import (
    APIError,
    handle_api_error,
    handle_unhandled_exception,
    handle_validation_error,
)
from pollypm.web_api.routes import audit as audit_routes
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


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan that owns the doctor executor + locks.

    Startup attaches the following attributes to ``app.state`` so the
    doctor routes resolve them via ``request.app.state`` rather than
    reaching into module-level globals:

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

    Shutdown cancels not-yet-started futures, then calls
    ``executor.shutdown(wait=False, cancel_futures=True)`` so the
    teardown doesn't block on a wedged check. We then wait up to
    :data:`_DOCTOR_SHUTDOWN_GRACE_S` for cooperative completion and
    log a warning if any worker is still alive (the v1 RC tradeoff:
    we can't kill the thread, but we can refuse to block on it).
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
    try:
        yield
    finally:
        executor = app.state.doctor_executor
        try:
            # ``cancel_futures=True`` drops anything still queued;
            # ``wait=False`` so a wedged in-flight worker can't block
            # FastAPI teardown indefinitely (the v1 RC tradeoff
            # documented in routes/doctor.py:_await_with_budget).
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            logger.warning(
                "lifespan: doctor executor shutdown raised", exc_info=True,
            )
        # Best-effort wait for the worker threads to finish naturally.
        # If they don't, log + leak rather than hang the app teardown.
        deadline = time.monotonic() + _DOCTOR_SHUTDOWN_GRACE_S
        try:
            workers = list(getattr(executor, "_threads", []) or [])
        except Exception:  # noqa: BLE001
            workers = []
        while time.monotonic() < deadline and any(t.is_alive() for t in workers):
            time.sleep(0.05)
        still_running = [t for t in workers if t.is_alive()]
        if still_running:
            logger.warning(
                "lifespan: doctor executor: %d worker(s) still running after "
                "%.1fs grace; leaking thread(s) past app teardown",
                len(still_running),
                _DOCTOR_SHUTDOWN_GRACE_S,
            )
            # ``concurrent.futures`` registers an ``atexit`` hook
            # (``_python_exit``) that joins every executor worker
            # via the module-level ``_threads_queues`` dict. That
            # join blocks ``pm serve`` interpreter exit even when
            # the workers are daemon threads. Pop our workers out
            # of that mapping so the atexit hook can't reach them
            # — combined with ``daemon=True`` (see
            # :class:`DaemonThreadPoolExecutor`) the process can
            # now exit. Codex round-6 on #2058.
            try:
                for t in still_running:
                    _futures_thread._threads_queues.pop(t, None)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "lifespan: failed to evict wedged doctor worker(s) "
                    "from concurrent.futures._threads_queues",
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
        # Lifespan owns the doctor ThreadPoolExecutor + single-flight
        # fix lock + last-report cache so they're cleanly torn down on
        # app exit (Codex round-5 on #2058). Routes read them off
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
    app.add_exception_handler(Exception, handle_unhandled_exception)

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
    # entry point that injects the on-disk token as the
    # ``pollypm-session`` cookie. The static files (app.js, styles.css)
    # are served raw; the entry route is custom so it can set the
    # cookie + return the HTML in one round-trip.
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


def _attach_security_scheme(app: FastAPI) -> None:
    """Inject the OpenAPI ``securitySchemes`` block.

    FastAPI's auto-generated OpenAPI doesn't add bearer auth unless
    we wire it through ``OAuth2PasswordBearer`` or override
    ``openapi_schema``. The simplest path is to customize the
    schema once after first generation.
    """
    base_openapi = app.openapi

    def _custom_openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = base_openapi()
        schema.setdefault("components", {})
        schema["components"].setdefault("securitySchemes", {})
        schema["components"]["securitySchemes"]["bearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque",
            "description": (
                "Token from `~/.pollypm/api-token`. Generated on first "
                "`pm serve` startup; rotated via `pm api regen-token`."
            ),
        }
        # Default security applies to every operation; ``/health``
        # opts out via the route definition (security: []).
        schema["security"] = [{"bearerAuth": []}]
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

    - ``GET /ui/`` returns ``index.html`` and sets the
      ``pollypm-session`` cookie from the on-disk token, so subsequent
      ``fetch(..., {credentials: 'include'})`` calls authenticate
      without the user ever seeing the token.
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

    @app.get("/ui", include_in_schema=False)
    def _ui_root_redirect() -> Response:
        # 307 preserves method + the spec for "the trailing slash form
        # is canonical" without altering verbs.
        return Response(status_code=307, headers={"Location": "/ui/"})

    @app.get("/ui/", include_in_schema=False)
    def _ui_index(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Response:
        # Load the token at request time (not app-build time) so a
        # ``pm api regen-token`` rotation flows into the cookie on the
        # next page load without an app restart.
        resolved_token_path = token_path or DEFAULT_TOKEN_PATH
        token_value = load_token(resolved_token_path)
        response = FileResponse(index_path, media_type="text/html")

        # Cookie issuance is credential issuance — gate it behind a
        # proven local-operator path so a LAN device (or anyone who
        # can reach this port from a network we don't trust) can't
        # GET /ui/ and walk away with the bearer token via
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
            response.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=token_value,
                httponly=True,
                samesite="lax",
                secure=False,
                path="/",
                max_age=60 * 60 * 24 * 7,  # 7 days
            )
        # If the token file doesn't exist yet, or the caller isn't on
        # a trusted path, we still serve the HTML — the SPA will
        # surface a 401 the first time it hits /api/ and the operator
        # will know to access from loopback / Tailscale (or to run
        # `pm api regen-token`).
        return response

    # Static assets served raw. ``html=False`` keeps StaticFiles from
    # hijacking the bare ``/ui/`` path (we've already taken that route
    # above with the cookie-setting handler).
    app.mount(
        "/ui",
        StaticFiles(directory=str(ui_dir), html=False),
        name="ui-static",
    )


__all__ = ["API_V1_PREFIX", "create_app"]
