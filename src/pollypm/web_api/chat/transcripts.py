"""Parse the normalized ``events.jsonl`` archive into MessageEnvelopes.

The upstream :class:`pollypm.transcript_ingest.TranscriptIngestor` polls
provider JSONLs (Claude Code, Codex) and rewrites them into a project-
scoped, normalized event stream at::

    <project>/.pollypm/transcripts/<session_id>/events.jsonl

Each line is one ``_event_base`` dict (see ``transcript_ingest.py``):

    {
      "timestamp": "2026-05-21T20:48:11Z",
      "event_type": "user_turn" | "assistant_turn" | "tool_call"
                  | "tool_result" | "error" | "token_usage"
                  | "session_state" | "turn_end",
      "session_id": "<provider-internal uuid>",
      "account_name": "claude_main",
      "provider": "claude" | "codex",
      "project_key": "samblog",
      "source_path": "...",
      "source_offset": 12345,
      "cwd": "/path/to/project",
      "model_name": "claude-opus-4-7",
      "payload": {...}    # provider-shape preserved verbatim
    }

This module translates each event into a :class:`MessageEnvelope`, with
the following non-trivial mappings:

- ``token_usage`` and ``session_state`` are dropped (not chat messages).
- ``tool_call`` for the Claude ``Task`` tool becomes a
  ``subagent_spawn`` envelope (subagent_id = tool_use_id).
- The matching ``tool_result`` for a Task call becomes
  ``subagent_result``.
- Claude ``AskUserQuestion`` tool calls become ``ask_user`` envelopes.
- Claude ``SendUserFile`` tool calls become ``file`` envelopes.
- ``thinking`` blocks (when surfaced by the provider) only flow when
  ``include_thinking=True``.
- Compaction / session-start markers become ``system_event``.

The parser is pure: takes a file path + flags, returns a list of
envelopes. The P2 router layers pagination / filtering on top.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from pollypm.projects import project_transcripts_dir
from pollypm.web_api.chat.envelope import (
    MessageEnvelope,
    MessageRole,
    MessageType,
)

logger = logging.getLogger(__name__)


# Spec §4.7 — when the archive hasn't been written in ``N`` seconds AND
# the tmux pane is live, fall back to ``tmux capture-pane``. Default 60s
# per the spec; exposed as a module-level constant so callers can tune
# (the future SSE / polling endpoints may want a different threshold).
STALE_THRESHOLD_SECONDS = 60.0


# Claude's special tool names that get bespoke envelope discriminators.
# Anything else falls through to the generic ``tool_use`` rendering.
_TASK_TOOL_NAMES = frozenset({"Task", "Agent"})
_ASK_USER_TOOL_NAMES = frozenset({"AskUserQuestion"})
_FILE_TOOL_NAMES = frozenset({"SendUserFile"})


# Codex payload-type discriminators that need their own translation.
# Most Codex events flow through the generic mapping; these are the
# exceptions where the spec wants a richer envelope.
_CODEX_PAYLOAD_KINDS = {
    "tool_call": MessageType.TOOL_USE,
    "tool_result": MessageType.TOOL_RESULT,
}


def resolve_transcript_path(
    project_root: Path,
    cwd: str | None = None,
    *,
    session_hint: str | None = None,
) -> Path | None:
    """Find the most recent ``events.jsonl`` for a surface.

    The ingestor names subdirectories by the provider-internal session
    UUID (Claude ``sessionId`` / Codex ``session_meta.id``), not by the
    tmux session name. We resolve the right subdir by walking the
    project's transcripts root and preferring:

    1. A subdir whose tail event's ``cwd`` matches ``cwd``, OR
    2. The most-recently-modified ``events.jsonl`` under the root.

    Returns ``None`` when no events file exists yet (e.g. brand-new
    session that hasn't streamed its first turn). Per spec §4.8 this is
    not an error — callers surface ``messages: []`` to the client.
    """
    root = project_transcripts_dir(project_root)
    if not root.exists():
        return None
    candidates: list[tuple[float, Path, str | None]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if child.name in {"tasks", ".ingestion-state.lock"}:
            # ``tasks/`` holds per-task raw-provider-JSONL archives
            # written by SessionManager._archive_jsonl; not normalized.
            continue
        if session_hint and child.name == session_hint:
            events_path = child / "events.jsonl"
            if events_path.exists():
                return events_path
        events_path = child / "events.jsonl"
        if not events_path.exists():
            continue
        try:
            mtime = events_path.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, events_path, _tail_cwd(events_path)))
    if not candidates:
        return None
    if cwd:
        normalized = str(Path(cwd).resolve())
        matching = [
            (mtime, path)
            for mtime, path, tail_cwd in candidates
            if tail_cwd and str(Path(tail_cwd).resolve()) == normalized
        ]
        if matching:
            matching.sort(reverse=True)
            return matching[0][1]
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _tail_cwd(events_path: Path) -> str | None:
    """Best-effort: return the ``cwd`` from the last well-formed event.

    Used by :func:`resolve_transcript_path` to pick the right subdir
    when multiple Claude sessions have streamed into the same project.
    Reads only the tail of the file (~16 KB) so we don't choke on
    multi-MB transcripts.
    """
    try:
        size = events_path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    read_size = min(size, 16 * 1024)
    try:
        with events_path.open("rb") as handle:
            handle.seek(size - read_size)
            tail = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    last_cwd: str | None = None
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            cwd = obj.get("cwd")
            if isinstance(cwd, str) and cwd:
                last_cwd = cwd
    return last_cwd


def is_archive_stale(
    events_path: Path | None,
    *,
    threshold_seconds: float = STALE_THRESHOLD_SECONDS,
    now: float | None = None,
) -> bool:
    """True when the archive is missing or older than the threshold.

    Drives the spec §4.7 jsonl→capture fallback decision: when the
    ingestor hasn't appended in over ``threshold_seconds``, the live
    pane is the authoritative source. ``now`` is injectable for tests.
    """
    if events_path is None or not events_path.exists():
        return True
    try:
        mtime = events_path.stat().st_mtime
    except OSError:
        return True
    current = now if now is not None else time.time()
    return (current - mtime) > threshold_seconds


def parse_events_jsonl(
    events_path: Path,
    *,
    include_thinking: bool = False,
    actor_fallback: str = "agent",
) -> list[MessageEnvelope]:
    """Parse an ``events.jsonl`` archive into envelopes.

    ``include_thinking`` — when ``False`` (default), ``thinking``
    blocks are dropped per spec §3.4.

    ``actor_fallback`` — used when the event doesn't carry a clear
    actor name. The registry layer passes the surface's persona name
    here so worker transcripts say ``"worker"`` and operator transcripts
    say ``"Polly"``.

    Malformed lines are skipped with a debug log; the parser never
    raises on bad JSON because partially-flushed archives are normal
    (the ingestor appends line-by-line, the API may read mid-flush).
    """
    envelopes: list[MessageEnvelope] = []
    if not events_path.exists():
        return envelopes
    source_key = hashlib.blake2b(
        str(events_path).encode("utf-8"), digest_size=4,
    ).hexdigest()
    try:
        with events_path.open("r", encoding="utf-8", errors="ignore") as handle:
            while True:
                start_offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(
                        "chat.transcripts: skipping malformed line in %s @ %d",
                        events_path, start_offset,
                    )
                    continue
                if not isinstance(event, dict):
                    continue
                converted = _event_to_envelopes(
                    event,
                    offset=start_offset,
                    source_key=source_key,
                    include_thinking=include_thinking,
                    actor_fallback=actor_fallback,
                )
                envelopes.extend(converted)
    except OSError as exc:
        logger.warning(
            "chat.transcripts: read failed for %s: %s",
            events_path, exc,
        )
        return envelopes
    return envelopes


def _event_to_envelopes(
    event: dict[str, Any],
    *,
    offset: int,
    source_key: str,
    include_thinking: bool,
    actor_fallback: str,
) -> list[MessageEnvelope]:
    """Translate one ingestor event into 0+ envelopes.

    Most events emit exactly one envelope; the multi-emit case is
    reserved for future Codex shapes that bundle several user/assistant
    turns in a single ``event_msg`` line.
    """
    event_type = event.get("event_type")
    timestamp = str(event.get("timestamp") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    provider = str(event.get("provider") or "")
    msg_id = _envelope_id(source_key, offset, payload, event_type)

    if event_type == "user_turn":
        return [_envelope_user_turn(event, payload, msg_id, timestamp)]
    if event_type == "assistant_turn":
        return [_envelope_assistant_turn(
            event, payload, msg_id, timestamp, actor_fallback,
        )]
    if event_type == "tool_call":
        return _envelope_tool_call(
            event, payload, msg_id, timestamp, provider, actor_fallback,
        )
    if event_type == "tool_result":
        return _envelope_tool_result(
            event, payload, msg_id, timestamp, provider,
        )
    if event_type == "error":
        return [_envelope_error(event, payload, msg_id, timestamp)]
    if event_type == "turn_end":
        return [_envelope_turn_end(event, payload, msg_id, timestamp)]
    # ``token_usage`` and ``session_state`` are not chat messages per
    # the spec — skip silently. The token data lives on the analytics
    # rail and the session_state marker is handled by the registry.
    return []


def _envelope_id(
    source_key: str,
    offset: int,
    payload: dict[str, Any],
    event_type: Any,
) -> str:
    """Derive a stable, unique envelope id.

    Prefer the provider's own ``tool_use_id`` when present (so a
    follow-up ``tool_result`` can link to the spawn deterministically
    even when the offsets differ). Fall back to ``msg_<sourcekey>_<offset>``
    which is unique within a file.
    """
    tool_use_id = payload.get("id") if isinstance(payload, dict) else None
    if isinstance(tool_use_id, str) and tool_use_id:
        if event_type == "tool_call":
            return f"msg_{tool_use_id}"
    return f"msg_{source_key}_{offset:08x}"


def _extract_text_from_blocks(value: Any) -> str:
    """Mirror :func:`transcript_ingest._extract_text` for our parsing.

    Claude content can be a string, a list of ``{type, text}`` blocks,
    or a dict. The ingestor flattens it into one string for
    ``user_turn`` / ``assistant_turn`` events, but ``tool_result``
    events keep the raw content list — we have to unwrap it here.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif isinstance(item, str) and item:
                parts.append(item)
        return "\n".join(parts).strip()
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
    return ""


