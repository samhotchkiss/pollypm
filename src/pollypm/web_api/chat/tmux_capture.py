"""Fallback transcript reader using ``tmux capture-pane``.

Per spec §4.7 and §4.12 the GET endpoint falls back to a live pane
capture when the normalized archive is missing or stale (>60s) AND the
tmux pane is alive. The Codex CLI doesn't write the Claude JSONL shape
either, so Codex surfaces always land here.

Each captured line becomes one :class:`MessageEnvelope` with
``type=text`` and ``metadata.from_capture=true``. Envelope ids are
synthesized as ``cap_<hex>`` from a blake2b digest of
``f"{session_name}:{line_index}:{content}"`` so the same line read
twice yields the same id (stable, sortable within a capture).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from typing import Any

from pollypm.web_api.chat.envelope import (
    MessageEnvelope,
    MessageRole,
    MessageType,
)

logger = logging.getLogger(__name__)


# Default capture depth — spec §4.7 calls out ``tmux capture-pane -p -S -3000``.
# The pane scrollback ceiling lives in tmux config; -3000 lines is plenty
# for the typical agent session without flooding the response.
DEFAULT_CAPTURE_LINES = 3000


# ANSI escape sequence stripper. Tmux capture preserves color codes by
# default; strip them so the API returns clean text. Same regex shape
# as :mod:`pollypm.recovery.worker_turn_end`.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_C0_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def synthesize_capture_id(session_name: str, line_index: int, content: str) -> str:
    """Deterministic id for one captured pane line.

    Hash includes the session name + line offset + content so the same
    line captured twice yields the same id (stable) and two different
    lines on the same offset still differ (collision-resistant).
    """
    key = f"{session_name}:{line_index}:{content}".encode("utf-8")
    digest = hashlib.blake2b(key, digest_size=8).hexdigest()
    return f"cap_{digest}"


def capture_envelopes(
    tmux_client: Any,
    *,
    session_name: str,
    target: str,
    actor_fallback: str = "agent",
    lines: int = DEFAULT_CAPTURE_LINES,
    timestamp: str | None = None,
    role: MessageRole = MessageRole.ASSISTANT,
    strict: bool = False,
) -> list[MessageEnvelope]:
    """Capture a tmux pane and translate each line into an envelope.

    ``tmux_client`` — a :class:`pollypm.tmux.client.TmuxClient` (or
    test double exposing ``capture_pane(target, lines=...)``).

    ``session_name`` — the surface's session_name; used only for
    envelope id stability and the ``metadata.session_name`` field.

    ``target`` — tmux target string the client expects (e.g.
    ``"storage-closet:pm-operator"``). Built by the registry layer
    from the surface's tmux session + window.

    ``actor_fallback`` — actor name to stamp on every envelope.

    ``lines`` — capture depth (-S argument to tmux capture-pane).

    ``timestamp`` — ISO-8601 string for every envelope; defaults to
    capture wall-clock. (Pane captures don't carry per-line timestamps
    — the whole capture happened at the same instant from our PoV.)

    ``strict`` — when ``False`` (default) every tmux failure mode
    collapses to ``[]`` so the caller can fall back to the JSONL
    archive (spec §4.7 / §4.8 / ``source=auto``). When ``True`` the
    underlying ``capture_pane`` exception is re-raised so the caller
    (explicit ``source=capture``) can map it to a typed 503 instead of
    silently returning ``200`` + empty messages.
    """
    if tmux_client is None:
        return []
    capture_fn = getattr(tmux_client, "capture_pane", None)
    if not callable(capture_fn):
        return []
    try:
        raw = capture_fn(target, lines=lines)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "chat.tmux_capture: capture_pane failed for %s (target=%s): %s",
            session_name, target, exc,
        )
        if strict:
            raise
        return []
    if not isinstance(raw, str) or not raw:
        return []
    cleaned = _strip_control_codes(raw)
    ts = timestamp or _utc_now()
    envelopes: list[MessageEnvelope] = []
    for line_index, line in enumerate(cleaned.splitlines()):
        # Skip leading/trailing pure-whitespace lines but preserve internal
        # blank lines so the structure of a Claude-Code box is recognisable.
        if not line.strip() and not envelopes:
            continue
        envelope_id = synthesize_capture_id(session_name, line_index, line)
        envelopes.append(MessageEnvelope(
            id=envelope_id,
            ts=ts,
            role=role,
            actor=actor_fallback,
            type=MessageType.TEXT,
            text=line,
            metadata={
                "from_capture": True,
                "session_name": session_name,
                "line_index": line_index,
            },
        ))
    # Drop any trailing blank lines that we appended along the way —
    # they're just visual padding at the bottom of the pane.
    while envelopes and not envelopes[-1].text.strip():
        envelopes.pop()
    return envelopes


def _strip_control_codes(text: str) -> str:
    text = _ANSI_CSI_RE.sub("", text)
    text = _C0_CTRL_RE.sub("", text)
    return text


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_CAPTURE_LINES",
    "capture_envelopes",
    "synthesize_capture_id",
]
