"""Uniform :class:`MessageEnvelope` shape for chat endpoints.

Every message returned by ``GET /api/v1/chat/{session_name}/messages``
is one of these envelopes — even tool calls and subagent spawns. The
shape mirrors spec §3 verbatim so the JSON serialization (handled by
:mod:`pollypm.web_api.chat` consumers / the P2 router) is a direct
``dataclasses.asdict`` away.

Eight discriminators are defined in :class:`MessageType`. Each
envelope's ``metadata`` payload is type-specific; callers branch on
``envelope.type`` to interpret it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class MessageRole(StrEnum):
    """Speaker role per spec §3."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class MessageType(StrEnum):
    """Envelope discriminator per spec §3."""

    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    ASK_USER = "ask_user"
    FILE = "file"
    SUBAGENT_SPAWN = "subagent_spawn"
    SUBAGENT_RESULT = "subagent_result"
    SYSTEM_EVENT = "system_event"


@dataclass(slots=True)
class MessageEnvelope:
    """One chat message, normalized.

    Fields:

    ``id`` — stable id derived from the source event's file offset
    (archive path) or capture-line hash (tmux fallback). Sortable by
    timestamp when paired with ``ts``. Format: ``msg_<hex>`` for
    archived events, ``cap_<hex>`` for tmux-captured lines, or the raw
    ``tool_use_id`` / ``subagent_id`` when a more specific id exists.

    ``ts`` — ISO-8601 UTC with ``Z`` suffix, matching the ingestor
    output. Tmux-capture envelopes inherit the capture wall-clock.

    ``role`` — one of :class:`MessageRole`.

    ``actor`` — display name (``"Polly"``, ``"Archie"``, persona name,
    ``"system"``, etc.). The registry layer fills this in based on
    surface type when the event itself doesn't carry it.

    ``type`` — one of :class:`MessageType`; chooses the metadata shape.

    ``text`` — always populated with a display-ready string. For
    tool calls this is a one-liner like ``"[Bash] git status"``; for
    ``tool_result`` it's the textual content; for ``ask_user`` it's
    the question text. Front-ends can render this directly.

    ``metadata`` — type-specific payload. See spec §3.1-§3.8 for the
    per-type schemas.
    """

    id: str
    ts: str
    role: MessageRole
    actor: str
    type: MessageType
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for JSON encoding.

        Enum fields are coerced to their string values so
        ``json.dumps`` works without a custom encoder.
        """
        payload = asdict(self)
        payload["role"] = str(self.role)
        payload["type"] = str(self.type)
        return payload


__all__ = [
    "MessageEnvelope",
    "MessageRole",
    "MessageType",
]
