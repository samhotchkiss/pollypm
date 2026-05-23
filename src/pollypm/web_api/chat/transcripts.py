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
- Compaction / session-start markers become ``system_event``.

The ingestor does NOT currently preserve provider ``thinking`` blocks,
so the P1 envelope contract does not surface them. Tracking that work
as a follow-up arc (see GitHub issue linked in the P1 PR description).

The parser is pure: takes a file path + flags, returns a list of
envelopes. The P2 router layers pagination / filtering on top.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
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


# A "session-index" entry: one record per ingested ``events.jsonl``
# under a project's transcripts root. The registry builds the index
# ONCE per request (via :func:`build_session_index`) and then matches
# each surface by its fingerprint (cwd + account + provider) via
# :func:`lookup_transcript_path` — no per-surface filesystem fan-out.
#
# Codex review blocker (#2044): the prior implementation fell back to
# "freshest mtime under the project" when cwd matching had ties, which
# could cross-attach the operator's transcript to the architect (or
# vice-versa) whenever two surfaces shared a cwd. The fingerprint-based
# index returns ``None`` for ambiguous surfaces instead of guessing.
@dataclass(frozen=True, slots=True)
class _SessionIndexEntry:
    path: Path
    session_id: str
    cwd: str
    account_name: str
    provider: str
    mtime: float


def build_session_index(transcripts_root: Path) -> list[_SessionIndexEntry]:
    """Walk a project's transcripts root and index every ``events.jsonl``.

    Reads the first well-formed event from each ``<session_id>/events.jsonl``
    to extract ``(session_id, cwd, account_name, provider)`` — the
    fingerprint we match a :class:`pollypm.models.SessionConfig` against.

    Skips the ``tasks/`` subdirectory (raw-provider JSONLs archived by
    :meth:`SessionManager._archive_jsonl`) and any malformed/empty files.

    Cheaper than the prior tail-read because the first event of any
    normalized ``events.jsonl`` already carries the full ``_event_base``
    metadata (see :func:`pollypm.transcript_ingest._event_base`).
    """
    if not transcripts_root.exists():
        return []
    entries: list[_SessionIndexEntry] = []
    for child in transcripts_root.iterdir():
        if not child.is_dir():
            continue
        if child.name in {"tasks", ".ingestion-state.lock"}:
            # ``tasks/`` holds per-task raw-provider-JSONL archives
            # written by SessionManager._archive_jsonl; not normalized.
            continue
        events_path = child / "events.jsonl"
        if not events_path.exists():
            continue
        try:
            mtime = events_path.stat().st_mtime
        except OSError:
            continue
        fingerprint = _read_first_event_fingerprint(events_path)
        if fingerprint is None:
            # Empty / malformed — skip so we never serve an
            # unidentifiable transcript to the wrong surface.
            continue
        session_id, cwd, account_name, provider = fingerprint
        # Prefer the directory name as the canonical session_id (the
        # ingestor names dirs by session_id), but fall back to the event's
        # session_id if the dir name diverges.
        entries.append(_SessionIndexEntry(
            path=events_path,
            session_id=child.name or session_id,
            cwd=cwd,
            account_name=account_name,
            provider=provider,
            mtime=mtime,
        ))
    return entries


def lookup_transcript_path(
    index: list[_SessionIndexEntry],
    *,
    cwd: str | None,
    account_name: str | None = None,
    provider: str | None = None,
) -> Path | None:
    """Resolve a single surface's events.jsonl path from the index.

    Matching is fingerprint-based, NOT "freshest under the project":

    1. ``cwd`` must match the surface's cwd (resolved). Required.
    2. When ``account_name`` is given, entries with a different
       ``account_name`` are filtered out.
    3. When ``provider`` is given, entries with a different
       ``provider`` are filtered out.
    4. If multiple entries remain (e.g. a restarted session left two
       events.jsonl with the same fingerprint), the most-recent mtime
       wins — same fingerprint means same surface identity, so this
       can never cross-attach to a different surface.
    5. No matches → ``None`` (spec §4.8 — surface ``messages: []``).
       Cross-surface fallback is explicitly forbidden.
    """
    if not index or not cwd:
        return None
    try:
        normalized = str(Path(cwd).resolve())
    except (OSError, RuntimeError):
        normalized = str(cwd)
    matches: list[_SessionIndexEntry] = []
    for entry in index:
        if not entry.cwd:
            continue
        try:
            entry_cwd = str(Path(entry.cwd).resolve())
        except (OSError, RuntimeError):
            entry_cwd = entry.cwd
        if entry_cwd != normalized:
            continue
        if account_name and entry.account_name and entry.account_name != account_name:
            continue
        if provider and entry.provider and entry.provider != provider:
            continue
        matches.append(entry)
    if not matches:
        return None
    matches.sort(key=lambda item: item.mtime, reverse=True)
    return matches[0].path


