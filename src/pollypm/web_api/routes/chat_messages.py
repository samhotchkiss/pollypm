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

import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from pollypm.tmux.client import TmuxClient
from pollypm.web_api.chat import (
    STALE_THRESHOLD_SECONDS,
    ChatSurface,
    MessageEnvelope,
    SurfaceType,
    capture_envelopes,
    enumerate_chat_surfaces,
    is_archive_stale,
    parse_events_jsonl,
)
from pollypm.web_api.errors import APIError
from pollypm.web_api.routes._deps import ConfigDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])


# ---------------------------------------------------------------------------
# Limits + defaults (kept module-level so tests can patch)
# ---------------------------------------------------------------------------


# Spec §2.2: ``limit`` default 100, cap 500.
DEFAULT_MESSAGE_LIMIT = 100
MAX_MESSAGE_LIMIT = 500


SourceMode = Literal["auto", "jsonl", "capture"]
Direction = Literal["asc", "desc"]


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


class ChatMessageEnvelope(BaseModel):
    """Wire form of :class:`MessageEnvelope` (spec §3).

    The dataclass version is kept for internal callers; this Pydantic
    model exists so the OpenAPI document carries the right schema and
    FastAPI handles the JSON serialization uniformly.
    """

    id: str
    ts: str
    role: str
    actor: str
    type: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatMessagesResponse(BaseModel):
    """``GET /api/v1/chat/{session_name}/messages`` envelope (spec §2.2)."""

    session_name: str
    surface_type: str
    persona: str | None = None
    transcript_source: str | None = None
    transcript_path: str | None = None
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


def _open_work_service_for_discovery(config: Any) -> Any | None:
    """Best-effort handle for enumerating per-task workers.

    Returns ``None`` if the work-service can't be opened (no DB yet,
    pg pool down, etc.). The discovery endpoint then falls back to
    configured surfaces only — never 500s. Matches the fail-open
    posture of the other read endpoints.
    """
    try:
        from pollypm.web_api.service import _open_work_service_readonly
    except Exception:  # noqa: BLE001
        return None
    # ``_open_work_service_readonly`` is a context manager that wants
    # a project_key + path; for chat discovery we need a single handle
    # that covers every project. We use the operator's default project
    # because the work-service is workspace-wide in pg mode (the
    # project_key arg is informational only).
    project = getattr(config, "project", None)
    if project is None:
        return None
    project_key = getattr(project, "name", "")
    project_path = getattr(project, "root_dir", None)
    if not project_key or project_path is None:
        return None
    return _WorkServiceHandle(_open_work_service_readonly,
                              config=config,
                              project_key=project_key,
                              project_path=project_path)


