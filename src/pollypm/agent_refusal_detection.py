"""Heuristics for server-side agent refusal observability.

The primary contract still lives in the agent prompt: agents should run
``pm audit agent-refusal`` when they refuse an unsigned or bad-marker
PollyPM control message. This module is the server-side safety net for
live transcripts where the visible refusal landed but the CLI audit call
did not.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable

from pollypm.audit.log import (
    AGENT_REFUSAL_REASON_BAD_AUTH_MARKER,
    AGENT_REFUSAL_REASON_UNSIGNED_POLLYPM_CLAIM,
)

_AUTH_MARKER_RE = re.compile(
    r"^\s*\[PollyPM-Auth:\s*([^\]\r\n]+)\]\s*",
    re.IGNORECASE,
)

_POLLYPM_CONTROL_CLAIM_RE = re.compile(
    r"""
    ^\s*
    (?:
        WATCHDOG\s+ESCALATION\b
        | RECOVERY\s+MODE\b
        | FROM\s+POLLYPM\b
        | POLLYPM\s+SAYS\b
        | POLLYPM\s*(?::|-)
        | POLLYPM\b.{0,120}\b(?:WATCHDOG|CONTROL|ESCALATION|RECOVERY|AUTH|OPERATOR)\b
        | PM\s+WATCHDOG\b
        | WATCHDOG\b.{0,120}\bPOLLYPM\b
    )
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

_REFUSAL_LANGUAGE_RE = re.compile(
    r"""
    \b(?:
        refus(?:e|ed|al|ing)?
        | prompt[-\s]?injection
        | untrusted
        | missing.{0,80}auth
        | auth.{0,80}(?:missing|invalid|mismatch|marker|token)
        | cannot.{0,60}verif(?:y|ied)
        | can['’]?t.{0,60}verif(?:y|ied)
        | will\s+not\s+comply
        | won['’]?t\s+comply
        | not\s+comply
        | ignored\s+unsigned
    )\b
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


def classify_pollypm_auth_claim(
    text: str | None,
    *,
    valid_auth_tokens: Iterable[str | None] = (),
) -> str | None:
    """Return the refusal audit reason for a suspicious PollyPM claim.

    ``None`` means the prompt is not part of the auth-marker refusal
    contract, or it carries a valid per-session marker. The raw prompt
    text is used only for classification and must not be written into
    audit metadata.
    """
    raw = str(text or "")
    if not raw.strip():
        return None

    marker = _AUTH_MARKER_RE.match(raw)
    if marker is not None:
        supplied = marker.group(1).strip()
        for token in valid_auth_tokens:
            expected = str(token or "").strip()
            if expected and secrets.compare_digest(supplied, expected):
                return None
        return AGENT_REFUSAL_REASON_BAD_AUTH_MARKER

    if _POLLYPM_CONTROL_CLAIM_RE.search(raw):
        return AGENT_REFUSAL_REASON_UNSIGNED_POLLYPM_CLAIM
    return None


def contains_refusal_language(text: str | None) -> bool:
    """True when assistant text visibly reads like a refusal."""
    raw = str(text or "")
    return bool(raw.strip() and _REFUSAL_LANGUAGE_RE.search(raw))


__all__ = [
    "classify_pollypm_auth_claim",
    "contains_refusal_language",
]
