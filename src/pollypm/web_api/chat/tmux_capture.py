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
_ASK_USER_NUMBERED_ROW_RE = re.compile(
    r"^(?P<indent>\s*)(?:[❯>]\s*)?(?P<number>\d+)[.)]\s+(?P<label>.*\S)\s*$"
)
_ASK_USER_SUBMIT_RE = re.compile(r"\s*[✔✓]?\s*Submit\b.*$", re.IGNORECASE)
_ASK_USER_TAB_ITEM_RE = re.compile(
    r"(?P<glyph>[☐☑○◉◯●✔✓])\s+"
    r"(?P<label>.*?)(?=\s+[☐☑○◉◯●✔✓]\s+|$)"
)
_ASK_USER_TRAILING_AFFORDANCES = frozenset({
    "chat about this",
    "type something.",
    "type something",
})


@dataclass(frozen=True, slots=True)
class _CapturedMenuRow:
    line_index: int
    indent: int
    glyph: str
    label: str
    description: str = ""
    is_numbered: bool = False


@dataclass(frozen=True, slots=True)
class _CapturedTabHeader:
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

    menu_end_pos = _find_active_menu_end_pos(lines, submit_pos)
    if menu_end_pos is None:
        return None

    row_search_start = max(0, submit_pos - 80)
    menu_row_positions = [
        pos for pos in range(row_search_start, menu_end_pos + 1)
        if _parse_ask_user_row(lines[pos][0], lines[pos][1]) is not None
    ]
    if not menu_row_positions:
        return None
    rows_after_submit = [pos for pos in menu_row_positions if pos > submit_pos]
    if rows_after_submit:
        menu_start_pos = submit_pos
    else:
        menu_start_pos = menu_row_positions[0]
        for pos in reversed(menu_row_positions):
            prior_text = lines[pos - 1][1] if pos > 0 else ""
            if pos == 0 or _is_capture_chrome_or_blank(prior_text):
                menu_start_pos = pos
                break

    rows = _parse_ask_user_rows(lines, menu_start_pos, menu_end_pos)
    if not rows:
        return None

    prompt_pos, prompt = _find_ask_user_prompt(lines, menu_start_pos)
    tab_headers = _parse_ask_user_tab_headers(lines[submit_pos][1])
    questions = _questions_from_menu_rows(rows, prompt, tab_headers=tab_headers)
    if not questions:
        return None

    end_pos = menu_end_pos
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
        menu_end_pos = _find_active_menu_end_pos(lines, pos)
        if menu_end_pos is None:
            continue
        if not all(
            _is_capture_chrome_or_blank(line)
            for _idx, line in lines[menu_end_pos + 1:]
        ):
            continue
        if _has_ask_user_rows_near_submit(lines, pos, menu_end_pos):
            return pos
    return None


def _find_active_menu_end_pos(
    lines: list[tuple[int, str]],
    submit_pos: int,
) -> int | None:
    """Return the last line occupied by the live menu around ``submit_pos``."""
    end_pos = submit_pos
    saw_row_after_submit = False
    previous_row: _CapturedMenuRow | None = None

    for pos in range(submit_pos + 1, len(lines)):
        line_index, line = lines[pos]
        row = _parse_ask_user_row(line_index, line)
        if row is not None:
            saw_row_after_submit = True
            previous_row = row
            end_pos = pos
            continue
        if (
            saw_row_after_submit
            and previous_row is not None
            and _is_ask_user_continuation_line(line, previous_row.indent)
        ):
            end_pos = pos
            continue
        if _is_capture_chrome_or_blank(line) or _is_ask_user_affordance(line):
            end_pos = pos
            continue
        break

    return end_pos


def _has_ask_user_rows_near_submit(
    lines: list[tuple[int, str]],
    submit_pos: int,
    menu_end_pos: int,
) -> bool:
    window_start = max(0, submit_pos - 80)
    window_end = min(len(lines), max(submit_pos, menu_end_pos) + 1)
    return any(
        _parse_ask_user_row(line_index, line) is not None
        for line_index, line in lines[window_start:window_end]
    )


def _find_ask_user_prompt(
    lines: list[tuple[int, str]],
    menu_start_pos: int,
) -> tuple[int | None, str]:
    fallback: tuple[int | None, str] = (None, "")
    for pos in range(menu_start_pos - 1, max(-1, menu_start_pos - 12), -1):
        line = _normalise_capture_menu_line(lines[pos][1])
        if not line or _is_capture_chrome_or_blank(line):
            continue
        if "submit" in line.lower() and _parse_ask_user_tab_headers(line):
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
    *,
    tab_headers: list[_CapturedTabHeader] | None = None,
) -> list[dict[str, Any]]:
    tab_header = _active_tab_header(tab_headers or [])
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
                {"label": child.label, "description": child.description}
                for child in child_rows
            ],
        })

    loose_options = [
        row for row_index, row in enumerate(rows)
        if row_index not in row_to_group
        and not _child_row_indices(rows, row_index)
    ]
    if loose_options:
        header = tab_header.label if tab_header is not None else ""
        grouped_questions.append({
            "question": prompt or "Choose an option",
            "header": header,
            "multiSelect": any(
                row.glyph in _ASK_USER_CHECKBOX_GLYPHS
                for row in loose_options
            ),
            "options": [
                {"label": row.label, "description": row.description}
                for row in loose_options
            ],
        })

    if grouped_questions:
        return grouped_questions
    return [{
        "question": prompt or "Choose an option",
        "header": tab_header.label if tab_header is not None else "",
        "multiSelect": any(row.glyph in _ASK_USER_CHECKBOX_GLYPHS for row in rows),
        "options": [
            {"label": row.label, "description": row.description}
            for row in rows
        ],
    }]