class _WorkServiceHandle:
    """Tiny wrapper that defers entering the contextmanager until use.

    ``enumerate_chat_surfaces`` only calls ``list_worker_sessions`` so
    we can stage the contextmanager exit until after the call. This
    keeps the route function readable (one ``with`` block in
    :func:`list_chat_sessions_endpoint`).
    """

    def __init__(self, factory: Any, **kwargs: Any) -> None:
        self._factory = factory
        self._kwargs = kwargs
        self._cm: Any = None
        self._svc: Any = None

    def __enter__(self) -> Any:
        self._cm = self._factory(**self._kwargs)
        self._svc = self._cm.__enter__()
        return self._svc

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._cm is not None:
            try:
                self._cm.__exit__(exc_type, exc, tb)
            finally:
                self._cm = None
                self._svc = None


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
    include_workers: bool = True,
) -> ChatSurface:
    """Resolve ``session_name`` to a :class:`ChatSurface` or 404."""
    tmux_client = _build_tmux_client()
    work_handle = _open_work_service_for_discovery(config) if include_workers else None
    if work_handle is not None:
        with work_handle as work_service:
            surfaces = enumerate_chat_surfaces(
                config,
                work_service=work_service,
                tmux_client=tmux_client,
            )
    else:
        surfaces = enumerate_chat_surfaces(
            config,
            work_service=None,
            tmux_client=tmux_client,
        )
    for surface in surfaces:
        if surface.session_name == session_name:
            return surface
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
    """Lenient ISO-8601 parse for envelope timestamps. ``None`` on failure."""
    if not ts:
        return None
    try:
        candidate = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.fromisoformat(candidate)
    except (TypeError, ValueError):
        return None


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
    include_thinking: bool,
) -> tuple[list[MessageEnvelope], str | None, Path | None]:
    """Return ``(envelopes, transcript_source, transcript_path)``.

    ``transcript_source`` is one of ``"jsonl"`` / ``"capture"`` /
    ``None`` (no transcript yet, spec §4.8).
    """
    archive = surface.transcript_path
    actor_fallback = surface.persona or "agent"

    if source == "jsonl":
        if archive is None or not archive.exists():
            raise _archive_missing(surface.session_name)
        envelopes = parse_events_jsonl(
            archive,
            include_thinking=include_thinking,
            actor_fallback=actor_fallback,
        )
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
        envelopes = parse_events_jsonl(
            archive,
            include_thinking=include_thinking,
            actor_fallback=actor_fallback,
        )
        return envelopes, "jsonl", archive

    # Stale or missing archive — try capture, fall back to whatever
    # JSONL we have (better stale data than no data, per spec §4.8
    # which prefers empty over erroring).
    captured = _capture_for_surface(surface, actor_fallback=actor_fallback)
    if captured:
        return captured, "capture", None
    if archive is not None and archive.exists():
        envelopes = parse_events_jsonl(
            archive,
            include_thinking=include_thinking,
            actor_fallback=actor_fallback,
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
    include_thinking: bool,
    include_subagents: bool,
    subagent_loader: Any,
    allowed_subagent_roots: list[Path] | None = None,
) -> tuple[list[ChatMessageEnvelope], bool, str | None]:
    """Apply spec §2.2 query semantics and return ``(rows, has_more, cursor)``.

    Filtering order (cursor MUST be applied after sort, because
    ``next_cursor`` is the LAST id in the page in the requested
    direction — applying ``since_id`` in source order would slice the
    wrong half for ``direction=desc``, duplicating page 1 on page 2):

    1. Drop ``thinking`` envelopes when ``include_thinking`` is false
       (defensive — the P1 parser already drops these by default, but
       a custom ``parse_events_jsonl(..., include_thinking=True)``
       caller could feed them through here).
    2. ``since`` lower-bound on envelope ``ts``.
    3. Sort by timestamp + position. JSONL ordering is already
       chronological; we re-sort defensively so the response is
       deterministic even when the parser returns shuffled rows.
    4. Reverse for ``direction=desc``.
    5. ``since_id`` cursor walk — strictly after the matching id in
       the ordered sequence (the cursor itself is excluded from the
       slice, matching the ``next_cursor`` semantics of the inbox
       endpoint).
    6. Slice to ``limit`` and compute ``has_more`` / ``next_cursor``.
    7. Expand ``subagent_result.metadata.subagent_transcript`` when
       ``include_subagents`` is set.
    """
    filtered: list[MessageEnvelope] = []
    for envelope in envelopes:
        if not include_thinking and str(envelope.type) == "thinking":
            continue
        if since is not None:
            ts = _parse_envelope_ts(envelope.ts)
            if ts is None:
                # Drop rows we can't compare — they'd otherwise sort
                # to a non-deterministic position relative to ``since``.
                continue
            # Make both sides comparable: if either is naive, drop tz.
            if ts.tzinfo is None and since.tzinfo is not None:
                ts = ts.replace(tzinfo=since.tzinfo)
            elif since.tzinfo is None and ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            if ts < since:
                continue
        filtered.append(envelope)

    # Stable sort by (ts, original position). Original position is
    # preserved because Python's sort is stable, but we've already
    # filtered so we capture indices first.
    indexed = list(enumerate(filtered))
    indexed.sort(key=lambda item: (
        _parse_envelope_ts(item[1].ts) or datetime.min,
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

    if include_subagents and subagent_loader is not None:
        page = [
            _inline_subagent(
                env,
                subagent_loader,
                include_thinking,
                allowed_roots=allowed_subagent_roots,
            )
            for env in page
        ]

    return [_envelope_to_wire(env) for env in page], has_more, next_cursor


def _allowed_transcript_roots(config: Any, work_service: Any = None) -> list[Path]:
    """Compute the set of directories that may contain subagent transcripts.

    Security boundary for :func:`_inline_subagent`: ``metadata.output_file``
    is supplied by transcript content and must never be trusted as a
    filesystem authority. Callers constrain inlining to paths that
    live under one of these roots.

    Roots covered:

    - ``<workspace>/.pollypm/transcripts/`` for the operator workspace.
    - ``<project>/.pollypm/transcripts/`` for every known project in
      ``config.projects`` (which is the same set
      :mod:`pollypm.web_api.chat.registry` enumerates).
    - Worktree roots when ``work_service`` is provided — kept best-effort
      because the worker discovery flow already gates on the same
      work-service handle and the lookup is fail-soft.

    Roots are returned ``resolve()``d (symlinks collapsed) so the
    ``is_relative_to`` check in :func:`_inline_subagent` lines up with
    a likewise-resolved candidate path.
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

    if work_service is not None:
        # Worktrees can host their own .pollypm/transcripts/ tree when a
        # task is running detached from the project root. Probe both
        # workers_iter() and a generic "list_worktree_paths" shape so the
        # check stays useful regardless of work-service revision.
        for method_name in ("list_worktree_paths", "worktree_paths"):
            method = getattr(work_service, method_name, None)
            if not callable(method):
                continue
            try:
                for entry in method() or []:
                    if isinstance(entry, str):
                        entry_path: Path | None = Path(entry)
                    elif isinstance(entry, Path):
                        entry_path = entry
                    else:
                        entry_path = None
                    if entry_path is not None:
                        _add(project_transcripts_dir(entry_path))
            except Exception:  # noqa: BLE001
                logger.debug(
                    "chat_messages: worktree path probe failed via %s",
                    method_name, exc_info=True,
                )
            break

    return roots


def _inline_subagent(
    envelope: MessageEnvelope,
    subagent_loader: Any,
    include_thinking: bool,
    allowed_roots: list[Path] | None = None,
) -> MessageEnvelope:
    """When the envelope is a ``subagent_result``, inline the sub-transcript.

    Spec §3.7: ``metadata.subagent_transcript`` holds the subagent's
    own envelopes. We resolve the path via
    ``metadata.output_file`` (set by the P1 parser when the parent
    ``task-notification`` block carries one).

    Security: ``metadata.output_file`` comes from transcript content
    and an authenticated caller who can influence what an agent writes
    could otherwise make the API stat/read arbitrary local paths
    (``/etc/passwd``, ``../../../escape.jsonl``, etc.). We constrain
    candidates to ``allowed_roots`` (project + workspace transcript
    dirs) via :meth:`Path.resolve` + :meth:`Path.is_relative_to`. When
    the path lives outside every allowed root we skip inlining
    silently — no raise, just a debug log — so a malformed transcript
    can't 500 the endpoint.
    """
    if str(envelope.type) != "subagent_result":
        return envelope
    output_file = envelope.metadata.get("output_file")
    if not isinstance(output_file, str) or not output_file:
        return envelope
    roots = allowed_roots or []
    try:
        sub_path = Path(output_file).resolve()
    except (OSError, RuntimeError):
        logger.debug(
            "chat_messages: subagent path resolve failed for %s",
            output_file, exc_info=True,
        )
        return envelope
    # Reject paths outside every allowed transcript root.
    if not any(_is_within(sub_path, root) for root in roots):
        logger.debug(
            "chat_messages: subagent path %s outside allowed transcript roots "
            "(%d roots); skipping inline",
            output_file, len(roots),
        )
        return envelope
    try:
        if not sub_path.exists():
            return envelope
        sub_envelopes = subagent_loader(
            sub_path,
            include_thinking=include_thinking,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "chat_messages: subagent transcript load failed for %s",
            output_file, exc_info=True,
        )
        return envelope
    envelope.metadata = dict(envelope.metadata)
    envelope.metadata["subagent_transcript"] = [
        _envelope_to_wire(env).model_dump() for env in sub_envelopes
    ]
    return envelope


def _is_within(candidate: Path, root: Path) -> bool:
    """``Path.is_relative_to`` shim that tolerates pre-3.9 semantics.

    Both ``candidate`` and ``root`` are expected to already be
    ``resolve()``d so symlinks don't sneak a candidate past the
    boundary.
    """
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


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
    tmux_client = _build_tmux_client()
    work_handle = _open_work_service_for_discovery(config)
    if work_handle is not None:
        with work_handle as work_service:
            surfaces = enumerate_chat_surfaces(
                config,
                work_service=work_service,
                tmux_client=tmux_client,
            )
    else:
        surfaces = enumerate_chat_surfaces(
            config,
            work_service=None,
            tmux_client=tmux_client,
        )
    return ChatSessionsResponse(
        sessions=[_surface_to_wire(surface) for surface in surfaces],
    )


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
    include_subagents: Annotated[bool, Query(
        description=(
            "When true, inline the subagent's transcript into "
            "subagent_result.metadata.subagent_transcript (spec §3.7)."
        ),
    )] = False,
    include_thinking: Annotated[bool, Query(
        description=(
            "Include type=thinking envelopes (spec §3.4). Default false "
            "because thinking blocks are usually noisy."
        ),
    )] = False,
    source: Annotated[SourceMode, Query(
        description=(
            "Transcript source: 'auto' (jsonl with capture fallback when "
            f">{int(STALE_THRESHOLD_SECONDS)}s stale), 'jsonl' (force "
            "JSONL, 404 if absent), 'capture' (force tmux capture)."
        ),
    )] = "auto",
) -> ChatMessagesResponse:
    """GET /api/v1/chat/{session_name}/messages per spec §2.2."""
    surface = _find_surface(config, session_name)
    since_dt = _parse_since(since)

    envelopes, transcript_source, transcript_path = _load_envelopes(
        surface, source=source, include_thinking=include_thinking,
    )

    # Compute path-traversal allowlist once per request — the
    # subagent inliner uses it to reject ``metadata.output_file``
    # values that escape known transcript roots (blocker 3).
    allowed_roots = (
        _allowed_transcript_roots(config) if include_subagents else None
    )

    rows, has_more, next_cursor = _apply_filters_and_paginate(
        envelopes,
        since=since_dt,
        since_id=since_id,
        direction=direction,
        limit=limit,
        include_thinking=include_thinking,
        include_subagents=include_subagents,
        subagent_loader=parse_events_jsonl,
        allowed_subagent_roots=allowed_roots,
    )

    return ChatMessagesResponse(
        session_name=surface.session_name,
        surface_type=str(surface.surface_type),
        persona=surface.persona,
        transcript_source=transcript_source,
        transcript_path=str(transcript_path) if transcript_path else None,
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
