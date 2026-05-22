"""Heartbeats GET endpoints (Phase 2 — §11 of the endpoints spec).

Implements:

- ``GET /api/v1/heartbeats`` — latest heartbeat row per configured
  session.
- ``GET /api/v1/heartbeats/{session_name}`` — single-session detail
  with the recent-tick history attached.

Reads-only — pg-only paths via the canonical
:mod:`pollypm.storage.pg_heartbeats` facade so the surface composes
with the cockpit / ``pm sessions`` / supervisor without becoming a
second reader implementation. Phase 2 §11.1 also lists an SSE stream
endpoint; that ships separately (see follow-up) so this PR stays
focused on the read-side surface.

The route classifies each session into ``healthy`` / ``stale`` /
``unknown`` / ``initializing`` using the same 5-minute staleness
threshold as :mod:`pollypm.cli_features.sessions_health` so the API
agrees with the CLI summary verbatim.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from pollypm import session_health
from pollypm.web_api.errors import not_found, service_unavailable
from pollypm.web_api.routes._deps import ConfigDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Heartbeats"])


# ---------------------------------------------------------------------------
# Classification — delegated to the shared :mod:`pollypm.session_health`
# helper layer (Codex PR #2055 round 2). The heartbeat staleness threshold
# and the ``healthy`` / ``stale`` / ``unknown`` predicate are owned there
# so the API and ``pm sessions`` CLI cannot drift. We keep ONLY the
# endpoint-specific ``initializing`` override here — when no ledger row
# exists at all, that's a brand-new session, not a stale one.
# ---------------------------------------------------------------------------


# Default + max for the per-session ``?limit`` query on the detail
# endpoint. ``pg_heartbeats.recent_heartbeats`` accepts any positive
# integer; we clamp at the route layer so a frontend can't ask for a
# million rows in one shot (spec §11.3 — clamp without erroring).
_DEFAULT_HISTORY_LIMIT = 20
_MAX_HISTORY_LIMIT = 200


# ---------------------------------------------------------------------------
# Response models (spec §11.2)
# ---------------------------------------------------------------------------


class HeartbeatLatest(BaseModel):
    """Latest heartbeat snapshot for one session (spec §11.2).

    ``last_tick_ts`` is ``None`` for sessions that have never reported
    in — the route flips ``status`` to ``"initializing"`` for that
    case so the caller doesn't conflate a brand-new session with a
    stale one (spec §11.3 edge case).
    """

    session_name: str
    role: str | None = None
    project: str | None = None
    status: str
    last_tick_ts: str | None = None
    age_seconds: int | None = None
    pane_command: str | None = None
    pane_dead: bool = False
    log_bytes: int = 0
    snapshot_path: str | None = None
    snapshot_hash: str | None = None
    tmux_window: str | None = None
    pane_id: str | None = None


class HeartbeatTick(BaseModel):
    """One raw heartbeat row from the history table (spec §11.2)."""

    ts: str
    pane_command: str | None = None
    pane_dead: bool = False
    log_bytes: int = 0
    snapshot_path: str | None = None
    snapshot_hash: str | None = None
    tmux_window: str | None = None
    pane_id: str | None = None


class HeartbeatsListResponse(BaseModel):
    """``GET /api/v1/heartbeats`` envelope."""

    heartbeats: list[HeartbeatLatest]


class HeartbeatDetailResponse(BaseModel):
    """``GET /api/v1/heartbeats/{session_name}`` envelope.

    ``ticks`` is the most-recent ``limit`` rows, newest first. ``clamped``
    is ``True`` when the caller's ``?limit`` exceeded
    :data:`_MAX_HISTORY_LIMIT` and the route silently clamped (spec
    §11.3 — clamp without erroring).
    """

    latest: HeartbeatLatest
    ticks: list[HeartbeatTick]
    clamped: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classify_status(
    *,
    has_record: bool,
    age_seconds: int | None,
) -> str:
    """Map ``(record-exists?, age)`` to one of the spec's status strings.

    Single-source delegation to
    :func:`pollypm.session_health.classify_status` (Codex PR #2055 round
    2) — the ``healthy`` / ``stale`` / ``unknown`` threshold is owned
    there. We layer ONE endpoint-specific override on top: when no
    ledger row exists at all, return ``"initializing"`` so the caller
    doesn't conflate a brand-new session with a stale one (spec §11.3
    edge case). Heartbeats endpoint doesn't probe tmux, so we always
    pass ``window_present=True`` — the supervisor-side health cascade
    adds the window-present check; the API stays storage-only so a tmux
    outage doesn't 500 a read.

    Resolved through the ``session_health`` module attribute (not a
    bound name) so test patches on
    ``pollypm.session_health.classify_status`` flow through verbatim.
    """
    if not has_record:
        return "initializing"
    return session_health.classify_status(
        window_present=True,
        age_seconds=age_seconds,
    )


def _session_meta(config, session_name: str) -> tuple[str | None, str | None]:
    """Return ``(role, project)`` from the config or ``(None, None)``.

    Used to enrich heartbeat rows so a single GET answers "which session
    is this? which project does it belong to?" without the caller
    having to cross-reference ``GET /chat/sessions``.
    """
    sessions = getattr(config, "sessions", {}) or {}
    session = sessions.get(session_name)
    if session is None:
        return (None, None)
    return (
        getattr(session, "role", None),
        getattr(session, "project", None),
    )


def _row_to_latest(
    *,
    config,
    session_name: str,
    row,
) -> HeartbeatLatest:
    """Pack a :class:`HeartbeatRecord` (or ``None``) into a wire row."""
    role, project = _session_meta(config, session_name)
    if row is None:
        return HeartbeatLatest(
            session_name=session_name,
            role=role,
            project=project,
            status=_classify_status(has_record=False, age_seconds=None),
        )
    created_at = getattr(row, "created_at", None)
    age = session_health.age_seconds(created_at)
    return HeartbeatLatest(
        session_name=session_name,
        role=role,
        project=project,
        status=_classify_status(has_record=True, age_seconds=age),
        last_tick_ts=created_at,
        age_seconds=age,
        pane_command=getattr(row, "pane_command", None),
        pane_dead=bool(getattr(row, "pane_dead", False)),
        log_bytes=int(getattr(row, "log_bytes", 0) or 0),
        snapshot_path=getattr(row, "snapshot_path", None),
        snapshot_hash=getattr(row, "snapshot_hash", None),
        tmux_window=getattr(row, "tmux_window", None),
        pane_id=getattr(row, "pane_id", None),
    )


def _row_to_tick(row) -> HeartbeatTick:
    return HeartbeatTick(
        ts=getattr(row, "created_at", "") or "",
        pane_command=getattr(row, "pane_command", None),
        pane_dead=bool(getattr(row, "pane_dead", False)),
        log_bytes=int(getattr(row, "log_bytes", 0) or 0),
        snapshot_path=getattr(row, "snapshot_path", None),
        snapshot_hash=getattr(row, "snapshot_hash", None),
        tmux_window=getattr(row, "tmux_window", None),
        pane_id=getattr(row, "pane_id", None),
    )


def _configured_session_names(config) -> list[str]:
    """Return the list of session names to enumerate.

    Honors ``enabled`` on each :class:`pollypm.models.SessionConfig` so
    a disabled session doesn't appear with status ``initializing`` (it
    would be misleading — disabled sessions never run, so the ledger
    not having a row for them is correct, not a bug). Returned sorted
    for stable ordering.
    """
    sessions = getattr(config, "sessions", {}) or {}
    return sorted(
        name
        for name, session in sessions.items()
        if getattr(session, "enabled", True)
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/heartbeats",
    response_model=HeartbeatsListResponse,
    summary="Latest heartbeat per configured session",
    operation_id="listHeartbeats",
)
def list_heartbeats_endpoint(config: ConfigDep) -> HeartbeatsListResponse:
    """GET /api/v1/heartbeats — one ``HeartbeatLatest`` row per session.

    Sessions that have never reported in get ``status="initializing"``
    and ``last_tick_ts=null``. Disabled sessions are filtered out
    (their absence from the ledger is correct, not a bug).

    Backing-store outages bubble as 503 ``service_unavailable`` so the
    caller can retry; we don't paper over a pg-pool outage with an
    empty list because that would tell the cockpit "every session is
    initializing" which is dangerously misleading.

    Performance contract (pinned by
    ``test_list_heartbeats_uses_bulk_query``): exactly ONE
    ``pg_heartbeats`` query for N sessions, not N. The previous loop
    was an N+1 read that hurt dashboard polling at session counts
    >~10. See PR #2055 round 1.
    """
    from pollypm.storage.pg_heartbeats import (
        latest_heartbeats_bulk as _latest_bulk,
    )

    session_names = _configured_session_names(config)
    try:
        records_by_name = _latest_bulk(session_names, config=config)
    except Exception as exc:  # noqa: BLE001 — typed translation below
        logger.warning(
            "heartbeats: latest_heartbeats_bulk(%d sessions) raised; "
            "surfacing 503",
            len(session_names),
            exc_info=True,
        )
        raise service_unavailable(
            f"heartbeats ledger unavailable while listing "
            f"{len(session_names)} session(s): {exc.__class__.__name__}",
            hint=(
                "The Postgres heartbeats facade is unreachable. "
                "Retry shortly; check `pm doctor` and pg pool health."
            ),
        ) from exc

    rows = [
        _row_to_latest(
            config=config,
            session_name=session_name,
            # Sessions missing from the bulk result have never reported
            # in — map to None so ``_row_to_latest`` produces an
            # ``initializing`` row (matches the previous per-session
            # ``latest_heartbeat() -> None`` behaviour).
            row=records_by_name.get(session_name),
        )
        for session_name in session_names
    ]
    return HeartbeatsListResponse(heartbeats=rows)


@router.get(
    "/heartbeats/{session_name}",
    response_model=HeartbeatDetailResponse,
    summary="Latest heartbeat + recent history for one session",
    operation_id="getHeartbeat",
)
def get_heartbeat_endpoint(
    session_name: str,
    config: ConfigDep,
    limit: Annotated[int, Query(
        ge=1,
        # No ``le=`` — spec §11.3 says clamp rather than 422 so a
        # cockpit pull with ``?limit=500`` succeeds with ``clamped:true``
        # instead of asking the user to retune their query.
        description=(
            f"Max history ticks (default {_DEFAULT_HISTORY_LIMIT}, "
            f"clamped at {_MAX_HISTORY_LIMIT})."
        ),
    )] = _DEFAULT_HISTORY_LIMIT,
) -> HeartbeatDetailResponse:
    """GET /api/v1/heartbeats/{session_name} — detail + recent ticks.

    404 ``not_found`` when ``session_name`` is neither in the config
    nor in the heartbeats ledger — i.e. nothing in the system has ever
    heard of that session. Configured-but-never-reported sessions get
    a 200 with ``status="initializing"`` and an empty ``ticks`` list.
    """
    from pollypm.storage.pg_heartbeats import (
        latest_heartbeat as _latest,
        recent_heartbeats as _recent,
    )

    clamped = False
    effective_limit = limit
    if effective_limit > _MAX_HISTORY_LIMIT:
        effective_limit = _MAX_HISTORY_LIMIT
        clamped = True

    configured = _configured_session_names(config)
    is_configured = session_name in configured

    try:
        latest_row = _latest(session_name, config=config)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "heartbeats: latest_heartbeat(%s) raised; surfacing 503",
            session_name,
            exc_info=True,
        )
        raise service_unavailable(
            f"heartbeats ledger unavailable while reading session "
            f"{session_name!r}: {exc.__class__.__name__}",
            hint=(
                "The Postgres heartbeats facade is unreachable. "
                "Retry shortly; check `pm doctor` and pg pool health."
            ),
        ) from exc

    if not is_configured and latest_row is None:
        raise not_found(
            f"No heartbeat ledger entry or configured session named "
            f"{session_name!r}.",
            hint=(
                "Use GET /api/v1/heartbeats to discover sessions the "
                "supervisor has heard from, or check config.sessions."
            ),
        )

    latest = _row_to_latest(
        config=config,
        session_name=session_name,
        row=latest_row,
    )

    try:
        history_rows = _recent(
            session_name,
            effective_limit,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "heartbeats: recent_heartbeats(%s) raised; surfacing 503",
            session_name,
            exc_info=True,
        )
        raise service_unavailable(
            f"heartbeats history unavailable for session "
            f"{session_name!r}: {exc.__class__.__name__}",
            hint=(
                "The Postgres heartbeats facade is unreachable. "
                "Retry shortly; check `pm doctor` and pg pool health."
            ),
        ) from exc

    return HeartbeatDetailResponse(
        latest=latest,
        ticks=[_row_to_tick(row) for row in history_rows],
        clamped=clamped,
    )


__all__ = [
    "HeartbeatDetailResponse",
    "HeartbeatLatest",
    "HeartbeatTick",
    "HeartbeatsListResponse",
    "get_heartbeat_endpoint",
    "list_heartbeats_endpoint",
    "router",
]
