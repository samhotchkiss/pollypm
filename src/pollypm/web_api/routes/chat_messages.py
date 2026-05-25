"""GET chat-history endpoints (Phase 1 — P2 of the chat-endpoints spec).

Implements:

- ``GET /api/v1/chat/sessions`` — discover every chat surface.
- ``GET /api/v1/chat/{session_name}/messages`` — paginated history
  for one surface, with optional inline-subagent expansion + tmux
  capture fallback.

Per ``~/Desktop/pollypm-chat-endpoints-spec.md`` §2.1 / §2.2 / §3 / §4.
The router is a thin adapter over :mod:`pollypm.web_api.chat`:

- :func:`pollypm.web_api.chat.enumerate_chat_surfaces` powers the
  discovery endpoint.
- :func:`pollypm.web_api.chat.parse_events_jsonl` powers the JSONL
  branch of the history endpoint.
- :func:`pollypm.web_api.chat.capture_envelopes` is the tmux
  capture fallback driven by ``?source=capture`` or by ``?source=auto``
  when the archive is stale (>60 s, per spec §4.7).

The router never imports the work-service directly when the request
doesn't need worker surfaces; per-task workers are opted into by
opening a read-only work-service handle (matches the pattern used by
:mod:`pollypm.web_api.service`).
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from pollypm.tmux.client import TmuxClient
from pollypm.web_api.chat import (
    STALE_THRESHOLD_SECONDS,
    ChatSurface,
    MessageEnvelope,
    MessageRole,
    MessageType,
    SurfaceType,
    capture_envelopes,
    enumerate_chat_surfaces,
    find_chat_surface,
    is_archive_stale,
    parse_events_jsonl,
    parse_events_jsonl_tail,
    parse_raw_subagent_jsonl,
)
from pollypm.web_api.errors import APIError, service_unavailable
from pollypm.web_api.routes._deps import ConfigDep
from pollypm.work.task_state import parse_task_window_name

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])


# ---------------------------------------------------------------------------
# Limits + defaults (kept module-level so tests can patch)
# ---------------------------------------------------------------------------


# Spec §2.2: ``limit`` default 100, cap 500.
DEFAULT_MESSAGE_LIMIT = 100
MAX_MESSAGE_LIMIT = 500

# Issue #2070: ``desc`` + small-limit + no-cursor + ``source=auto`` is
# the common cockpit-poll shape. For requests under this threshold we
# tail-read the JSONL archive instead of forward-readlining the full
# file. The forward parser stays authoritative for ``asc``, cursor
# paging, and the strict ``source=jsonl`` validation path.
TAIL_READ_LIMIT_THRESHOLD = 200


SourceMode = Literal["auto", "jsonl", "capture"]
Direction = Literal["asc", "desc"]


def _is_worker_session(session_name: str) -> bool:
    """Return True iff ``session_name`` matches the worker naming pattern.

    Delegates to the canonical
    :func:`pollypm.work.task_state.parse_task_window_name` so the
    chat-messages router stays in sync with the launcher / recovery
    sweep / worker-marker reaper. The canonical parser accepts
    ``task-<project>-<N>`` (with arbitrary characters in the project
    slug as long as the trailing ``-N`` digits exist) and returns
    ``None`` for everything else.
    """
    return parse_task_window_name(session_name) is not None


class _WorkerFacadeUnavailable(Exception):
    """Raised by ``_build_work_service_stub`` when the facade can't be opened.

    Distinct from "facade returned no records" (which collapses to
    ``None``). Lets ``_find_surface`` translate a transient pg-pool
    outage into a typed 503 ``service_unavailable`` instead of a
    misleading 404 ``session_unknown`` (blocker 3).
    """


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ChatSurfaceWindow(BaseModel):
    """tmux window state for one surface (spec §2.1)."""

    tmux_session: str
    window_name: str
    present: bool = False
    pane_id: str | None = None
    pane_dead: bool = False


class ChatSurfaceTranscript(BaseModel):
    """Transcript locator for one surface (spec §2.1).

    ``source`` is ``"jsonl"`` when an ``events.jsonl`` archive exists
    on disk; ``null`` when there's no archive yet (brand-new surface
    per spec §4.8). The discovery endpoint never probes tmux for the
    capture fallback — that decision belongs to the history endpoint.
    """

    source: str | None = None
    path: str | None = None


class ChatSurfaceResponse(BaseModel):
    """One entry in the ``GET /sessions`` response (spec §2.1)."""

    session_name: str
    surface_type: str
    persona: str | None = None
    project: str | None = None
    task_id: int | None = None
    window: ChatSurfaceWindow
    transcript: ChatSurfaceTranscript
    cwd: str | None = None
    provider: str = ""
    auth_token_present: bool = False
    worktree_path: str | None = None


class ChatSessionsResponse(BaseModel):
    """``GET /api/v1/chat/sessions`` envelope."""

    sessions: list[ChatSurfaceResponse]


_CHAT_SESSIONS_TTL_SECONDS = 1.0
_CHAT_SESSIONS_LOCK = threading.Lock()
_CHAT_SESSIONS_CACHE: dict[
    tuple[int, int, int, int],
    tuple[float, tuple[ChatSurfaceResponse, ...]],
] = {}


class ChatMessageEnvelope(BaseModel):
    """Wire form of :class:`MessageEnvelope` (spec §3).

    The dataclass version is kept for internal callers; this Pydantic
    model exists so the OpenAPI document carries the right schema and
    FastAPI handles the JSON serialization uniformly.
    """

    id: str
    ts: str
    role: MessageRole
    actor: str
    type: MessageType
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatMessagesResponse(BaseModel):
    """``GET /api/v1/chat/{session_name}/messages`` envelope (spec §2.2)."""

    session_name: str
    surface_type: str
    persona: str | None = None
    transcript_source: str | None = None
    messages: list[ChatMessageEnvelope]
    has_more: bool = False
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Typed errors (spec §2.3 codes reused for symmetry with chat_send)
# ---------------------------------------------------------------------------


def _session_unknown(session_name: str) -> APIError:
    return APIError(
        status_code=404,
        code="session_unknown",
        message=f"No chat surface registered for session: {session_name!r}",
        hint=(
            "Use GET /api/v1/chat/sessions to discover registered surfaces, "
            "or check config.sessions / work-service for per-task workers."
        ),
    )


def _window_missing(target: str) -> APIError:
    return APIError(
        status_code=503,
        code="window_missing",
        message=f"Window not present in tmux: {target}",
        hint=(
            "The session is configured but its tmux window is not running. "
            "Re-run `pm up` or check `pm sessions`."
        ),
    )


def _archive_missing(session_name: str) -> APIError:
    return APIError(
        status_code=404,
        code="archive_missing",
        message=(
            f"No events.jsonl archive on disk for session {session_name!r} "
            "(source=jsonl was requested)."
        ),
        hint="Drop ?source=jsonl to fall back to tmux capture, or wait for the ingestor to flush.",
    )


def _archive_unreadable(session_name: str, detail: str) -> APIError:
    """Spec §2.3 — explicit ``source=jsonl`` against an unreadable archive.

    Returned as 503 (not 4xx): the archive exists on disk but the
    process can't read it (permissions, transient I/O fault, mounted
    volume gone). That's a server-side condition the caller can retry,
    not a request-shape error (round-5 blocker 2).
    """
    return APIError(
        status_code=503,
        code="archive_unreadable",
        message=(
            f"events.jsonl archive for session {session_name!r} is "
            f"present but cannot be read: {detail}"
        ),
        hint=(
            "Check filesystem permissions on the archive, or drop "
            "?source=jsonl to fall back to tmux capture."
        ),
    )


def _capture_unavailable(session_name: str) -> APIError:
    return APIError(
        status_code=503,
        code="capture_unavailable",
        message=(
            f"tmux client unavailable; cannot satisfy source=capture for "
            f"session {session_name!r}."
        ),
        hint="Install tmux or drop ?source=capture to fall back to the JSONL archive.",
    )


def _capture_failed(session_name: str, detail: str) -> APIError:
    return APIError(
        status_code=503,
        code="capture_failed",
        message=(
            f"tmux capture failed for session {session_name!r}: {detail}"
        ),
        hint="Retry shortly; check tmux server health with `pm sessions`.",
    )


def _invalid_query(field: str, message: str) -> APIError:
    return APIError(
        status_code=400,
        code="invalid_request",
        message=f"Invalid {field}: {message}",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_work_service_stub(config: Any) -> Any | None:
    """Return a duck-typed stub the registry can call for workers (fail-soft).

    The chat registry only needs ``list_worker_sessions(active_only=...)``
    on the work-service. Rather than reach into a private context
    manager, we call the public
    :func:`pollypm.web_api.service.list_active_worker_sessions` facade
    once and hand the registry a tiny adapter that re-emits those
    records. Returns ``None`` when the facade yields no records — the
    registry then skips worker enumeration entirely.

    This (non-strict) variant is used by discovery: any unexpected
    import/runtime error collapses to ``None`` so the response still
    returns configured surfaces and never 500s. For the strict-mode
    worker lookup path (where pg outages must surface as 503 instead
    of 404), use :func:`_build_work_service_stub_strict` — it bypasses
    the public facade so transient facade failures aren't swallowed
    into an empty list (round-5 blocker).
    """
    try:
        from pollypm.web_api.service import list_active_worker_sessions
    except Exception:  # noqa: BLE001
        return None
    try:
        records = list_active_worker_sessions(config)
    except Exception:  # noqa: BLE001
        logger.debug(
            "chat_messages: list_active_worker_sessions failed; "
            "skipping worker surfaces",
            exc_info=True,
        )
        return None
    if not records:
        return None
    return _WorkerSessionStub(records)


def _build_work_service_stub_strict(
    config: Any, *, project: str | None = None
) -> Any | None:
    """Strict variant: surfaces facade outages instead of swallowing them.

    Consumes the public
    :func:`pollypm.web_api.service.list_active_worker_sessions_strict`
    facade — the fail-soft sibling :func:`list_active_worker_sessions`
    collapses pg-pool outages to ``[]`` (fail-open posture for
    discovery), but explicit per-session worker lookups need to tell
    "no workers right now" apart from "the work-service can't be
    opened" so the route can map the outage to a typed 503
    ``service_unavailable`` instead of a misleading 404
    ``session_unknown`` (round-6 blocker — moves the previously-inline
    ``_open_work_service_readonly`` call behind the public service
    boundary so the route layer doesn't reach into private helpers).

    Facade errors propagate as :class:`_WorkerFacadeUnavailable`. An
    empty result list still collapses to ``None`` (matches the
    non-strict contract: the registry skips worker enumeration when
    the stub is ``None``).
    """
    try:
        from pollypm.web_api.service import (
            WorkServiceFacadeUnavailable,
            list_active_worker_sessions_strict,
        )
    except Exception as exc:  # noqa: BLE001
        raise _WorkerFacadeUnavailable(
            "list_active_worker_sessions_strict import failed"
        ) from exc
    try:
        records = list_active_worker_sessions_strict(config, project=project)
    except WorkServiceFacadeUnavailable as exc:
        raise _WorkerFacadeUnavailable(str(exc)) from exc
    if not records:
        return None
    return _WorkerSessionStub(records)


class _WorkerSessionStub:
    """Minimal stand-in exposing ``list_worker_sessions`` for the registry.

    ``enumerate_worker_surfaces`` in
    :mod:`pollypm.web_api.chat.registry` only calls
    ``list_worker_sessions(active_only=True)`` on the work-service it
    receives, so we expose exactly that method and ignore the
    ``active_only`` flag (the public facade always filters to active
    records).
    """

    def __init__(self, records: list[Any]) -> None:
        self._records = list(records)

    def list_worker_sessions(
        self,
        *,
        active_only: bool = True,  # noqa: ARG002 — registry-API compat
        project: str | None = None,
    ) -> list[Any]:
        if project is None:
            return list(self._records)
        return [
            record for record in self._records
            if getattr(record, "task_project", None) == project
        ]


def _build_tmux_client() -> TmuxClient | None:
    """Construct a :class:`TmuxClient` for window-presence probing.

    Returns ``None`` if tmux isn't installed (or the client init
    raises) — the registry treats a missing client as "presence
    unknown" and returns surfaces with ``window.present=false``.
    """
    try:
        return TmuxClient()
    except Exception:  # noqa: BLE001
        logger.debug("chat_messages: TmuxClient init failed; presence unknown", exc_info=True)
        return None


def _find_surface(
    config: Any,
    session_name: str,
    *,
    include_workers: bool | None = None,
) -> ChatSurface:
    """Resolve ``session_name`` to a :class:`ChatSurface` or raise.

    Hot-path optimization (blocker 3): when ``session_name`` doesn't
    match the worker pattern (``task-{project}-{N}``), we skip the
    work-service open entirely — operator/architect/advisor surfaces
    are always discoverable from ``config`` alone, so probing pg for
    every lookup is pure overhead and turns a pg-pool outage into a
    misleading 404 ``session_unknown``.

    Callers may pin the behavior explicitly via ``include_workers``;
    when unset (default) the helper infers from the session name.

    For genuine worker lookups, the facade is opened in strict mode:
    if pg is unreachable we raise 503 ``service_unavailable`` instead
    of falling through to 404 ``session_unknown``, which would tell
    the client to give up rather than retry.
    """
    if include_workers is None:
        include_workers = _is_worker_session(session_name)
    tmux_client = _build_tmux_client()
    work_service: Any | None = None
    worker_project: str | None = None
    if include_workers:
        parsed = parse_task_window_name(session_name)
        worker_project = parsed[0] if parsed is not None else None
        try:
            try:
                work_service = _build_work_service_stub_strict(
                    config, project=worker_project
                )
            except TypeError:
                # Older tests / adapters predate the project-scoped
                # keyword. Fall back to the original strict call shape;
                # the registry still filters the returned records.
                work_service = _build_work_service_stub_strict(config)
        except _WorkerFacadeUnavailable as exc:
            raise service_unavailable(
                f"work-service unavailable; cannot resolve worker session "
                f"{session_name!r}",
                hint=(
                    "The per-task worker registry depends on the work-service. "
                    "Retry shortly; check `pm sessions` / pg pool health."
                ),
            ) from exc
    surface = find_chat_surface(
        config,
        session_name,
        work_service=work_service,
        tmux_client=tmux_client,
    )
    if surface is not None and surface.session_name == session_name:
        return surface
    # Compatibility fallback for tests and unusual callers that patch the
    # broad enumerator directly. The normal configured-session hot path
    # returns above and never pays this workspace-wide scan.
    surfaces = enumerate_chat_surfaces(
        config,
        work_service=work_service,
        tmux_client=tmux_client,
    )
    for candidate in surfaces:
        if candidate.session_name == session_name:
            return candidate
    raise _session_unknown(session_name)


def _parse_since(since: str | None) -> datetime | None:
    """Parse the ``?since`` ISO-8601 timestamp or raise 400.

    Accepts both ``...Z`` and ``...+00:00`` suffix shapes (the
    transcript writer normalises to ``Z`` but clients may roundtrip
    through ``datetime.isoformat`` which emits ``+00:00``).
    """
    if not since:
        return None
    try:
        candidate = since.replace("Z", "+00:00") if since.endswith("Z") else since
        return datetime.fromisoformat(candidate)
    except (TypeError, ValueError) as exc:
        raise _invalid_query("since", f"not an ISO-8601 timestamp: {since!r}") from exc


def _parse_envelope_ts(ts: str) -> datetime | None:
    """Lenient ISO-8601 parse for envelope timestamps. ``None`` on failure.

    Always returns a timezone-aware ``datetime`` when parse succeeds:
    naive timestamps are coerced to UTC. This lets callers mix the
    parsed value with the ``datetime.min.replace(tzinfo=timezone.utc)``
    sort sentinel without ``TypeError: can't compare offset-naive and
    offset-aware datetimes`` (blocker 2).
    """
    if not ts:
        return None
    try:
        candidate = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        parsed = datetime.fromisoformat(candidate)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# Sentinel used when sorting envelopes whose ``ts`` is missing /
# unparseable. Timezone-aware so it compares against parsed tz-aware
# timestamps without ``TypeError`` (blocker 2). Unparseable rows sort
# to the head of an ascending sort (matching the prior behavior with
# naive ``datetime.min``) but stay in the page — they just surface
# with ``ts=""`` in the response.
_TS_SORT_FLOOR = datetime.min.replace(tzinfo=timezone.utc)


def _envelope_to_wire(envelope: MessageEnvelope) -> ChatMessageEnvelope:
    """Coerce a dataclass envelope to its Pydantic wire shape."""
    return ChatMessageEnvelope(
        id=envelope.id,
        ts=envelope.ts,
        role=str(envelope.role),
        actor=envelope.actor,
        type=str(envelope.type),
        text=envelope.text,
        metadata=envelope.metadata,
    )


def _surface_to_wire(surface: ChatSurface) -> ChatSurfaceResponse:
    data = surface.to_dict()
    return ChatSurfaceResponse(
        session_name=data["session_name"],
        surface_type=data["surface_type"],
        persona=data.get("persona"),
        project=data.get("project"),
        task_id=data.get("task_id"),
        window=ChatSurfaceWindow(**data["window"]),
        transcript=ChatSurfaceTranscript(**data["transcript"]),
        cwd=data.get("cwd"),
        provider=data.get("provider", ""),
        auth_token_present=bool(data.get("auth_token_present", False)),
        worktree_path=data.get("worktree_path"),
    )


def _load_envelopes(
    surface: ChatSurface,
    *,
    source: SourceMode,
    tail_hint: int | None = None,
    include_thinking: bool = False,
) -> tuple[list[MessageEnvelope], str | None, Path | None]:
    """Return ``(envelopes, transcript_source, transcript_path)``.

    ``transcript_source`` is one of ``"jsonl"`` / ``"capture"`` /
    ``None`` (no transcript yet, spec §4.8).

    ``tail_hint`` (issue #2070): when set to a small positive integer,
    the ``source=auto`` JSONL branch uses ``parse_events_jsonl_tail``
    instead of the full forward parse. The hint is the smallest number
    of envelopes the caller would consider returning (the route uses
    ``limit + 1`` so ``has_more`` can still detect overflow). Strict
    ``source=jsonl`` callers ignore the hint — they want the
    authoritative full parse so unreadable-archive errors propagate
    cleanly. The hint must be paired with ``direction=desc`` + no
    cursor + small ``limit`` at the call site (the helper doesn't
    re-check those conditions).

    ``include_thinking`` (issue #2082): forwarded straight to
    :func:`parse_events_jsonl` / :func:`parse_events_jsonl_tail`. When
    ``True``, Anthropic extended-thinking blocks are surfaced as
    ``MessageType.THINKING`` envelopes; when ``False`` (default), they
    are dropped at parse time. The parser caches separately for each
    flag value, so a default-False request never sees a thinking-True
    cache hit and vice versa.
    """
    archive = surface.transcript_path
    actor_fallback = surface.persona or "agent"

    if source == "jsonl":
        if archive is None or not archive.exists():
            raise _archive_missing(surface.session_name)
        try:
            envelopes = parse_events_jsonl(
                archive,
                actor_fallback=actor_fallback,
                strict=True,
                include_thinking=include_thinking,
            )
        except OSError as exc:
            # The archive exists but we can't read it (permissions,
            # transient I/O fault, etc). Explicit ``source=jsonl``
            # callers asked for the archive specifically — return a
            # typed 503 instead of silently swallowing to ``[]`` and
            # responding ``200 messages=[]`` (round-5 blocker 2).
            raise _archive_unreadable(surface.session_name, str(exc)) from exc
        return envelopes, "jsonl", archive

    if source == "capture":
        # Explicit capture: surface errors instead of returning 200 +
        # empty (spec §4.7 / blocker 5). Missing window → 503
        # window_missing, no tmux client → 503 capture_unavailable,
        # raise → 503 capture_failed.
        envelopes = _capture_for_surface(
            surface,
            actor_fallback=actor_fallback,
            strict=True,
        )
        return envelopes, "capture", None

    # source == "auto" — prefer JSONL, fall back to capture when the
    # archive is missing or stale (spec §4.7).
    if archive is not None and not is_archive_stale(archive):
        if tail_hint is not None and tail_hint > 0:
            # Issue #2070: short cockpit polls only need the last N
            # envelopes; tail-read keeps cold-path latency proportional
            # to the request size instead of the full archive size.
            # The parser falls back to the forward parse on any
            # decode/parse failure, so semantics match the forward
            # path for malformed archives.
            envelopes = parse_events_jsonl_tail(
                archive,
                limit=tail_hint,
                actor_fallback=actor_fallback,
                include_thinking=include_thinking,
            )
        else:
            envelopes = parse_events_jsonl(
                archive,
                actor_fallback=actor_fallback,
                include_thinking=include_thinking,
            )
        if envelopes:
            return envelopes, "jsonl", archive
        # Empty result on a fresh (non-stale) archive could be a
        # legitimately empty transcript OR an unreadable archive whose
        # ``OSError`` the fail-soft parser swallowed (round-6 blocker 5
        # — ``source=auto`` previously returned an empty 200 in that
        # case, with no signal to retry). Re-parse with ``strict=True``
        # to see if the file is actually unreadable; if so, fall
        # through to capture (auto's job). If strict re-parse also
        # returns empty, the archive is genuinely empty and we return
        # the empty page.
        try:
            strict_envelopes = parse_events_jsonl(
                archive,
                actor_fallback=actor_fallback,
                strict=True,
                include_thinking=include_thinking,
            )
        except OSError:
            logger.debug(
                "chat_messages: auto-mode jsonl unreadable for %s; "
                "falling back to capture",
                surface.session_name,
                exc_info=True,
            )
        else:
            return strict_envelopes, "jsonl", archive

    # Stale or missing archive (or unreadable archive in the auto
    # branch above) — try capture, fall back to whatever JSONL we have
    # (better stale data than no data, per spec §4.8 which prefers
    # empty over erroring).
    if include_thinking and archive is not None and archive.exists():
        # tmux capture can only see rendered pane text; Anthropic
        # thinking blocks live exclusively in the normalized JSONL.
        # When the caller explicitly opts in and the archive has any
        # thinking envelope, prefer that archive over stale capture so
        # the request can actually satisfy ``include_thinking=true``.
        envelopes = parse_events_jsonl(
            archive,
            actor_fallback=actor_fallback,
            include_thinking=True,
        )
        if any(env.type == MessageType.THINKING for env in envelopes):
            return envelopes, "jsonl", archive

    captured = _capture_for_surface(surface, actor_fallback=actor_fallback)
    if captured:
        return captured, "capture", None
    if archive is not None and archive.exists():
        envelopes = parse_events_jsonl(
            archive,
            actor_fallback=actor_fallback,
            include_thinking=include_thinking,
        )
        return envelopes, "jsonl", archive
    return [], None, None


def _capture_for_surface(
    surface: ChatSurface,
    *,
    actor_fallback: str,
    strict: bool = False,
) -> list[MessageEnvelope]:
    """Capture the surface's tmux pane into envelopes.

    ``strict=False`` (used by ``source=auto``) keeps the fail-soft
    behavior: every failure mode collapses to ``[]`` so ``auto`` can
    fall back to the JSONL archive (spec §4.7 / §4.8).

    ``strict=True`` (used by explicit ``source=capture``) raises a
    typed :class:`APIError` for each failure mode so the caller gets
    an actionable 503 instead of an empty 200 (blocker 5):

    - missing tmux window → ``window_missing``
    - no tmux client / not installed → ``capture_unavailable``
    - capture call raises → ``capture_failed``
    """
    if not surface.window.present:
        if strict:
            target = f"{surface.window.tmux_session}:{surface.window.window_name}"
            raise _window_missing(target)
        return []
    tmux_client = _build_tmux_client()
    if tmux_client is None:
        if strict:
            raise _capture_unavailable(surface.session_name)
        return []
    target = f"{surface.window.tmux_session}:{surface.window.window_name}"
    try:
        return capture_envelopes(
            tmux_client,
            session_name=surface.session_name,
            target=target,
            actor_fallback=actor_fallback,
            strict=strict,
        )
    except APIError:
        # Already a typed error — propagate without wrapping.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "chat_messages: capture failed for %s", surface.session_name,
            exc_info=True,
        )
        if strict:
            raise _capture_failed(surface.session_name, str(exc)) from exc
        return []


def _apply_filters_and_paginate(
    envelopes: list[MessageEnvelope],
    *,
    since: datetime | None,
    since_id: str | None,
    direction: Direction,
    limit: int,
    post_process: Any = None,
) -> tuple[list[ChatMessageEnvelope], bool, str | None]:
    """Apply spec §2.2 query semantics and return ``(rows, has_more, cursor)``.

    Filtering order (cursor MUST be applied after sort, because
    ``next_cursor`` is the LAST id in the page in the requested
    direction — applying ``since_id`` in source order would slice the
    wrong half for ``direction=desc``, duplicating page 1 on page 2):

    1. ``since`` lower-bound on envelope ``ts``.
    2. Sort by timestamp + position. JSONL ordering is already
       chronological; we re-sort defensively so the response is
       deterministic even when the parser returns shuffled rows.
    3. Reverse for ``direction=desc``.
    4. ``since_id`` cursor walk — strictly after the matching id in
       the ordered sequence (the cursor itself is excluded from the
       slice, matching the ``next_cursor`` semantics of the inbox
       endpoint).
    5. Slice to ``limit`` and compute ``has_more`` / ``next_cursor``.

    NOTE: ``thinking`` envelopes are gated upstream in
    :func:`_load_envelopes` via the ``include_thinking`` flag on the
    parser (#2082) — they never reach this helper unless the caller
    opted in.

    NOTE: subagent inlining lives in
    :func:`_inline_subagent_transcript` and runs in the endpoint after
    pagination — see the doc-block above that function for the
    allowlist + raw-parser contract (issue #2052).
    """
    # Normalize the lower bound to tz-aware UTC so the comparison with
    # parsed timestamps (always tz-aware after _parse_envelope_ts) never
    # mixes naive + aware values (blocker 2).
    since_aware: datetime | None = None
    if since is not None:
        since_aware = (
            since if since.tzinfo is not None
            else since.replace(tzinfo=timezone.utc)
        )

    filtered: list[MessageEnvelope] = []
    for envelope in envelopes:
        if since_aware is not None:
            ts = _parse_envelope_ts(envelope.ts)
            if ts is None:
                # Drop rows we can't compare — they'd otherwise sort
                # to a non-deterministic position relative to ``since``.
                continue
            if ts < since_aware:
                continue
        filtered.append(envelope)

    # Stable sort by (ts, original position). Original position is
    # preserved because Python's sort is stable, but we've already
    # filtered so we capture indices first. Unparseable / missing ``ts``
    # values fall back to a tz-aware sentinel so mixed-shape rows don't
    # raise ``TypeError`` from a naive/aware comparison (blocker 2).
    indexed = list(enumerate(filtered))
    indexed.sort(key=lambda item: (
        _parse_envelope_ts(item[1].ts) or _TS_SORT_FLOOR,
        item[0],
    ))
    ordered = [envelope for _, envelope in indexed]
    if direction == "desc":
        ordered.reverse()

    # Apply cursor AFTER ordering so ``next_cursor`` (the last id of
    # the previous page in the same direction) advances to the next
    # slice instead of slicing source-order and re-emitting page 1.
    if since_id is not None:
        cursor_index = -1
        for idx, envelope in enumerate(ordered):
            if envelope.id == since_id:
                cursor_index = idx
                break
        if cursor_index >= 0:
            ordered = ordered[cursor_index + 1 :]

    has_more = len(ordered) > limit
    page = ordered[:limit]

    next_cursor: str | None = None
    if has_more and page:
        next_cursor = page[-1].id

    if post_process is not None:
        page = [post_process(env) for env in page]

    return [_envelope_to_wire(env) for env in page], has_more, next_cursor


# ---------------------------------------------------------------------------
# Subagent inlining (#2052)
#
# When ``include_subagents=true`` is passed, each ``subagent_result``
# envelope in the page is enriched with ``metadata.subagent_transcript``
# — a list of envelopes parsed from the raw Claude JSONL the parent
# ``task-notification`` block points at (``metadata.output_file``).
#
# Security boundary: ``metadata.output_file`` is supplied by transcript
# content and an authenticated caller who can influence what an agent
# writes could otherwise make the API stat/read arbitrary local paths
# (``/etc/passwd``, ``../../../escape.jsonl``). The inliner constrains
# candidates to :func:`_allowed_transcript_roots` (project + workspace
# transcript dirs) via ``Path.resolve`` + ``is_relative_to``. Paths
# outside every allowed root are skipped silently (debug log, no
# raise) so a malformed transcript can't 500 the endpoint.
#
# Parser asymmetry: subagent JSONLs are the RAW Claude per-session
# stream, not the normalized ``events.jsonl`` archive — we route the
# read through :func:`parse_raw_subagent_jsonl`. See the docstring on
# that function for the shape contract.
# ---------------------------------------------------------------------------


# Cap per-subagent inlined envelopes so a runaway subagent transcript
# can't balloon a single parent response. The cap is generous (matches
# the messages endpoint's hard max) but bounded; callers who need the
# full subagent transcript should query it through its own session.
_SUBAGENT_INLINE_LIMIT = 500


def _allowed_transcript_roots(config: Any) -> list[Path]:
    """Compute directories that may contain subagent transcripts.

    Security boundary for :func:`_inline_subagent`. ``metadata.output_file``
    is supplied by transcript content and must never be trusted as a
    filesystem authority — the inliner only reads paths that resolve
    under one of these roots.

    Roots covered:

    - ``<workspace>/.pollypm/transcripts/`` for the operator workspace.
    - ``<project>/.pollypm/transcripts/`` for every known project in
      ``config.projects`` (same set the chat registry enumerates).

    Each returned root is ``resolve()``d so the membership check lines
    up with a likewise-resolved candidate path.
    """
    from pollypm.projects import project_transcripts_dir

    roots: list[Path] = []
    seen: set[str] = set()

    def _add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        roots.append(resolved)

    project_settings = getattr(config, "project", None)
    if project_settings is not None:
        for attr in ("workspace_root", "root_dir"):
            base = getattr(project_settings, attr, None)
            if isinstance(base, Path):
                _add(project_transcripts_dir(base))

    projects = getattr(config, "projects", None) or {}
    for known in projects.values():
        project_path = getattr(known, "path", None)
        if isinstance(project_path, Path):
            _add(project_transcripts_dir(project_path))

    return roots


def _is_within(candidate: Path, root: Path) -> bool:
    """``Path.is_relative_to`` shim that returns ``False`` instead of raising.

    Both ``candidate`` and ``root`` are expected to already be
    ``resolve()``d so symlinks don't sneak a candidate past the
    boundary.
    """
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _inline_subagent_transcript(
    envelope: MessageEnvelope,
    *,
    allowed_roots: list[Path],
    actor_fallback: str,
) -> MessageEnvelope:
    """Enrich a ``subagent_result`` envelope with its subagent transcript.

    Reads the raw Claude JSONL at ``metadata.output_file`` (set by the
    parser when the parent ``task-notification`` block carries one),
    rejects any candidate outside ``allowed_roots``, and dumps the
    parsed envelopes into ``metadata.subagent_transcript``.

    Failures are silent — a missing, unreadable, or out-of-bounds
    subagent transcript just leaves the envelope unchanged. The chat
    history endpoint never 500s on enrichment.
    """
    if str(envelope.type) != "subagent_result":
        return envelope
    output_file = envelope.metadata.get("output_file")
    if not isinstance(output_file, str) or not output_file:
        return envelope
    try:
        sub_path = Path(output_file).resolve()
    except (OSError, RuntimeError):
        logger.debug(
            "chat_messages: subagent path resolve failed for %s",
            output_file, exc_info=True,
        )
        return envelope
    if not allowed_roots or not any(
        _is_within(sub_path, root) for root in allowed_roots
    ):
        logger.debug(
            "chat_messages: subagent path %s outside allowed transcript "
            "roots (%d roots); skipping inline",
            output_file, len(allowed_roots),
        )
        return envelope
    if not sub_path.exists():
        return envelope
    try:
        sub_envelopes = parse_raw_subagent_jsonl(
            sub_path,
            actor_fallback=actor_fallback,
            limit=_SUBAGENT_INLINE_LIMIT,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "chat_messages: subagent parse failed for %s",
            sub_path, exc_info=True,
        )
        return envelope
    # Cache-poisoning guard: the transcript parser cache stores the
    # same ``MessageEnvelope`` instances we receive here (the cache
    # only copies the outer list, not the dataclass or its mutable
    # ``metadata`` dict — see ``transcripts._PARSE_CACHE``). Mutating
    # ``envelope.metadata`` in place would persist ``subagent_transcript``
    # on the cached entry, so a later default ``include_subagents=false``
    # request against the same archive/mtime would still return the
    # inlined payload. Build a fresh envelope with a shallow-cloned
    # metadata dict so the enrichment never leaks back into the cache.
    new_metadata = dict(envelope.metadata or {})
    new_metadata["subagent_transcript"] = [
        env.to_dict() for env in sub_envelopes
    ]
    return dataclasses.replace(envelope, metadata=new_metadata)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/sessions",
    response_model=ChatSessionsResponse,
    summary="Discover every chat surface (operator/architect/advisor/worker)",
    operation_id="listChatSessions",
)
def list_chat_sessions_endpoint(config: ConfigDep) -> ChatSessionsResponse:
    """GET /api/v1/chat/sessions — return every configured chat surface.

    Workers are best-effort: when the work-service can't be opened
    (fresh workspace, pg pool down) the response still lists the
    configured surfaces and skips workers silently. The discovery
    endpoint never 500s — the cockpit / frontend depend on it for
    sidebar bootstrap.
    """
    cache_key = (
        id(config),
        id(enumerate_chat_surfaces),
        id(_build_tmux_client),
        id(_build_work_service_stub),
    )
    now = time.monotonic()
    cached = _CHAT_SESSIONS_CACHE.get(cache_key)
    if cached is not None and now - cached[0] < _CHAT_SESSIONS_TTL_SECONDS:
        return ChatSessionsResponse(sessions=list(cached[1]))

    with _CHAT_SESSIONS_LOCK:
        now = time.monotonic()
        cached = _CHAT_SESSIONS_CACHE.get(cache_key)
        if cached is not None and now - cached[0] < _CHAT_SESSIONS_TTL_SECONDS:
            return ChatSessionsResponse(sessions=list(cached[1]))

        tmux_client = _build_tmux_client()
        work_service = _build_work_service_stub(config)
        surfaces = enumerate_chat_surfaces(
            config,
            work_service=work_service,
            tmux_client=tmux_client,
        )
        rows = tuple(_surface_to_wire(surface) for surface in surfaces)
        completed_at = time.monotonic()
        if len(_CHAT_SESSIONS_CACHE) > 8:
            for stale_key in [
                k for k, (ts, _v) in _CHAT_SESSIONS_CACHE.items()
                if completed_at - ts >= _CHAT_SESSIONS_TTL_SECONDS
            ]:
                _CHAT_SESSIONS_CACHE.pop(stale_key, None)
        _CHAT_SESSIONS_CACHE[cache_key] = (completed_at, rows)
        return ChatSessionsResponse(sessions=list(rows))


@router.get(
    "/{session_name}/messages",
    response_model=ChatMessagesResponse,
    summary="Paginated message history for one chat surface",
    operation_id="getChatMessages",
)
def get_chat_messages_endpoint(  # noqa: PLR0913 — query surface mirrors spec §2.2
    session_name: str,
    config: ConfigDep,
    since: Annotated[str | None, Query(
        description="ISO-8601 lower bound on message timestamp.",
    )] = None,
    since_id: Annotated[str | None, Query(
        description="Return messages strictly after this message id (cursor).",
    )] = None,
    limit: Annotated[int, Query(
        ge=1, le=MAX_MESSAGE_LIMIT,
        description=(
            f"Max messages, capped at {MAX_MESSAGE_LIMIT}. "
            f"Defaults to {DEFAULT_MESSAGE_LIMIT}."
        ),
    )] = DEFAULT_MESSAGE_LIMIT,
    direction: Annotated[Direction, Query(
        description="Ordering: 'desc' (newest first, default) or 'asc'.",
    )] = "desc",
    source: Annotated[SourceMode, Query(
        description=(
            "Transcript source: 'auto' (jsonl with capture fallback when "
            f">{int(STALE_THRESHOLD_SECONDS)}s stale), 'jsonl' (force "
            "JSONL, 404 if absent), 'capture' (force tmux capture)."
        ),
    )] = "auto",
    include_subagents: Annotated[bool, Query(
        description=(
            "Inline each subagent_result envelope's child transcript at "
            "metadata.subagent_transcript. The child JSONL is read from "
            "the parent task-notification.output-file via the raw "
            "Claude-shape parser; paths are constrained to project + "
            "workspace transcript roots (issue #2052)."
        ),
    )] = False,
    include_thinking: Annotated[bool, Query(
        description=(
            "When true, Anthropic extended-thinking blocks are emitted "
            "as `type=thinking` envelopes alongside the normal turns. "
            "Default false preserves the historical envelope contract — "
            "callers must opt in explicitly. See issues #2048 / #2082."
        ),
    )] = False,
) -> ChatMessagesResponse:
    """GET /api/v1/chat/{session_name}/messages per spec §2.2."""
    surface = _find_surface(config, session_name)
    since_dt = _parse_since(since)

    # Issue #2070: tail-read only when the request shape is safe —
    # ``direction=desc`` returns the newest N envelopes (which live at
    # the file tail), no ``since_id`` cursor (paging beyond the head
    # walks into older history the tail can't see), small ``limit``
    # (large ``limit`` defeats the optimization), and ``source=auto``
    # (strict ``source=jsonl`` wants the full parse so OSError
    # propagates cleanly). The pad of ``+1`` lets the route's
    # ``has_more`` detection still fire on the boundary.
    #
    # Issue #2160: also skip the tail when ``include_thinking=true``.
    # Anthropic extended-thinking blocks usually land near the head of
    # a long conversation (model-side reasoning at the start), not the
    # tail. A ``?include_thinking=true&limit=200`` request against a
    # multi-thousand-line archive would otherwise tail-read only the
    # last ~200 envelopes and miss every earlier thinking block —
    # which is exactly the user-facing failure mode #2160 reported
    # (39/39 sessions returned zero thinking envelopes). The flag is
    # explicit opt-in, so trading the tail-read optimization for
    # actually-surfaced thinking is the right call.
    tail_hint: int | None = None
    if (
        source == "auto"
        and direction == "desc"
        and since_id is None
        and limit <= TAIL_READ_LIMIT_THRESHOLD
        and not include_thinking
    ):
        tail_hint = limit + 1

    envelopes, transcript_source, _transcript_path = _load_envelopes(
        surface,
        source=source,
        tail_hint=tail_hint,
        include_thinking=include_thinking,
    )

    post_process: Any = None
    if include_subagents:
        allowed_roots = _allowed_transcript_roots(config)
        actor_fallback = surface.persona or "subagent"

        def post_process(env: MessageEnvelope) -> MessageEnvelope:
            return _inline_subagent_transcript(
                env,
                allowed_roots=allowed_roots,
                actor_fallback=actor_fallback,
            )

    rows, has_more, next_cursor = _apply_filters_and_paginate(
        envelopes,
        since=since_dt,
        since_id=since_id,
        direction=direction,
        limit=limit,
        post_process=post_process,
    )

    return ChatMessagesResponse(
        session_name=surface.session_name,
        surface_type=str(surface.surface_type),
        persona=surface.persona,
        transcript_source=transcript_source,
        messages=rows,
        has_more=has_more,
        next_cursor=next_cursor,
    )


# Silence the unused-import lint for SurfaceType — we re-export so the
# spec's "discovery endpoint returns 4 surface types" contract is
# discoverable from the router module too.
_ = SurfaceType


__all__ = [
    "ChatMessageEnvelope",
    "ChatMessagesResponse",
    "ChatSessionsResponse",
    "ChatSurfaceResponse",
    "ChatSurfaceTranscript",
    "ChatSurfaceWindow",
    "DEFAULT_MESSAGE_LIMIT",
    "MAX_MESSAGE_LIMIT",
    "get_chat_messages_endpoint",
    "list_chat_sessions_endpoint",
    "router",
]