def _envelope_user_turn(
    event: dict[str, Any],
    payload: dict[str, Any],
    msg_id: str,
    timestamp: str,
) -> MessageEnvelope:
    text = str(payload.get("text") or "")
    return MessageEnvelope(
        id=msg_id,
        ts=timestamp,
        role=MessageRole.USER,
        actor="user",
        type=MessageType.TEXT,
        text=text,
        metadata={"provider": event.get("provider", "")},
    )


def _envelope_assistant_turn(
    event: dict[str, Any],
    payload: dict[str, Any],
    msg_id: str,
    timestamp: str,
    actor_fallback: str,
) -> MessageEnvelope:
    text = str(payload.get("text") or "")
    return MessageEnvelope(
        id=msg_id,
        ts=timestamp,
        role=MessageRole.ASSISTANT,
        actor=actor_fallback,
        type=MessageType.TEXT,
        text=text,
        metadata={
            "provider": event.get("provider", ""),
            "model": event.get("model_name") or "",
        },
    )


def _envelope_tool_call(
    event: dict[str, Any],
    payload: dict[str, Any],
    msg_id: str,
    timestamp: str,
    provider: str,
    actor_fallback: str,
) -> list[MessageEnvelope]:
    """Branch the ``tool_call`` event by tool name into the right shape.

    Claude tool calls carry ``{type:"tool_use", id, name, input}``.
    Codex tool calls have a different shape (handled inline below).
    """
    if provider == "claude":
        tool_name = str(payload.get("name") or "")
        tool_input = payload.get("input") if isinstance(payload.get("input"), dict) else {}
        tool_use_id = str(payload.get("id") or msg_id)
        if tool_name in _TASK_TOOL_NAMES:
            return [_envelope_subagent_spawn(
                event, payload, msg_id, timestamp,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=tool_use_id,
                actor_fallback=actor_fallback,
            )]
        if tool_name in _ASK_USER_TOOL_NAMES:
            return [_envelope_ask_user(
                event, payload, msg_id, timestamp,
                tool_input=tool_input,
                tool_use_id=tool_use_id,
                actor_fallback=actor_fallback,
            )]
        if tool_name in _FILE_TOOL_NAMES:
            return [_envelope_file(
                event, payload, msg_id, timestamp,
                tool_input=tool_input,
                tool_use_id=tool_use_id,
                actor_fallback=actor_fallback,
            )]
        text = _format_tool_use_summary(tool_name, tool_input)
        return [MessageEnvelope(
            id=msg_id,
            ts=timestamp,
            role=MessageRole.ASSISTANT,
            actor=actor_fallback,
            type=MessageType.TOOL_USE,
            text=text,
            metadata={
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            },
        )]
    # Codex path — payload is the raw event_msg payload. Best-effort:
    # render the tool name + a short input summary, defer the deeper
    # parsing (Codex tool shapes are still in flux) to a P3 follow-up.
    tool_name = str(
        payload.get("name") or payload.get("tool") or payload.get("type") or "tool"
    )
    return [MessageEnvelope(
        id=msg_id,
        ts=timestamp,
        role=MessageRole.ASSISTANT,
        actor=actor_fallback,
        type=MessageType.TOOL_USE,
        text=f"[{tool_name}]",
        metadata={
            "tool_use_id": msg_id,
            "tool_name": tool_name,
            "tool_input": payload,
        },
    )]


