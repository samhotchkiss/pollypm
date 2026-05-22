"""POST chat-send endpoint (Phase 1 — P3 of the chat-endpoints spec).

Implements:

- ``POST /api/v1/chat/{session_name}/send`` — push a message into
  the running CLI agent backing ``session_name`` via tmux.

Per ``~/Desktop/pollypm-chat-endpoints-spec.md`` §2.3 / §4. The router
owns:

1. Session lookup — ``config.sessions[session_name]`` first, then a
   work-service fall-back for per-task workers whose ``session_name``
   has the canonical shape ``task-{project}-{task_id}`` (see
   :func:`pollypm.work.session_manager.task_window_name`).
2. tmux target resolution — ``<storage-closet>:<window_name>`` for
   configured sessions; ``<project>-storage-closet:<task-window>``
   for workers. Presence is verified via
   :meth:`pollypm.tmux.client.TmuxClient.list_windows` and the
   ``pane_dead`` flag.
3. Safety gates (§4.1, §4.3) —

   * Mid-tool: tail-read the session's ``events.jsonl`` and reject if
     the most-recent assistant turn has an unmatched ``tool_use_id``.
   * Mid-stream: read the latest heartbeat from
     :func:`pollypm.storage.pg_heartbeats.latest_heartbeat` and reject
     if it landed within the last 2 seconds.

   ``safety=strict`` (default) enforces both. ``safety=loose`` keeps
   the mid-tool check but allows mid-stream sends with a warning
   header. ``safety=force`` bypasses everything.
4. AskUserQuestion answer translation (§4.5) — when ``answer_to`` is
   set, the router resolves the referenced ``ask_user`` envelope from
   the transcript, validates each ``selections`` entry against the
   question's options, and types the option labels joined by newlines
   (followed by optional ``notes``) into the pane.

The router intentionally does NOT touch storage writes for the send
itself — the user-visible side effect is the tmux ``send-keys`` call.
The transcript will pick the message up on the next ingest tick.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from pollypm.projects import project_transcripts_dir
from pollypm.tmux.client import DeadPaneError, TmuxClient
from pollypm.web_api.errors import APIError
from pollypm.web_api.routes._deps import ConfigDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ChatSendRequest(BaseModel):
    """Body of ``POST /api/v1/chat/{session_name}/send`` per spec §2.3."""

    text: str | None = Field(
        default=None,
        description=(
            "Free-text body to send. Required when ``selections`` is empty."
        ),
    )
    press_enter: bool = Field(
        default=True,
        description="Submit the buffer by pressing Enter after the text lands.",
    )
    answer_to: str | None = Field(
        default=None,
        description=(
            "ID of an ``ask_user`` message in the session's transcript that "
            "this send answers. When set, ``selections`` must reference one "
            "of the question's options (§4.5)."
        ),
    )
    selections: list[str] = Field(
        default_factory=list,
        description=(
            "Selected option labels for an AskUserQuestion reply. Each entry "
            "must exactly match one of the question's option labels."
        ),
    )
    notes: str | None = Field(
        default=None,
        description=(
            "Optional free-text addition appended after ``selections`` when "
            "answering an ask_user. Ignored for plain text sends."
        ),
    )
    safety: Literal["strict", "loose", "force"] = Field(
        default="strict",
        description=(
            "Safety gate level: strict (default) enforces mid-tool and "
            "mid-stream checks; loose keeps mid-tool but skips mid-stream "
            "(emits warning header); force bypasses both."
        ),
    )
    pane: int | None = Field(
        default=None,
        description=(
            "0-based pane index inside the target window. Defaults to the "
            "primary pane (matches existing send_keys behaviour)."
        ),
    )


class ChatSendResponse(BaseModel):
    """Response body for ``POST /api/v1/chat/{session_name}/send``."""

    ok: bool
    message_id: str
    session_name: str
    window_target: str
    characters_sent: int
    method: Literal["send_keys", "paste_buffer"]
    press_enter_at: str | None = None


# ---------------------------------------------------------------------------
# Typed error helpers (spec §2.3 error codes)
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
        hint="The session is configured but its tmux window is not running.",
    )


def _pane_dead(target: str) -> APIError:
    return APIError(
        status_code=409,
        code="pane_dead",
        message=f"tmux pane is dead: {target}",
    )


def _unsafe_mid_tool() -> APIError:
    return APIError(
        status_code=409,
        code="unsafe_mid_tool",
        message=(
            "Refusing to send while the agent has an open tool_use without a "
            "matching tool_result. Override with safety=force."
        ),
    )


def _unsafe_mid_stream() -> APIError:
    return APIError(
        status_code=409,
        code="unsafe_mid_stream",
        message=(
            "Refusing to send while the agent appears to be streaming "
            "(heartbeat <2s old). Override with safety=loose or safety=force."
        ),
    )


def _answer_to_missing(answer_to: str) -> APIError:
    return APIError(
        status_code=400,
        code="answer_to_missing",
        message=f"No transcript message found for answer_to={answer_to!r}",
    )


def _selections_no_question(answer_to: str) -> APIError:
    return APIError(
        status_code=400,
        code="selections_no_question",
        message=(
            f"answer_to={answer_to!r} does not reference an ask_user message; "
            "selections are only valid against AskUserQuestion envelopes."
        ),
    )


def _selections_invalid(invalid: list[str], valid: list[str]) -> APIError:
    return APIError(
        status_code=400,
        code="selections_invalid",
        message=(
            f"Selections not in question options: {invalid!r}. "
            f"Valid options: {valid!r}"
        ),
    )


# ---------------------------------------------------------------------------
# Session resolution
# ---------------------------------------------------------------------------


def _parse_task_session_name(session_name: str) -> tuple[str, int] | None:
    """Split ``task-{project}-{task_id}`` into ``(project, task_id)``.

    Mirrors :func:`pollypm.work.session_manager.task_window_name` —
    workers don't appear in ``config.sessions`` so the resolver has to
    re-derive the project/task split from the wire name. ``None`` for
    anything that doesn't fit the canonical shape.
    """
    if not session_name.startswith("task-"):
        return None
    body = session_name[len("task-"):]
    sep = body.rfind("-")
    if sep <= 0 or sep == len(body) - 1:
        return None
    project = body[:sep]
    try:
        task_id = int(body[sep + 1 :])
    except ValueError:
        return None
    if not project:
        return None
    return project, task_id


def _storage_closet_session_name(tmux_session: str) -> str:
    """Mirror ``Supervisor.storage_closet_session_name`` without importing it.

    The supervisor owns this string at runtime; the chat-send router only
    needs the same suffix convention (``-storage-closet``) so configured
    chat surfaces resolve to the right tmux session without dragging the
    full supervisor wiring into the HTTP layer.
    """
    return f"{tmux_session}-storage-closet"


def _resolve_session(
    config: Any,
    session_name: str,
) -> tuple[str, str, str | None]:
    """Resolve ``session_name`` → ``(window_name, project_key, role)``.

    Falls back to the canonical ``task-{project}-{task_id}`` shape for
    per-task workers that aren't tracked in ``config.sessions``. Raises
    ``404 session_unknown`` if neither resolution succeeds.
    """
    session = config.sessions.get(session_name) if hasattr(config, "sessions") else None
    if session is not None:
        window_name = session.window_name or session.name
        return window_name, session.project, getattr(session, "role", None)
    parsed = _parse_task_session_name(session_name)
    if parsed is not None:
        project, _task_id = parsed
        # Per-task worker windows reuse the session_name verbatim as
        # window_name (that's what ``task_window_name`` returns).
        return session_name, project, "worker"
    raise _session_unknown(session_name)


def _resolve_project_root(config: Any, project_key: str) -> Path:
    """Return the on-disk project root for ``project_key``.

    Per-task workers may name a project that isn't tracked under
    ``config.projects``; in that case we fall back to the operator's
    ``config.project.root_dir`` (matches the mid-tool detector's only
    other purpose — scanning JSONL — which is fine to skip when the
    project root is unknown).
    """
    projects = getattr(config, "projects", {}) or {}
    known = projects.get(project_key)
    if known is not None:
        return Path(known.path)
    project = getattr(config, "project", None)
    root_dir = getattr(project, "root_dir", None)
    if root_dir is None:
        # Last-resort: cwd. The mid-tool scan will simply find no
        # events.jsonl and skip the check (fail-open) — the operator
        # already opted to force or loose if they hit this path.
        return Path.cwd()
    return Path(root_dir)


# ---------------------------------------------------------------------------
# events.jsonl tail reader (mid-tool detection)
# ---------------------------------------------------------------------------


# Heuristic: enough bytes to capture the latest assistant turn plus any
# tool_result that should match its tool_use blocks. JSONL events from
# Claude are typically a few hundred bytes each; 64 KiB gives ~300+
# events even for verbose Bash output. We never need more than the
# tail for the mid-tool check (we only care about the last assistant
# turn's open tool_use ids).
_EVENTS_TAIL_BYTES = 64 * 1024


def _read_events_tail(events_path: Path, max_bytes: int = _EVENTS_TAIL_BYTES) -> list[dict[str, Any]]:
    """Return the tail JSON events from ``events_path`` as parsed dicts.

    Reads at most ``max_bytes`` from the end via ``os.lseek`` so very
    long-running sessions don't force the whole transcript into memory
    each request. Skips the (likely truncated) first line when the
    file is larger than ``max_bytes``.
    """
    try:
        size = events_path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    start = max(0, size - max_bytes)
    try:
        fd = os.open(events_path, os.O_RDONLY)
    except OSError:
        return []
    try:
        os.lseek(fd, start, os.SEEK_SET)
        raw = os.read(fd, size - start)
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []
    lines = text.splitlines()
    if start > 0 and lines:
        # First line is almost certainly truncated mid-record.
        lines = lines[1:]
    events: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _find_session_events_path(project_root: Path, session_name: str) -> Path | None:
    """Locate the events.jsonl most likely to belong to ``session_name``.

    PollyPM's transcript mirror writes one ``events.jsonl`` per Claude
    session UUID under ``<project>/.pollypm/transcripts/<uuid>/``. We
    don't currently store a ``session_name → session_uuid`` mapping
    (P1 of the chat spec is the natural home for that registry), so
    this function picks the freshest ``events.jsonl`` in the project
    that has been written to recently. For the mid-tool safety check
    that's sufficient — there is realistically one active session per
    project at a time, and a stale mismatch only fails-open (allowing
    a send when we should have blocked it, which is exactly what
    ``safety=force`` is for).

    TODO(P1): replace with the registry-backed lookup once P1 lands.
    """
    transcripts_root = project_transcripts_dir(project_root)
    if not transcripts_root.exists():
        return None
    candidate: Path | None = None
    candidate_mtime = -1.0
    try:
        entries = list(transcripts_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        events = entry / "events.jsonl"
        if not events.is_file():
            continue
        try:
            mtime = events.stat().st_mtime
        except OSError:
            continue
        if mtime > candidate_mtime:
            candidate_mtime = mtime
            candidate = events
    return candidate


def _last_assistant_open_tool_ids(events: list[dict[str, Any]]) -> set[str]:
    """Return ``tool_use_id``s in the *latest* assistant turn lacking a result.

    Walks the events tail in reverse to find the most recent
    ``assistant_turn``; collects every ``tool_call`` (with the same
    or later timestamp) and subtracts every ``tool_result`` matched by
    ``tool_use_id``. The remaining set is the mid-flight tool calls.
    The result is empty for: no assistant turn at all, no open tools,
    or fully-matched tool_use/tool_result pairs.
    """
    # Find the latest assistant_turn index — anything before that is
    # an earlier completed turn and irrelevant to the "mid-tool right
    # now" decision.
    last_assistant = -1
    for idx in range(len(events) - 1, -1, -1):
        if events[idx].get("event_type") == "assistant_turn":
            last_assistant = idx
            break
    if last_assistant < 0:
        # No assistant turn in tail — could be very long pre-assistant
        # window, but for safety check we treat "no recent assistant"
        # as "not mid-tool".
        return set()
    open_ids: set[str] = set()
    seen_results: set[str] = set()
    for event in events[last_assistant:]:
        etype = event.get("event_type")
        payload = event.get("payload") or {}
        if etype == "tool_call":
            tid = payload.get("id")
            if isinstance(tid, str) and tid:
                open_ids.add(tid)
        elif etype == "tool_result":
            tid = payload.get("tool_use_id")
            if isinstance(tid, str) and tid:
                seen_results.add(tid)
    return open_ids - seen_results


def _is_mid_tool(project_root: Path, session_name: str) -> bool:
    """True when the session's tail shows an unmatched assistant tool_use."""
    events_path = _find_session_events_path(project_root, session_name)
    if events_path is None:
        return False
    events = _read_events_tail(events_path)
    if not events:
        return False
    return bool(_last_assistant_open_tool_ids(events))


