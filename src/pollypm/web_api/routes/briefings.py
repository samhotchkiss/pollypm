"""Briefings endpoints (Phase 2 — §9 of the phase-2 endpoints spec).

Implements three routes under ``/api/v1/briefings``:

- ``GET  /api/v1/briefings``                       — list briefing types
- ``GET  /api/v1/briefings/{type_name}``           — render last generated
- ``POST /api/v1/briefings/{type_name}/regenerate`` — force regenerate

The phase-2 spec (``~/Desktop/pollypm-phase2-endpoints-spec.md`` §9)
calls for a small, sync-only surface in this PR: long-running renders
exceed a 30 s budget and return ``504 timeout`` (§2.7 / §9.2 third
bullet). ``?async=true`` + a job registry is deferred to Phase 2.5.

The route is a thin adapter over the existing briefing seam:

- :func:`pollypm.briefings_registry.list_briefings` powers the discovery
  endpoint (presence of a registered provider implies the type is
  available).
- :func:`pollypm.plugins_builtin.morning_briefing.inbox.list_briefings`
  + :func:`pollypm.plugins_builtin.morning_briefing.inbox.read_briefing`
  back the render endpoint (newest cached entry on disk).
- :func:`pollypm.plugins_builtin.morning_briefing.handlers.briefing_tick.fire_briefing`
  is the regenerate hook, wrapped in a thread-pool timeout so a slow
  provider surfaces as ``504 timeout`` rather than hanging the request.

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


def _morning_available(config: Any) -> bool:  # noqa: ARG001 — registry is module-level
    """Return True iff the morning-briefing plugin is registered.

    The :mod:`pollypm.briefings_registry` seam holds the active provider
    callable; ``None`` means the plugin tree isn't loaded (config didn't
    enable the plugin, or ``[plugins].disabled`` includes
    ``morning_briefing``) and the type is reported as ``available=false``
    so clients know not to attempt regenerate. Uses the public
    :func:`is_briefing_provider_registered` helper instead of reaching
    into the module-private slot (Codex round-3 on PR #2059).
    """
    from pollypm.briefings_registry import is_briefing_provider_registered

    return is_briefing_provider_registered()


def _morning_render_last(config: Any) -> BriefingResponse | None:
    """Read the newest morning briefing off disk (spec §9.1 ``last``)."""
    from pollypm.plugins_builtin.morning_briefing import inbox as _inbox

    base_dir = config.project.base_dir
    entries = _inbox.list_briefings(base_dir, status="all", limit=1)
    if not entries:
        return None
    entry = entries[0]
    read = _inbox.read_briefing(base_dir, entry.date_local)
    if read is None:
        # The metadata json existed during list_briefings but the body
        # disappeared between calls (rare — atomic-write replaces both;
        # treat as missing so the caller can regenerate).
        return None
    _entry, markdown = read
    return BriefingResponse(
        type="morning",
        generated_at=_parse_iso(_entry.created_at),
        date_local=_entry.date_local,
        mode=_entry.mode or None,
        markdown=markdown,
        metadata={
            "status": _entry.status,
            "pinned": _entry.pinned,
            "yesterday": _entry.yesterday,
            "priorities": list(_entry.priorities),
            "watch": list(_entry.watch),
            **dict(_entry.meta),
        },
    )


def _morning_regenerate(config: Any, body: RegenerateRequest) -> BriefingResponse:
    """Force-fire the morning briefing pipeline (spec §9.1 ``regenerate``).

    Mirrors ``pm briefing now`` (see
    :mod:`pollypm.plugins_builtin.morning_briefing.cli`): runs the full
    gather → synthesize → emit chain and writes to the inbox. The
    ``project`` body field is rejected with 400 for the morning type —
    morning briefings are whole-workspace only today (Phase 2 round-1
    Codex feedback on #2059: the field used to be silently ignored,
    which let a client think they'd narrowed scope when they hadn't).
    """
    from pollypm.plugins_builtin.morning_briefing.handlers import (
        briefing_tick as _tick,
    )
    from pollypm.plugins_builtin.morning_briefing.settings import (
        load_briefing_settings,
    )
    from pollypm.plugins_builtin.morning_briefing.state import load_state
    from pollypm.config import DEFAULT_CONFIG_PATH, resolve_config_path
    from pollypm.tz import get_timezone

    if body.project is not None:
        raise invalid_request(
            "project-scoped morning briefings are not implemented",
            hint=(
                "Omit the `project` field — the morning briefing is "
                "always whole-workspace. Per-project briefings will land "
                "with the plugin-contributed briefing types."
            ),
        )

    base_dir = config.project.base_dir
    project_root = config.project.root_dir

    # ``load_briefing_settings`` reads ``[briefing]`` overrides off the
    # toml. Prefer the path the running ``pm serve`` actually loaded
    # (``config.config_path``) so a non-default ``--config`` flag is
    # honored. Falling back to ``DEFAULT_CONFIG_PATH`` keeps the path
    # working in tests that construct ``PollyPMConfig`` directly without
    # touching disk. (Codex round-1 P0 on #2059: the old code always
    # read ``DEFAULT_CONFIG_PATH``, so ``pm serve --config /tmp/foo``
    # would write into ``foo``'s ``base_dir`` while honoring the
    # default global TOML's briefing hour/timezone/quiet-mode.)
    config_path = getattr(config, "config_path", None) or DEFAULT_CONFIG_PATH
    settings = load_briefing_settings(resolve_config_path(config_path))

    fallback_tz = getattr(config.pollypm, "timezone", "") or ""
    timezone = get_timezone(fallback_tz)
    now_local = datetime.now(timezone)

    state = load_state(base_dir)

    result = _tick.fire_briefing(
        project_root=project_root,
        base_dir=base_dir,
        settings=settings,
        now_local=now_local,
        state=state,
        config=config,
        emit_to_inbox=True,
    )
    if not isinstance(result, dict) or not result.get("fired"):
        reason = (
            str(result.get("reason"))
            if isinstance(result, dict) else "unknown"
        )
        raise service_unavailable(
            f"briefing regenerate did not fire ({reason})",
            hint=(
                "Check the morning_briefing plugin logs; the gather / "
                "synthesize stage declined to produce a draft."
            ),
        )
    draft = result.get("draft") or {}
    return BriefingResponse(
        type="morning",
        generated_at=now_local.astimezone(UTC),
        date_local=str(draft.get("date_local") or ""),
        mode=str(draft.get("mode") or "") or None,
        markdown=str(draft.get("markdown") or ""),
        metadata={
            "emitted_to_inbox": bool(result.get("emitted", False)),
            "yesterday": draft.get("yesterday"),
            "priorities": list(draft.get("priorities") or []),
            "watch": list(draft.get("watch") or []),
            "quiet_mode": bool(result.get("quiet_mode", False)),
            **dict(draft.get("meta") or {}),
        },
    )


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