def _envelope_tool_result(
    event: dict[str, Any],
    payload: dict[str, Any],
    msg_id: str,
    timestamp: str,
    provider: str,
) -> list[MessageEnvelope]:
    """Tool result envelope; link to spawn via ``tool_use_id``."""
    if provider == "claude":
        tool_use_id = str(payload.get("tool_use_id") or "")
        content = payload.get("content") or []
        is_error = bool(payload.get("is_error", False))
        text = _extract_text_from_blocks(content)
        # If this is a Task tool result, emit ``subagent_result``.
        # The spawn-side envelope ALSO carries the tool_use_id under
        # ``metadata.subagent_id`` so the P2 router can pair them.
        if _looks_like_subagent_result(content):
            return [_envelope_subagent_result(
                event, payload, msg_id, timestamp,
                tool_use_id=tool_use_id,
                content=content,
                text=text,
            )]
        return [MessageEnvelope(
            id=msg_id,
            ts=timestamp,
            role=MessageRole.TOOL,
            actor="tool",
            type=MessageType.TOOL_RESULT,
            text=text,
            metadata={
                "tool_use_id": tool_use_id,
                "is_error": is_error,
                "content": content,
            },
        )]
    # Codex: leave the structured payload intact, render a short text.
    summary = _extract_text_from_blocks(
        payload.get("output") or payload.get("text") or payload.get("content"),
    )
    return [MessageEnvelope(
        id=msg_id,
        ts=timestamp,
        role=MessageRole.TOOL,
        actor="tool",
        type=MessageType.TOOL_RESULT,
        text=summary,
        metadata={"tool_use_id": "", "is_error": False, "content": payload},
    )]