# ---------------------------------------------------------------------------
# Mid-stream detection (heartbeat freshness)
# ---------------------------------------------------------------------------


_MID_STREAM_WINDOW_SECONDS = 2.0


def _heartbeat_age_seconds(config: Any, session_name: str) -> float | None:
    """Return seconds since ``session_name``'s latest heartbeat, or ``None``.

    Reads via :func:`pollypm.storage.pg_heartbeats.latest_heartbeat` per
    #2039 (pg-only). Any failure is treated as "no signal" — the caller
    fails-open so a flaky pg pool doesn't block legitimate sends.
    """
    try:
        from pollypm.storage.pg_heartbeats import latest_heartbeat
    except Exception:  # noqa: BLE001
        return None
    try:
        record = latest_heartbeat(session_name, config=config)
    except Exception:  # noqa: BLE001
        logger.debug("chat_send: pg_heartbeats.latest_heartbeat failed", exc_info=True)
        return None
    if record is None:
        return None
    stamp = record.created_at
    try:
        # ISO-8601 with or without ``Z`` suffix.
        normalised = stamp.replace("Z", "+00:00") if stamp.endswith("Z") else stamp
        ts = datetime.fromisoformat(normalised)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - ts).total_seconds())


# ---------------------------------------------------------------------------
# AskUserQuestion answer translation (§4.5)
# ---------------------------------------------------------------------------