def _child_row_indices(
    rows: list[_CapturedMenuRow],
    row_index: int,
) -> list[int]:
    parent = rows[row_index]
    if parent.is_numbered:
        return []
    children: list[int] = []
    for candidate_index, candidate in enumerate(rows[row_index + 1:], row_index + 1):
        if candidate.is_numbered:
            break
        if candidate.indent <= parent.indent:
            break
        children.append(candidate_index)
    return children


def _parse_ask_user_rows(
    lines: list[tuple[int, str]],
    start_pos: int,
    end_pos: int,
) -> list[_CapturedMenuRow]:
    rows: list[_CapturedMenuRow] = []
    pending_description_parts: list[str] = []

    for pos in range(start_pos, end_pos + 1):
        line_index, line = lines[pos]
        row = _parse_ask_user_row(line_index, line)
        if row is not None:
            if rows and pending_description_parts:
                rows[-1] = _row_with_description(rows[-1], pending_description_parts)
                pending_description_parts = []
            rows.append(row)
            continue

        if not rows:
            continue
        if _is_ask_user_affordance(line) or _is_capture_chrome_or_blank(line):
            if pending_description_parts:
                rows[-1] = _row_with_description(rows[-1], pending_description_parts)
                pending_description_parts = []
            continue
        if _is_ask_user_continuation_line(line, rows[-1].indent):
            pending_description_parts.append(_normalise_capture_menu_line(line).strip())
            continue
        if pending_description_parts:
            rows[-1] = _row_with_description(rows[-1], pending_description_parts)
            pending_description_parts = []

    if rows and pending_description_parts:
        rows[-1] = _row_with_description(rows[-1], pending_description_parts)
    return rows


def _row_with_description(
    row: _CapturedMenuRow,
    parts: list[str],
) -> _CapturedMenuRow:
    description = " ".join(part for part in parts if part)
    if row.description and description:
        description = f"{row.description} {description}"
    else:
        description = row.description or description
    return _CapturedMenuRow(
        line_index=row.line_index,
        indent=row.indent,
        glyph=row.glyph,
        label=row.label,
        description=description,
        is_numbered=row.is_numbered,
    )


def _active_tab_header(
    tab_headers: list[_CapturedTabHeader],
) -> _CapturedTabHeader | None:
    if not tab_headers:
        return None
    for header in tab_headers:
        if header.glyph != "☑":
            return header
    return tab_headers[0]


def _parse_ask_user_row(
    line_index: int,
    line: str,
) -> _CapturedMenuRow | None:
    normalised = _normalise_capture_menu_line(line)
    match = _ASK_USER_ROW_RE.match(normalised)
    if match is not None:
        label = _ASK_USER_SUBMIT_RE.sub("", match.group("label")).strip()
        if not label:
            return None
        return _CapturedMenuRow(
            line_index=line_index,
            indent=len(match.group("indent").replace("\t", "    ")),
            glyph=match.group("glyph"),
            label=label,
        )
    match = _ASK_USER_NUMBERED_ROW_RE.match(normalised)
    if match is None:
        return None
    label = _ASK_USER_SUBMIT_RE.sub("", match.group("label")).strip()
    if not label:
        return None
    return _CapturedMenuRow(
        line_index=line_index,
        indent=len(match.group("indent").replace("\t", "    ")),
        glyph="○",
        label=label,
        is_numbered=True,
    )


def _parse_ask_user_tab_headers(line: str) -> list[_CapturedTabHeader]:
    text = _normalise_capture_menu_line(line).strip()
    if "submit" not in text.lower():
        return []
    text = text.strip("←→ ")
    headers: list[_CapturedTabHeader] = []
    for match in _ASK_USER_TAB_ITEM_RE.finditer(text):
        glyph = match.group("glyph")
        label = match.group("label").strip("←→ ").strip()
        if not label or label.lower().startswith("submit"):
            continue
        headers.append(_CapturedTabHeader(glyph=glyph, label=label))
    return headers


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


def _is_ask_user_continuation_line(line: str, parent_indent: int) -> bool:
    normalised = _normalise_capture_menu_line(line)
    if not normalised.strip():
        return False
    if _is_ask_user_affordance(normalised):
        return False
    expanded = normalised.replace("\t", "    ")
    indent = len(expanded) - len(expanded.lstrip(" "))
    return indent > parent_indent


def _is_ask_user_affordance(line: str) -> bool:
    stripped = _normalise_capture_menu_line(line).strip()
    return stripped.lower() in _ASK_USER_TRAILING_AFFORDANCES


def _is_capture_chrome_or_blank(line: str) -> bool:
    stripped = _normalise_capture_menu_line(line)
    if not stripped:
        return True
    lower = stripped.lower()
    if (
        "bypass permissions" in lower
        or "shift+tab to cycle" in lower
        or "ctrl+t to" in lower
        or _is_ask_user_affordance(stripped)
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
