"""Sessions admin endpoints — Phase 2 surface #8 (§10 of the endpoint spec).

Mirrors the ``pm sessions`` CLI (#2039) plus restart / pause / resume
actions documented in ``~/Desktop/pollypm-phase2-endpoints-spec.md``
§10.

Endpoints
---------

- ``GET    /api/v1/sessions``                  — list configured
  sessions with live health (heartbeat age + tmux window presence).
- ``GET    /api/v1/sessions/{name}``           — one session's detail
  payload (config row + health snapshot + last heartbeat).
- ``POST   /api/v1/sessions/{name}/restart``   — kill the tmux window
  and re-create it. Refuses with ``409 unsafe_mid_turn`` when the agent
  is mid-turn unless ``?safety=force``.
- ``POST   /api/v1/sessions/{name}/pause``     — write a pause marker
  so the recovery / dispatch loops skip this session. Idempotent.
- ``POST   /api/v1/sessions/{name}/resume``    — remove the pause
  marker. Idempotent.

Design notes
------------

* **Read-side health.** Mirrors :mod:`pollypm.cli_features.sessions_health`
  classification (``healthy`` / ``stale`` / ``missing`` / ``unknown``) so
  the API returns the same status field a CLI operator sees in
  ``pm sessions``. Heartbeats come from the pg facade
  :func:`pollypm.storage.pg_heartbeats.latest_heartbeat`; tmux probes
  come from :mod:`pollypm.tmux.client`. Either side may be missing —
  read failures collapse to ``unknown`` / ``missing`` rather than
  500-ing.

* **Restart.** Uses :class:`pollypm.session_services.tmux.TmuxSessionService`
  to ``destroy`` then ``create``. The CLI is the only other caller that
  performs that pair, and we deliberately re-use that helper so any
  future change to launch automation lands in both surfaces at once.
  Restart is intentionally synchronous (spec §10.4) — typical restart
  time is < 5s and the spec defers ``?async`` to Phase 2.5.

* **Pause / resume.** No existing helper does this; we use a small
  on-disk JSON marker at ``<base_dir>/paused-sessions.json`` so the
  state survives daemon restarts and is visible to any other reader
  (the supervisor will learn to consume this in a follow-up; for now
  the marker is informational + the API surfaces ``status="paused"``).
  Both operations are idempotent and return 200.

* **pg outages → 503.** ``GET`` endpoints downgrade pg outages to a
  best-effort response (``status="unknown"`` on the affected row).
  ``POST`` endpoints surface pg outages as ``503 service_unavailable``
  so the client knows to retry.

Auth follows the standard bearer-dependency wiring in
:mod:`pollypm.web_api.app`; this router is mounted under ``/api/v1`` so
``auth_deps`` applies to every operation here.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from pollypm.web_api.errors import APIError, service_unavailable
from pollypm.web_api.models import ActionResult
from pollypm.web_api.routes._deps import ConfigDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Sessions"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Heartbeats older than this seconds are classified ``stale``. Matches
# :data:`pollypm.cli_features.sessions_health._STALE_HEARTBEAT_SECONDS`
# (intentionally duplicated so the API does not import a private CLI
# constant — the comment is the cross-reference).
_STALE_HEARTBEAT_SECONDS = 5 * 60

# Filename used by :func:`_pause_marker_path`. One JSON file per
# project; the document is a list of paused session names. Sitting in
# the project's ``base_dir`` keeps it next to ``state.db`` /
# ``audit.jsonl`` so storage hygiene already covers it.
_PAUSE_MARKER_FILENAME = "paused-sessions.json"


SafetyMode = Literal["strict", "loose", "force"]


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SessionInfo(BaseModel):
    """Flat list row mirroring ``pm sessions`` output (spec §10.2)."""

    name: str
    role: str
    project: str
    provider: str
    account: str
    window_name: str
    tmux_session: str
    window_present: bool
    status: Literal["healthy", "stale", "missing", "unknown", "paused"]
    last_heartbeat_iso: str | None = None
    last_heartbeat_age_seconds: int | None = None
    auth_token_present: bool
    enabled: bool
    paused: bool = False


class SessionsListResponse(BaseModel):
    """``GET /api/v1/sessions`` envelope."""

    sessions: list[SessionInfo]


class SessionHealthSnapshot(BaseModel):
    """Per-session health (spec §10.2 ``SessionDetail`` member).

    Mirrors :class:`pollypm.session_services.base.SessionHealth` minus
    ``pane_text`` (we don't ship multi-KB pane captures over the API by
    default — clients can opt into ``/sessions/{name}?include_pane_text``
    in a follow-up).
    """

    window_present: bool
    pane_alive: bool
    pane_dead: bool
    pane_command: str | None = None


class SessionConfigView(BaseModel):
    """Mirror of :class:`pollypm.models.SessionConfig` on the wire."""

    name: str
    role: str
    provider: str
    account: str
    project: str
    cwd: str
    window_name: str
    enabled: bool
    auth_token_present: bool
    agent_profile: str | None = None
    args: list[str] = Field(default_factory=list)


class SessionDetail(BaseModel):
    """``GET /api/v1/sessions/{name}`` envelope (spec §10.1)."""

    info: SessionInfo
    config: SessionConfigView
    health: SessionHealthSnapshot
    is_turn_active: bool = False


# ---------------------------------------------------------------------------
# Typed errors (spec §10.3)
# ---------------------------------------------------------------------------


def _session_not_found(name: str) -> APIError:
    return APIError(
        status_code=404,
        code="not_found",
        message=f"No configured session named {name!r}",
        hint=(
            "List sessions via GET /api/v1/sessions. Worker (per-task) "
            "sessions are not exposed by this admin surface — use "
            "GET /api/v1/chat/sessions for those."
        ),
    )


def _unsafe_mid_turn(name: str) -> APIError:
    return APIError(
        status_code=409,
        code="unsafe_mid_turn",
        message=(
            f"Refusing to restart {name!r}: agent appears to be mid-turn. "
            "Re-issue with ?safety=force to override."
        ),
        hint="Wait for the agent to finish or pass ?safety=force to restart anyway.",
    )


def _daemon_unavailable(name: str, detail: str) -> APIError:
    return APIError(
        status_code=503,
        code="daemon_unavailable",
        message=(
            f"Cannot act on session {name!r}: supervisor/daemon unavailable "
            f"({detail})."
        ),
        hint="Confirm the daemon / cockpit is running, then retry.",
    )


# ---------------------------------------------------------------------------
# Helpers — heartbeat, tmux probe, pause marker
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        candidate = value.replace("Z", "+00:00") if value.endswith("Z") else value
        parsed = datetime.fromisoformat(candidate)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _age_seconds(iso_ts: str | None) -> int | None:
    parsed = _parse_iso(iso_ts)
    if parsed is None:
        return None
    return int(max(0, (_now_utc() - parsed).total_seconds()))


def _classify_status(
    *,
    window_present: bool,
    age_seconds: int | None,
    paused: bool,
) -> str:
    """Return one of healthy/stale/missing/unknown/paused.

    Matches :func:`pollypm.cli_features.sessions_health._classify_status`
    except we surface ``paused`` first — operator intent overrides the
    runtime signals (a paused session may still have a healthy pane,
    but the cockpit / recovery loop should treat it as quiesced).
    """
    if paused:
        return "paused"
    if not window_present:
        return "missing"
    if age_seconds is None:
        return "unknown"
    if age_seconds > _STALE_HEARTBEAT_SECONDS:
        return "stale"
    return "healthy"


def _latest_heartbeat(config: Any, session_name: str):
    """Fetch the most-recent heartbeat record; ``None`` on any failure.

    Mirrors :func:`pollypm.cli_features.sessions_health._latest_heartbeat`
    — read failures must never crash the endpoint. The caller
    classifies ``None`` as ``unknown`` (no heartbeat row yet) and
    ``status="unknown"`` is a legitimate live state for a freshly-booted
    session.
    """
    try:
        from pollypm.storage.pg_heartbeats import latest_heartbeat
    except Exception:  # noqa: BLE001
        logger.debug("pg_heartbeats import failed", exc_info=True)
        return None
    try:
        return latest_heartbeat(session_name, config=config)
    except Exception:  # noqa: BLE001
        logger.debug(
            "latest_heartbeat lookup failed for %s",
            session_name,
            exc_info=True,
        )
        return None


def _storage_session_name(config: Any) -> str:
    """Return the storage-closet tmux session name.

    Hard-coded suffix matches
    :data:`pollypm.cli_features.sessions_health._STORAGE_CLOSET_SUFFIX`
    so we resolve windows without spinning up a Supervisor / service.
    """
    base = getattr(getattr(config, "project", None), "tmux_session", "") or ""
    return f"{base}-storage-closet"


def _list_storage_closet_windows(tmux_session: str) -> dict[str, Any]:
    """Return ``{window_name: TmuxWindow}`` for the storage-closet session.

    Empty dict on any failure (tmux missing, server down, session not
    found). The caller treats every session as ``window_present=False``
    in that state, which is the right answer when tmux itself is
    unreachable.
    """
    try:
        from pollypm.tmux.client import TmuxClient

        tmux = TmuxClient()
        if not tmux.has_session(tmux_session):
            return {}
        return {w.name: w for w in tmux.list_windows(tmux_session)}
    except Exception:  # noqa: BLE001
        logger.debug("tmux probe failed for %s", tmux_session, exc_info=True)
        return {}


def _pause_marker_path(config: Any) -> Path | None:
    """Return ``<base_dir>/paused-sessions.json`` or ``None`` if no base_dir."""
    base_dir = getattr(getattr(config, "project", None), "base_dir", None)
    if base_dir is None:
        return None
    return Path(base_dir) / _PAUSE_MARKER_FILENAME


def _load_paused_names(config: Any) -> set[str]:
    """Read the pause marker; empty set on missing / malformed file."""
    path = _pause_marker_path(config)
    if path is None or not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        logger.debug("pause marker unreadable: %s", path, exc_info=True)
        return set()
    if not isinstance(data, list):
        return set()
    return {str(name) for name in data if isinstance(name, str)}


def _write_paused_names(config: Any, names: set[str]) -> None:
    """Atomically write the pause marker. Raises on filesystem failure."""
    path = _pause_marker_path(config)
    if path is None:
        raise _daemon_unavailable(
            "<unknown>",
            "no base_dir on config; pause marker has nowhere to live",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = sorted(names)
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Builders — SessionInfo / SessionDetail / SessionConfigView
# ---------------------------------------------------------------------------


def _build_session_info(
    *,
    config: Any,
    session: Any,
    storage_session: str,
    windows: dict[str, Any],
    paused: bool,
) -> SessionInfo:
    """Construct a :class:`SessionInfo` row for one configured session.

    Mirrors :func:`pollypm.cli_features.sessions_health._build_row` so
    the API surface and CLI agree on every field that overlaps.
    """
    window_name = session.window_name or session.name
    window = windows.get(window_name)
    heartbeat = _latest_heartbeat(config, session.name)
    hb_iso = getattr(heartbeat, "created_at", None) if heartbeat else None
    age = _age_seconds(hb_iso)
    window_present = window is not None
    status = _classify_status(
        window_present=window_present,
        age_seconds=age,
        paused=paused,
    )
    auth_token_present = bool(getattr(session, "auth_token", "") or "")
    return SessionInfo(
        name=session.name,
        role=getattr(session, "role", "") or "",
        project=getattr(session, "project", "") or "",
        provider=str(getattr(session, "provider", "") or ""),
        account=getattr(session, "account", "") or "",
        window_name=window_name,
        tmux_session=storage_session,
        window_present=window_present,
        status=status,  # type: ignore[arg-type]
        last_heartbeat_iso=hb_iso,
        last_heartbeat_age_seconds=age,
        auth_token_present=auth_token_present,
        enabled=bool(getattr(session, "enabled", True)),
        paused=paused,
    )


def _build_session_config_view(session: Any) -> SessionConfigView:
    """Mirror :class:`pollypm.models.SessionConfig` on the wire."""
    return SessionConfigView(
        name=session.name,
        role=getattr(session, "role", "") or "",
        provider=str(getattr(session, "provider", "") or ""),
        account=getattr(session, "account", "") or "",
        project=getattr(session, "project", "") or "",
        cwd=str(getattr(session, "cwd", "") or ""),
        window_name=session.window_name or session.name,
        enabled=bool(getattr(session, "enabled", True)),
        auth_token_present=bool(getattr(session, "auth_token", "") or ""),
        agent_profile=getattr(session, "agent_profile", None),
        args=[str(a) for a in (getattr(session, "args", []) or [])],
    )


def _build_tmux_service(config: Any) -> Any | None:
    """Construct a TmuxSessionService for restart / health, or None on failure.

    Returns ``None`` when the work-service / tmux client can't be
    opened. Callers that need the service for a mutation translate
    ``None`` into a 503 ``daemon_unavailable``; read-only callers can
    fall back to the raw tmux probe path.
    """
    try:
        from pollypm.session_services.tmux import TmuxSessionService
        from pollypm.store.registry import get_store
    except Exception:  # noqa: BLE001
        logger.debug("session-service imports failed", exc_info=True)
        return None
    try:
        store = get_store(config)
    except Exception:  # noqa: BLE001
        logger.debug("get_store failed for sessions_admin", exc_info=True)
        # Some installs don't have a state store yet; fall back to a
        # tiny stub so TmuxSessionService.list() doesn't blow up — its
        # only call into the store is ``list_sessions``.
        class _EmptyStore:
            def list_sessions(self) -> list[Any]:
                return []
        store = _EmptyStore()
    try:
        return TmuxSessionService(config=config, store=store)
    except Exception:  # noqa: BLE001
        logger.debug("TmuxSessionService init failed", exc_info=True)
        return None


def _session_health(svc: Any | None, name: str) -> SessionHealthSnapshot:
    """Best-effort health probe via :class:`TmuxSessionService`."""
    if svc is None:
        return SessionHealthSnapshot(
            window_present=False, pane_alive=False, pane_dead=True,
            pane_command=None,
        )
    try:
        h = svc.health(name)
    except Exception:  # noqa: BLE001
        logger.debug("svc.health(%s) failed", name, exc_info=True)
        return SessionHealthSnapshot(
            window_present=False, pane_alive=False, pane_dead=True,
            pane_command=None,
        )
    return SessionHealthSnapshot(
        window_present=bool(getattr(h, "window_present", False)),
        pane_alive=bool(getattr(h, "pane_alive", False)),
        pane_dead=bool(getattr(h, "pane_dead", True)),
        pane_command=getattr(h, "pane_command", None),
    )


def _is_turn_active(svc: Any | None, name: str) -> bool:
    if svc is None:
        return False
    try:
        return bool(svc.is_turn_active(name))
    except Exception:  # noqa: BLE001
        logger.debug("svc.is_turn_active(%s) failed", name, exc_info=True)
        return False


def _find_session(config: Any, name: str) -> Any:
    """Lookup a SessionConfig by ``name``; raise 404 if absent."""
    sessions = getattr(config, "sessions", None) or {}
    # ``sessions`` is keyed by name in the typed config, but we accept
    # either keying for resilience.
    if name in sessions:
        return sessions[name]
    for session in sessions.values():
        if getattr(session, "name", "") == name:
            return session
    raise _session_not_found(name)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/sessions",
    response_model=SessionsListResponse,
    summary="List configured sessions with live tmux + heartbeat state",
    operation_id="listSessions",
)
def list_sessions_endpoint(config: ConfigDep) -> SessionsListResponse:
    """GET /api/v1/sessions — mirror ``pm sessions``.

    Returns one row per configured (and enabled) session. ``paused``
    rows come from the pause marker (see :func:`_load_paused_names`);
    ``status`` follows the same thresholds the CLI uses.
    """
    storage_session = _storage_session_name(config)
    windows = _list_storage_closet_windows(storage_session)
    paused = _load_paused_names(config)
    raw_sessions = getattr(config, "sessions", None) or {}
    sessions = sorted(
        (s for s in raw_sessions.values() if getattr(s, "enabled", True)),
        key=lambda s: s.name,
    )
    rows = [
        _build_session_info(
            config=config,
            session=session,
            storage_session=storage_session,
            windows=windows,
            paused=session.name in paused,
        )
        for session in sessions
    ]
    return SessionsListResponse(sessions=rows)


@router.get(
    "/sessions/{name}",
    response_model=SessionDetail,
    summary="One session's detail incl. config + health + heartbeat",
    operation_id="getSession",
)
def get_session_endpoint(name: str, config: ConfigDep) -> SessionDetail:
    """GET /api/v1/sessions/{name} — detail payload."""
    session = _find_session(config, name)
    storage_session = _storage_session_name(config)
    windows = _list_storage_closet_windows(storage_session)
    paused = name in _load_paused_names(config)
    info = _build_session_info(
        config=config,
        session=session,
        storage_session=storage_session,
        windows=windows,
        paused=paused,
    )
    svc = _build_tmux_service(config)
    health = _session_health(svc, name)
    turn_active = _is_turn_active(svc, name)
    return SessionDetail(
        info=info,
        config=_build_session_config_view(session),
        health=health,
        is_turn_active=turn_active,
    )


@router.post(
    "/sessions/{name}/restart",
    response_model=ActionResult,
    summary="Restart (destroy + relaunch) the tmux window for one session",
    operation_id="restartSession",
)
def restart_session_endpoint(
    name: str,
    config: ConfigDep,
    safety: Annotated[SafetyMode, Query(
        description=(
            "Safety gate. 'strict' (default) refuses restart while the "
            "agent is mid-turn (409 unsafe_mid_turn). 'force' bypasses "
            "the mid-turn gate. 'loose' is treated like 'strict' for "
            "restart (no warning-emit semantics defined yet)."
        ),
    )] = "strict",
) -> ActionResult:
    """POST /api/v1/sessions/{name}/restart — destroy + re-create the window.

    Returns 200 on success. 404 if the session isn't configured. 409
    ``unsafe_mid_turn`` if strict-mode and the agent is actively
    streaming a turn. 503 ``daemon_unavailable`` if the tmux service
    can't be constructed (no tmux, store unavailable, etc).
    """
    session = _find_session(config, name)
    svc = _build_tmux_service(config)
    if svc is None:
        raise _daemon_unavailable(name, "tmux session service unavailable")
    if safety != "force" and _is_turn_active(svc, name):
        raise _unsafe_mid_turn(name)

    # Destroy first; the helper is a no-op when the window isn't
    # present, so it works for an "already stopped" session too — the
    # spec defers between 200 and 409 here, and the cockpit-friendly
    # choice is 200 (idempotent) so a client can always issue "restart"
    # without first probing the live state.
    try:
        svc.destroy(name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("svc.destroy(%s) failed", name, exc_info=True)
        raise _daemon_unavailable(name, f"destroy raised: {exc!s}") from exc

    # Re-create the window. The session config carries cwd / provider /
    # account / window_name; the create() call accepts a partial set —
    # missing prompts / accounts mean the launcher just opens a blank
    # pane, which is the right answer for "restart". A full restart
    # with bootstrap is a separate operation the cockpit "Launch"
    # button performs (Phase 3 surface).
    try:
        cwd = getattr(session, "cwd", None)
        svc.create(
            name=name,
            provider=str(getattr(session, "provider", "") or ""),
            account=getattr(session, "account", "") or "",
            cwd=Path(cwd) if cwd else Path.cwd(),
            window_name=session.window_name or name,
            stabilize=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("svc.create(%s) failed", name, exc_info=True)
        raise _daemon_unavailable(name, f"create raised: {exc!s}") from exc

    return ActionResult(ok=True, message=f"restarted {name}")


@router.post(
    "/sessions/{name}/pause",
    response_model=ActionResult,
    summary="Pause heartbeat dispatch / recovery for one session",
    operation_id="pauseSession",
)
def pause_session_endpoint(name: str, config: ConfigDep) -> ActionResult:
    """POST /api/v1/sessions/{name}/pause — mark the session paused.

    Writes ``<base_dir>/paused-sessions.json`` so other readers (the
    recovery loop, ``GET /api/v1/sessions``) can skip / surface the
    quiesced state. Idempotent: pausing an already-paused session
    returns 200 with no state change.
    """
    _find_session(config, name)  # 404 if unknown
    try:
        names = _load_paused_names(config)
        if name in names:
            return ActionResult(ok=True, message=f"{name} already paused")
        names.add(name)
        _write_paused_names(config, names)
    except APIError:
        raise
    except OSError as exc:
        logger.debug("pause marker write failed for %s", name, exc_info=True)
        raise service_unavailable(
            f"Cannot write pause marker for {name!r}: {exc!s}",
            hint="Check filesystem permissions on ~/.pollypm.",
        ) from exc
    return ActionResult(ok=True, message=f"paused {name}")


@router.post(
    "/sessions/{name}/resume",
    response_model=ActionResult,
    summary="Resume heartbeat dispatch / recovery for one session",
    operation_id="resumeSession",
)
def resume_session_endpoint(name: str, config: ConfigDep) -> ActionResult:
    """POST /api/v1/sessions/{name}/resume — inverse of pause.

    Idempotent: resuming a non-paused session returns 200 with no
    state change.
    """
    _find_session(config, name)  # 404 if unknown
    try:
        names = _load_paused_names(config)
        if name not in names:
            return ActionResult(ok=True, message=f"{name} already running")
        names.discard(name)
        _write_paused_names(config, names)
    except APIError:
        raise
    except OSError as exc:
        logger.debug("pause marker write failed for %s", name, exc_info=True)
        raise service_unavailable(
            f"Cannot write pause marker for {name!r}: {exc!s}",
            hint="Check filesystem permissions on ~/.pollypm.",
        ) from exc
    return ActionResult(ok=True, message=f"resumed {name}")


__all__ = [
    "SessionConfigView",
    "SessionDetail",
    "SessionHealthSnapshot",
    "SessionInfo",
    "SessionsListResponse",
    "get_session_endpoint",
    "list_sessions_endpoint",
    "pause_session_endpoint",
    "restart_session_endpoint",
    "resume_session_endpoint",
    "router",
]
