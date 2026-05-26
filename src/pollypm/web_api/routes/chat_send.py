"""POST chat-send endpoint — P3 of the chat-endpoints spec.

Implements ``POST /api/v1/chat/{session_name}/send`` — push a message
into the running CLI agent backing ``session_name`` via tmux.

The router is a thin adapter over the P1 shared facade in
:mod:`pollypm.web_api.chat`:

- :func:`pollypm.web_api.chat.find_chat_surface` resolves
  ``session_name`` → :class:`ChatSurface` (window, transcript, cwd).
  Workers are validated against the work-service's active
  :class:`WorkerSessionRecord` rows via the public service facade
  :func:`pollypm.web_api.service.list_active_worker_sessions`;
  an authenticated caller cannot address a stale or unrelated tmux
  window by guessing ``task-{project}-{id}`` names (Codex review
  block 2 in #2043, public-facade refactor for #2043 review v3).
- :func:`pollypm.web_api.chat.resolve_transcript_path` produces the
  exact ``events.jsonl`` for the session, with ``cwd``-based scoping
  so multi-session projects don't bleed transcripts (Codex review
  block 3 in #2043). Strict safety fails closed when the resolver
  can only fall back to "freshest" — ``safety=force`` bypasses.
- :class:`pollypm.web_api.chat.MessageEnvelope` / the AskUserQuestion
  detection helpers route from the same transcript parser the read
  endpoints (P2) use.

Safety gates per spec §4.1 / §4.3:

- **Mid-tool** — tail-read the resolved ``events.jsonl`` and reject
  when the latest assistant response has unmatched ``tool_use`` ids.
  Tool-only assistant turns (no text → no ``assistant_turn``) are
  caught by anchoring on the most-recent ``user_turn`` boundary
  (Codex review block 4 in #2043).
- **Mid-stream** — read the latest heartbeat from
  :func:`pollypm.storage.pg_heartbeats.latest_heartbeat` and reject
  when it landed within the last 2s.

``safety=strict`` (default) enforces both AND fails closed when the
signals themselves can't be evaluated — an unreadable transcript
yields ``409 unsafe_unavailable_transcript`` and a pg/heartbeat outage
yields ``409 unsafe_unavailable_heartbeat`` (Codex #2043 review v5
blockers 1+2; pre-v5 both modes silently collapsed to "safe to
send"). ``safety=loose`` keeps the mid-tool check but allows
mid-stream sends with a warning header; on unavailable signals it
continues with ``X-PollyPM-Warning: transcript-unavailable`` /
``heartbeat-unavailable`` so the operator sees the gate was
best-effort. ``safety=force`` bypasses the mid-tool / mid-stream /
missing-transcript safety gates only. Pane and window existence +
liveness errors (``409 pane_invalid``, ``409 pane_dead``,
``503 window_missing``, ``503 tmux_unavailable``) are NOT bypassed
and will still fail-closed.

Pane validation: explicit ``pane`` (including ``pane=0``) always goes
through ``list_panes(target)`` to verify the requested index exists
AND is alive before any ``send_keys``. Only ``pane is None`` falls
back to the window-level (default active-pane) target — explicit
``pane=0`` no longer silently aliases to the active pane (Codex
review v3 block 1 in #2043). Negative or nonexistent panes raise
``409 pane_invalid``; dead panes raise ``409 pane_dead``.

Send failures: ``tmux.send_keys`` can raise ``DeadPaneError`` (mapped
to ``409 pane_dead``), ``FileNotFoundError`` when the tmux binary is
missing (``503 tmux_unavailable``), ``subprocess.CalledProcessError``
when the window/pane disappears between validation and send
(``503 window_missing``), ``subprocess.TimeoutExpired`` when the tmux
server is wedged (``503 tmux_unavailable``), and arbitrary
``OSError`` from paste-buffer load failures (``503 send_failed``).
Every send-time failure now returns a typed envelope instead of a
generic 500 (Codex review v3 block 2 in #2043).
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from pollypm.audit.log import EVENT_CHAT_SEND_FORCE_BYPASS, emit as audit_emit
from pollypm.tmux.client import DeadPaneError, TmuxClient
from pollypm.web_api.chat import (
    ChatSurface,
    find_chat_surface,
)
from pollypm.web_api.errors import APIError
from pollypm.web_api.routes._deps import ConfigDep
from pollypm.web_api.service import (
    list_active_worker_sessions,
    list_active_worker_sessions_strict,
)
from pollypm.work.task_state import parse_task_window_name

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])


# ---------------------------------------------------------------------------
# Strict-mode "signal unavailable" exceptions (Codex #2043 review v5)
# ---------------------------------------------------------------------------
#
# Pre-v5 the tail reader returned ``[]`` on every read/decode failure and
# the heartbeat helper returned ``None`` on every pg/import failure. Both
# routes interpreted the "empty" signal as "no problem detected" — so an
# unreadable transcript or a pg outage silently let a strict send
# through. That defeated the whole point of strict mode.
#
# The fix is tri-state: distinguish "signal evaluated and clean" from
# "signal could not be evaluated". The exceptions below mark the
# unavailable case explicitly so the route can fail closed in strict
# mode, emit a warning header in loose mode, and ignore in force mode.


class TranscriptUnavailable(Exception):
    """Raised when the transcript tail can't be evaluated.

    Covers ``stat`` / ``open`` / ``read`` / ``decode`` failures on the
    resolved ``events.jsonl``. The strict-mode mid-tool gate maps this
    to ``409 unsafe_unavailable_transcript``; loose mode continues with
    a warning header; force mode ignores entirely.
    """


class HeartbeatUnavailable(Exception):
    """Raised when the heartbeat signal can't be evaluated.

    Covers ``pollypm.storage.pg_heartbeats`` import failure,
    ``latest_heartbeat`` raising (pg pool down, network outage,
    auth failure), or a malformed ``created_at`` string. The
    strict-mode mid-stream gate maps this to
    ``409 unsafe_unavailable_heartbeat``; loose mode continues with a
    warning header; force mode ignores entirely.

    NOTE: a *missing* heartbeat row (no record exists for the session
    yet) is NOT unavailable — that's a real "not streaming" signal and
    the helper returns ``None`` for it. Only actual evaluation failures
    raise.
    """


class _WorkerFacadeUnavailable(Exception):
    """Raised when the work-service open/list itself failed.

    Codex #2043 review v6 blocker 1: the *public* facade
    :func:`pollypm.web_api.service.list_active_worker_sessions` catches
    ``_open_work_service_readonly`` open failures and
    ``list_worker_sessions`` read failures, returning ``[]`` in both
    cases. That collapsed "no active workers" and "could not query
    workers" into the same signal — so a real pg outage on a
    ``task-<project>-<n>`` send fell out as ``404 session_unknown``
    instead of ``503 service_unavailable``.

    The strict worker-lookup path
    (:func:`_list_worker_sessions_strict`) bypasses the public facade
    and re-raises the underlying exception wrapped in this typed class
    so :func:`_resolve_surface` can map it to a typed 503. The public
    facade is unchanged — non-strict callers (e.g. read endpoints that
    legitimately want "best-effort list, empty on failure") still get
    the swallow-and-return-`[]` behavior.
    """


# Per-spec §2.3 — worker sessions follow ``task-<project>-<n>``. The
# pattern check below lets ``_resolve_surface`` skip the worker facade
# for sessions that syntactically cannot be workers, avoiding a pg hit
# on every operator/architect/advisor send (Codex #2043 review v5
# blocker 3). Codex #2043 review v7 blocker 3: delegate to the
# canonical parser in :mod:`pollypm.work.task_state` so we don't
# duplicate the ``task-<project>-<n>`` regex (worker / project keys
# may legitimately include ``_``, ``.``, ``-`` — the parser is the
# source of truth).


def _looks_like_worker_session(session_name: str) -> bool:
    """True when ``session_name`` syntactically matches ``task-<proj>-<n>``."""
    return parse_task_window_name(session_name) is not None


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ChatSendRequest(BaseModel):
    """Body of ``POST /api/v1/chat/{session_name}/send`` per spec §2.3."""

    text: str | None = Field(
        default=None,
        description=(
            "Free-text body to send. Required when ``selections`` is empty."
        ),
    )
    press_enter: bool = Field(
        default=True,
        description="Submit the buffer by pressing Enter after the text lands.",
    )
    answer_to: str | None = Field(
        default=None,
        description=(
            "ID of an ``ask_user`` message in the session's transcript that "
            "this send answers. When set, ``selections`` must reference one "
            "of the question's options (§4.5)."
        ),
    )
    selections: list[str] = Field(
        default_factory=list,
        description=(
            "Selected option labels for an AskUserQuestion reply. Each entry "
            "must exactly match one of the question's option labels."
        ),
    )
    notes: str | None = Field(
        default=None,
        description=(
            "Optional free-text addition appended after ``selections`` when "
            "answering an ask_user. Ignored for plain text sends."
        ),
    )
    safety: Literal["strict", "loose", "force"] = Field(
        default="strict",
        description=(
            "Safety gate level.\n\n"
            "- strict (default): All safety gates active. Fails closed "
            "(409) when signals are unavailable — unreadable transcript "
            "yields unsafe_unavailable_transcript; pg/heartbeat outage "
            "yields unsafe_unavailable_heartbeat.\n"
            "- loose: Mid-tool gate active. Mid-stream check best-effort "
            "with X-PollyPM-Warning header on unavailable signals "
            "(agent-may-be-streaming, heartbeat-unavailable, "
            "transcript-unavailable).\n"
            "- force: All safety gates bypassed. Pane and window "
            "validation still active."
        ),
    )
    pane: int | None = Field(
        default=None,
        description=(
            "0-based pane index inside the target window. Defaults to the "
            "primary pane (matches existing send_keys behaviour)."
        ),
    )


class ChatSendResponse(BaseModel):
    """Response body for ``POST /api/v1/chat/{session_name}/send``."""

    ok: bool
    message_id: str
    session_name: str
    window_target: str
    characters_sent: int
    method: Literal["send_keys", "paste_buffer"]
    press_enter_at: str | None = None


# ---------------------------------------------------------------------------
# Typed error helpers (spec §2.3 error codes)
# ---------------------------------------------------------------------------


def _session_unknown(session_name: str) -> APIError:
    # Codex #2043 review v6 blocker 3: do not advertise
    # ``GET /api/v1/chat/sessions`` here — that endpoint ships in
    # #2045 and is still blocked. This PR (send-only) stays
    # independently mergeable by pointing the operator at surfaces
    # they can enumerate today (CLI / config).
    return APIError(
        status_code=404,
        code="session_unknown",
        message=f"No chat surface registered for session: {session_name!r}",
        hint=(
            "List available sessions via the `pm sessions` CLI or "
            "`pollypm.toml` (`[sessions.*]` for operator/architect/"
            "advisor, `task-{project}-{N}` for active workers). "
            "Per-task workers must appear in "
            "`WorkService.list_worker_sessions(active_only=True)`."
        ),
    )


def _window_missing(target: str) -> APIError:
    return APIError(
        status_code=503,
        code="window_missing",
        message=f"Window not present in tmux: {target}",
        hint="The session is configured but its tmux window is not running.",
    )


def _pane_dead(target: str) -> APIError:
    return APIError(
        status_code=409,
        code="pane_dead",
        message=f"tmux pane is dead: {target}",
    )


def _pane_invalid(pane: int, target: str) -> APIError:
    return APIError(
        status_code=409,
        code="pane_invalid",
        message=(
            f"Pane index {pane} is not valid for window {target!r}. "
            "Pane must be >= 0 and refer to an existing pane."
        ),
    )


def _send_failed(target: str, detail: str) -> APIError:
    """Generic send failure (subprocess error, paste-buffer load fail).

    Spec §6 ``send_failed``. Wraps the underlying subprocess /
    paste-buffer exception so the operator gets actionable text
    instead of a generic 500 envelope (Codex #2043 review v3 block 2).
    """
    return APIError(
        status_code=503,
        code="send_failed",
        message=f"tmux send_keys failed for {target!r}: {detail}",
        hint=(
            "Retry the request; if it keeps failing inspect the tmux "
            "session directly or check the cockpit for an unhealthy pane."
        ),
    )


def _tmux_unavailable(detail: str) -> APIError:
    """tmux process is unreachable (binary missing, server wedged/timed out).

    Spec §6 ``tmux_unavailable``. Distinguishes "the tmux server
    itself is down" from "the specific window vanished" so the
    operator knows whether retrying will help.
    """
    return APIError(
        status_code=503,
        code="tmux_unavailable",
        message=f"tmux unavailable: {detail}",
        hint=(
            "The tmux binary is missing or the tmux server timed out. "
            "Try `pm up` to restart the storage-closet session."
        ),
    )


def _unsafe_mid_tool() -> APIError:
    return APIError(
        status_code=409,
        code="unsafe_mid_tool",
        message=(
            "Refusing to send while the agent has an open tool_use without a "
            "matching tool_result. Override with safety=force."
        ),
    )


def _unsafe_mid_stream() -> APIError:
    return APIError(
        status_code=409,
        code="unsafe_mid_stream",
        message=(
            "Refusing to send while the agent appears to be streaming "
            "(heartbeat <2s old). Override with safety=loose or safety=force."
        ),
    )


def _unsafe_unavailable_transcript(detail: str) -> APIError:
    """Strict-mode fail-closed when the transcript can't be read.

    Codex #2043 review v5 blocker 1: pre-v5 ``_read_events_tail``
    returned ``[]`` on stat/open/read/decode failures, which the
    mid-tool gate treated as "no open tools". An unreadable transcript
    (chmod 000, IO error, rotated mid-request) therefore let a strict
    send through. The fix raises :class:`TranscriptUnavailable` from
    the reader and maps it here in strict mode.
    """
    return APIError(
        status_code=409,
        code="unsafe_unavailable_transcript",
        message=(
            "Refusing to send under safety=strict because the transcript "
            f"can't be evaluated for mid-tool safety: {detail}. "
            "Use safety=loose to continue with a best-effort warning, or "
            "safety=force to bypass entirely."
        ),
    )


def _unsafe_unavailable_heartbeat(detail: str) -> APIError:
    """Strict-mode fail-closed when the heartbeat signal is unavailable.

    Codex #2043 review v5 blocker 2: pre-v5 ``_heartbeat_age_seconds``
    swallowed pg outages and returned ``None``, which the mid-stream
    gate treated as "not streaming". A pg outage therefore let a
    strict send through even though the endpoint couldn't tell whether
    the agent was mid-stream. The fix raises
    :class:`HeartbeatUnavailable` from the helper and maps it here.
    """
    return APIError(
        status_code=409,
        code="unsafe_unavailable_heartbeat",
        message=(
            "Refusing to send under safety=strict because the heartbeat "
            f"signal can't be evaluated for mid-stream safety: {detail}. "
            "Use safety=loose to continue with a best-effort warning, or "
            "safety=force to bypass entirely."
        ),
    )


def _service_unavailable_worker_lookup(detail: str) -> APIError:
    """503 when the worker-session lookup fails for an actual worker name.

    Codex #2043 review v5 blocker 3: the work-service facade returned
    ``[]`` on both "no active workers" AND open/list failure. For a
    request whose ``session_name`` syntactically matches the worker
    pattern, that conflation hid pg outages as ``404 session_unknown``.
    We now route real lookup failures to ``503 service_unavailable``
    so operators can distinguish "no such worker" from "can't ask".
    """
    return APIError(
        status_code=503,
        code="service_unavailable",
        message=(
            "Worker-session lookup failed; cannot resolve session: "
            f"{detail}"
        ),
        hint=(
            "Retry the request once the work-service backing store is "
            "reachable. Check pg pool health."
        ),
    )


def _answer_to_missing(answer_to: str) -> APIError:
    return APIError(
        status_code=400,
        code="answer_to_missing",
        message=f"No transcript message found for answer_to={answer_to!r}",
    )


def _selections_no_question(answer_to: str) -> APIError:
    return APIError(
        status_code=400,
        code="selections_no_question",
        message=(
            f"answer_to={answer_to!r} does not reference an ask_user message; "
            "selections are only valid against AskUserQuestion envelopes."
        ),
    )


def _selections_invalid(invalid: list[str], valid: list[str]) -> APIError:
    return APIError(
        status_code=400,
        code="selections_invalid",
        message=(
            f"Selections not in question options: {invalid!r}. "
            f"Valid options: {valid!r}"
        ),
    )


# ---------------------------------------------------------------------------
# Worker-session discovery (thin wrapper over the public service facade)
# ---------------------------------------------------------------------------


# Test seam: routes monkeypatch this to inject fake worker records
# without spinning up pg. The default implementation delegates to the
# public :func:`pollypm.web_api.service.list_active_worker_sessions`
# facade so chat_send doesn't reach into private service internals
# (Codex #2043 review v3 block 3).
#
# Codex #2043 review v5 blocker 3: this wrapper re-raises any
# exception from the underlying facade so :func:`_resolve_surface`
# can map "worker-syntactic session_name + lookup failed" to a typed
# ``503 service_unavailable`` (instead of the previous ``[]``
# fallback that masqueraded as ``404 session_unknown``). The facade
# itself already catches and returns ``[]`` on failure — tests that
# need to simulate a worker-service outage monkeypatch THIS wrapper
# (or its caller) to raise.
def _list_worker_sessions(config: Any) -> list[Any]:
    """Return active :class:`WorkerSessionRecord` rows or ``[]``.

    Delegates to the public service-layer facade. Kept for any
    non-strict caller / test scaffolding that wants the empty-on-
    outage behavior. The strict send path uses
    :func:`_list_worker_sessions_strict` instead — see Codex #2043
    review v6 blocker 1 for why the strict path bypasses this facade.
    """
    return list(list_active_worker_sessions(config)) or []


def _list_worker_sessions_strict(
    config: Any, *, project: str | None = None,
) -> list[Any]:
    """Strict worker-lookup via the public service facade.

    Codex #2043 review v6 blocker 1 / v7 blocker 2: the *non-strict*
    public facade
    :func:`pollypm.web_api.service.list_active_worker_sessions`
    swallows ``_open_work_service_readonly`` open failures and
    ``list_worker_sessions`` read failures and returns ``[]``. That
    conflates "no active workers" with "could not query workers", so
    a real pg / work-service outage on a ``task-<project>-<n>`` send
    fell out as ``404 session_unknown`` instead of
    ``503 service_unavailable``.

    v7 fix: route through the *strict* public facade
    :func:`pollypm.web_api.service.list_active_worker_sessions_strict`
    which propagates open/list failures. This wrapper then translates
    any raised exception into the typed
    :class:`_WorkerFacadeUnavailable` that :func:`_resolve_surface`
    maps to ``503 service_unavailable``. Pre-v7 we reached into the
    private ``_open_work_service_readonly`` context manager directly,
    a service-boundary violation flagged by Codex round-7 review.

    Tests monkeypatch this module-level name to feed fake records or
    simulate the outage path without touching pg. The mandatory
    regression test for v6 monkeypatches
    :func:`pollypm.web_api.service._open_work_service_readonly` to
    raise, exercising the real public-facade-bypass path end-to-end.
    """
    try:
        return list(list_active_worker_sessions_strict(config, project=project))
    except _WorkerFacadeUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _WorkerFacadeUnavailable(str(exc) or type(exc).__name__) from exc


# ---------------------------------------------------------------------------
# Session resolution
# ---------------------------------------------------------------------------


def _resolve_surface(
    config: Any,
    session_name: str,
    *,
    include_transcripts: bool = True,
) -> ChatSurface:
    """Resolve ``session_name`` → one :class:`ChatSurface`.

    Configured operator/architect/advisor sessions come from
    ``config.sessions``. Per-task workers are looked up in the
    work-service via :func:`_list_worker_sessions` — a syntactically
    valid ``task-{project}-{id}`` name is rejected with
    ``404 session_unknown`` when no matching active worker is
    registered (Codex #2043 review block 2).

    Worker-facade gating (Codex #2043 review v5 blocker 3):

    * The worker facade is **only** queried when ``session_name``
      syntactically matches ``task-<project>-<n>``. Operator /
      architect / advisor sends never pay pg cost — pre-v5 every send
      opened the work-service facade for the worker enumeration.
    * If the lookup itself fails (pool down, table missing) for a
      worker-syntactic name, we raise ``503 service_unavailable``
      instead of letting the empty list collapse into a misleading
      ``404 session_unknown``. The operator needs to know the answer
      was "couldn't ask" rather than "definitely not registered".
    """
    is_worker_pattern = _looks_like_worker_session(session_name)
    worker_records: list[Any]
    if is_worker_pattern:
        parsed_worker = parse_task_window_name(session_name)
        project_filter = parsed_worker[0] if parsed_worker else None
        try:
            try:
                worker_records = _list_worker_sessions_strict(
                    config, project=project_filter,
                )
            except TypeError:
                worker_records = _list_worker_sessions_strict(config)
        except _WorkerFacadeUnavailable as exc:
            logger.debug(
                "chat_send: worker session lookup failed for %r",
                session_name,
                exc_info=True,
            )
            raise _service_unavailable_worker_lookup(
                str(exc) or type(exc).__name__,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            # Any other unexpected exception (test seam raising
            # ``RuntimeError`` directly, an unhandled programming
            # error in the helper) also routes to 503 rather than
            # leaking a 500 — the operator's actionable signal is
            # still "lookup failed, can't tell".
            logger.debug(
                "chat_send: worker session lookup raised for %r",
                session_name,
                exc_info=True,
            )
            raise _service_unavailable_worker_lookup(
                str(exc) or type(exc).__name__,
            ) from exc
    else:
        worker_records = []

    class _Adapter:
        @staticmethod
        def list_worker_sessions(
            *, project: str | None = None, active_only: bool = True,
        ) -> list[Any]:
            records = worker_records
            if project is not None:
                records = [
                    r for r in records
                    if getattr(r, "task_project", "") == project
                ]
            if not active_only:
                return records
            return [
                r for r in records if getattr(r, "ended_at", None) is None
            ]

    surface = find_chat_surface(
        config,
        session_name,
        work_service=_Adapter if is_worker_pattern else None,
        tmux_client=None,
        include_transcripts=include_transcripts,
    )
    if surface is not None:
        return surface
    raise _session_unknown(session_name)


# ---------------------------------------------------------------------------
# events.jsonl tail reader (mid-tool detection)
# ---------------------------------------------------------------------------


# Heuristic: enough bytes to capture the latest assistant turn plus any
# tool_result that should match its tool_use blocks. JSONL events from
# Claude are typically a few hundred bytes each; 64 KiB gives ~300+
# events even for verbose Bash output. We never need more than the
# tail for the mid-tool check (we only care about the latest assistant
# turn's open tool_use ids).
_EVENTS_TAIL_BYTES = 64 * 1024


def _read_events_tail(
    events_path: Path, max_bytes: int = _EVENTS_TAIL_BYTES,
) -> list[dict[str, Any]]:
    """Return the tail JSON events from ``events_path`` as parsed dicts.

    Reads at most ``max_bytes`` from the end via ``os.lseek`` so very
    long-running sessions don't force the whole transcript into memory
    each request. Skips the (likely truncated) first line when the
    file is larger than ``max_bytes``.

    Tri-state failure semantics (Codex #2043 review v5 blocker 1):

    * Returns ``[]`` when the file does not exist OR exists with zero
      bytes — that's an unambiguous "no transcript content yet"
      signal that the mid-tool gate treats as safe.
    * Raises :class:`TranscriptUnavailable` when the file *exists*
      but can't be evaluated (``stat`` succeeded but ``open``/``read``
      raised ``OSError``, decode raised, etc.). Pre-v5 these all
      collapsed into the empty list, so chmod 000 on an events.jsonl
      with an open ``tool_call`` made the strict-mode mid-tool gate
      fail open.

    Per-line JSON parse failures still skip silently — a single
    malformed mid-stream line shouldn't void the whole tail.
    """
    try:
        size = events_path.stat().st_size
    except FileNotFoundError:
        # No transcript yet — distinct from "can't read"; safe.
        return []
    except OSError as exc:
        raise TranscriptUnavailable(
            f"stat({events_path}) failed: {exc}",
        ) from exc
    if size == 0:
        return []
    start = max(0, size - max_bytes)
    try:
        fd = os.open(events_path, os.O_RDONLY)
    except OSError as exc:
        # File exists (we just stat'd it) but we can't open it —
        # permissions error, race with rotation, FS error. Unavailable.
        raise TranscriptUnavailable(
            f"open({events_path}) failed: {exc}",
        ) from exc
    try:
        try:
            os.lseek(fd, start, os.SEEK_SET)
            raw = os.read(fd, size - start)
        except OSError as exc:
            raise TranscriptUnavailable(
                f"read({events_path}) failed: {exc}",
            ) from exc
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise TranscriptUnavailable(
            f"decode({events_path}) failed: {exc}",
        ) from exc
    lines = text.splitlines()
    if start > 0 and lines:
        # First line is almost certainly truncated mid-record.
        lines = lines[1:]
    events: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            import json  # local import keeps the import block tight

            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            # Per-line parse failures: skip — one bad line shouldn't
            # void the whole tail.
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


# Outcome of resolving the transcript path for a session.
# ``exact``  — P1's fingerprint resolver matched a transcript subdir to
#             the surface's ``cwd`` / ``account`` / ``provider`` (high
#             confidence — never cross-attaches to another surface).
# ``absent`` — no transcript archive matches the surface yet. May mean
#             "brand new session" (archive about to be written) or
#             "operator addressing the wrong session_name" — strict
#             mode treats the ambiguity as unsafe and fails closed
#             *only when other archives exist* in the project root.
_ResolutionQuality = Literal["exact", "absent_but_others_exist", "absent_clean"]


def _project_has_other_transcripts(project_root: Path) -> bool:
    """True when the project's transcripts dir holds at least one archive.

    Distinguishes "brand new project, no transcripts at all" (safe to
    send — no signal yet) from "other sessions are streaming but ours
    isn't matched" (unsafe — operator is probably addressing the
    wrong surface).
    """
    try:
        from pollypm.projects import project_transcripts_dir

        root = project_transcripts_dir(project_root)
    except Exception:  # noqa: BLE001
        return False
    if not root.exists():
        return False
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if child.name in {"tasks", ".ingestion-state.lock"}:
                continue
            if (child / "events.jsonl").exists():
                return True
    except OSError:
        return False
    return False


def _resolve_session_events(
    surface: ChatSurface,
    project_root: Path,
) -> tuple[Path | None, _ResolutionQuality]:
    """Locate the exact events.jsonl for ``surface``.

    Trusts :attr:`ChatSurface.transcript_path` populated by P1's
    single-surface resolver — that's the same fingerprint mapping the
    read endpoints (P2) use, so the send path can never disagree with
    the GET path on "which transcript is this session's". P1's
    resolver returns ``None`` when no archive
    matches the surface's ``(cwd, account, provider)`` fingerprint;
    it never opportunistically picks the freshest.

    Returns ``(path, quality)``. ``absent_but_others_exist`` is the
    fail-closed signal in strict mode (Codex #2043 review block 3):
    archives exist in the project but none fingerprints to this
    surface, which usually means the operator addressed the wrong
    session_name.
    """
    if surface.transcript_path is not None:
        return surface.transcript_path, "exact"
    if _project_has_other_transcripts(project_root):
        return None, "absent_but_others_exist"
    return None, "absent_clean"


def _last_assistant_open_tool_ids(
    events: list[dict[str, Any]],
    *,
    exempt_tool_ids: set[str] | None = None,
) -> set[str]:
    """Return ``tool_use_id``s in the *current* assistant turn lacking a result.

    The intuition is "is the agent right now waiting on a tool result
    we haven't seen yet?" An assistant response is the span from the
    most-recent ``user_turn`` (or ``assistant_turn`` boundary) to the
    end of the tail. ``tool_call`` events in that span without a
    matching ``tool_result`` are the open ones.

    The ``user_turn`` boundary matters because
    :class:`pollypm.transcript_ingest.TranscriptIngestor` only emits
    ``assistant_turn`` when the assistant message carries text. A
    tool-only assistant response (no preface text) produces
    ``tool_call`` events without a preceding ``assistant_turn`` —
    anchoring on ``user_turn`` instead means the mid-tool gate still
    fires for that case (Codex #2043 review block 4).

    ``exempt_tool_ids`` carves out tool_use ids that the *caller* is
    answering. A real AskUserQuestion stays "open" (no tool_result
    until the user types) so without this carveout the strict gate
    409s every ``answer_to`` send. The carveout is intentionally
    narrow — only the specific id the caller addresses is exempted;
    any *other* open tool still blocks the send (Codex #2043 review
    v4 block 1).
    """
    # Find the most-recent boundary. ``turn_end`` / ``session_state``
    # close stale orphaned tool calls left by a prior crashed turn.
    # Everything after that belongs to the current in-flight response.
    boundary = -1
    for idx in range(len(events) - 1, -1, -1):
        event_type = events[idx].get("event_type")
        if event_type in (
            "user_turn",
            "assistant_turn",
            "turn_end",
            "session_state",
        ):
            boundary = idx
            break
    # When no boundary exists in the tail we still scan: that's the
    # very-long-tool-output case where the assistant_turn fell out of
    # the 64 KiB window. Treat the whole tail as the current response.
    span = events[boundary + 1:] if boundary >= 0 else events
    open_ids: set[str] = set()
    seen_results: set[str] = set()
    for event in span:
        etype = event.get("event_type")
        payload = event.get("payload") or {}
        if etype == "tool_call":
            tid = payload.get("id")
            if isinstance(tid, str) and tid:
                open_ids.add(tid)
        elif etype == "tool_result":
            tid = payload.get("tool_use_id")
            if isinstance(tid, str) and tid:
                seen_results.add(tid)
    unmatched = open_ids - seen_results
    if exempt_tool_ids:
        unmatched -= exempt_tool_ids
    return unmatched


def _is_mid_tool(
    events_path: Path | None,
    *,
    exempt_tool_ids: set[str] | None = None,
) -> bool:
    """True when ``events_path``'s tail shows an unmatched tool_use.

    ``exempt_tool_ids`` lets the caller exclude specific tool_use ids
    from the gate — used to allow the ``answer_to`` ask_user reply
    path through (the AskUserQuestion stays "open" until the user
    types a response, see :func:`_last_assistant_open_tool_ids`).

    Propagates :class:`TranscriptUnavailable` from
    :func:`_read_events_tail` so the caller can map it per safety
    level (Codex #2043 review v5 blocker 1). Returning ``False`` here
    on read failure (the pre-v5 behavior) defeats strict mode.
    """
    if events_path is None:
        return False
    events = _read_events_tail(events_path)
    if not events:
        return False
    return bool(
        _last_assistant_open_tool_ids(events, exempt_tool_ids=exempt_tool_ids),
    )


def _force_bypass_gates(
    *,
    events_path: Path | None,
    resolution: _ResolutionQuality,
    config: Any,
    session_name: str,
    exempt_tool_ids: set[str] | None,
) -> list[str]:
    """Best-effort list of gates ``safety=force`` bypassed.

    This never blocks the send path. It only evaluates the same safety
    signals for audit metadata so a later forensic read can distinguish
    a routine force send from a force send during active streaming.
    """
    bypassed: list[str] = []
    if resolution == "absent_but_others_exist":
        bypassed.append("unsafe_mid_tool")
    try:
        if _is_mid_tool(events_path, exempt_tool_ids=exempt_tool_ids):
            bypassed.append("unsafe_mid_tool")
    except TranscriptUnavailable:
        bypassed.append("unsafe_unavailable_transcript")
    try:
        age = _heartbeat_age_seconds(config, session_name)
    except HeartbeatUnavailable:
        bypassed.append("unsafe_unavailable_heartbeat")
    else:
        if age is not None and age < _MID_STREAM_WINDOW_SECONDS:
            bypassed.append("unsafe_mid_stream")
    return sorted(set(bypassed))


def _emit_force_bypass_audit(
    *,
    config: Any,
    project_key: str | None,
    project_root: Path,
    session_name: str,
    target: str,
    message_id: str,
    text_to_send: str,
    method: str,
    press_enter: bool,
    press_enter_at: str | None,
    bypassed_gates: list[str],
) -> None:
    project = project_key or getattr(getattr(config, "project", None), "name", "")
    try:
        audit_emit(
            event=EVENT_CHAT_SEND_FORCE_BYPASS,
            project=str(project or ""),
            subject=session_name,
            actor="rest_client",
            status="warn",
            project_path=project_root,
            metadata={
                "message_id": message_id,
                "window_target": target,
                "characters_sent": len(text_to_send),
                "method": method,
                "press_enter": press_enter,
                "press_enter_at": press_enter_at,
                "bypassed_gates": list(bypassed_gates),
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("chat_send: force-bypass audit emit failed", exc_info=True)


# ---------------------------------------------------------------------------
# Mid-stream detection (heartbeat freshness)
# ---------------------------------------------------------------------------


_MID_STREAM_WINDOW_SECONDS = 2.0


def _heartbeat_age_seconds(config: Any, session_name: str) -> float | None:
    """Return seconds since ``session_name``'s latest heartbeat, or ``None``.

    Reads via :func:`pollypm.storage.pg_heartbeats.latest_heartbeat`
    (pg-only).

    Tri-state semantics (Codex #2043 review v5 blocker 2):

    * ``float`` — heartbeat record exists and is parseable; value is
      seconds since it landed.
    * ``None`` — no heartbeat record exists yet for the session
      (genuine "not streaming" — safe).
    * Raises :class:`HeartbeatUnavailable` — the lookup itself
      failed (pg outage, import failure, malformed timestamp). The
      route maps to ``409 unsafe_unavailable_heartbeat`` in strict
      mode rather than silently failing open.

    Pre-v5 every failure mode collapsed to ``None``, which the
    mid-stream gate interpreted as "not streaming" — a pg outage
    therefore allowed a strict send through.
    """
    try:
        from pollypm.storage.pg_heartbeats import latest_heartbeat
    except Exception as exc:  # noqa: BLE001
        raise HeartbeatUnavailable(
            f"pollypm.storage.pg_heartbeats import failed: {exc}",
        ) from exc
    try:
        record = latest_heartbeat(session_name, config=config)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "chat_send: pg_heartbeats.latest_heartbeat failed", exc_info=True,
        )
        raise HeartbeatUnavailable(
            f"latest_heartbeat({session_name!r}) raised: {exc}",
        ) from exc
    if record is None:
        # Genuine "no row" — safe; not unavailable.
        return None
    stamp = record.created_at
    try:
        normalised = (
            stamp.replace("Z", "+00:00") if stamp.endswith("Z") else stamp
        )
        ts = datetime.fromisoformat(normalised)
    except (TypeError, ValueError, AttributeError) as exc:
        # Record exists but the timestamp is malformed — we *do* have
        # a signal, we just can't interpret it. Treat as unavailable
        # rather than "no record" so strict mode fails closed.
        raise HeartbeatUnavailable(
            f"malformed heartbeat timestamp {stamp!r}: {exc}",
        ) from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - ts).total_seconds())


# ---------------------------------------------------------------------------
# AskUserQuestion answer translation (§4.5)
# ---------------------------------------------------------------------------


def _find_ask_user_envelope(
    events_path: Path | None,
    answer_to: str,
) -> dict[str, Any] | None:
    """Locate the ask_user message matching ``answer_to``.

    Returns the parsed event dict or ``None`` if the id isn't in the
    tail. The lookup is restricted to the events tail — the spec
    only supports answering the most-recent open question.

    Message id contract: P1's :func:`pollypm.web_api.chat.transcripts.
    _envelope_id` returns ``msg_<tool_use_id>`` for tool_call envelopes
    (see ``transcripts.py:398-415`` and ``_envelope_ask_user`` at
    ``transcripts.py:690-720``). A client reading GET ``/messages``
    therefore sees the envelope id as ``msg_toolu_ask`` and POSTs
    ``answer_to=msg_toolu_ask``. The raw events.jsonl payload carries
    only ``toolu_ask`` in ``payload.id`` though, so we normalise the
    incoming ``answer_to`` by stripping the ``msg_`` prefix before the
    lookup — and also try the literal value so callers that pass the
    raw id (CLI tooling, integration tests) still resolve.
    (Codex #2043 review v4 block 2.)
    """
    if events_path is None:
        return None
    try:
        events = _read_events_tail(events_path)
    except TranscriptUnavailable:
        # Surface the same fail-closed signal the mid-tool gate would
        # raise; the route decides how to handle per safety level.
        # Re-raise so the route's strict/loose/force fan-out applies
        # uniformly instead of silently returning "not found" here.
        raise
    # P1's envelope id is ``msg_<tool_use_id>``; the raw events.jsonl
    # only carries the bare id. Try both forms so the route accepts
    # the envelope id the GET endpoints hand out AND the raw id.
    candidates = {answer_to}
    if answer_to.startswith("msg_"):
        candidates.add(answer_to[len("msg_"):])
    for event in events:
        event_id = _event_message_id(event)
        if event_id is None:
            continue
        if event_id in candidates:
            return event
    return None


def _event_message_id(event: dict[str, Any]) -> str | None:
    """Extract the message id from a raw events.jsonl event.

    Different event types carry the id in different places; the chat
    transcript layer (P1) normalises these into a single ``id`` field
    on each envelope. The raw events.jsonl that P3 reads has the id
    in payload.id (tool_use), top-level uuid, or source_offset as a
    stable fallback.
    """
    payload = event.get("payload") or {}
    if isinstance(payload, dict):
        for key in ("id", "uuid", "message_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("uuid", "message_id", "id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _ask_user_options(event: dict[str, Any]) -> list[str] | None:
    """Pull the list of option labels from an ask_user event.

    Returns ``None`` when the event isn't an ask_user. The shape
    comes from §3.5 — ``metadata.questions[].options[].label``.
    Accepts the raw events.jsonl shape too (tool_call payload for
    AskUserQuestion) so the router works pre-normalisation.
    """
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        questions = metadata.get("questions")
        if isinstance(questions, list):
            return _flatten_question_options(questions)
    payload = event.get("payload") or {}
    if isinstance(payload, dict):
        if payload.get("name") == "AskUserQuestion":
            tool_input = payload.get("input") or {}
            if isinstance(tool_input, dict):
                questions = tool_input.get("questions")
                if isinstance(questions, list):
                    return _flatten_question_options(questions)
        if payload.get("type") == "ask_user":
            questions = payload.get("questions")
            if isinstance(questions, list):
                return _flatten_question_options(questions)
    return None


def _flatten_question_options(questions: list[Any]) -> list[str]:
    out: list[str] = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        options = question.get("options")
        if not isinstance(options, list):
            continue
        for option in options:
            if isinstance(option, dict):
                label = option.get("label")
                if isinstance(label, str) and label:
                    out.append(label)
            elif isinstance(option, str) and option:
                out.append(option)
    return out


def _build_answer_text(selections: list[str], notes: str | None) -> str:
    """Format the typed string for an AskUserQuestion reply.

    Spec §4.5 default: option labels joined by newlines, optional
    ``notes`` appended after another newline. The trailing newline is
    handled by ``press_enter`` — we never append our own ``\\n`` at
    the end so ``press_enter=False`` produces a buffer the caller can
    inspect.

    TODO(P4): Sam's open-question #1 — confirm Claude Code's
    AskUserQuestion stdin format. Current implementation matches the
    spec default ("typing the option label verbatim works"). No live
    Claude session reachable from this isolated worktree.
    """
    body = "\n".join(selections)
    if notes:
        if body:
            body = f"{body}\n{notes}"
        else:
            body = notes
    return body


# ---------------------------------------------------------------------------
# Project root helper
# ---------------------------------------------------------------------------


def _resolve_project_root(config: Any, project_key: str | None) -> Path:
    """Return the on-disk root for ``project_key`` or the workspace root.

    Operators (``project=None``) and surfaces whose project isn't
    tracked fall back to ``config.project.root_dir`` — the resolver
    then walks the workspace transcripts dir.
    """
    if project_key:
        projects = getattr(config, "projects", {}) or {}
        known = projects.get(project_key)
        if known is not None:
            return Path(known.path)
    project = getattr(config, "project", None)
    root_dir = getattr(project, "root_dir", None)
    if root_dir is None:
        return Path.cwd()
    return Path(root_dir)


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------


def _tmux_session_for_send(config: Any, project_key: str | None) -> str:
    """Return the storage-closet tmux session name to address.

    All chat surfaces (operator, architect, advisor, per-task workers)
    live inside the project's storage-closet session — matches the
    invariant the supervisor enforces
    (``Supervisor.storage_closet_session_name``).

    Codex #2043 review v7 blocker 4: source the ``-storage-closet``
    suffix from :data:`Supervisor._STORAGE_CLOSET_SESSION_SUFFIX` so
    we don't inline the literal alongside the supervisor's own
    builder. Mirrors :func:`pollypm.cli_features.tier4
    ._resolve_storage_closet_session` and the runtime-services
    ``storage_closet_name`` builder — keep one source of truth.
    """
    from pollypm.supervisor import Supervisor

    project = getattr(config, "project", None)
    tmux_session = getattr(project, "tmux_session", None) if project else None
    if not isinstance(tmux_session, str) or not tmux_session:
        tmux_session = project_key or "pollypm"
    suffix = getattr(
        Supervisor, "_STORAGE_CLOSET_SESSION_SUFFIX", "-storage-closet",
    )
    return f"{tmux_session}{suffix}"


def _resolve_pane_target(
    tmux: TmuxClient,
    storage_session: str,
    window_name: str,
    pane_index: int | None,
) -> tuple[str, bool]:
    """Return ``(target, present)`` for a window + optional pane index.

    ``present`` is True when the window exists.

    - ``pane_index is None`` — default active-pane target
      (``session:window``). Uses ``window.pane_dead`` from
      ``list_windows`` as the liveness signal.
    - ``pane_index >= 0`` (including 0) — explicit pane target. ALWAYS
      probes ``list_panes(window_target)`` to verify the index exists
      AND is alive, then routes to ``session:window.N``. Explicit
      ``pane=0`` does NOT alias to the window-level target — tmux's
      active pane is not necessarily index 0, and silently aliasing
      bypasses the pane existence/liveness checks the explicit form
      is supposed to add (Codex #2043 review v3 block 1).
    - ``pane_index < 0`` — rejected with ``409 pane_invalid``.

    Raises ``409 pane_invalid`` for out-of-range / missing panes,
    ``409 pane_dead`` for dead panes (Codex #2043 review block 5).
    Distinguishes "tmux itself is down" (``503 tmux_unavailable``)
    from "the window/pane doesn't exist" (``503 window_missing`` /
    ``409 pane_invalid``) so the operator gets actionable typed
    errors instead of generic 500s (Codex #2043 review v4 block 3).
    """
    window_target = f"{storage_session}:{window_name}"
    if pane_index is None:
        get_window = getattr(tmux, "get_window", None)
        if callable(get_window):
            try:
                try:
                    window = get_window(window_target, timeout=1)
                except TypeError:
                    window = get_window(window_target)
            except FileNotFoundError as exc:
                raise _tmux_unavailable(
                    f"tmux binary not found: {exc}",
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise _tmux_unavailable(
                    f"tmux command timed out after {exc.timeout}s",
                ) from exc
            except subprocess.CalledProcessError:
                logger.debug(
                    "chat_send: get_window(%r) CalledProcessError",
                    window_target,
                    exc_info=True,
                )
                return window_target, False
            except OSError as exc:
                raise _tmux_unavailable(str(exc)) from exc
            if window is None:
                return window_target, False
            if getattr(window, "pane_dead", False):
                raise _pane_dead(window_target)
            return window_target, True

    try:
        windows = tmux.list_windows(storage_session)
    except FileNotFoundError as exc:
        # tmux binary not on PATH — deployment failure, not a
        # per-request bug.
        raise _tmux_unavailable(f"tmux binary not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        # tmux server wedged (see ``TmuxClient.run`` 15s default).
        raise _tmux_unavailable(
            f"tmux command timed out after {exc.timeout}s",
        ) from exc
    except subprocess.CalledProcessError:
        # ``list-windows`` exits non-zero when the target session
        # doesn't exist (e.g. storage-closet was torn down). That's
        # the documented ``window_missing`` envelope — surface it
        # as "absent" rather than tmux-down so the operator restarts
        # the right thing.
        logger.debug(
            "chat_send: list_windows(%r) CalledProcessError",
            storage_session,
            exc_info=True,
        )
        return window_target, False
    except OSError as exc:
        # Other subprocess plumbing errors (permission denied on
        # ``tmux`` binary, etc.) — treat as tmux_unavailable so the
        # operator looks at the deployment, not at this specific
        # window.
        raise _tmux_unavailable(str(exc)) from exc
    matching = [w for w in windows if w.name == window_name]
    if not matching:
        return f"{storage_session}:{window_name}", False
    window = matching[0]
    # Default-pane path: caller didn't pick a specific pane, so we
    # use tmux's active-pane target. window.pane_dead from
    # list_windows is the active-pane health bit, which is the right
    # signal here.
    if pane_index is None:
        if window.pane_dead:
            raise _pane_dead(window_target)
        return window_target, True
    # Specific pane requested (including 0). Pane indexes must be
    # >= 0 and the pane has to exist on the window AND be alive.
    # Without this gate a negative or out-of-range index slips
    # through to ``send_keys`` and surfaces as a 500 — and explicit
    # ``pane=0`` would silently go to tmux's active pane, which is
    # not guaranteed to be pane 0.
    if pane_index < 0:
        raise _pane_invalid(pane_index, window_target)
    try:
        panes = tmux.list_panes(window_target)
    except FileNotFoundError as exc:
        raise _tmux_unavailable(f"tmux binary not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise _tmux_unavailable(
            f"tmux command timed out after {exc.timeout}s",
        ) from exc
    except subprocess.CalledProcessError as exc:
        # ``list-panes`` exits non-zero when the *target* (window) no
        # longer exists. That's an "actionable for the operator"
        # state — surface as pane_invalid so they pick a different
        # pane / surface, rather than blaming tmux.
        logger.debug(
            "chat_send: list_panes(%r) CalledProcessError",
            window_target,
            exc_info=True,
        )
        raise _pane_invalid(pane_index, window_target) from exc
    except OSError as exc:
        raise _tmux_unavailable(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # Fake clients / unforeseen errors — preserve the original
        # pane_invalid mapping to avoid a 500 leak. The narrow
        # subprocess branches above cover the production cases.
        logger.debug(
            "chat_send: list_panes(%r) failed", window_target, exc_info=True,
        )
        raise _pane_invalid(pane_index, window_target) from exc
    match = next((p for p in panes if p.pane_index == pane_index), None)
    if match is None:
        raise _pane_invalid(pane_index, window_target)
    if match.pane_dead:
        raise _pane_dead(f"{window_target}.{pane_index}")
    return f"{window_target}.{pane_index}", True


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/{session_name}/send",
    response_model=ChatSendResponse,
    summary="Send a message to a chat surface",
    operation_id="sendChatMessage",
)
def send_chat_message(  # noqa: PLR0912, PLR0915 — gate logic is intentionally inline
    session_name: str,
    body: ChatSendRequest,
    config: ConfigDep,
    response: Response,
    safety: Annotated[
        Literal["strict", "loose", "force"] | None,
        Query(
            description=(
                "Optional query-string safety override. When present, "
                "this takes precedence over the JSON body safety field."
            )
        ),
    ] = None,
) -> ChatSendResponse:
    """POST /api/v1/chat/{session_name}/send — push text into a tmux pane."""
    safety_mode = safety or body.safety
    needs_transcript = safety_mode != "force" or body.answer_to is not None

    # 1. Resolve the one target surface. Plain force sends do not need
    # a transcript before touching tmux, so keep the latency-critical
    # path out of the session-index filesystem walk.
    surface = _resolve_surface(
        config,
        session_name,
        include_transcripts=needs_transcript,
    )
    # ``surface.project`` is intentionally ``None`` for operators in P1
    # (the operator is workspace-wide). The send path still needs a
    # concrete project_key for storage-closet naming and transcripts
    # discovery — pull it from the session config the same way P1's
    # ``_project_key_for_session`` helper does.
    project_key = surface.project
    if not project_key:
        session = (config.sessions or {}).get(session_name)
        project_key = (
            getattr(session, "project", None)
            or getattr(getattr(config, "project", None), "name", None)
        )

    storage_session = _tmux_session_for_send(config, project_key)
    window_name = surface.window.window_name

    tmux = TmuxClient()
    target, present = _resolve_pane_target(
        tmux, storage_session, window_name, body.pane,
    )
    if not present:
        raise _window_missing(target)

    project_root = _resolve_project_root(config, project_key)

    # 2. AskUserQuestion answer lookup runs BEFORE the safety gates so
    # we can exempt the specific ask_user tool_use_id from the
    # mid-tool check (Codex #2043 review v4 block 1). A real
    # AskUserQuestion has no matching tool_result until the user
    # types a response — without this carveout the strict gate 409s
    # every legitimate ``answer_to`` send.
    #
    # Transcript-unavailable handling (Codex #2043 review v5
    # blocker 1) is uniform across this lookup and the mid-tool gate:
    # strict mode fails closed via ``unsafe_unavailable_transcript``,
    # loose mode continues with a warning header, force mode ignores
    # the failure entirely. We track the helper's outcome rather than
    # short-circuiting on the first call so the warning header lands
    # even when the answer_to lookup is what raised.
    events_path: Path | None = None
    resolution: _ResolutionQuality = "absent_clean"
    if needs_transcript:
        events_path, resolution = _resolve_session_events(surface, project_root)
    ask_envelope: dict[str, Any] | None = None
    ask_exempt_ids: set[str] = set()
    transcript_unavailable_detail: str | None = None
    if body.answer_to is not None:
        try:
            ask_envelope = _find_ask_user_envelope(events_path, body.answer_to)
        except TranscriptUnavailable as exc:
            transcript_unavailable_detail = str(exc)
            # Strict and loose can't validate the answer_to lookup
            # without the transcript. Force still proceeds (no
            # ask_envelope means selections-only sends will fail
            # later — that's fine; force is "I know what I'm doing").
            if safety_mode == "strict":
                raise _unsafe_unavailable_transcript(str(exc)) from exc
            # loose: continue; the answer_to branch below will surface
            # ``answer_to_missing`` if the envelope can't be found, and
            # we emit the warning header at the end of the safety
            # block.
        if ask_envelope is not None:
            # The exempted id is the raw ``payload.id`` because the
            # mid-tool gate matches on that field; the envelope id
            # comparison was already normalised inside
            # ``_find_ask_user_envelope``.
            raw_id = _event_message_id(ask_envelope)
            if isinstance(raw_id, str) and raw_id:
                ask_exempt_ids.add(raw_id)

    # 3. Safety gates (§4.1, §4.3) — use the exact session transcript
    # (Codex review block 3).
    force_bypassed_gates: list[str] = []
    force_gate_audit_deferred = safety_mode == "force" and not needs_transcript
    if safety_mode == "force":
        if not force_gate_audit_deferred:
            force_bypassed_gates = _force_bypass_gates(
                events_path=events_path,
                resolution=resolution,
                config=config,
                session_name=session_name,
                exempt_tool_ids=ask_exempt_ids,
            )
    else:
        if resolution == "absent_but_others_exist":
            # Strict and loose both require an exact transcript
            # mapping. Other transcripts exist for the project but
            # none fingerprints to this surface — the operator is
            # almost certainly addressing the wrong session_name.
            # Fail closed; safety=force bypasses.
            raise _unsafe_mid_tool()
        try:
            if _is_mid_tool(events_path, exempt_tool_ids=ask_exempt_ids):
                raise _unsafe_mid_tool()
        except TranscriptUnavailable as exc:
            transcript_unavailable_detail = (
                transcript_unavailable_detail or str(exc)
            )
            if safety_mode == "strict":
                raise _unsafe_unavailable_transcript(str(exc)) from exc
            # loose: continue — warning header is set below.
        try:
            age = _heartbeat_age_seconds(config, session_name)
        except HeartbeatUnavailable as exc:
            # Strict: fail closed; loose: continue with warning header.
            if safety_mode == "strict":
                raise _unsafe_unavailable_heartbeat(str(exc)) from exc
            # loose
            response.headers["X-PollyPM-Warning"] = "heartbeat-unavailable"
            streaming = False
        else:
            streaming = age is not None and age < _MID_STREAM_WINDOW_SECONDS
        if safety_mode == "strict" and streaming:
            raise _unsafe_mid_stream()
        if safety_mode == "loose" and streaming:
            response.headers["X-PollyPM-Warning"] = "agent-may-be-streaming"
        if (
            safety_mode == "loose"
            and transcript_unavailable_detail is not None
            and "X-PollyPM-Warning" not in response.headers
        ):
            # Loose mode + unreadable transcript: continue but surface
            # the best-effort warning so the operator knows the
            # mid-tool gate couldn't actually be evaluated.
            response.headers["X-PollyPM-Warning"] = "transcript-unavailable"

    # 4. AskUserQuestion answer handling (§4.5).
    text_to_send: str | None
    if body.answer_to is not None:
        if ask_envelope is None:
            raise _answer_to_missing(body.answer_to)
        options = _ask_user_options(ask_envelope)
        if options is None:
            raise _selections_no_question(body.answer_to)
        invalid = [s for s in body.selections if s not in options]
        if body.selections and invalid:
            raise _selections_invalid(invalid, options)
        if body.selections:
            text_to_send = _build_answer_text(body.selections, body.notes)
        elif body.text:
            text_to_send = body.text
        elif body.notes:
            text_to_send = body.notes
        else:
            raise APIError(
                status_code=400,
                code="invalid_request",
                message="answer_to requires selections, text, or notes.",
            )
    else:
        if not body.text:
            raise APIError(
                status_code=400,
                code="invalid_request",
                message="text is required when answer_to is not set.",
            )
        text_to_send = body.text

    assert text_to_send is not None  # noqa: S101 — narrowed above

    # 5. Send via tmux.
    method: Literal["send_keys", "paste_buffer"] = (
        "paste_buffer" if len(text_to_send) > 100 else "send_keys"
    )
    message_id = f"msg_{uuid.uuid4().hex}"
    press_enter_at: str | None = None
    enter_delay_seconds = 0.0 if method == "send_keys" else 0.5
    try:
        tmux.send_keys(
            target,
            text_to_send,
            press_enter=body.press_enter,
            enter_delay_seconds=enter_delay_seconds,
        )
    except DeadPaneError as exc:
        # Pane died between validation and send. Already mapped to a
        # typed 409 before this refactor — keep that shape.
        raise _pane_dead(str(exc)) from exc
    except FileNotFoundError as exc:
        # Tmux binary missing entirely. The server can't reach tmux —
        # this is a deployment problem, not a per-request bug.
        raise _tmux_unavailable(f"tmux binary not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        # Tmux server itself wedged (see ``TmuxClient.run`` for the
        # canonical 15s timeout). The send may or may not have landed;
        # 503 lets the operator retry with knowledge of the cause.
        raise _tmux_unavailable(
            f"tmux command timed out after {exc.timeout}s",
        ) from exc
    except subprocess.CalledProcessError as exc:
        # Most commonly: the window disappeared between
        # ``list_windows`` validation and the actual ``send-keys``
        # invocation (e.g. supervisor torn it down mid-request).
        # ``send-keys`` returns exit code 1 with "can't find session"
        # / "can't find window" on stderr. The window_missing envelope
        # is documented for exactly this race (Codex #2043 review v3
        # block 2).
        stderr = (exc.stderr or "").strip().lower()
        if "can't find" in stderr or "no such" in stderr or "session not found" in stderr:
            raise _window_missing(target) from exc
        # Other subprocess failures (permissions, malformed args we
        # didn't sanitize, etc.) — generic send_failed envelope.
        raise _send_failed(
            target, exc.stderr.strip() if exc.stderr else f"exit {exc.returncode}",
        ) from exc
    except OSError as exc:
        # Paste-buffer load failure (tempfile creation, load-buffer
        # disk IO error). Map to send_failed so the operator can
        # retry rather than seeing a 500.
        raise _send_failed(target, str(exc)) from exc
    if body.press_enter:
        press_enter_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if safety_mode == "force":
        if force_gate_audit_deferred:
            try:
                force_bypassed_gates = _force_bypass_gates(
                    events_path=None,
                    resolution="absent_clean",
                    config=config,
                    session_name=session_name,
                    exempt_tool_ids=ask_exempt_ids,
                )
            except Exception:  # noqa: BLE001
                force_bypassed_gates = []
                logger.debug(
                    "chat_send: force-bypass gate audit failed",
                    exc_info=True,
                )
        _emit_force_bypass_audit(
            config=config,
            project_key=project_key,
            project_root=project_root,
            session_name=session_name,
            target=target,
            message_id=message_id,
            text_to_send=text_to_send,
            method=method,
            press_enter=body.press_enter,
            press_enter_at=press_enter_at,
            bypassed_gates=force_bypassed_gates,
        )

    return ChatSendResponse(
        ok=True,
        message_id=message_id,
        session_name=session_name,
        window_target=target,
        characters_sent=len(text_to_send),
        method=method,
        press_enter_at=press_enter_at,
    )


__all__ = [
    "ChatSendRequest",
    "ChatSendResponse",
    "HeartbeatUnavailable",
    "TranscriptUnavailable",
    "router",
    "send_chat_message",
]