def _envelope_subagent_spawn(
    event: dict[str, Any],
    payload: dict[str, Any],
    msg_id: str,
    timestamp: str,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
    actor_fallback: str,
) -> MessageEnvelope:
    description = str(tool_input.get("description") or "")
    summary = description or "subagent"
    return MessageEnvelope(
        id=msg_id,
        ts=timestamp,
        role=MessageRole.ASSISTANT,
        actor=actor_fallback,
        type=MessageType.SUBAGENT_SPAWN,
        text=f"[subagent] {summary}",
        metadata={
            "subagent_id": tool_use_id,
            "subagent_type": str(tool_input.get("subagent_type") or "general-purpose"),
            "description": description,
            "prompt": str(tool_input.get("prompt") or ""),
            "isolation": str(tool_input.get("isolation") or ""),
            "run_in_background": bool(tool_input.get("run_in_background", False)),
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
        },
    )


def _envelope_subagent_result(
    event: dict[str, Any],
    payload: dict[str, Any],
    msg_id: str,
    timestamp: str,
    *,
    tool_use_id: str,
    content: Any,
    text: str,
) -> MessageEnvelope:
    """Render a Task tool's result as a subagent_result envelope.

    P1 surfaces the text summary + the ``tool_use_id`` (which the
    spawn-side envelope ALSO carries as ``subagent_id``) so the P2
    router can pair spawn ↔ result. We do NOT inline the subagent's
    own transcript here — that requires the ingestor to preserve
    ``task-notification`` block ``output-file`` references (the JSONL
    path of the subagent's own session). See follow-up issue noted in
    the P1 PR description.
    """
    notification = _extract_task_notification(content)
    metadata: dict[str, Any] = {
        "subagent_id": tool_use_id,
        "tool_use_id": tool_use_id,
        "summary": text[:200] if text else "",
        "result_body_truncated": False,
        "content": content,
    }
    if notification:
        metadata.update({
            "task_id": str(notification.get("task-id") or ""),
            "output_file": str(notification.get("output-file") or ""),
            "duration_ms": int(notification.get("duration-ms") or 0),
            "total_tokens": int(notification.get("total-tokens") or 0),
            "worktree_path": str(notification.get("worktree-path") or ""),
        })
    return MessageEnvelope(
        id=msg_id,
        ts=timestamp,
        role=MessageRole.TOOL,
        actor="tool",
        type=MessageType.SUBAGENT_RESULT,
        text=text or "(subagent completed)",
        metadata=metadata,
    )