def _find_ask_user_envelope(
    project_root: Path,
    session_name: str,
    answer_to: str,
) -> dict[str, Any] | None:
    """Locate the ask_user message in ``events.jsonl`` matching ``answer_to``.

    Returns the parsed event dict or ``None`` if the id isn't present.
    The lookup is restricted to the events tail — the spec only
    supports answering the most-recent open question (matches how
    Claude Code presents AskUserQuestion: stale questions disappear
    after the next turn).
    """
    events_path = _find_session_events_path(project_root, session_name)
    if events_path is None:
        return None
    events = _read_events_tail(events_path)
    for event in events:
        if _event_message_id(event) == answer_to:
            return event
    return None


def _event_message_id(event: dict[str, Any]) -> str | None:
    """Extract the message id from an events.jsonl event.

    Different event types carry the id in different places; the chat
    transcript layer (P1) normalises these into a single ``id`` field
    on each envelope. Until P1 lands we look in the canonical places:
    payload.id (tool_use), top-level uuid, and source_offset as a last
    resort (still stable per file).
    """
    payload = event.get("payload") or {}
    for key in ("id", "uuid", "message_id"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, str) and value:
            return value
    for key in ("uuid", "message_id", "id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _ask_user_options(event: dict[str, Any]) -> list[str] | None:
    """Pull the list of option labels from an ask_user event.

    Returns ``None`` when the event isn't an ask_user. The shape comes
    from §3.5 — ``metadata.questions[].options[].label``. We accept
    the legacy shape (``payload.questions[]`` from the AskUserQuestion
    tool_use payload) as well so the P3 router works against the
    raw events.jsonl that P1 hasn't normalised yet.
    """
    # Spec envelope (post-P1 normalisation).
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        questions = metadata.get("questions")
        if isinstance(questions, list):
            return _flatten_question_options(questions)
    # Raw events.jsonl (pre-P1): tool_call payload for AskUserQuestion.
    payload = event.get("payload") or {}
    if isinstance(payload, dict):
        if payload.get("name") == "AskUserQuestion":
            tool_input = payload.get("input") or {}
            if isinstance(tool_input, dict):
                questions = tool_input.get("questions")
                if isinstance(questions, list):
                    return _flatten_question_options(questions)
        # type==ask_user direct (matches §3.5 raw form).
        if payload.get("type") == "ask_user":
            questions = payload.get("questions")
            if isinstance(questions, list):
                return _flatten_question_options(questions)
    return None


def _flatten_question_options(questions: list[Any]) -> list[str]:
    out: list[str] = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        options = question.get("options")
        if not isinstance(options, list):
            continue
        for option in options:
            if isinstance(option, dict):
                label = option.get("label")
                if isinstance(label, str) and label:
                    out.append(label)
            elif isinstance(option, str) and option:
                out.append(option)
    return out


def _build_answer_text(selections: list[str], notes: str | None) -> str:
    """Format the typed string for an AskUserQuestion reply.

    Spec §4.5 default: option labels joined by newlines, optional
    ``notes`` appended after another newline. Trailing newline is
    handled by ``press_enter`` — we never append our own ``\\n`` at the
    end here so ``press_enter=False`` produces a buffer the caller can
    inspect.

    TODO(P4): Sam's open-question #1 — confirm Claude Code's
    AskUserQuestion stdin format. The chat-endpoint spec calls out
    this as a user-testing gate. The current implementation matches
    the spec's default assumption ("typing the option label verbatim
    works"). No live Claude session is reachable from this isolated
    worktree to run the experiment in-flight; deferred to P4
    user-testing.
    """
    body = "\n".join(selections)
    if notes:
        if body:
            body = f"{body}\n{notes}"
        else:
            body = notes
    return body


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------


def _tmux_session_for_send(config: Any, project_key: str) -> str:
    """Return the storage-closet tmux session name to address.

    All chat surfaces (operator, architect, advisor, per-task workers)
    live inside the project's storage-closet session — that's the
    invariant the supervisor enforces (``Supervisor.storage_closet_session_name``).
    We don't import the supervisor here to keep the HTTP layer light;
    the suffix convention is stable enough to inline.
    """
    project = getattr(config, "project", None)
    tmux_session = getattr(project, "tmux_session", None) if project else None
    if not isinstance(tmux_session, str) or not tmux_session:
        # Fall back to the project key when the project block is sparse
        # (e.g. tests that pass a minimal config).
        tmux_session = project_key or "pollypm"
    return _storage_closet_session_name(tmux_session)


def _resolve_pane_target(
    tmux: TmuxClient,
    storage_session: str,
    window_name: str,
    pane_index: int | None,
) -> tuple[str, bool]:
    """Return ``(target, present)`` for a window + optional pane index.

    ``present`` is True when the window exists; the caller maps
    ``False`` to ``503 window_missing``. When the window exists but a
    requested pane index is out of range we still raise — that's a
    client bug, not a missing window.
    """
    try:
        windows = tmux.list_windows(storage_session)
    except Exception:  # noqa: BLE001
        logger.debug("chat_send: list_windows(%r) failed", storage_session, exc_info=True)
        return f"{storage_session}:{window_name}", False
    matching = [w for w in windows if w.name == window_name]
    if not matching:
        return f"{storage_session}:{window_name}", False
    window = matching[0]
    if window.pane_dead:
        raise _pane_dead(f"{storage_session}:{window_name}")
    if pane_index is None or pane_index == 0:
        # Default: address the window — send_keys hits the active pane.
        return f"{storage_session}:{window_name}", True
    # Specific pane requested: target ``session:window.<N>``.
    return f"{storage_session}:{window_name}.{int(pane_index)}", True


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/{session_name}/send",
    response_model=ChatSendResponse,
    summary="Send a message to a chat surface",
    operation_id="sendChatMessage",
)
def send_chat_message(  # noqa: PLR0912, PLR0915 — gate logic is intentionally inline
    session_name: str,
    body: ChatSendRequest,
    config: ConfigDep,
    response: Response,
) -> ChatSendResponse:
    """POST /api/v1/chat/{session_name}/send — push text into a tmux pane."""
    # 1. Resolve session + tmux target.
    window_name, project_key, _role = _resolve_session(config, session_name)
    storage_session = _tmux_session_for_send(config, project_key)
    tmux = TmuxClient()
    target, present = _resolve_pane_target(tmux, storage_session, window_name, body.pane)
    if not present:
        raise _window_missing(target)

    project_root = _resolve_project_root(config, project_key)

    # 2. Safety gates (§4.1, §4.3).
    if body.safety != "force":
        if _is_mid_tool(project_root, session_name):
            raise _unsafe_mid_tool()
        age = _heartbeat_age_seconds(config, session_name)
        streaming = age is not None and age < _MID_STREAM_WINDOW_SECONDS
        if body.safety == "strict" and streaming:
            raise _unsafe_mid_stream()
        if body.safety == "loose" and streaming:
            response.headers["X-PollyPM-Warning"] = "agent-may-be-streaming"

    # 3. AskUserQuestion answer handling (§4.5).
    text_to_send: str | None
    if body.answer_to is not None:
        envelope = _find_ask_user_envelope(project_root, session_name, body.answer_to)
        if envelope is None:
            raise _answer_to_missing(body.answer_to)
        options = _ask_user_options(envelope)
        if options is None:
            raise _selections_no_question(body.answer_to)
        invalid = [s for s in body.selections if s not in options]
        if body.selections and invalid:
            raise _selections_invalid(invalid, options)
        if body.selections:
            text_to_send = _build_answer_text(body.selections, body.notes)
        elif body.text:
            # Freeform reply against an ask_user (§4.5 last paragraph).
            text_to_send = body.text
        elif body.notes:
            text_to_send = body.notes
        else:
            raise APIError(
                status_code=400,
                code="invalid_request",
                message="answer_to requires selections, text, or notes.",
            )
    else:
        if not body.text:
            raise APIError(
                status_code=400,
                code="invalid_request",
                message="text is required when answer_to is not set.",
            )
        text_to_send = body.text

    assert text_to_send is not None  # noqa: S101 — narrowed above

    # 4. Send via tmux.
    method: Literal["send_keys", "paste_buffer"] = (
        "paste_buffer" if len(text_to_send) > 100 else "send_keys"
    )
    press_enter_at: str | None = None
    try:
        tmux.send_keys(target, text_to_send, press_enter=body.press_enter)
    except DeadPaneError as exc:
        raise _pane_dead(str(exc)) from exc
    if body.press_enter:
        press_enter_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    return ChatSendResponse(
        ok=True,
        message_id=f"msg_{uuid.uuid4().hex}",
        session_name=session_name,
        window_target=target,
        characters_sent=len(text_to_send),
        method=method,
        press_enter_at=press_enter_at,
    )


__all__ = [
    "ChatSendRequest",
    "ChatSendResponse",
    "router",
    "send_chat_message",
]