def resolve_transcript_path(
    project_root: Path,
    cwd: str | None = None,
    *,
    account_name: str | None = None,
    provider: str | None = None,
) -> Path | None:
    """One-shot fingerprint resolver for a single surface.

    Builds the index for ``project_root`` then looks up by fingerprint.
    Callers iterating multiple surfaces should use
    :func:`build_session_index` + :func:`lookup_transcript_path` to
    amortize the scan; this helper exists for one-off resolution and to
    keep the public surface ergonomic.

    Returns ``None`` when no transcript matches the fingerprint — never
    falls back to another surface's archive.
    """
    root = project_transcripts_dir(project_root)
    index = build_session_index(root)
    return lookup_transcript_path(
        index, cwd=cwd, account_name=account_name, provider=provider,
    )


def _read_first_event_fingerprint(
    events_path: Path,
) -> tuple[str, str, str, str] | None:
    """Return ``(session_id, cwd, account_name, provider)`` from line 1.

    The ingestor writes ``_event_base`` dicts that always carry these
    four fields, so we only need to read one well-formed line. Returns
    ``None`` when the file is empty or contains nothing decodable.
    """
    try:
        with events_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for _ in range(8):  # Tolerate a handful of leading blanks.
                line = handle.readline()
                if not line:
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                session_id = str(obj.get("session_id") or "")
                cwd = str(obj.get("cwd") or "")
                account_name = str(obj.get("account_name") or "")
                provider = str(obj.get("provider") or "")
                return (session_id, cwd, account_name, provider)
    except OSError:
        return None
    return None


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


# mtime-keyed cache for ``parse_events_jsonl`` (issue #2069).
#
# The history endpoint polls every ~5s and the parse body forward-
# readline's the full archive each call (operator transcripts hit
# ~2,800 lines quickly). When the file hasn't been touched since the
# last parse we can return the cached envelopes instead of re-parsing.
#
# Cache key is the ``Path`` instance the caller passed (resolution
# left to the caller — the chat surfaces always pass the same Path).
# Cache value is ``(mtime, envelopes, actor_fallback)`` so a caller
# changing ``actor_fallback`` (different surface persona on the same
# archive) doesn't get a stale fallback baked into the envelopes.
#
# Insertion-ordered dict + ``next(iter(...))`` gives us LRU-by-
# insertion eviction without an extra dependency. The cap (100) is
# generous given each entry is one events.jsonl per chat surface.
_PARSE_CACHE: dict[Path, tuple[float, list[MessageEnvelope], str]] = {}
_PARSE_CACHE_MAX = 100


def _parse_cache_clear() -> None:
    """Drop every cached parse result. Test-only helper."""
    _PARSE_CACHE.clear()