def _envelope_ask_user(
    event: dict[str, Any],
    payload: dict[str, Any],
    msg_id: str,
    timestamp: str,
    *,
    tool_input: dict[str, Any],
    tool_use_id: str,
    actor_fallback: str,
) -> MessageEnvelope:
    questions_raw = tool_input.get("questions")
    if not isinstance(questions_raw, list):
        questions_raw = []
    primary_text = ""
    if questions_raw:
        first = questions_raw[0]
        if isinstance(first, dict):
            primary_text = str(first.get("question") or "")
    return MessageEnvelope(
        id=msg_id,
        ts=timestamp,
        role=MessageRole.ASSISTANT,
        actor=actor_fallback,
        type=MessageType.ASK_USER,
        text=primary_text or "[ask_user]",
        metadata={
            "tool_use_id": tool_use_id,
            "questions": questions_raw,
            "answered": False,
            "answers": None,
        },
    )


def _envelope_file(
    event: dict[str, Any],
    payload: dict[str, Any],
    msg_id: str,
    timestamp: str,
    *,
    tool_input: dict[str, Any],
    tool_use_id: str,
    actor_fallback: str,
) -> MessageEnvelope:
    files_raw = tool_input.get("files")
    if isinstance(files_raw, str):
        files = [files_raw]
    elif isinstance(files_raw, list):
        files = [str(item) for item in files_raw if isinstance(item, (str, Path))]
    else:
        files = []
    caption = str(tool_input.get("caption") or "")
    status = str(tool_input.get("status") or "")
    display = ", ".join(files) if files else "(no files)"
    return MessageEnvelope(
        id=msg_id,
        ts=timestamp,
        role=MessageRole.ASSISTANT,
        actor=actor_fallback,
        type=MessageType.FILE,
        text=f"[file] {display}",
        metadata={
            "tool_use_id": tool_use_id,
            "files": files,
            "caption": caption,
            "status": status,
        },
    )


