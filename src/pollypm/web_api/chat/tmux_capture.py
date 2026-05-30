"""Fallback transcript reader using ``tmux capture-pane``.

Per spec §4.7 and §4.12 the GET endpoint falls back to a live pane
capture when the normalized archive is missing or stale (>60s) AND the
tmux pane is alive. The Codex CLI doesn't write the Claude JSONL shape
either, so Codex surfaces always land here.

Each captured line normally becomes one :class:`MessageEnvelope` with
``type=text`` and ``metadata.from_capture=true``. When the live pane is
parked on Claude's interactive ``AskUserQuestion`` form, the rendered
menu block is instead synthesized into a single ``type=ask_user``
envelope with ``metadata.questions[].options[]`` so web clients can
render controls against the same public chat contract.

Envelope ids are synthesized as ``cap_<hex>`` from a blake2b digest of
``f"{session_name}:{line_index}:{content}"`` so the same line read
twice yields the same id (stable, sortable within a capture).
"""

from __future__ import annotations

from dataclasses import dataclass
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
_ASK_USER_OPTION_GLYPHS = frozenset({"☐", "☑", "○", "◉", "◯", "●"})
_ASK_USER_CHECKBOX_GLYPHS = frozenset({"☐", "☑"})
_ASK_USER_ROW_RE = re.compile(
    r"^(?P<indent>\s*)(?P<glyph>[☐☑○◉◯●])\s+(?P<label>.*\S)\s*$"
)
_ASK_USER_SUBMIT_RE = re.compile(r"\s*[✔✓]?\s*Submit\b.*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _CapturedMenuRow:
    line_index: int
    indent: int
    glyph: str
    label: str


@dataclass(frozen=True, slots=True)
class _CapturedAskUserMenu:
    start_line_index: int
    end_line_index: int
    prompt: str
    questions: list[dict[str, Any]]
    digest_text: str


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
    lines = list(enumerate(cleaned.splitlines()))
    ask_menu: _CapturedAskUserMenu | None = None
    if role == MessageRole.ASSISTANT:
        ask_menu = _extract_active_ask_user_menu(lines)
    line_pos = 0
    while line_pos < len(lines):
        line_index, line = lines[line_pos]
        if ask_menu is not None and line_index == ask_menu.start_line_index:
            envelope_id = synthesize_capture_id(
                session_name,
                ask_menu.start_line_index,
                f"ask_user:{ask_menu.digest_text}",
            )
            envelopes.append(MessageEnvelope(
                id=envelope_id,
                ts=ts,
                role=MessageRole.ASSISTANT,
                actor=actor_fallback,
                type=MessageType.ASK_USER,
                text=ask_menu.prompt or "[ask_user]",
                metadata={
                    "from_capture": True,
                    "synthetic": True,
                    "session_name": session_name,
                    "line_index": ask_menu.start_line_index,
                    "line_start": ask_menu.start_line_index,
                    "line_end": ask_menu.end_line_index,
                    "tool_use_id": envelope_id,
                    "questions": ask_menu.questions,
                    "answered": False,
                    "answers": None,
                },
            ))
            while (
                line_pos < len(lines)
                and lines[line_pos][0] <= ask_menu.end_line_index
            ):
                line_pos += 1
            continue
        # Skip leading/trailing pure-whitespace lines but preserve internal
        # blank lines so the structure of a Claude-Code box is recognisable.
        if not line.strip() and not envelopes:
            line_pos += 1
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
        line_pos += 1
    # Drop any trailing blank lines that we appended along the way —
    # they're just visual padding at the bottom of the pane.
    while envelopes and not envelopes[-1].text.strip():
        envelopes.pop()
    return envelopes


def _extract_active_ask_user_menu(
    lines: list[tuple[int, str]],
) -> _CapturedAskUserMenu | None:
    """Return a synthetic AskUserQuestion menu parsed from the pane tail.

    The capture fallback sees Claude's interactive form only as rendered
    text. To avoid turning old scrollback menus into live controls, this
    parser only accepts a menu whose submit line is followed by blank or
    Claude chrome lines.
    """
    if not lines:
        return None
    submit_pos = _find_active_submit_pos(lines)
    if submit_pos is None:
        return None

    menu_row_positions = [
        pos for pos in range(0, submit_pos + 1)
        if _parse_ask_user_row(lines[pos][0], lines[pos][1]) is not None
    ]
    if not menu_row_positions:
        return None
    menu_start_pos = menu_row_positions[0]
    for pos in reversed(menu_row_positions):
        prior_text = lines[pos - 1][1] if pos > 0 else ""
        if pos == 0 or _is_capture_chrome_or_blank(prior_text):
            menu_start_pos = pos
            break

    rows = [
        parsed for pos in range(menu_start_pos, submit_pos + 1)
        if (parsed := _parse_ask_user_row(lines[pos][0], lines[pos][1]))
        is not None
    ]
    if not rows:
        return None

    prompt_pos, prompt = _find_ask_user_prompt(lines, menu_start_pos)
    questions = _questions_from_menu_rows(rows, prompt)
    if not questions:
        return None

    end_pos = submit_pos
    while end_pos + 1 < len(lines) and _is_capture_chrome_or_blank(
        lines[end_pos + 1][1]
    ):
        end_pos += 1
    start_line = lines[prompt_pos][0] if prompt_pos is not None else rows[0].line_index
    digest_text = "\n".join(
        line for _idx, line in lines[menu_start_pos : end_pos + 1]
    )
    if prompt:
        digest_text = f"{prompt}\n{digest_text}"
    return _CapturedAskUserMenu(
        start_line_index=start_line,
        end_line_index=lines[end_pos][0],
        prompt=prompt or "[ask_user]",
        questions=questions,
        digest_text=digest_text,
    )


def _find_active_submit_pos(lines: list[tuple[int, str]]) -> int | None:
    for pos in range(len(lines) - 1, -1, -1):
        if "submit" not in lines[pos][1].lower():
            continue
        if not all(_is_capture_chrome_or_blank(line) for _idx, line in lines[pos + 1:]):
            continue
        if any(
            any(glyph in lines[prior][1] for glyph in _ASK_USER_OPTION_GLYPHS)
            for prior in range(max(0, pos - 80), pos + 1)
        ):
            return pos
    return None


def _find_ask_user_prompt(
    lines: list[tuple[int, str]],
    menu_start_pos: int,
) -> tuple[int | None, str]:
    fallback: tuple[int | None, str] = (None, "")
    for pos in range(menu_start_pos - 1, max(-1, menu_start_pos - 12), -1):
        line = _normalise_capture_menu_line(lines[pos][1])
        if not line or _is_capture_chrome_or_blank(line):
            continue
        if _parse_ask_user_row(lines[pos][0], line) is not None:
            continue
        line = line.lstrip("⏺").strip()
        if not line:
            continue
        if fallback[1] == "":
            fallback = (pos, line)
        if "?" in line:
            return pos, line
    return fallback


def _questions_from_menu_rows(
    rows: list[_CapturedMenuRow],
    prompt: str,
) -> list[dict[str, Any]]:
    grouped_questions: list[dict[str, Any]] = []
    row_to_group = set()

    for row_index, row in enumerate(rows):
        child_indices = _child_row_indices(rows, row_index)
        if not child_indices:
            continue
        child_rows = [rows[index] for index in child_indices]
        row_to_group.add(row_index)
        row_to_group.update(child_indices)
        question_text = row.label
        if len(rows) == len(child_rows) + 1 and prompt:
            question_text = prompt
        grouped_questions.append({
            "question": question_text,
            "header": row.label,
            "multiSelect": any(
                child.glyph in _ASK_USER_CHECKBOX_GLYPHS
                for child in child_rows
            ),
            "options": [
                {"label": child.label, "description": ""}
                for child in child_rows
            ],
        })

    loose_options = [
        row for row_index, row in enumerate(rows)
        if row_index not in row_to_group
        and not _child_row_indices(rows, row_index)
    ]
    if loose_options:
        grouped_questions.append({
            "question": prompt or "Choose an option",
            "header": "",
            "multiSelect": any(
                row.glyph in _ASK_USER_CHECKBOX_GLYPHS
                for row in loose_options
            ),
            "options": [
                {"label": row.label, "description": ""}
                for row in loose_options
            ],
        })

    if grouped_questions:
        return grouped_questions
    return [{
        "question": prompt or "Choose an option",
        "header": "",
        "multiSelect": any(row.glyph in _ASK_USER_CHECKBOX_GLYPHS for row in rows),
        "options": [{"label": row.label, "description": ""} for row in rows],
    }]


def _child_row_indices(
    rows: list[_CapturedMenuRow],
    row_index: int,
) -> list[int]:
    parent = rows[row_index]
    children: list[int] = []
    for candidate_index, candidate in enumerate(rows[row_index + 1:], row_index + 1):
        if candidate.indent <= parent.indent:
            break
        children.append(candidate_index)
    return children


def _parse_ask_user_row(
    line_index: int,
    line: str,
) -> _CapturedMenuRow | None:
    normalised = _normalise_capture_menu_line(line)
    match = _ASK_USER_ROW_RE.match(normalised)
    if match is None:
        return None
    label = _ASK_USER_SUBMIT_RE.sub("", match.group("label")).strip()
    if not label:
        return None
    return _CapturedMenuRow(
        line_index=line_index,
        indent=len(match.group("indent").replace("\t", "    ")),
        glyph=match.group("glyph"),
        label=label,
    )


def _normalise_capture_menu_line(line: str) -> str:
    text = line.rstrip()
    left = text.lstrip()
    prefix = text[: len(text) - len(left)]
    if left.startswith(("│", "┆")):
        left = left[1:]
        if left.startswith(" "):
            left = left[1:]
        text = f"{prefix}{left.rstrip()}"
    if text.rstrip().endswith(("│", "┆")):
        text = text.rstrip()[:-1].rstrip()
    return text


def _is_capture_chrome_or_blank(line: str) -> bool:
    stripped = _normalise_capture_menu_line(line)
    if not stripped:
        return True
    lower = stripped.lower()
    if (
        "bypass permissions" in lower
        or "shift+tab to cycle" in lower
        or "ctrl+t to" in lower
        or stripped.startswith("⏵⏵")
    ):
        return True
    box_chars = {"─", "━", "╭", "╮", "╰", "╯", "│", "┆", " "}
    return set(stripped) <= box_chars


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