def parse_events_jsonl(
    events_path: Path,
    *,
    actor_fallback: str = "agent",
    strict: bool = False,
) -> list[MessageEnvelope]:
    """Parse an ``events.jsonl`` archive into envelopes.

    ``actor_fallback`` — used when the event doesn't carry a clear
    actor name. The registry layer passes the surface's persona name
    here so worker transcripts say ``"worker"`` and operator transcripts
    say ``"Polly"``.

    Malformed lines are skipped with a debug log; the parser never
    raises on bad JSON because partially-flushed archives are normal
    (the ingestor appends line-by-line, the API may read mid-flush).

    ``strict=False`` (default) preserves the historical fail-soft
    posture: ``OSError`` (unreadable archive, permission denied,
    transient I/O fault) is logged and the parser returns whatever
    envelopes were accumulated before the failure. This is what
    ``source=auto`` needs so it can fall back to tmux capture.

    ``strict=True`` propagates ``OSError`` so explicit ``source=jsonl``
    callers can map an unreadable archive to a typed 503
    ``archive_unreadable`` instead of silently returning ``200`` with
    an empty list (round-5 blocker 2). The chat-messages route catches
    the propagated ``OSError`` and translates it. ``strict=True``
    callers also skip the mtime cache so validation paths always see a
    fresh parse.

    Caching (issue #2069): for ``strict=False`` callers we memoize on
    ``(events_path, mtime, actor_fallback)``. The history endpoint
    polls every few seconds and the underlying read forward-readlines
    the full archive each call; with operator transcripts running into
    the thousands of lines this dominates poll latency. When the file
    mtime is unchanged the cached envelopes are returned directly.

    NOTE: provider ``thinking`` blocks are not surfaced — the
    transcript ingestor does not currently preserve them. Tracking the
    follow-up work as a separate arc (linked from the P1 PR).
    """
    envelopes: list[MessageEnvelope] = []
    if not events_path.exists():
        return envelopes
    # Cache lookup happens BEFORE the heavy parse. We stat once up
    # front; ``strict=True`` callers skip the cache so validation paths
    # always see a fresh parse + propagated OSError.
    cache_eligible = not strict
    cached_mtime: float | None = None
    if cache_eligible:
        try:
            cached_mtime = events_path.stat().st_mtime
        except OSError:
            # Stat failure mirrors the existing fail-soft posture —
            # fall through to the normal parse path so any OSError is
            # logged with the same context as before.
            cached_mtime = None
        if cached_mtime is not None:
            cached = _PARSE_CACHE.get(events_path)
            if (
                cached is not None
                and cached[0] == cached_mtime
                and cached[2] == actor_fallback
            ):
                # Defensive copy: callers downstream sort/filter the
                # list in-place (see _apply_filters_and_paginate's
                # ``indexed.sort``), so handing them the cached list
                # directly would corrupt the cache on the next hit.
                return list(cached[1])
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
                    actor_fallback=actor_fallback,
                )
                envelopes.extend(converted)
    except OSError as exc:
        if strict:
            # Let the route translate this into a typed 503
            # ``archive_unreadable`` (round-5 blocker 2).
            raise
        logger.warning(
            "chat.transcripts: read failed for %s: %s",
            events_path, exc,
        )
        return envelopes
    if cache_eligible and cached_mtime is not None:
        # Evict oldest insertion before storing the new entry. Python
        # dicts preserve insertion order so ``next(iter(...))`` gives
        # us the oldest key without an auxiliary structure. Pop the
        # current key first (if present) so re-stores refresh ordering.
        _PARSE_CACHE.pop(events_path, None)
        if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX:
            _PARSE_CACHE.pop(next(iter(_PARSE_CACHE)))
        # Store a fresh list so test/in-place mutations on the
        # returned copy don't bleed back into the cache.
        _PARSE_CACHE[events_path] = (cached_mtime, list(envelopes), actor_fallback)
    return envelopes


# Tail-read chunk progression for ``parse_events_jsonl_tail`` (issue
# #2070). 64KB is enough to cover ~200 average lines, which satisfies
# the common ``?limit=50`` cockpit poll without re-reading. If the
# requested envelope count isn't met we widen to 256KB, then 1MB,
# then give up and fall through to the full forward parse. The
# progression is deliberately short — three attempts is enough to
# absorb the long-tail of multi-kilobyte tool_result payloads without
# silently re-reading the whole file in a loop.
_TAIL_CHUNK_PROGRESSION = (64 * 1024, 256 * 1024, 1024 * 1024)