def _envelope_error(
    event: dict[str, Any],
    payload: dict[str, Any],
    msg_id: str,
    timestamp: str,
) -> MessageEnvelope:
    error_blob = payload.get("error") if isinstance(payload, dict) else payload
    text = ""
    if isinstance(error_blob, dict):
        text = str(
            error_blob.get("message")
            or error_blob.get("text")
            or json.dumps(error_blob, default=str)[:200],
        )
    else:
        text = str(error_blob or "(error)")
    return MessageEnvelope(
        id=msg_id,
        ts=timestamp,
        role=MessageRole.SYSTEM,
        actor="system",
        type=MessageType.SYSTEM_EVENT,
        text=text,
        metadata={"subtype": "error", "error": error_blob},
    )


def _envelope_turn_end(
    event: dict[str, Any],
    payload: dict[str, Any],
    msg_id: str,
    timestamp: str,
) -> MessageEnvelope:
    """Codex-only — Claude doesn't emit a turn-end marker.

    Surfaced as a system_event so clients can render turn boundaries
    when they want them; default UIs should hide it.
    """
    return MessageEnvelope(
        id=msg_id,
        ts=timestamp,
        role=MessageRole.SYSTEM,
        actor="system",
        type=MessageType.SYSTEM_EVENT,
        text="(turn end)",
        metadata={"subtype": "turn_end", "raw": payload},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_tool_use_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
    """One-liner display string for a tool_use envelope.

    Per spec §3.2 the API formats things like ``[Bash] git status`` so
    front-ends can render tool calls without parsing the input dict.
    """
    if not tool_name:
        return "[tool]"
    if tool_name == "Bash":
        cmd = str(tool_input.get("command") or "").strip()
        # Bash commands can be multi-line; clamp to a single line.
        first_line = cmd.splitlines()[0] if cmd else ""
        clamp = first_line[:120]
        return f"[Bash] {clamp}" if clamp else "[Bash]"
    if tool_name in {"Read", "Edit", "Write", "Glob", "Grep"}:
        path = (
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("pattern")
            or ""
        )
        return f"[{tool_name}] {path}".rstrip()
    if tool_name == "WebFetch":
        return f"[WebFetch] {tool_input.get('url') or ''}".rstrip()
    if tool_name == "WebSearch":
        return f"[WebSearch] {tool_input.get('query') or ''}".rstrip()
    description = tool_input.get("description") or tool_input.get("query") or ""
    return f"[{tool_name}] {description}".rstrip() if description else f"[{tool_name}]"


def _looks_like_subagent_result(content: Any) -> bool:
    """Detect whether a ``tool_result`` content list is from Task/Agent.

    Claude returns Task results as a list with a ``task-notification``
    block carrying ``task-id`` + ``output-file`` keys (along with the
    summary text). This is the strongest signal that the parent
    tool_use was a subagent spawn.
    """
    if not isinstance(content, list):
        return False
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "task-notification":
            return True
        # Some Claude versions wrap in a structured dict with this key.
        if "task-id" in item and "output-file" in item:
            return True
    return False


def _extract_task_notification(content: Any) -> dict[str, Any] | None:
    """Pull the first ``task-notification`` block out of a result content."""
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "task-notification":
            return item
        if "task-id" in item and "output-file" in item:
            return item
    return None


__all__ = [
    "STALE_THRESHOLD_SECONDS",
    "is_archive_stale",
    "parse_events_jsonl",
    "resolve_transcript_path",
]
