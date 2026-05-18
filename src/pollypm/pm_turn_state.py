"""Detect + record "PM persona ended its turn" transitions (#1633).

The smallest defensible wedge for the "no signal when Polly/PM is
waiting on the user" gap:

* :func:`is_pm_session` — true for the workspace operator (``operator``)
  and per-project architect sessions (``architect_<project>``). Workers,
  reviewers, and heartbeat are deliberately excluded: their idleness
  doesn't mean the user owes them a reply.

* :func:`detect_pm_turn_ended` — pane-text predicate. Reuses the
  existing Claude empty-``❯`` / Codex idle-placeholder detectors from
  :mod:`pollypm.idle_placeholders`. An empty input prompt on a PM
  pane is the canonical signal that the agent finished its turn and
  is awaiting the user.

* :func:`record_turn_state` — persists per-session state to
  ``~/.pollypm/pm_turn_state.json``. Emits a ``pm.turn_ended`` audit
  event on the active → ended transition (one event per transition,
  not per poll), so the cockpit and downstream observers can later
  badge / notify without re-running the heuristic. Symmetrically
  records the ended → active transition without an audit emit (we
  only care about the moment attention is needed).

* :func:`is_turn_ended` — for the rail glyph code to consume. Returns
  True iff the last recorded state for ``session_name`` was "turn
  ended" (i.e., the agent is currently waiting on the user).

The state file is best-effort: a missing / unreadable file means
"no PM sessions are currently flagged ended" — the next poll will
re-populate it. We deliberately do NOT persist into the state DB
because the rail's glyph path is hot and the state-DB connection
isn't always available from the recurring handler.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pollypm.idle_placeholders import (
    pane_shows_claude_empty_prompt,
    pane_shows_codex_idle_placeholder,
)

logger = logging.getLogger(__name__)

# Audit event name for the active → ended transition. Stable string —
# downstream observers (cockpit rail glyph override, future OS notifier,
# future topbar pill) pin this.
EVENT_PM_TURN_ENDED = "pm.turn_ended"

# Override hook for tests. When set, the state file lives under
# ``$POLLYPM_PM_TURN_STATE_HOME/pm_turn_state.json``.
_HOME_ENV = "POLLYPM_PM_TURN_STATE_HOME"


def _state_path() -> Path:
    """Return the JSON state file path.

    Mirrors :func:`pollypm.audit.log._central_root` — uses
    ``DEFAULT_CONFIG_PATH.parent`` (typically ``~/.pollypm``) so a
    custom config home stays internally consistent. Tests override
    via ``$POLLYPM_PM_TURN_STATE_HOME``.
    """
    override = os.environ.get(_HOME_ENV)
    if override:
        return Path(override).expanduser() / "pm_turn_state.json"
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH

        return Path(DEFAULT_CONFIG_PATH).parent / "pm_turn_state.json"
    except Exception:  # noqa: BLE001 — config errors never break detection
        return Path.home() / ".pollypm" / "pm_turn_state.json"


def is_pm_session(session_name: str) -> bool:
    """True iff ``session_name`` is a PM-facing persona.

    PM personas are sessions whose conversational turn ends with the
    user owing the next reply:

    * ``operator`` — the workspace-level Polly chat.
    * ``architect_<project>`` — per-project PM persona.

    Workers (``worker_*``), reviewers (``reviewer`` / ``reviewer_*``),
    and heartbeat (``heartbeat``) are autonomous and intentionally
    excluded — the user doesn't ack their turn-end (#1633 scope).
    """
    if not session_name:
        return False
    if session_name == "operator":
        return True
    if session_name.startswith("architect_") or session_name.startswith("architect-"):
        return True
    return False


def detect_pm_turn_ended(pane_text: str) -> bool:
    """True iff the pane snapshot shows the PM agent waiting for input.

    Reuses the existing prompt-marker detectors:

    * Claude CLI: standalone ``❯`` at the input box with no body.
    * Codex CLI: rotating idle-placeholder hints in the input box.

    Both are the canonical "agent finished its turn" signal — they
    only appear when the model has emitted a complete response and
    the input box is empty waiting for the next user prompt.
    """
    if not pane_text:
        return False
    return (
        pane_shows_claude_empty_prompt(pane_text)
        or pane_shows_codex_idle_placeholder(pane_text)
    )


def _load_state() -> dict[str, Any]:
    """Read the state file. Returns an empty dict on any failure."""
    path = _state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.debug("pm_turn_state: read failed for %s: %s", path, exc)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.debug("pm_turn_state: parse failed for %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_state(data: dict[str, Any]) -> None:
    """Atomically replace the state file. Best-effort — never raises."""
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("pm_turn_state: mkdir failed for %s: %s", path, exc)
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(data, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("pm_turn_state: write failed for %s: %s", path, exc)
        # Best-effort cleanup of the tmp file.
        try:
            tmp.unlink()
        except OSError:
            pass


def _emit_turn_ended_audit(session_name: str) -> None:
    """Best-effort ``pm.turn_ended`` audit emit.

    Lazy-imported so a missing / broken audit module never blocks the
    detection sweep (mirrors :meth:`PollyCockpitRail._emit_audit`).
    The project key is ``_workspace`` because the operator is not
    scoped to a single project; per-project architects could carry
    their project key but we keep one shape for downstream consumers.
    """
    try:
        from pollypm.audit.log import emit as audit_emit
    except Exception:  # noqa: BLE001
        return
    try:
        audit_emit(
            event=EVENT_PM_TURN_ENDED,
            project="_workspace",
            subject=session_name,
            actor="pm_turn_state",
            status="ok",
            metadata={"session": session_name},
        )
    except Exception:  # noqa: BLE001
        return


def record_turn_state(session_name: str, *, turn_ended: bool) -> bool:
    """Persist the current turn state for ``session_name``.

    Returns True iff this call flipped the state from active to ended
    (i.e., the user just became responsible for the next move) and
    therefore emitted a ``pm.turn_ended`` audit event. Repeated calls
    with the same state are idempotent and emit nothing.
    """
    if not is_pm_session(session_name):
        return False
    data = _load_state()
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    prev = sessions.get(session_name)
    prev_ended = bool(prev.get("turn_ended")) if isinstance(prev, dict) else False
    transitioned = (not prev_ended) and turn_ended

    sessions[session_name] = {"turn_ended": bool(turn_ended)}
    data["sessions"] = sessions
    _write_state(data)

    if transitioned:
        _emit_turn_ended_audit(session_name)
    return transitioned


def is_turn_ended(session_name: str) -> bool:
    """True iff the most-recently-recorded state is "turn ended".

    Cheap (one JSON read). The rail's ``_indicator`` calls this on
    every render to decide whether to paint ``◆`` (awaits user) over
    the normal heartbeat / working glyph for a PM row.
    """
    if not is_pm_session(session_name):
        return False
    data = _load_state()
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return False
    entry = sessions.get(session_name)
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("turn_ended"))


def clear_state() -> None:
    """Remove the state file. Used by tests."""
    path = _state_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("pm_turn_state: clear failed for %s: %s", path, exc)


__all__ = [
    "EVENT_PM_TURN_ENDED",
    "is_pm_session",
    "detect_pm_turn_ended",
    "record_turn_state",
    "is_turn_ended",
    "clear_state",
]