def parse_events_jsonl_tail(
    events_path: Path,
    *,
    limit: int,
    actor_fallback: str = "agent",
) -> list[MessageEnvelope]:
    """Return roughly the last ``limit`` envelopes by tail-reading the file.

    Optimization for the cold-path miss of the mtime cache (issue #2069
    composes with this — that cache covers steady-state polls; tail-
    read covers the first switch + every post-write call where the
    file has changed). The default ``parse_events_jsonl`` forward-
    readlines the entire archive even when the caller only wants the
    last ``?limit=50`` envelopes; for operator transcripts running to
    thousands of lines this dominates first-load latency (issue #2070).

    Strategy:

    1. Seek ``chunk`` bytes from EOF (start 64KB) and read forward.
    2. Drop the first partial line (we almost certainly seeked into
       the middle of one) UNLESS we landed at byte 0 (whole file fits
       in one chunk).
    3. Parse the remaining lines forward, mirroring
       :func:`parse_events_jsonl` line-by-line.
    4. If we got fewer than ``limit`` valid envelopes AND we haven't
       hit byte 0, widen to the next chunk size (64KB → 256KB → 1MB)
       and retry. After the last chunk size still falls short, fall
       through to the full forward parse and slice — the file is
       legitimately small or sparsely populated.
    5. ``UnicodeDecodeError`` or any other parse exception bails to
       the full forward parser (multi-line / pretty-printed events
       can confuse a chunk-boundary read; the forward parser handles
       those at line granularity).

    Cache composition: a hit against ``_PARSE_CACHE`` short-circuits
    even cheaper than the tail read, so we check it first. We do NOT
    populate the cache from this path — the tail returns a partial
    list, and seeding the cache with a partial list would corrupt
    subsequent full-history reads.

    Returns ``[]`` for missing files (matches ``parse_events_jsonl``).
    """
    if limit <= 0:
        return []
    if not events_path.exists():
        return []

    # Cache lookup first — the mtime cache holds the FULL parse, which
    # is strictly more accurate than what tail-read can synthesize.
    cached_mtime: float | None = None
    try:
        cached_mtime = events_path.stat().st_mtime
    except OSError:
        cached_mtime = None
    if cached_mtime is not None:
        cached = _PARSE_CACHE.get(events_path)
        if (
            cached is not None
            and cached[0] == cached_mtime
            and cached[2] == actor_fallback
        ):
            # Tail of the cached list. Defensive copy: downstream
            # callers sort/filter in place.
            return list(cached[1][-limit:])

    source_key = hashlib.blake2b(
        str(events_path).encode("utf-8"), digest_size=4,
    ).hexdigest()

    try:
        file_size = events_path.stat().st_size
    except OSError as exc:
        logger.warning(
            "chat.transcripts: tail stat failed for %s: %s",
            events_path, exc,
        )
        return parse_events_jsonl(
            events_path, actor_fallback=actor_fallback,
        )[-limit:]

    if file_size == 0:
        return []

    for chunk_size in _TAIL_CHUNK_PROGRESSION:
        try:
            envelopes = _parse_tail_chunk(
                events_path,
                file_size=file_size,
                chunk_size=chunk_size,
                source_key=source_key,
                actor_fallback=actor_fallback,
            )
        except (UnicodeDecodeError, OSError) as exc:
            # Chunk-boundary read landed inside a multi-byte sequence
            # or the file went away mid-read — fall back to the full
            # forward parser which reads with ``errors="ignore"`` and
            # tolerates partial reads.
            logger.debug(
                "chat.transcripts: tail-read fell back for %s (%s)",
                events_path, exc,
            )
            return parse_events_jsonl(
                events_path, actor_fallback=actor_fallback,
            )[-limit:]
        if envelopes is None:
            # Sentinel: chunk parse hit a defensive bailout (e.g. a
            # malformed-but-not-skipped line). Treat the same as a
            # decode error and use the forward parser.
            return parse_events_jsonl(
                events_path, actor_fallback=actor_fallback,
            )[-limit:]
        if len(envelopes) >= limit:
            return envelopes[-limit:]
        if chunk_size >= file_size:
            # We read the whole file already — no point widening.
            return envelopes[-limit:]

    # Exhausted the chunk progression without satisfying ``limit``.
    # Fall through to the forward parser — the file is large but
    # sparsely populated with valid envelopes (lots of dropped
    # token_usage / malformed lines).
    return parse_events_jsonl(
        events_path, actor_fallback=actor_fallback,
    )[-limit:]


