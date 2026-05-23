"""Briefings endpoints (Phase 2 — §9 of the phase-2 endpoints spec).

Implements three routes under ``/api/v1/briefings``:

- ``GET  /api/v1/briefings``                       — list briefing types
- ``GET  /api/v1/briefings/{type_name}``           — render last generated
- ``POST /api/v1/briefings/{type_name}/regenerate`` — force regenerate

The phase-2 spec (``~/Desktop/pollypm-phase2-endpoints-spec.md`` §9)
calls for a small, sync-only surface in this PR: long-running renders
exceed a 30 s budget and return ``504 timeout`` (§2.7 / §9.2 third
bullet). ``?async=true`` + a job registry is deferred to Phase 2.5.

The route is a thin adapter over the briefing registry seams:

- :func:`pollypm.briefings_registry.list_briefings` powers availability
  for the discovery endpoint (presence of a registered provider implies
  the type is available).
- :func:`pollypm.briefings_registry.get_briefing_render_provider` returns
  the plugin-installed render adapter for ``{type_name}`` — the route
  consumes its :class:`BriefingArtifact` outputs and never imports the
  plugin tree directly (Codex round-9 on #2059: previously
  ``pollypm.plugins_builtin.morning_briefing.inbox`` /
  ``handlers.briefing_tick`` / ``settings`` / ``state`` were imported
  here, which violated the documented core boundary).
- The regenerate path wraps the provider call in a thread-pool timeout
  so a slow provider surfaces as ``504 timeout`` rather than hanging
  the request.

Only one briefing type (``morning``) ships built-in today; the registry
seam is honored so a future plugin can register additional types
without touching this module. When the registry is empty, list returns
an empty array (spec §9 / Phase 1 fail-soft posture).
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
from datetime import UTC, datetime
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from pollypm.web_api.errors import (
    APIError,
    conflict,
    invalid_request,
    not_found,
    service_unavailable,
)
from pollypm.web_api.routes._deps import ConfigDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Briefings"])


# ---------------------------------------------------------------------------
# Timeouts (the executor + in-flight registry live on ``app.state``;
# see :func:`pollypm.web_api.app._lifespan`).
# ---------------------------------------------------------------------------


# Spec §2.7 — sync mode budget. Median morning briefing render is
# 8–12 s today, so 30 s is comfortable for the happy path; pathological
# providers (GitHub rate-limit, LLM stall) trip the 504 path.
DEFAULT_REGENERATE_TIMEOUT_SECONDS = 30.0


def _get_executor(request: Request) -> concurrent.futures.ThreadPoolExecutor:
    """Resolve the briefings executor from app-level state.

    The executor's lifecycle is owned by the FastAPI lifespan context
    (``app._lifespan`` in ``web_api/app.py``) so it's cleanly shut
    down at app teardown — no zombie worker threads after ``pm
    serve`` exits (Codex round-4 on #2059).
    """
    executor = getattr(request.app.state, "briefing_executor", None)
    if executor is None:  # pragma: no cover — only hit if lifespan didn't run
        raise service_unavailable(
            "briefings executor is not initialized",
            hint=(
                "The FastAPI lifespan didn't run; ensure ``create_app`` "
                "is invoked through the standard ASGI server (uvicorn) "
                "or a TestClient context manager."
            ),
        )
    return executor


def _get_inflight(
    request: Request,
) -> tuple[dict[tuple[str, str], concurrent.futures.Future[Any]], threading.Lock]:
    """Resolve the in-flight registry + its lock from app-level state."""
    state = request.app.state
    inflight = getattr(state, "briefing_inflight", None)
    lock = getattr(state, "briefing_inflight_lock", None)
    if inflight is None or lock is None:  # pragma: no cover
        raise service_unavailable(
            "briefings in-flight registry is not initialized",
            hint=(
                "The FastAPI lifespan didn't run; ensure ``create_app`` "
                "is invoked through the standard ASGI server."
            ),
        )
    return inflight, lock


# ---------------------------------------------------------------------------
# Pydantic wire models
# ---------------------------------------------------------------------------


class BriefingTypeInfo(BaseModel):
    """One entry in ``GET /briefings`` (spec §9.1)."""

    name: str
    description: str
    available: bool = True


class BriefingTypesResponse(BaseModel):
    """``GET /api/v1/briefings`` envelope."""

    types: list[BriefingTypeInfo]


class BriefingResponse(BaseModel):
    """``GET /briefings/{type}`` and the regenerate POST response."""

    type: str
    generated_at: datetime | None = None
    date_local: str | None = None
    mode: str | None = None
    markdown: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RegenerateRequest(BaseModel):
    """``POST /briefings/{type}/regenerate`` body.

    ``project`` narrows the regenerate scope to a single project key.
    Today's morning briefing ignores it (it's always whole-workspace);
    we accept the field for forward compatibility so plugin-contributed
    briefing types (e.g. per-project weekly) don't need a body-schema
    bump later.
    """

    project: str | None = None


# ---------------------------------------------------------------------------
# Type registry (briefing-type name -> adapter)
# ---------------------------------------------------------------------------


# Each adapter exposes three callables: ``available()``, ``render()``,
# ``regenerate()``. Built-in ``morning`` reads through the
# ``briefings_registry`` seam so disabling the plugin downgrades the
# type to "absent" instead of breaking imports.
class _BriefingAdapter:
    """Glue between the route and a concrete briefing implementation."""

    def __init__(
        self,
        name: str,
        *,
        description: str,
        available: Callable[[Any], bool],
        render_last: Callable[[Any], BriefingResponse | None],
        regenerate: Callable[[Any, RegenerateRequest], BriefingResponse],
    ) -> None:
        self.name = name
        self.description = description
        self._available = available
        self._render_last = render_last
        self._regenerate = regenerate

    def is_available(self, config: Any) -> bool:
        try:
            return bool(self._available(config))
        except Exception:  # noqa: BLE001
            logger.debug(
                "briefings: availability probe failed for %s", self.name,
                exc_info=True,
            )
            return False

    def render_last(self, config: Any) -> BriefingResponse | None:
        return self._render_last(config)

    def regenerate(self, config: Any, body: RegenerateRequest) -> BriefingResponse:
        return self._regenerate(config, body)


# Plugin name the morning briefing type is gated on. Kept as a route
# module constant (rather than importing from
# ``pollypm.briefings_bootstrap``) because this module's only contract
# with the bootstrap is the registry's render-provider slot — we don't
# want the route taking a hard dep on the bootstrap module.
_MORNING_BRIEFING_PLUGIN_NAME = "morning_briefing"
_MORNING_BRIEFING_TYPE_NAME = "morning"


def _artifact_to_response(
    type_name: str,
    artifact: Any,
) -> BriefingResponse:
    """Translate a registry :class:`BriefingArtifact` into the wire model."""
    return BriefingResponse(
        type=type_name,
        generated_at=_parse_iso(artifact.generated_at or ""),
        date_local=artifact.date_local or None,
        mode=artifact.mode,
        markdown=artifact.markdown,
        metadata=dict(artifact.metadata or {}),
    )


def _morning_available(config: Any) -> bool:
    """Return True iff the morning-briefing render provider is usable now.

    Two gates:

    1. ``config.plugins.disabled`` must not contain ``morning_briefing``.
       This is the **per-request** disable check (Codex round-9 on
       #2059): ``create_app`` reloads config every request (#2056),
       so an operator editing ``pollypm.toml`` mid-run must see
       ``available=false`` without restart even if the registry slot
       is still populated from startup.
    2. A render provider must be registered for the ``morning`` type
       (the ``morning_briefing`` plugin's ``initialize`` hook /
       :mod:`pollypm.briefings_bootstrap` installs it).

    Both gates also indirectly cover the legacy
    ``is_briefing_provider_registered`` semantics: when the plugin
    bootstrap saw a disabled config it clears the registry slot, so
    requirement (2) fails too. We check (1) explicitly so a request
    that arrives between a config edit and the next bootstrap call
    (in practice never happens — bootstrap runs at startup only —
    but the contract should not depend on that ordering) still
    reports correctly.
    """
    from pollypm.briefings_registry import (
        get_briefing_render_provider,
        is_plugin_disabled_in_config,
    )

    if is_plugin_disabled_in_config(config, _MORNING_BRIEFING_PLUGIN_NAME):
        return False
    return get_briefing_render_provider(_MORNING_BRIEFING_TYPE_NAME) is not None


def _morning_render_last(config: Any) -> BriefingResponse | None:
    """Read the newest morning briefing via the registered render provider.

    Honors the per-request plugin-disable check first (Codex round-9 on
    #2059): a config flip to ``[plugins].disabled = ["morning_briefing"]``
    must make render return ``None`` immediately, even if the registry
    slot still references the previously-installed provider. The route
    layer translates ``None`` into 404 / 503 per its existing rules.
    """
    from pollypm.briefings_registry import (
        get_briefing_render_provider,
        is_plugin_disabled_in_config,
    )

    if is_plugin_disabled_in_config(config, _MORNING_BRIEFING_PLUGIN_NAME):
        return None
    provider = get_briefing_render_provider(_MORNING_BRIEFING_TYPE_NAME)
    if provider is None:
        return None
    artifact = provider.render_last(config)
    if artifact is None:
        return None
    return _artifact_to_response(_MORNING_BRIEFING_TYPE_NAME, artifact)


def _morning_regenerate(config: Any, body: RegenerateRequest) -> BriefingResponse:
    """Force-fire the morning briefing via the registered render provider.

    Per-request plugin-disable check runs first (Codex round-9 on
    #2059) — a disabled config returns 503 without ever calling into
    the provider. ``ValueError`` from the provider (the morning
    briefing's signal that ``project`` narrowing is not supported) is
    translated into a typed 400; any other provider exception becomes
    a 503 ``service_unavailable``.
    """
    from pollypm.briefings_registry import (
        get_briefing_render_provider,
        is_plugin_disabled_in_config,
    )

    if is_plugin_disabled_in_config(config, _MORNING_BRIEFING_PLUGIN_NAME):
        raise service_unavailable(
            f"{_MORNING_BRIEFING_PLUGIN_NAME} is disabled in [plugins].disabled",
            hint=(
                "Remove `morning_briefing` from [plugins].disabled and "
                "re-trigger; or rely on the next briefing tick."
            ),
        )
    provider = get_briefing_render_provider(_MORNING_BRIEFING_TYPE_NAME)
    if provider is None:
        raise service_unavailable(
            "morning briefing provider is not registered",
            hint=(
                "Enable the morning_briefing plugin and restart pm serve."
            ),
        )

    try:
        artifact = provider.regenerate(config, project=body.project)
    except ValueError as exc:
        # Spec §9.2: provider signals "project narrowing not supported"
        # via ValueError. Translate to a typed 400 with the route's
        # canonical hint (clients should omit `project` for morning).
        raise invalid_request(
            str(exc) or "project-scoped morning briefings are not implemented",
            hint=(
                "Omit the `project` field — the morning briefing is "
                "always whole-workspace. Per-project briefings will land "
                "with the plugin-contributed briefing types."
            ),
        ) from exc
    return _artifact_to_response(_MORNING_BRIEFING_TYPE_NAME, artifact)


_REGISTRY: dict[str, _BriefingAdapter] = {
    "morning": _BriefingAdapter(
        name="morning",
        description=(
            "Daily morning briefing — yesterday's progress, today's "
            "priorities, watch items. Fires at the configured local hour."
        ),
        available=_morning_available,
        render_last=_morning_render_last,
        regenerate=_morning_regenerate,
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        candidate = value.replace("Z", "+00:00") if value.endswith("Z") else value
        parsed = datetime.fromisoformat(candidate)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _lookup_type(type_name: str) -> _BriefingAdapter:
    """Resolve a briefing type or raise 404."""
    adapter = _REGISTRY.get(type_name)
    if adapter is None:
        raise not_found(
            f"Unknown briefing type: {type_name!r}",
            hint=(
                "Use GET /api/v1/briefings to list available types. "
                f"Known types: {sorted(_REGISTRY)}"
            ),
        )
    return adapter


def _timeout_error(type_name: str, seconds: float) -> APIError:
    return APIError(
        status_code=504,
        code="timeout",
        message=(
            f"briefing {type_name!r} regenerate exceeded the "
            f"{seconds:.0f}s sync budget"
        ),
        hint=(
            "The provider is slow (LLM/network stall). Retry; future "
            "phases will add ?async=true with a job registry (spec §2.7)."
        ),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/briefings",
    response_model=BriefingTypesResponse,
    summary="List available briefing types",
    operation_id="listBriefingTypes",
    # #2059 round-8: route decorators must enumerate the same error
    # surface as ``docs/api/openapi.yaml`` so the live ``/openapi.json``
    # served by ``pm serve`` matches the static spec generated clients
    # consume. Without these entries FastAPI only advertises 200+422
    # and the conformance check passes vacuously.
    responses={
        "401": {"description": "Missing or invalid bearer token."},
    },
)
def list_briefing_types_endpoint(config: ConfigDep) -> BriefingTypesResponse:
    """GET /api/v1/briefings — discover briefing types.

    Returns every type in the local registry, with ``available=false``
    for types whose provider plugin isn't loaded. Clients can render a
    "morning briefing not configured" hint without trying to regenerate
    and getting a 5xx.
    """
    types = [
        BriefingTypeInfo(
            name=adapter.name,
            description=adapter.description,
            available=adapter.is_available(config),
        )
        for adapter in _REGISTRY.values()
    ]
    return BriefingTypesResponse(types=types)


@router.get(
    "/briefings/{type_name}",
    response_model=BriefingResponse,
    summary="Render the last generated briefing of this type",
    operation_id="renderBriefing",
    # #2059 round-8: mirror static yaml :2032-2048 so the live OpenAPI
    # exposes the typed 404 (no briefing rendered yet) and 503
    # (provider plugin not loaded) branches generated clients depend
    # on. See ``test_briefings_runtime_openapi_matches_static_error_codes``.
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "No briefing has been generated for this type."},
        "503": {"description": "Provider plugin not loaded for this briefing type."},
    },
)
def render_briefing_endpoint(
    type_name: str,
    config: ConfigDep,
) -> BriefingResponse:
    """GET /api/v1/briefings/{type_name} — newest cached briefing.

    Returns 404 ``not_found`` when no briefing has ever been generated
    for this type (Phase 1 fail-soft posture: the cockpit treats
    "missing" identically to "plugin not loaded"; the API surfaces it
    as a typed 404 so the client can show a "regenerate" affordance).
    """
    adapter = _lookup_type(type_name)
    if not adapter.is_available(config):
        raise service_unavailable(
            f"Briefing type {type_name!r} is registered but its provider "
            "is not loaded",
            hint=(
                "Enable the corresponding plugin in pollypm.toml "
                "(e.g. morning_briefing) and restart pm serve."
            ),
        )
    response = adapter.render_last(config)
    if response is None:
        raise not_found(
            f"No briefing has been generated for type {type_name!r}",
            hint=(
                "POST /api/v1/briefings/"
                f"{type_name}/regenerate to produce one now."
            ),
        )
    return response


@router.post(
    "/briefings/{type_name}/regenerate",
    response_model=BriefingResponse,
    summary="Force regenerate a briefing (sync)",
    operation_id="regenerateBriefing",
    # #2059 round-8: mirror static yaml :2079-2124 — the regenerate
    # path has the richest error surface (400 invalid request, 404
    # unknown type, 409 in-flight scope conflict, 503 provider
    # unavailable, 504 sync timeout). Without these entries the live
    # OpenAPI only listed 200+422 and clients had no way to branch on
    # the typed envelopes the handler actually raises.
    responses={
        "400": {
            "description": (
                "Invalid request — empty/whitespace ``project`` or "
                "``project`` passed with ``type_name='morning'`` "
                "(morning is whole-workspace only in Phase 2)."
            ),
        },
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Unknown briefing type."},
        "409": {
            "description": (
                "A regenerate for this ``(type, project)`` scope is "
                "already running on the shared executor; retry after "
                "it completes."
            ),
        },
        "503": {
            "description": (
                "Provider plugin not loaded or backing store "
                "unavailable for this briefing type."
            ),
        },
        "504": {
            "description": (
                "Sync regenerate exceeded ``timeout_seconds``. The "
                "worker keeps running on the shared executor so "
                "retries (after it finishes) do not duplicate work."
            ),
        },
    },
)
def regenerate_briefing_endpoint(
    type_name: str,
    config: ConfigDep,
    request: Request,
    body: RegenerateRequest | None = None,
    timeout_seconds: Annotated[float, Query(
        ge=1.0, le=600.0,
        description=(
            "Sync regenerate budget in seconds. Capped at 600. Default "
            f"{DEFAULT_REGENERATE_TIMEOUT_SECONDS:.0f}s per spec §2.7."
        ),
    )] = DEFAULT_REGENERATE_TIMEOUT_SECONDS,
) -> BriefingResponse:
    """POST /api/v1/briefings/{type_name}/regenerate — force regen.

    Runs the regenerate path on a shared process-wide thread pool with
    a wall-clock timeout. On timeout returns ``504 timeout`` per spec
    §2.7 **immediately** — the worker keeps running in the background,
    so the next-request retry (or the operator) doesn't have to wait
    for the slow provider to finish.

    Duplicate retries for the same ``(type, project)`` while a render
    is still in flight are rejected with ``409 conflict``; this stops
    a panicked client from queuing N redundant regenerates onto the
    shared pool (Codex round-1 P0 on #2059).
    """
    adapter = _lookup_type(type_name)
    if not adapter.is_available(config):
        raise service_unavailable(
            f"Briefing type {type_name!r} is registered but its provider "
            "is not loaded",
            hint=(
                "Enable the corresponding plugin in pollypm.toml "
                "(e.g. morning_briefing) and restart pm serve."
            ),
        )

    request_body = body or RegenerateRequest()
    if request_body.project is not None and not request_body.project.strip():
        # An empty string vs. unset is almost always a client bug — the
        # caller probably meant ``null``. Surface as 400 so the mistake
        # is visible rather than silently ignored.
        raise invalid_request(
            "body.project must be a non-empty string or omitted",
            hint="Drop the field entirely for whole-workspace regenerate.",
        )

    # In-flight key — separate by project so e.g. ``morning`` and a
    # future ``weekly/projA`` can run concurrently while preventing
    # duplicates of the same scope.
    inflight_key = (type_name, request_body.project or "")
    executor = _get_executor(request)
    inflight, inflight_lock = _get_inflight(request)
    with inflight_lock:
        existing = inflight.get(inflight_key)
        if existing is not None and not existing.done():
            raise conflict(
                (
                    f"briefing {type_name!r} regenerate already in "
                    "progress for this scope"
                ),
                hint=(
                    "Wait for the running regenerate to finish, then "
                    "retry. Concurrent regenerates of the same scope "
                    "are deduplicated to protect the shared executor."
                ),
            )
        future = executor.submit(adapter.regenerate, config, request_body)
        inflight[inflight_key] = future

    # Detach the in-flight entry once the worker finishes — runs on
    # the executor's thread, so cleanup happens whether the request
    # 504'd or returned normally. Wrapped in its own try so a bookkeeping
    # exception can never propagate into the worker's result.
    #
    # We also observe ``future.exception()`` / completion here so that
    # late terminal state — after the HTTP client already saw 504 — is
    # logged. Without this, a provider that fails minutes after the 504
    # is operationally invisible (Codex round-5 on #2059). We log the
    # type/scope + repr(exc) only; the adapter is responsible for
    # ensuring its exception message does not leak secrets.
    inflight_key_log = inflight_key

    def _on_regenerate_done(fut: concurrent.futures.Future[Any]) -> None:
        with inflight_lock:
            current = inflight.get(inflight_key)
            if current is fut:
                inflight.pop(inflight_key, None)
        try:
            if fut.cancelled():
                logger.info(
                    "briefing regenerate %r cancelled", inflight_key_log,
                )
                return
            exc = fut.exception()
        except concurrent.futures.CancelledError:
            logger.info(
                "briefing regenerate %r cancelled", inflight_key_log,
            )
            return
        if exc is not None:
            logger.warning(
                "briefing regenerate %r failed after worker completed: %r",
                inflight_key_log,
                exc,
            )
        else:
            logger.info(
                "briefing regenerate %r completed", inflight_key_log,
            )

    future.add_done_callback(_on_regenerate_done)

    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        # Crucial: do NOT block on the future. The worker keeps
        # running on the shared pool; the in-flight registry keeps
        # tracking it so retries 409 instead of stacking. Returning
        # here in <timeout_seconds + epsilon> is the contract.
        raise _timeout_error(type_name, timeout_seconds) from exc
    except APIError:
        # The adapter already raised a typed error — propagate.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "briefings: regenerate failed for type %s", type_name,
        )
        raise service_unavailable(
            f"briefing {type_name!r} regenerate failed: {exc}",
            hint="Check the provider plugin logs.",
        ) from exc


__all__ = [
    "BriefingResponse",
    "BriefingTypeInfo",
    "BriefingTypesResponse",
    "DEFAULT_REGENERATE_TIMEOUT_SECONDS",
    "RegenerateRequest",
    "list_briefing_types_endpoint",
    "regenerate_briefing_endpoint",
    "render_briefing_endpoint",
    "router",
]
