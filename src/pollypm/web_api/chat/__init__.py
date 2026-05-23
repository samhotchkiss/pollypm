"""Chat-surface data layer for the PollyPM Web API.

P1 ships the read-side primitives the chat endpoints (P2) compose:

- :mod:`envelope` — the uniform :class:`MessageEnvelope` dataclass and
  the type discriminators (``text``, ``thinking``, ``tool_use``,
  ``tool_result``, ``ask_user``, ``file``, ``subagent_spawn``,
  ``subagent_result``, ``system_event``) per spec §3. ``thinking`` is
  gated behind ``include_thinking=True`` on :func:`parse_events_jsonl`
  so default callers see no new types — see GitHub #2048.
- :mod:`registry` — enumerates every live chat surface (operator,
  architect, advisor, worker) into :class:`ChatSurface` dataclasses,
  resolving the tmux window + transcript path for each.
- :mod:`transcripts` — parses the normalized ``events.jsonl`` archive
  emitted by :mod:`pollypm.transcript_ingest` into envelopes.
- :mod:`tmux_capture` — fallback reader using ``tmux capture-pane`` when
  the archive is absent or stale (>60s).

No HTTP routes here — P2 (#TBD) wires these primitives into FastAPI.
Per the chat-endpoints spec (``~/Desktop/pollypm-chat-endpoints-spec.md``)
§6, the layered split keeps the parser unit-testable and lets the
router be a thin adapter.
"""

from __future__ import annotations

from pollypm.web_api.chat.envelope import (
    MessageEnvelope,
    MessageRole,
    MessageType,
)
from pollypm.web_api.chat.registry import (
    ChatSurface,
    SurfaceType,
    enumerate_chat_surfaces,
    enumerate_config_surfaces,
    enumerate_worker_surfaces,
)
from pollypm.web_api.chat.tmux_capture import (
    capture_envelopes,
    synthesize_capture_id,
)
from pollypm.web_api.chat.transcripts import (
    STALE_THRESHOLD_SECONDS,
    is_archive_stale,
    parse_events_jsonl,
    parse_events_jsonl_tail,
    resolve_transcript_path,
)

__all__ = [
    "STALE_THRESHOLD_SECONDS",
    "ChatSurface",
    "MessageEnvelope",
    "MessageRole",
    "MessageType",
    "SurfaceType",
    "capture_envelopes",
    "enumerate_chat_surfaces",
    "enumerate_config_surfaces",
    "enumerate_worker_surfaces",
    "is_archive_stale",
    "parse_events_jsonl",
    "parse_events_jsonl_tail",
    "resolve_transcript_path",
    "synthesize_capture_id",
]