def _parse_tail_chunk(
    events_path: Path,
    *,
    file_size: int,
    chunk_size: int,
    source_key: str,
    actor_fallback: str,
) -> list[MessageEnvelope] | None:
    """Read ``chunk_size`` bytes from EOF, parse the lines, return envelopes.

    Returns ``None`` to signal "give up, use the forward parser"
    (e.g. multi-line JSON event broken across the seek boundary).
    Raises ``UnicodeDecodeError`` / ``OSError`` on read failure — the
    caller catches and falls back.
    """
    seek_offset = max(0, file_size - chunk_size)
    with events_path.open("rb") as handle:
        handle.seek(seek_offset)
        raw = handle.read()
    # Decode strictly so a mid-codepoint seek raises and the caller
    # falls back. The forward parser uses ``errors="ignore"`` for
    # fault tolerance, but here we'd rather bail than emit garbled
    # text from a half-codepoint at the chunk head.
    text = raw.decode("utf-8")

    # Drop the first partial line unless we landed at byte 0 — the
    # seek almost certainly bisected a line.
    if seek_offset > 0:
        newline_idx = text.find("\n")
        if newline_idx < 0:
            # No newline in the chunk at all — single huge line we
            # can't tail-parse safely. Bail to the forward parser.
            return None
        # The byte AFTER the newline is the first complete line.
        first_line_byte = seek_offset + len(
            text[: newline_idx + 1].encode("utf-8"),
        )
        text = text[newline_idx + 1 :]
    else:
        first_line_byte = 0

    envelopes: list[MessageEnvelope] = []
    cursor = first_line_byte
    for line in text.splitlines(keepends=True):
        line_offset = cursor
        cursor += len(line.encode("utf-8"))
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            logger.debug(
                "chat.transcripts: skipping malformed tail line in %s @ %d",
                events_path, line_offset,
            )
            continue
        if not isinstance(event, dict):
            continue
        converted = _event_to_envelopes(
            event,
            offset=line_offset,
            source_key=source_key,
            actor_fallback=actor_fallback,
        )
        envelopes.extend(converted)
    return envelopes


