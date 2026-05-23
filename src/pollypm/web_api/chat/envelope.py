"""Uniform :class:`MessageEnvelope` shape for chat endpoints.

Every message returned by ``GET /api/v1/chat/{session_name}/messages``
is one of these envelopes — even tool calls and subagent spawns. The
shape mirrors spec §3 verbatim so the JSON serialization (handled by
:mod:`pollypm.web_api.chat` consumers / the P2 router) is a direct
``dataclasses.asdict`` away.

Nine discriminators are defined in :class:`MessageType`. Each
envelope's ``metadata`` payload is type-specific; callers branch on
``envelope.type`` to interpret it.

Parser-internal discriminators
------------------------------

:class:`ParserInternalType` mirrors the public ``MessageType`` shape
(``StrEnum`` with ``.value`` semantics) but is intentionally NOT part
of the HTTP-public catalog. ``parse_events_jsonl`` may emit envelopes
whose ``type`` is a :class:`ParserInternalType` member when the caller
asks for it (today: ``include_thinking=True`` surfaces Anthropic
extended-thinking blocks under
:attr:`ParserInternalType.THINKING`). The chat-messages route
defensively drops these before serialization so the wire contract — and
the ``ChatMessageType`` enum in ``docs/api/openapi.yaml`` — stays
exactly equal to the public :class:`MessageType` value set. Follow-up
#2082 will wire ``thinking`` through the public route + OpenAPI; until
then keeping the parser/route catalogs split is the documented
invariant pinned by
``tests/web_api/test_openapi_conformance.py::test_chat_message_type_enum_matches_runtime``.
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
    """Envelope discriminator per spec §3 — the HTTP-public catalog.

    This enum is the source of truth that ``ChatMessageType`` in
    ``docs/api/openapi.yaml`` mirrors. Adding a value here without
    updating the OpenAPI contract (and vice-versa) trips
    ``test_chat_message_type_enum_matches_runtime``.

    Parser-only discriminators (today: ``thinking``) live on
    :class:`ParserInternalType` instead so the public/wire enum stays
    closed until follow-up issues (#2082 for thinking) wire them through
    the endpoint contract.
    """

    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    ASK_USER = "ask_user"
    FILE = "file"
    SUBAGENT_SPAWN = "subagent_spawn"
    SUBAGENT_RESULT = "subagent_result"
    SYSTEM_EVENT = "system_event"


class ParserInternalType(StrEnum):
    """Parser-internal envelope discriminators.

    Values here are emitted by :func:`parse_events_jsonl` under explicit
    opt-in flags but are NOT part of the HTTP-public response catalog
    (``ChatMessageType`` in ``docs/api/openapi.yaml``). The
    chat-messages route filters envelopes whose ``type`` is in
    :data:`PARSER_INTERNAL_TYPE_VALUES` before serialization so generated
    HTTP clients never see them.

    Today this enum holds the single value ``thinking`` (Anthropic
    extended-thinking blocks, GitHub #2048). Follow-up #2082 will
    promote ``thinking`` into the public :class:`MessageType` and wire
    the matching ``include_thinking`` query param + OpenAPI enum entry
    through the route. At that point this enum can shrink (or be
    removed) and the route filter relaxed accordingly.
    """

    THINKING = "thinking"


# Frozen string set of parser-internal type values — used by the
# chat-messages route to drop envelopes that should not cross the HTTP
# boundary. Keeping it as a ``frozenset[str]`` (rather than reaching
# back to the enum) means callers can compare against a serialized
# envelope's ``type`` field without re-importing the enum.
PARSER_INTERNAL_TYPE_VALUES: frozenset[str] = frozenset(
    member.value for member in ParserInternalType
)


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

    ``type`` — one of :class:`MessageType` (HTTP-public) or
    :class:`ParserInternalType` (parser-only; filtered by the route).
    Chooses the metadata shape.

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
    type: MessageType | ParserInternalType
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
    "PARSER_INTERNAL_TYPE_VALUES",
    "ParserInternalType",
]
