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

import logging
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pollypm.config import PollyPMConfig
from pollypm.web_api.auth import (
    make_bearer_auth_dependency,
    make_sse_auth_dependency,
)
from pollypm.web_api.errors import (
    APIError,
    handle_api_error,
    handle_unhandled_exception,
    handle_validation_error,
)
from pollypm.web_api.routes import events as events_routes
from pollypm.web_api.routes import health as health_routes
from pollypm.web_api.routes import inbox as inbox_routes
from pollypm.web_api.routes import projects as projects_routes
from pollypm.web_api.routes import tasks as tasks_routes
from pollypm.web_api.routes._deps import _config_provider

logger = logging.getLogger(__name__)


API_V1_PREFIX = "/api/v1"


def create_app(
    *,
    config: PollyPMConfig,
    token_path: Path | None = None,
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
        expose_headers=["Last-Event-ID"],
    )

    # Wire the config provider so every endpoint shares the same
    # in-memory config rather than each route reloading from disk.
    app.dependency_overrides[_config_provider] = lambda: config

    # Exception handlers — turn all error paths into the spec's body
    # shape.
    app.add_exception_handler(APIError, handle_api_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unhandled_exception)

    # Health is exempt from auth per spec §3.
    app.include_router(health_routes.router, prefix=API_V1_PREFIX)

    auth_dependency = make_bearer_auth_dependency(token_path)
    auth_deps = [Depends(auth_dependency)]
    # SSE has its own auth dependency that also accepts ``?token=``
    # (browser EventSource cannot set custom headers — see spec §4).
    sse_auth_dependency = make_sse_auth_dependency(token_path)
    sse_auth_deps = [Depends(sse_auth_dependency)]

    app.include_router(projects_routes.router, prefix=API_V1_PREFIX, dependencies=auth_deps)
    app.include_router(tasks_routes.router, prefix=API_V1_PREFIX, dependencies=auth_deps)
    app.include_router(inbox_routes.router, prefix=API_V1_PREFIX, dependencies=auth_deps)
    app.include_router(events_routes.router, prefix=API_V1_PREFIX, dependencies=sse_auth_deps)

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


__all__ = ["API_V1_PREFIX", "create_app"]