def _event_to_envelopes(
    event: dict[str, Any],
    *,
    offset: int,
    source_key: str,
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


# ---------------------------------------------------------------------------
# Raw Claude subagent JSONL parser (issue #2052).
#
# Subagents spawned via the Claude ``Task`` tool write their own JSONL
# stream to the path carried in the parent's ``task-notification``
# block as ``output-file``. That file is the RAW provider JSONL — the
# same shape :func:`pollypm.transcript_ingest._normalize_claude_line`
# consumes — NOT the normalized ``events.jsonl`` archive that
# :func:`parse_events_jsonl` consumes.
#
# We deliberately keep this resolver-side (in the chat API) rather
# than forcing the ingestor to normalize per-subagent: subagent
# transcripts are read-only artifacts referenced only when a caller
# explicitly opts into ``include_subagents=true`` on the messages
# endpoint, so paying the parse cost lazily at request time is
# strictly better than pre-normalizing every subagent JSONL on every
# ingest tick.
# ---------------------------------------------------------------------------


_RAW_USER_TYPES = frozenset({"user"})
_RAW_ASSISTANT_TYPES = frozenset({"assistant"})


def parse_raw_subagent_jsonl(
    path: Path,
    *,
    actor_fallback: str = "subagent",
    limit: int | None = None,
) -> list[MessageEnvelope]:
    """Parse a raw Claude subagent JSONL into :class:`MessageEnvelope` rows.

    The subagent JSONL shape (one JSON object per line) is what the
    Claude CLI writes verbatim — distinct from the normalized
    ``events.jsonl`` :func:`parse_events_jsonl` consumes:

    .. code-block:: json

        {
          "type": "user" | "assistant" | "error",
          "sessionId": "<uuid>",
          "cwd": "/path/to/project",
          "timestamp": "2026-05-21T20:48:11Z",
          "message": {
            "model": "claude-opus-4-7",
            "content": "..." | [{"type": "text", "text": "..."}, ...],
            "usage": {...}
          }
        }

    Each ``user`` / ``assistant`` line becomes one envelope; ``error``
    lines become ``system_event`` envelopes with ``subtype=error``.
    Anything else (``summary``, telemetry-only lines, malformed JSON)
    is skipped.

    ``limit`` (optional): cap on emitted envelopes. ``None`` returns
    every envelope. The caller (the chat-messages route) sets a small
    cap so the inlined transcript doesn't balloon the parent response.

    Fail-soft: every I/O or JSON failure is logged at debug and the
    caller receives whatever envelopes were accumulated so far. This
    mirrors the posture of :func:`parse_events_jsonl` with
    ``strict=False`` — the subagent transcript is a best-effort
    enrichment, never the primary signal.
    """
    envelopes: list[MessageEnvelope] = []
    if not path.exists():
        return envelopes
    source_key = hashlib.blake2b(
        str(path).encode("utf-8"), digest_size=4,
    ).hexdigest()
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for index, line in enumerate(handle):
                if limit is not None and len(envelopes) >= limit:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.debug(
                        "chat.transcripts: skipping malformed raw "
                        "subagent line in %s @ %d", path, index,
                    )
                    continue
                if not isinstance(obj, dict):
                    continue
                envelope = _raw_subagent_line_to_envelope(
                    obj,
                    index=index,
                    source_key=source_key,
                    actor_fallback=actor_fallback,
                )
                if envelope is not None:
                    envelopes.append(envelope)
    except OSError as exc:
        logger.warning(
            "chat.transcripts: read failed for raw subagent %s: %s",
            path, exc,
        )
    return envelopes


def _raw_subagent_line_to_envelope(
    obj: dict[str, Any],
    *,
    index: int,
    source_key: str,
    actor_fallback: str,
) -> MessageEnvelope | None:
    """Map one raw Claude JSONL line to a :class:`MessageEnvelope`.

    Returns ``None`` for line types we don't surface (``summary``,
    ``session_state``, malformed shapes). The id is deterministic per
    ``(file, line index)`` so a repeated parse yields stable ids.
    """
    line_type = obj.get("type")
    timestamp = str(obj.get("timestamp") or "")
    msg_id = f"sub_{source_key}_{index:06d}"
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = message.get("content") if isinstance(message, dict) else None

    if line_type in _RAW_USER_TYPES:
        text = _extract_text_from_blocks(content) or _extract_text_from_blocks(message)
        return MessageEnvelope(
            id=msg_id,
            ts=timestamp,
            role=MessageRole.USER,
            actor="user",
            type=MessageType.TEXT,
            text=text,
            metadata={
                "provider": "claude",
                "source": "subagent_jsonl",
                "session_id": str(obj.get("sessionId") or ""),
            },
        )
    if line_type in _RAW_ASSISTANT_TYPES:
        text = _extract_text_from_blocks(content) or _extract_text_from_blocks(message)
        model_name = ""
        if isinstance(message, dict):
            raw_model = message.get("model")
            if isinstance(raw_model, str):
                model_name = raw_model
        return MessageEnvelope(
            id=msg_id,
            ts=timestamp,
            role=MessageRole.ASSISTANT,
            actor=actor_fallback,
            type=MessageType.TEXT,
            text=text,
            metadata={
                "provider": "claude",
                "source": "subagent_jsonl",
                "session_id": str(obj.get("sessionId") or ""),
                "model": model_name,
            },
        )
    if line_type == "error":
        # Surface subagent errors as system_event so the caller's UI
        # doesn't lose them in the inlined stream.
        return MessageEnvelope(
            id=msg_id,
            ts=timestamp,
            role=MessageRole.SYSTEM,
            actor="system",
            type=MessageType.SYSTEM_EVENT,
            text=_extract_text_from_blocks(obj.get("error")) or "(subagent error)",
            metadata={
                "subtype": "error",
                "provider": "claude",
                "source": "subagent_jsonl",
            },
        )
    return None


__all__ = [
    "STALE_THRESHOLD_SECONDS",
    "build_session_index",
    "is_archive_stale",
    "lookup_transcript_path",
    "parse_events_jsonl",
    "parse_events_jsonl_tail",
    "parse_raw_subagent_jsonl",
    "resolve_transcript_path",
]
