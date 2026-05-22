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
- ``POST   /api/v1/sessions/{name}/restart``   — destroy + relaunch
  via the canonical :meth:`pollypm.supervisor.Supervisor.restart_session`
  facade so the relaunched pane runs the configured provider command
  (not an ad-hoc ``echo`` placeholder). Refuses with
  ``409 unsafe_mid_turn`` when the agent is mid-turn (or 503
  ``unsafe_mid_turn_unknown`` when the safety probe itself fails)
  unless ``?safety=force``.
- ``POST   /api/v1/sessions/{name}/pause``     — write an
  **informational** pause marker. Consumers can observe it via the
  separate ``paused: bool`` field on ``GET /api/v1/sessions`` rows;
  the ``status`` field is unaffected (Codex PR #2061 round 2 —
  pause must NEVER mask the real runtime-health classification).
  The supervisor / recovery / dispatch loops do NOT yet consume
  this marker (see ``ActionResult.message`` and #2068).
  Idempotent.
- ``POST   /api/v1/sessions/{name}/resume``    — remove the marker.
  Idempotent.

Design notes
------------

* **Read-side health.** Shares the :mod:`pollypm.session_health`
  classification (``healthy`` / ``stale`` / ``missing`` / ``unknown``)
  with the ``pm sessions`` CLI so the API returns the same status
  field an operator sees there (Codex PR #2061 round 5 blocker 3 —
  single source of truth for the contract). Heartbeats come from the
  pg facade :func:`pollypm.storage.pg_heartbeats.latest_heartbeat`;
  tmux probes come from :mod:`pollypm.tmux.client`. Either side may
  be missing — read failures collapse to ``unknown`` / ``missing``
  rather than 500-ing.

* **Restart.** Routes through :meth:`pollypm.supervisor.Supervisor.restart_session`
  — the **same facade** ``pm switch-session-account``, the cockpit
  account-switch action (``cockpit_ui.restart_session``), and the
  upgrade flow already use. That facade owns the canonical kill +
  launch-spec relaunch pair: it tears down the existing window, calls
  :meth:`Supervisor.launch_session` (which consults the launch planner
  for the full provider/account/cwd/command), and injects the recovery
  prompt. The previous incarnation of this route called
  :class:`TmuxSessionService` ``destroy`` + ``create`` directly with no
  command spec, so ``create()`` fell back to its
  ``echo 'No command for {name}'`` placeholder — destroying live
  agents and replacing them with non-agent panes. See PR #2061 Codex
  P0 #1.

  The account passed to ``restart_session`` is the runtime's
  ``effective_account`` if set (so a session that previously failed
  over stays on the recovered account), otherwise the session's
  configured ``account``. Restart is intentionally synchronous (spec
  §10.4) — typical restart time is < 5s and the spec defers
  ``?async`` to Phase 2.5.

* **Pause / resume — informational only.** No existing supervisor /
  recovery / dispatch loop consumes the pause marker today, so this
  surface is documented as **informational**. Crucially, the marker
  is exposed as a *separate* ``paused: bool`` field on the
  ``SessionInfo`` row rather than as a value of ``status`` (Codex
  PR #2061 round 2). Folding it into ``status`` was incoherent with
  the round-1 "informational only" stance: a paused-but-missing
  session reported ``status="paused"`` and operators believed the
  daemon had quiesced when in fact the session was simply absent.
  Now ``status`` reflects pure runtime health (``healthy`` /
  ``stale`` / ``missing`` / ``unknown``) and ``paused`` carries the
  operator intent. The response ``message`` and OpenAPI description
  both call this out so operators are not misled into thinking the
  marker quiesces the daemon. Daemon-side enforcement is tracked as
  follow-up #2068. Both operations are idempotent and return 200,
  and concurrent writes are serialised by an ``fcntl.flock`` on the
  marker file.

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
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from pollypm.session_health import (
    TmuxProbeUnavailable as _TmuxProbeUnavailable,
)
from pollypm.session_health import (
    age_seconds as _age_seconds,
)
from pollypm.session_health import (
    classify_status as _classify_status,
)
from pollypm.session_health import (
    latest_heartbeat as _latest_heartbeat,
)
from pollypm.session_health import (
    list_storage_closet_windows as _list_storage_closet_windows,
)
from pollypm.session_health import (
    probe_strict_turn_active as _probe_strict_turn_active,
)
from pollypm.session_health import (
    storage_session_name as _shared_storage_session_name,
)
from pollypm.web_api.errors import APIError, service_unavailable
from pollypm.web_api.models import ActionResult
from pollypm.web_api.routes._deps import ConfigDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Sessions"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


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
    status: Literal["healthy", "stale", "missing", "unknown"]
    last_heartbeat_iso: str | None = None
    last_heartbeat_age_seconds: int | None = None
    auth_token_present: bool
    enabled: bool
    # Informational marker (Codex PR #2061 round 2). Decoupled from
    # ``status`` so a paused-but-missing or paused-but-stale session
    # still reports its true runtime health — the daemon does NOT
    # consume this marker today (see #2068).
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


def _unsafe_mid_turn_unknown(name: str, detail: str) -> APIError:
    """Strict-mode safety probe could not evaluate ``is_turn_active``.

    Fail-closed counterpart to :func:`_unsafe_mid_turn` (Codex PR #2061
    P0 #2). The previous incarnation of ``_is_turn_active`` swallowed
    every exception and returned ``False`` — a transient tmux / parse
    failure therefore looked like a green-light to restart an actively
    working agent. Strict mode now surfaces probe failures as 503 so
    the caller knows to retry or escalate to ``?safety=force``.
    """
    return APIError(
        status_code=503,
        code="unsafe_mid_turn_unknown",
        message=(
            f"Refusing to restart {name!r}: mid-turn safety probe "
            f"failed ({detail}). Cannot confirm whether the agent is idle."
        ),
        hint=(
            "Retry once the tmux service is healthy, or pass "
            "?safety=force to override the probe (destructive)."
        ),
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

# Classification / heartbeat / tmux-probe primitives now live in
# :mod:`pollypm.session_health` — see Codex PR #2061 round 5 blocker 3.
# This module imports them at the top of the file so the route and
# ``pm sessions`` CLI cannot drift on thresholds, naming, or fail-soft
# semantics. Only the surface-shape helpers (config-aware wrappers,
# pause-marker IO) remain below.


def _storage_session_name(config: Any) -> str:
    """Return the storage-closet tmux session name.

    Delegates the naming to
    :func:`pollypm.session_health.storage_session_name` so the CLI and
    API agree on the ``-storage-closet`` suffix.
    """
    base = getattr(getattr(config, "project", None), "tmux_session", "") or ""
    return _shared_storage_session_name(base)


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


# Operator-facing message appended to pause/resume responses so a CLI
# user or automation script knows the marker is *informational only*
# (Codex PR #2061 P0 #3). The supervisor / recovery / dispatch loops
# do NOT yet consult this marker — making this clear in the response
# stops operators from believing they have quiesced the daemon when
# they have only tagged the session.
_PAUSE_INFORMATIONAL_NOTE = (
    "informational marker only — supervisor / recovery / dispatch "
    "loops do NOT yet consult it; daemon-side enforcement is a "
    "follow-up. The marker is visible via GET /api/v1/sessions."
)


@contextmanager
def _pause_marker_lock(config: Any):
    """Serialise pause/resume read-modify-write across concurrent callers.

    Codex PR #2061 P0 #3 flagged the unguarded ``_load_paused_names``
    → mutate → ``_write_paused_names`` sequence as racy: two
    near-simultaneous pause + resume calls could clobber each other's
    writes. We take an ``fcntl.flock`` on a sibling ``.lock`` file
    that lives in the same directory as the marker — best-effort on
    platforms without ``fcntl`` (Windows), which is fine for the v1
    RC where the API runs on macOS/Linux only.
    """
    path = _pause_marker_path(config)
    if path is None:
        # No base_dir → no lock to take; caller will hit the same 503
        # in ``_write_paused_names`` anyway.
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        import fcntl  # type: ignore[import-not-found]
    except ImportError:
        # No fcntl (e.g. Windows): degrade to no-op lock. The marker
        # write is still atomic via tmp+rename; only the
        # read-modify-write window is unprotected, which matches the
        # pre-#2061 behaviour.
        yield
        return
    fh = open(lock_path, "a+")  # noqa: SIM115 — closed in finally
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


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

    Shares the classification / probe primitives with
    :mod:`pollypm.session_health` (see :func:`_classify_status`,
    :func:`_latest_heartbeat`, :func:`_list_storage_closet_windows`)
    so the API surface and the ``pm sessions`` CLI agree on every
    field that overlaps. Codex PR #2061 round 5 blocker 3 — the
    helpers used to be duplicated here verbatim, which is exactly the
    kind of "looks identical, drifts silently" coupling the round-5
    review called out.
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
    """Return a backend-correct TmuxSessionService for read/probe paths.

    Routes through the supervisor facade so the service is constructed
    with the same :class:`pollypm.storage.state.StateStore` the
    production rail uses — the only object that exposes the
    ``list_sessions()`` shape :class:`TmuxSessionService` (and the
    round-3 strict probe) depend on.

    Codex PR #2061 round 5 blocker 1 caught the prior construction:
    the route called ``TmuxSessionService(store=get_store(config))``,
    which passes the unified pg :class:`pollypm.store.protocol.Store`
    (no ``list_sessions``) into a service that requires
    ``StateStore.list_sessions``. In production this turned every
    detail-health probe into a false-dead read and every strict-mode
    restart safety probe into ``503 unsafe_mid_turn_unknown`` because
    the missing-method ``AttributeError`` looked like a probe outage.

    The supervisor's ``session_service`` property already wires the
    correct store in (``TmuxSessionService(config=self.config,
    store=self.store)``), so going through it is both correct and
    canonical. Returns ``None`` on any construction failure so the
    caller can map to ``503 daemon_unavailable`` (mutation paths) or a
    fail-soft empty health snapshot (read paths).
    """
    supervisor = _build_supervisor(config)
    if supervisor is None:
        return None
    try:
        return supervisor.session_service
    except Exception:  # noqa: BLE001
        logger.debug("supervisor.session_service unavailable", exc_info=True)
        return None


def _session_health(
    svc: Any | None,
    name: str,
    *,
    info_window_present: bool | None = None,
) -> SessionHealthSnapshot:
    """Best-effort health probe via :class:`TmuxSessionService`.

    ``info_window_present`` (when supplied) overrides the
    ``window_present`` field that ``svc.health(name)`` would compute.
    Codex PR #2061 round 6 caught that ``info.window_present`` and
    ``health.window_present`` could disagree for the same configured
    session: ``info`` is built from the shared
    :func:`pollypm.session_health.list_storage_closet_windows` helper
    (config + tmux direct), while ``svc.health()`` routes through
    :class:`TmuxSessionService.health` → ``self._store.list_sessions()``
    on the legacy :class:`StateStore`. On production the pg-facade owns
    session registration, so the legacy store can be empty and
    ``health.window_present`` would report ``False`` for a window that
    ``info.window_present`` reported ``True``. Threading the canonical
    ``windows`` dict in keeps both fields single-sourced.
    """
    if svc is None:
        return SessionHealthSnapshot(
            window_present=bool(info_window_present) if info_window_present is not None else False,
            pane_alive=False,
            pane_dead=True,
            pane_command=None,
        )
    try:
        h = svc.health(name)
    except Exception:  # noqa: BLE001
        logger.debug("svc.health(%s) failed", name, exc_info=True)
        return SessionHealthSnapshot(
            window_present=bool(info_window_present) if info_window_present is not None else False,
            pane_alive=False,
            pane_dead=True,
            pane_command=None,
        )
    window_present = (
        bool(info_window_present)
        if info_window_present is not None
        else bool(getattr(h, "window_present", False))
    )
    return SessionHealthSnapshot(
        window_present=window_present,
        pane_alive=bool(getattr(h, "pane_alive", False)),
        pane_dead=bool(getattr(h, "pane_dead", True)),
        pane_command=getattr(h, "pane_command", None),
    )


class _TurnProbeUnavailable(RuntimeError):
    """Raised by :func:`_is_turn_active` in strict mode on probe failure.

    The route maps this to a 503 ``unsafe_mid_turn_unknown`` so strict
    mode fails *closed* — a transient tmux / parse / service failure
    must not look like a green-light to restart an actively-working
    agent (Codex PR #2061 P0 #2).
    """


def _is_turn_active(svc: Any | None, name: str, *, strict: bool = False) -> bool:
    """Return True iff the agent appears mid-turn.

    ``strict=False`` (read-only / detail surface): exceptions degrade
    to ``False`` — the read path must not 500 on a transient probe
    failure.

    ``strict=True`` (destructive restart): exceptions raise
    :class:`_TurnProbeUnavailable` so the route maps to 503. Strict
    mode must fail *closed*; otherwise a flaky probe can clobber a
    working agent.

    Strict mode prefers ``svc.is_turn_active_strict(name)`` (Codex PR
    #2061 round 3): the default ``svc.is_turn_active`` runs through
    fail-soft helpers (``list`` / ``health`` swallow tmux+store
    failures and return "agent idle"), so even with the catch below the
    real :class:`TmuxSessionService` would never raise on an outage and
    the route would happily destroy a working agent. The strict variant
    raises :class:`pollypm.session_services.tmux.TmuxProbeUnavailable`
    on any probe failure; we wrap it as :class:`_TurnProbeUnavailable`
    so the route's existing 503-mapper handles both. Services that
    don't implement the strict variant (custom plugins) fall back to
    the plain method — the wrapping ``except`` still catches anything
    they raise, but those implementations carry the responsibility of
    actually raising on probe failure.
    """
    if svc is None:
        if strict:
            raise _TurnProbeUnavailable("tmux service unavailable")
        return False
    if strict:
        probe = getattr(svc, "is_turn_active_strict", None)
        if callable(probe):
            try:
                return bool(probe(name))
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "svc.is_turn_active_strict(%s) failed", name, exc_info=True,
                )
                raise _TurnProbeUnavailable(
                    str(exc) or exc.__class__.__name__,
                ) from exc
    try:
        return bool(svc.is_turn_active(name))
    except Exception as exc:  # noqa: BLE001
        logger.debug("svc.is_turn_active(%s) failed", name, exc_info=True)
        if strict:
            raise _TurnProbeUnavailable(str(exc) or exc.__class__.__name__) from exc
        return False


def _build_supervisor(config: Any) -> Any | None:
    """Construct a :class:`pollypm.supervisor.Supervisor` via service_api.

    Used by :func:`restart_session_endpoint` (Codex PR #2061 P0 #1) so
    the restart goes through the canonical
    :meth:`Supervisor.restart_session` facade — the same code path the
    cockpit account-switch button, ``pm switch-session-account``, and
    the upgrade flow already use.

    Constructs via :func:`pollypm.service_api.build_supervisor` so the
    direct ``from pollypm.supervisor import Supervisor`` import lives
    in the sanctioned facade module
    (``src/pollypm/service_api/v1.py`` — already on the
    ``_SUPERVISOR_IMPORT_ALLOWLIST`` in
    :mod:`tests.test_import_boundary`). Codex PR #2061 round 5 blocker
    2 caught the prior direct import here as a fresh boundary
    violation.

    Returns ``None`` on any construction failure (no store available,
    plugin host failure, etc.); the caller translates that into a
    503 ``daemon_unavailable`` so clients can retry.
    """
    try:
        from pollypm.service_api import build_supervisor
    except Exception:  # noqa: BLE001
        logger.debug("service_api.build_supervisor import failed", exc_info=True)
        return None
    try:
        return build_supervisor(config)
    except Exception:  # noqa: BLE001
        logger.debug("Supervisor construction failed", exc_info=True)
        return None


def _close_supervisor_quietly(supervisor: Any | None) -> None:
    """Close a transient per-request :class:`Supervisor` without raising.

    Codex PR #2061 round 6 lifecycle note: ``Supervisor.__init__``
    opens the legacy sqlite ``state.db`` (and runs migrations) as a
    side-effect, so each per-request construction holds a writable
    connection until close. The route uses the supervisor for one or
    two public method calls (``get_session_runtime`` /
    ``restart_session``) and then discards it; without an explicit
    close we'd leak fds across many restart calls. :meth:`Supervisor.stop`
    is the documented teardown — best-effort here because a failure to
    close should never mask a successful restart.
    """
    if supervisor is None:
        return
    stop = getattr(supervisor, "stop", None)
    if not callable(stop):
        return
    try:
        stop()
    except Exception:  # noqa: BLE001
        logger.debug("supervisor.stop() raised on quiet close", exc_info=True)


def _resolve_restart_account(supervisor: Any, session: Any) -> str | None:
    """Pick the account to relaunch under for an API-driven restart.

    Mirrors the precedence the recovery path uses: prefer the
    runtime's ``effective_account`` if set (so a session that
    previously failed over stays on the recovered account); otherwise
    fall back to the session's configured ``account``. Returns
    ``None`` if neither is available (caller maps to 503).

    Uses the public :meth:`Supervisor.get_session_runtime` wrapper —
    the private ``_get_session_runtime`` reach-through was a fresh
    boundary violation flagged in Codex PR #2061 round 5 blocker 2,
    and the public method has existed on Supervisor (the #1830
    cluster-A facade) since well before this route was introduced.
    """
    try:
        runtime = supervisor.get_session_runtime(session.name)
    except Exception:  # noqa: BLE001
        runtime = None
    if runtime is not None:
        eff = getattr(runtime, "effective_account", None)
        if eff:
            return str(eff)
    configured = getattr(session, "account", None)
    return str(configured) if configured else None


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

    Returns one row per configured (and enabled) session. ``status``
    is the pure runtime-health classification (``healthy`` / ``stale``
    / ``missing`` / ``unknown``) and follows the same thresholds the
    CLI uses. ``paused`` is a separate informational boolean drawn
    from the pause marker (see :func:`_load_paused_names`) and does
    NOT influence ``status`` — see :func:`_classify_status` for why.
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
    # Single-source ``window_present`` with the value already computed
    # for ``info`` from the shared ``list_storage_closet_windows``
    # helper. Avoids the round-6 split-source bug where
    # ``info.window_present`` (config/tmux direct) and
    # ``health.window_present`` (StateStore-dependent) could disagree
    # for the same configured session on pg-backed installs.
    health = _session_health(
        svc, name, info_window_present=info.window_present,
    )
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
    """POST /api/v1/sessions/{name}/restart — facade-driven relaunch.

    Routes through :meth:`pollypm.supervisor.Supervisor.restart_session`
    so the relaunched pane runs the configured provider command (the
    launch planner reconstructs cwd / provider / account / args /
    initial input / markers) instead of an ``echo`` placeholder.

    Responses:

    * **200** on success.
    * **404** ``not_found`` if the session isn't configured.
    * **409** ``unsafe_mid_turn`` if strict-mode and the agent is
      actively streaming a turn.
    * **503** ``unsafe_mid_turn_unknown`` if strict-mode and the
      mid-turn safety probe itself failed (fail-closed — Codex PR
      #2061 P0 #2).
    * **503** ``daemon_unavailable`` if the supervisor / tmux service
      can't be constructed, or if ``restart_session`` raises.
    """
    session = _find_session(config, name)

    # Safety gate (Codex PR #2061 P0 #2 + round 6). Strict mode (the
    # default) fails *closed* — a probe that cannot answer "is this
    # agent currently working?" must not be treated as "agent is idle".
    #
    # The probe now reads from the SAME source as the rest of the API
    # contract (config + tmux direct) instead of going through
    # :class:`TmuxSessionService.is_turn_active_strict`, which depends
    # on the legacy :class:`StateStore.list_sessions()` row set. In
    # production the pg facade owns session registration (via
    # :func:`pollypm.storage.pg_sessions.upsert_session` on the launch
    # path) so the legacy store is empty/stale and the in-service
    # strict probe returned ``False`` (fail-open) for actively-working
    # agents. ``probe_strict_turn_active`` re-resolves the configured
    # window from ``config.sessions[name]`` →
    # ``list_storage_closet_windows`` → direct
    # :class:`pollypm.tmux.client.TmuxClient` calls so the answer cannot
    # diverge from what ``GET /api/v1/sessions/{name}`` reports.
    #
    # Supervisor construction is deferred until after the probe passes:
    # ``Supervisor()`` opens the legacy sqlite ``state.db`` and runs
    # migrations as a side-effect, so building it up-front on every
    # probe / probe-failure would add sqlite open/migrate latency to a
    # read-flavoured safety check. We pay that cost only when we're
    # actually going to invoke the destructive restart.
    if safety != "force":
        try:
            from pollypm.tmux.client import TmuxClient

            # Reuse the same window dict the route's read-side helpers
            # use so the probe and ``GET /api/v1/sessions/{name}``
            # cannot diverge for the same session (Codex PR #2061
            # round 6 single-source contract).
            storage_session = _storage_session_name(config)
            windows = _list_storage_closet_windows(storage_session)
            mid_turn = _probe_strict_turn_active(
                config, name, TmuxClient(), windows=windows,
            )
        except _TmuxProbeUnavailable as exc:
            raise _unsafe_mid_turn_unknown(name, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            # Defensive: any unexpected error from the probe is treated
            # as "cannot evaluate" (fail-closed). The typed exception
            # above covers the documented tmux-call failures.
            logger.debug(
                "probe_strict_turn_active(%s) unexpected failure",
                name, exc_info=True,
            )
            raise _unsafe_mid_turn_unknown(name, str(exc)) from exc
        if mid_turn:
            raise _unsafe_mid_turn(name)

    # Route through the canonical supervisor restart facade (Codex PR
    # #2061 P0 #1). ``Supervisor.restart_session`` owns the
    # kill+relaunch pair: it kills the window, switches runtime status
    # to ``recovering``, then calls ``launch_session`` which consults
    # the launch planner for the full provider command (NOT an ad-hoc
    # ``echo`` placeholder). The cockpit account-switch button,
    # ``pm switch-session-account``, and the upgrade flow already use
    # the same facade — a future launch-automation change therefore
    # lands in every surface at once.
    supervisor = _build_supervisor(config)
    if supervisor is None:
        raise _daemon_unavailable(name, "supervisor unavailable")
    try:
        account_name = _resolve_restart_account(supervisor, session)
        if not account_name:
            raise _daemon_unavailable(
                name, "no effective or configured account on session",
            )

        try:
            supervisor.restart_session(
                name, account_name, failure_type="api_restart",
            )
        except KeyError as exc:
            # ``Supervisor.restart_session`` raises ``KeyError`` for an
            # unknown account; surface as 503 because the API contract is
            # "the daemon couldn't honor the request" rather than a client
            # validation error (the account was resolved from runtime/config).
            logger.debug(
                "restart_session unknown account for %s", name, exc_info=True,
            )
            raise _daemon_unavailable(
                name, f"unknown restart account: {exc!s}",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "supervisor.restart_session(%s) failed", name, exc_info=True,
            )
            raise _daemon_unavailable(
                name, f"restart_session raised: {exc!s}",
            ) from exc
    finally:
        # Per-request transient supervisor — close so we don't leak the
        # sqlite ``state.db`` connection on every restart call (Codex
        # PR #2061 round 6 lifecycle note). Best-effort: ``stop`` is
        # idempotent against missing internal state.
        _close_supervisor_quietly(supervisor)

    return ActionResult(ok=True, message=f"restarted {name}")


@router.post(
    "/sessions/{name}/pause",
    response_model=ActionResult,
    summary=(
        "Tag a session as paused (INFORMATIONAL marker; supervisor "
        "loops do not yet consume it)"
    ),
    operation_id="pauseSession",
)
def pause_session_endpoint(name: str, config: ConfigDep) -> ActionResult:
    """POST /api/v1/sessions/{name}/pause — write an informational marker.

    **This is an informational marker only.** Codex PR #2061 P0 #3
    flagged that the supervisor / recovery / dispatch loops do NOT
    consume ``<base_dir>/paused-sessions.json`` today — they will
    still act on the session even after a successful pause. The
    response ``message`` calls this out so operators know they have
    tagged the session, not quiesced the daemon. Daemon-side
    enforcement is tracked as a follow-up.

    Writes ``<base_dir>/paused-sessions.json`` (atomic tmp+rename,
    serialised via ``fcntl.flock`` against concurrent pause/resume
    callers) so ``GET /api/v1/sessions`` surfaces ``paused=true`` on
    the affected row. The ``status`` field continues to reflect
    actual runtime health (``healthy`` / ``stale`` / ``missing`` /
    ``unknown``) — a paused-but-missing session reports
    ``status="missing"`` AND ``paused=true``, not ``status="paused"``
    (Codex PR #2061 round 2). Idempotent: pausing an already-paused
    session returns 200 with no state change.
    """
    _find_session(config, name)  # 404 if unknown
    try:
        with _pause_marker_lock(config):
            names = _load_paused_names(config)
            if name in names:
                return ActionResult(
                    ok=True,
                    message=(
                        f"{name} already tagged paused — "
                        f"{_PAUSE_INFORMATIONAL_NOTE}"
                    ),
                )
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
    return ActionResult(
        ok=True,
        message=f"tagged {name} paused — {_PAUSE_INFORMATIONAL_NOTE}",
    )


@router.post(
    "/sessions/{name}/resume",
    response_model=ActionResult,
    summary=(
        "Clear the informational pause marker for a session (see "
        "pauseSession for the daemon-control caveat)"
    ),
    operation_id="resumeSession",
)
def resume_session_endpoint(name: str, config: ConfigDep) -> ActionResult:
    """POST /api/v1/sessions/{name}/resume — clear the informational marker.

    Idempotent inverse of :func:`pause_session_endpoint`. Same
    daemon-control caveat applies: the marker is informational, so
    "resuming" a session that the supervisor never stopped acting on
    is a no-op from the runtime's perspective.
    """
    _find_session(config, name)  # 404 if unknown
    try:
        with _pause_marker_lock(config):
            names = _load_paused_names(config)
            if name not in names:
                return ActionResult(
                    ok=True,
                    message=(
                        f"{name} already untagged — "
                        f"{_PAUSE_INFORMATIONAL_NOTE}"
                    ),
                )
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
    return ActionResult(
        ok=True,
        message=f"cleared pause tag on {name} — {_PAUSE_INFORMATIONAL_NOTE}",
    )


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
