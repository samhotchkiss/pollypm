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

When the pane is parked on an interactive ``AskUserQuestion`` TUI form
(Claude Code's checkbox/radio menu with a ``✔ Submit`` control), the
capture also synthesizes ONE structured ``type=ask_user`` envelope
carrying ``metadata.questions[].options[].label`` — the exact shape
``chat_send`` reads to validate an ``answer_to`` reply (§3.5) — so the
web UI can render answerable options instead of raw terminal chrome,
and the menu's own lines are dropped from the text stream.
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
    all_lines = cleaned.splitlines()

    # If the pane is parked on an interactive AskUserQuestion form, the
    # menu region is replaced by ONE structured ``ask_user`` envelope and
    # excluded from the text stream (it's terminal chrome otherwise).
    menu = extract_ask_user_menu(cleaned)
    text_line_count = menu["menu_start"] if menu else len(all_lines)

    envelopes: list[MessageEnvelope] = []
    for line_index, line in enumerate(all_lines[:text_line_count]):
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

    if menu:
        envelopes.append(_ask_user_envelope(
            menu,
            session_name=session_name,
            actor_fallback=actor_fallback,
            ts=ts,
        ))
    return envelopes


def _strip_control_codes(text: str) -> str:
    text = _ANSI_CSI_RE.sub("", text)
    text = _C0_CTRL_RE.sub("", text)
    return text


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# AskUserQuestion interactive-menu synthesis (M5 / #2476)
# --------------------------------------------------------------------------
# Claude Code renders an AskUserQuestion as a TUI form. The REAL on-screen
# shape (ground truth, captured live):
#
#   ──────────────────────────────────────────────────────────────────────
#   ←  ☐ Imagery medium  ☐ Hero treatment  ✔ Submit  →     <- tab-bar header
#                                                            <- blank
#   What medium should the imagery be? ... matches the palette —           |
#   and the existing IMAGE_NOTES literally warn against ... clichés.       | <- prompt (wraps)
#                                                            <- blank
#   ❯ 1. Bespoke SVG illustration                            <- option (selected)
#        Extend the existing book-stack.svg direction: ...   <- description (indented)
#     2. Curated photography
#        The original locked decision ...
#     3. Mix of both
#        ...
#     4. Type something.                                     <- affordance (EXCLUDE)
#   ──────────────────────────────────────────────────────────────────────
#     5. Chat about this                                     <- affordance (EXCLUDE)
#                                                            <- blank
#   Enter to select · Tab/Arrow keys to navigate · Esc to cancel  <- footer
#
# The three failure modes prior attempts hit, all handled here:
#   1. ``✔ Submit`` lives in the TOP tab-bar, with the prompt + options
#      BELOW it (not a trailing inline submit) — detection searches for the
#      submit line ABOVE the footer, and parsing walks DOWN from it.
#   2. The options sit below a blank line + a multi-line WRAPPED prompt —
#      the prompt run is skipped before option parsing begins.
#   3. ``4. Type something.`` / ``5. Chat about this`` are NUMBERED rows,
#      not bare lines — excluded by label after stripping the number.
#
# Output ``questions`` matches the §3.5 contract that ``chat_send`` reads
# to validate an ``answer_to`` reply: ``questions[].options[].label``.

_ASK_USER_FOOTER_RE = re.compile(r"enter to select.*esc to cancel", re.IGNORECASE)
_ASK_USER_TABARROW_RE = re.compile(r"(tab\s*/\s*arrow|arrow keys|tab\b.*\barrow)", re.IGNORECASE)
_ASK_USER_SUBMIT_RE = re.compile(r"[✔✓]\s*submit", re.IGNORECASE)
# Numbered option row: optional selection caret (❯ › ▶ >), a number, '.' or ')',
# then the option label.
_ASK_USER_NUMBERED_RE = re.compile(r"^[❯❯›▶▷>\-\*\s]*?(\d{1,2})[.)]\s+(\S.*?)\s*$")
_ASK_USER_TAB_GLYPHS = "☐☑☒✔✓◉○●◯◻◼▢▣"
# Trailing meta-affordances Claude appends to every AskUserQuestion — never
# real options. Matched on the label (after the leading number is stripped).
_ASK_USER_AFFORDANCE_LABELS = frozenset({
    "type something", "type something.",
    "chat about this", "chat about this.",
})
_ASK_USER_BOX_CHARS = set("─━╌╍┄┅╭╮╰╯│┆┇┊┋" + " ←→·")


def _ask_user_clean(line: str) -> str:
    """ANSI-strip + drop surrounding box-border chars, collapse to core text."""
    text = _ANSI_CSI_RE.sub("", line or "")
    text = _C0_CTRL_RE.sub("", text)
    text = text.strip()
    # Strip a leading/trailing vertical box border (│ ┆) the TUI may draw.
    if text[:1] in "│┆":
        text = text[1:].strip()
    if text[-1:] in "│┆":
        text = text[:-1].strip()
    return text


def _ask_user_is_separator(text: str) -> bool:
    """True for a blank line or a pure box/rule line (e.g. ───────)."""
    if not text:
        return True
    return set(text) <= _ASK_USER_BOX_CHARS


def _ask_user_is_footer(text: str) -> bool:
    if "`" in text:
        return False
    low = text.lower()
    return (
        _ASK_USER_FOOTER_RE.search(low) is not None
        and _ASK_USER_TABARROW_RE.search(low) is not None
    )


def _ask_user_is_submit(text: str) -> bool:
    return "`" not in text and _ASK_USER_SUBMIT_RE.search(text) is not None


def _ask_user_is_chrome_after_footer(text: str) -> bool:
    """Lines allowed to appear AFTER the footer (else it's not the live menu)."""
    if _ask_user_is_separator(text):
        return True
    low = text.lower()
    return any(tok in low for tok in (
        "bypass permissions", "shift+tab to cycle", "ctrl+t to", "tokens",
        "esc to", "ctrl+o", "shift+tab",
    ))


def _ask_user_headers(submit_line: str) -> list[str]:
    """Extract the per-question tab labels from the tab-bar / submit line.

    "←  ☐ Imagery medium  ☐ Hero treatment  ✔ Submit  →"
        -> ["Imagery medium", "Hero treatment"]   (Submit + arrows dropped)
    """
    segments = re.split(f"[{_ASK_USER_TAB_GLYPHS}]", submit_line)
    headers: list[str] = []
    for seg in segments:
        label = seg.replace("←", "").replace("→", "").strip(" ·\t")
        if not label or label.lower() == "submit":
            continue
        # A glyph-less single-tab form may put the whole bar in one segment;
        # drop a trailing "Submit" token if it rode along.
        label = re.sub(r"\s*\bsubmit\b\s*$", "", label, flags=re.IGNORECASE).strip()
        if label:
            headers.append(label)
    return headers


def extract_ask_user_menu(pane_text: str) -> dict[str, Any] | None:
    """Parse an active AskUserQuestion TUI form out of a captured pane.

    Returns ``None`` unless the pane is genuinely parked on the menu
    (a ``Enter to select … Esc to cancel`` footer with only chrome below
    it, and a ``✔ Submit`` line within the 80 lines above it). Otherwise
    returns a dict::

        {
          "menu_start": int,        # first pane line of the menu box
          "submit_index": int,
          "footer_index": int,
          "headers": [str, ...],    # all question tabs
          "selected_label": str | None,
          "questions": [            # §3.5 shape (active question only)
            {"header": str|None, "question": str,
             "options": [{"label": str, "description": str, "recommended": bool}, ...],
             "multiSelect": False},
          ],
          "pending_questions": [str, ...],   # tabs not yet rendered
        }
    """
    if not pane_text:
        return None
    raw_lines = pane_text.splitlines()
    cleaned = [_ask_user_clean(ln) for ln in raw_lines]

    # 1. Footer: the LAST footer line that has only chrome/blank below it.
    footer_index: int | None = None
    for pos in range(len(cleaned) - 1, -1, -1):
        if not _ask_user_is_footer(cleaned[pos]):
            continue
        if all(_ask_user_is_chrome_after_footer(cleaned[p]) for p in range(pos + 1, len(cleaned))):
            footer_index = pos
            break
    if footer_index is None:
        return None

    # 2. Submit/tab-bar line within 80 lines above the footer (nearest above).
    submit_index: int | None = None
    for pos in range(footer_index - 1, max(-1, footer_index - 81), -1):
        if _ask_user_is_submit(cleaned[pos]):
            submit_index = pos
            break
    if submit_index is None:
        return None

    headers = _ask_user_headers(cleaned[submit_index])

    # 3. Walk DOWN from the submit line: skip blanks/separators, capture the
    #    wrapped prompt run, then the numbered options + their descriptions.
    prompt_parts: list[str] = []
    options: list[dict[str, Any]] = []
    selected_label: str | None = None
    seen_option = False
    cur: dict[str, Any] | None = None

    def _flush() -> None:
        nonlocal cur
        if cur is None:
            return
        label = cur["label"]
        norm = label.strip().lower()
        if norm not in _ASK_USER_AFFORDANCE_LABELS:
            desc = " ".join(cur["desc"]).strip()
            options.append({
                "label": label,
                "description": desc,
                "recommended": "my recommendation" in desc.lower(),
            })
        cur = None

    for pos in range(submit_index + 1, footer_index):
        text = cleaned[pos]
        if _ask_user_is_separator(text):
            continue
        m = _ASK_USER_NUMBERED_RE.match(raw_lines[pos].rstrip())
        if m:
            _flush()
            seen_option = True
            label = m.group(2).strip()
            cur = {"label": label, "desc": []}
            if any(c in raw_lines[pos] for c in "❯›▶▷❯"):
                selected_label = label
            continue
        # Non-numbered, non-separator line:
        if not seen_option:
            prompt_parts.append(text)          # part of the wrapped prompt
        elif cur is not None:
            cur["desc"].append(text)           # description of current option
    _flush()

    if not options:
        return None

    prompt = re.sub(r"\s+", " ", " ".join(prompt_parts)).strip()
    active_header = headers[0] if headers else None
    questions = [{
        "header": active_header,
        "question": prompt,
        "options": options,
        "multiSelect": False,
    }]

    # The menu box visually starts at the separator just above the tab-bar
    # (if present), else at the tab-bar itself.
    menu_start = submit_index
    if menu_start > 0 and _ask_user_is_separator(cleaned[menu_start - 1]):
        menu_start -= 1

    return {
        "menu_start": menu_start,
        "submit_index": submit_index,
        "footer_index": footer_index,
        "headers": headers,
        "selected_label": selected_label,
        "questions": questions,
        "pending_questions": headers[1:] if len(headers) > 1 else [],
    }


def _ask_user_envelope(
    menu: dict[str, Any],
    *,
    session_name: str,
    actor_fallback: str,
    ts: str,
) -> MessageEnvelope:
    """Build the single structured ``ask_user`` envelope for a parsed menu."""
    questions = menu["questions"]
    prompt = questions[0]["question"] if questions else ""
    env_id = synthesize_capture_id(session_name, menu["submit_index"], "ask_user:" + prompt)
    return MessageEnvelope(
        id=env_id,
        ts=ts,
        role=MessageRole.ASSISTANT,
        actor=actor_fallback,
        type=MessageType.ASK_USER,
        text=prompt,
        metadata={
            "from_capture": True,
            "session_name": session_name,
            "questions": questions,
            "headers": menu["headers"],
            "pending_questions": menu["pending_questions"],
            "selected_label": menu["selected_label"],
        },
    )


__all__ = [
    "DEFAULT_CAPTURE_LINES",
    "capture_envelopes",
    "extract_ask_user_menu",
    "synthesize_capture_id",
]
