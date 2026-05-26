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
- ``POST   /api/v1/sessions/{name}/pause``     — write the pause
  marker. The marker is exposed via the separate ``paused: bool``
  field on ``GET /api/v1/sessions`` rows; the ``status`` field is
  unaffected (Codex PR #2061 round 2 — pause must NEVER mask the
  real runtime-health classification). **Partial enforcement**:
  recovery/relaunch, heartbeat per-session, heartbeat send, and
  task-assignment dispatch chokepoints honor this marker and yield
  with an audit event. Some direct/manual cockpit and chat sends
  remain explicit operator actions. Idempotent.
- ``POST   /api/v1/sessions/{name}/resume``    — remove the marker.
  Idempotent.

Design notes
------------

* **Read-side health.** Shares the :mod:`pollypm.session_health`
  classification (mechanical liveness plus runtime failure pins)
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

* **Pause / resume — partial enforcement.** The pause marker is
  HONORED by the recovery/relaunch, heartbeat per-session, heartbeat
  send, and task-assignment dispatch chokepoints wired under #2068.
  Those loops emit a ``session.pause.skip`` audit event and yield
  without acting. Direct/manual cockpit and chat sends remain explicit
  operator actions unless they route through those loop chokepoints.
  The marker is exposed as a *separate* ``paused:
  bool`` field on the ``SessionInfo`` row rather than as a value of
  ``status`` (Codex PR #2061 round 2). Folding it into ``status``
  was incoherent: a paused-but-missing session would report
  ``status="paused"`` and operators would believe the daemon had
  quiesced when in fact the session was simply absent. ``status``
  reflects pure runtime health (``healthy`` / ``stale`` /
  ``missing`` / ``unknown`` plus functional failures such as
  ``auth_broken`` and ``capacity_exhausted``) and ``paused`` carries
  the operator intent. The response ``message`` and OpenAPI description both
  spell out which loops do and do not yet consume the marker so an
  operator is never misled. Both operations are idempotent and
  return 200, and concurrent writes are serialised by an
  ``fcntl.flock`` on the marker file (see
  :func:`pollypm.session_paused.pause_marker_lock`).

* **pg outages → 503.** ``GET`` endpoints downgrade pg outages to a
  best-effort response (``status="unknown"`` on the affected row).
  ``POST`` endpoints surface pg outages as ``503 service_unavailable``
  so the client knows to retry.

Auth follows the standard bearer-dependency wiring in
:mod:`pollypm.web_api.app`; this router is mounted under ``/api/v1`` so
``auth_deps`` applies to every operation here.
"""

from __future__ import annotations

import logging
import subprocess
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
    latest_session_runtime as _latest_session_runtime,
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
from pollypm.audit.log import (
    EVENT_SESSION_PAUSE_PAUSED,
    EVENT_SESSION_PAUSE_REFUSED,
    EVENT_SESSION_PAUSE_RESUMED,
)
from pollypm.session_paused import (
    emit_pause_operator_action as _emit_pause_operator_action,
)
from pollypm.session_paused import (
    load_paused_state as _load_paused_state,
)
from pollypm.session_paused import (
    pause_marker_lock as _pause_marker_lock,
)
from pollypm.session_paused import (
    pause_status_from_state as _pause_status_from_state,
)
from pollypm.session_paused import (
    save_paused_names as _save_paused_names,
)
from pollypm.session_leases import SessionLeaseConflictError
from pollypm.web_api.errors import APIError, service_unavailable
from pollypm.web_api.models import ActionResult
from pollypm.web_api.routes._deps import ConfigDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Sessions"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Pause-marker filename + path + lock helpers all live in
# :mod:`pollypm.session_paused` so the read-side (recovery loops) and
# write-side (this route) cannot drift on filename or location (Codex
# PR #2081 round 1 blocker 2). Import aliases above.


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
    status: Literal[
        "healthy",
        "stale",
        "missing",
        "unknown",
        "auth_broken",
        "capacity_exhausted",
        "provider_outage",
        "blocked",
        "degraded",
    ]
    last_heartbeat_iso: str | None = None
    last_heartbeat_age_seconds: int | None = None
    auth_token_present: bool
    enabled: bool
    # Operator-intent marker (Codex PR #2061 round 2). Remains a
    # separate boolean from ``status`` so a paused-but-missing or
    # paused-but-stale session still reports its true runtime health.
    paused: bool = False
    # Only set when the marker itself is unreadable. Intentional pause
    # does not carry a reason; clean running rows are null/absent
    # depending on the client serializer.
    paused_reason: Literal["marker_unreadable"] | None = None


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


class RestartSessionRequest(BaseModel):
    """Optional body for ``POST /sessions/{name}/restart``."""

    force: bool = Field(
        default=False,
        description=(
            "Bypass a human-held session lease. This does not bypass the "
            "mid-turn safety probe; use safety=force for that."
        ),
    )


class SessionPauseMutationRequest(BaseModel):
    """Optional operator context for pause/resume audit rows."""

    actor: str = Field(
        default="operator",
        description="Operator identity to stamp into the audit event.",
    )
    reason: str | None = Field(
        default=None,
        description="Operator-supplied reason for the pause/resume action.",
    )


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


def _session_leased(name: str, detail: str) -> APIError:
    return APIError(
        status_code=409,
        code="session_leased",
        message=f"Cannot restart {name!r}: {detail}.",
        hint=(
            "Wait for the lease holder to release the session, or pass "
            "?force=true / {\"force\": true} to override the lease."
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


def _window_missing(name: str, window_name: str, storage_session: str) -> APIError:
    return APIError(
        status_code=503,
        code="window_missing",
        message=(
            f"Cannot interrupt {name!r}: tmux window {window_name!r} is "
            f"not present in storage session {storage_session!r}."
        ),
        hint="Refresh the session list; the agent may have exited or moved.",
    )


def _pane_unavailable(name: str, detail: str) -> APIError:
    return APIError(
        status_code=409,
        code="pane_unavailable",
        message=f"Cannot interrupt {name!r}: {detail}.",
        hint="Refresh the session list; the pane may have exited.",
    )


def _interrupt_failed(name: str, detail: str) -> APIError:
    return APIError(
        status_code=503,
        code="interrupt_failed",
        message=f"Failed to send interrupt to {name!r}: {detail}",
        hint="Check tmux health and retry.",
    )


def _marker_unreadable(name: str, path: str, reason: str) -> APIError:
    """503 envelope for a pause/resume call against an unreadable marker.

    Codex PR #2081 round 3 — finding 2: the write-side pause/resume
    endpoints used to call ``load_paused_names`` (which collapses an
    unreadable marker to ``set()``) inside their read-modify-write
    path. With a corrupt or permission-broken marker on disk, ``pause``
    would overwrite the file with just the newly requested name —
    silently losing every other paused session — and ``resume`` would
    return a cheerful 200 ``already untagged`` while the recovery loops
    stayed quiesced (they fail closed on the unreadable state).

    The fix is to refuse the mutation with a typed 503 that names the
    marker path and the parse failure. The operator can then either
    delete or repair the marker manually; we deliberately do NOT
    auto-repair because the operator's intent (which sessions were
    paused) is exactly the data we'd be making up.
    """
    return APIError(
        status_code=503,
        code="marker_unreadable",
        message=(
            f"Cannot act on session {name!r}: pause marker at {path} is "
            f"unreadable ({reason}). Recovery loops are quiesced (fail-"
            f"closed) until an operator repairs or deletes the marker."
        ),
        hint=(
            "Inspect / repair / delete the marker file manually, then "
            "retry. Auto-overwriting would silently drop other paused "
            "sessions, so the API refuses."
        ),
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


# Marker filename, path helper, atomic-write helper, and the
# pause/resume read-modify-write lock all live in
# :mod:`pollypm.session_paused` (Codex PR #2081 round 1 blocker 2 — the
# read-side recovery loops and the write-side route MUST share these,
# or a rename / shape change would silently desync them). Imported as
# ``_load_paused_state`` / ``_pause_status_from_state`` /
# ``_save_paused_names`` / ``_pause_marker_lock`` at the top of this
# module.


# Operator-facing message appended to pause/resume responses. The
# marker is PARTIALLY enforced through the daemon-loop chokepoints
# wired under #2068. Spelling out the remaining manual-action caveat
# keeps an operator from over- OR under-trusting the call: pausing is
# not a full daemon quiesce for every direct send path, but it is no
# longer a pure tag either.
_PAUSE_PARTIAL_ENFORCEMENT_NOTE = (
    "pause marker is HONORED by recovery/relaunch, heartbeat "
    "per-session processing, and task-assignment dispatch paths wired "
    "under #2068. Some direct/manual cockpit and chat send surfaces are "
    "not yet treated as daemon-loop dispatch. The marker is visible via "
    "GET /api/v1/sessions on the separate `paused` field."
)


def _pause_mutation_actor_reason(
    body: SessionPauseMutationRequest | None,
) -> tuple[str, str | None]:
    if body is None:
        return "operator", None
    return body.actor, body.reason


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
    paused_reason: str | None = None,
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
    runtime = _latest_session_runtime(config, session.name)
    status = _classify_status(
        window_present=window_present,
        age_seconds=age,
        runtime_status=getattr(runtime, "status", None),
        last_failure_type=getattr(runtime, "last_failure_type", None),
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
        paused_reason=paused_reason,  # type: ignore[arg-type]
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


def _task_surface_known(config: Any, name: str) -> bool:
    """Return True for syntactically valid task worker window names."""
    try:
        from pollypm.work.task_state import parse_task_window_name
    except Exception:  # noqa: BLE001
        return False
    parsed = parse_task_window_name(name)
    if parsed is None:
        return False
    project, _number = parsed
    projects = getattr(config, "projects", None) or {}
    return project in projects


def _interrupt_window_name(config: Any, name: str) -> str:
    """Resolve ``name`` to a storage-closet window name.

    Configured sessions use their configured ``window_name``. Active
    task-worker chat surfaces are not part of ``config.sessions``; for
    those we accept the canonical ``task-<project>-<n>`` window name
    only when the project is registered.
    """
    try:
        session = _find_session(config, name)
    except APIError:
        if _task_surface_known(config, name):
            return name
        raise
    return session.window_name or session.name


def _find_interrupt_pane(
    tmux: Any,
    *,
    name: str,
    storage_session: str,
    window_name: str,
) -> str:
    """Return the pane id backing ``window_name`` or raise a typed API error."""
    try:
        windows = tmux.list_windows(storage_session)
    except FileNotFoundError as exc:
        raise _interrupt_failed(name, f"tmux binary not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise _interrupt_failed(
            name, f"tmux command timed out after {exc.timeout}s",
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise _interrupt_failed(
            name,
            f"tmux list-windows failed with exit {exc.returncode}: {exc.stderr}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise _interrupt_failed(name, str(exc)) from exc
    for window in windows or []:
        if getattr(window, "name", None) != window_name:
            continue
        if getattr(window, "pane_dead", False):
            raise _pane_unavailable(name, "pane is dead")
        pane_id = getattr(window, "pane_id", None)
        if not pane_id:
            raise _pane_unavailable(name, "pane id missing")
        return str(pane_id)
    raise _window_missing(name, window_name, storage_session)


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
    CLI uses. ``paused`` is a separate operator-intent / fail-closed
    boolean drawn from the pause marker and does NOT influence
    ``status`` — see :func:`_classify_status` for why. If the marker
    exists but cannot be read, the endpoint still returns 200 and
    marks every row ``paused=True`` with
    ``paused_reason="marker_unreadable"``.
    """
    storage_session = _storage_session_name(config)
    windows = _list_storage_closet_windows(storage_session)
    pause_state = _load_paused_state(config)
    raw_sessions = getattr(config, "sessions", None) or {}
    sessions = sorted(
        (s for s in raw_sessions.values() if getattr(s, "enabled", True)),
        key=lambda s: s.name,
    )
    rows = []
    for session in sessions:
        paused, paused_reason = _pause_status_from_state(
            pause_state, session.name,
        )
        rows.append(
            _build_session_info(
                config=config,
                session=session,
                storage_session=storage_session,
                windows=windows,
                paused=paused,
                paused_reason=paused_reason,
            )
        )
    return SessionsListResponse(sessions=rows)


def _health_from_window(window: Any | None) -> SessionHealthSnapshot:
    """Compute a :class:`SessionHealthSnapshot` from a ``TmuxWindow``.

    Codex PR #2061 round 8 lifecycle fix: the GET detail path used to
    build a transient :class:`pollypm.supervisor.Supervisor` (via
    :func:`_build_tmux_service` → :attr:`Supervisor.session_service`)
    just to call :meth:`TmuxSessionService.health`. ``Supervisor.__init__``
    opens the legacy sqlite ``state.db`` and runs migrations as a
    side-effect, so every GET poll leaked a writable connection until
    GC eventually closed it.

    The ``TmuxWindow`` returned by :func:`list_storage_closet_windows`
    already carries everything the :class:`SessionHealthSnapshot`
    fields need: ``pane_id``, ``pane_dead``, ``pane_current_command``.
    No Supervisor, no StateStore, no transient connections — just the
    same fail-soft tmux read the GET path already performs for window
    discovery.
    """
    if window is None:
        return SessionHealthSnapshot(
            window_present=False,
            pane_alive=False,
            pane_dead=True,
            pane_command=None,
        )
    pane_dead = bool(getattr(window, "pane_dead", False))
    return SessionHealthSnapshot(
        window_present=True,
        pane_alive=not pane_dead,
        pane_dead=pane_dead,
        pane_command=getattr(window, "pane_current_command", None) or None,
    )


def _is_turn_active_failsoft(window: Any | None) -> bool:
    """Fail-soft mid-turn check for the GET detail surface.

    Replaces the read-side call to :meth:`TmuxSessionService.is_turn_active`
    so the GET path no longer needs a transient :class:`Supervisor` (the
    only source of a backend-correct :class:`TmuxSessionService`).
    Returns ``False`` on any failure — the read path must not 500 on a
    transient tmux outage, and "agent might be working" is not a useful
    signal to surface from a read endpoint anyway. The destructive
    restart path still routes through
    :func:`pollypm.session_health.probe_strict_turn_active` (config /
    tmux-direct, fail-closed).
    """
    if window is None:
        return False
    pane_id = getattr(window, "pane_id", None)
    if pane_id is None or getattr(window, "pane_dead", False):
        return False
    try:
        from pollypm.tmux.client import TmuxClient

        tmux = TmuxClient()
        text = tmux.capture_pane(pane_id, lines=200)
    except Exception:  # noqa: BLE001
        logger.debug(
            "GET-detail is_turn_active capture_pane failed", exc_info=True,
        )
        return False
    lowered = text.lower()
    if "⏺" in text or "working" in lowered:
        return True
    if "working (" in lowered and "esc to interrupt" in lowered:
        return True
    return False


@router.get(
    "/sessions/{name}",
    response_model=SessionDetail,
    summary="One session's detail incl. config + health + heartbeat",
    operation_id="getSession",
)
def get_session_endpoint(name: str, config: ConfigDep) -> SessionDetail:
    """GET /api/v1/sessions/{name} — detail payload.

    Codex PR #2061 round 8: this path no longer constructs a
    :class:`pollypm.supervisor.Supervisor`. The previous wiring went
    through :func:`_build_tmux_service` → :attr:`Supervisor.session_service`
    purely to call :meth:`TmuxSessionService.health` + ``is_turn_active``,
    but ``Supervisor.__init__`` opens the legacy sqlite ``state.db`` as
    a side-effect and the GET path never called ``Supervisor.stop()`` →
    every poll leaked an fd / sqlite connection.

    The detail payload is now built from the same sources the list
    endpoint uses: ``config.sessions`` (configured-session registry),
    :func:`list_storage_closet_windows` (fail-soft tmux read),
    :func:`latest_heartbeat` (pg facade), plus the pause marker. The
    health snapshot is derived from the ``TmuxWindow`` already in hand
    (``pane_id`` / ``pane_dead`` / ``pane_current_command``) and
    ``is_turn_active`` does its own small fail-soft ``capture_pane``
    call. No Supervisor, no StateStore, no transient connections.
    """
    session = _find_session(config, name)
    storage_session = _storage_session_name(config)
    windows = _list_storage_closet_windows(storage_session)
    pause_state = _load_paused_state(config)
    paused, paused_reason = _pause_status_from_state(pause_state, name)
    info = _build_session_info(
        config=config,
        session=session,
        storage_session=storage_session,
        windows=windows,
        paused=paused,
        paused_reason=paused_reason,
    )
    window_name = session.window_name or session.name
    window = windows.get(window_name)
    health = _health_from_window(window)
    turn_active = _is_turn_active_failsoft(window)
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
    body: RestartSessionRequest | None = None,
    safety: Annotated[SafetyMode, Query(
        description=(
            "Safety gate. 'strict' (default) refuses restart while the "
            "agent is mid-turn (409 unsafe_mid_turn). 'force' bypasses "
            "the mid-turn gate. 'loose' is treated like 'strict' for "
            "restart (no warning-emit semantics defined yet)."
        ),
    )] = "strict",
    force: Annotated[bool, Query(
        description=(
            "Bypass a human-held session lease. This does not bypass the "
            "mid-turn safety probe; use safety=force for that."
        ),
    )] = False,
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
    * **409** ``session_leased`` if the session is leased to another
      owner and ``force`` was not requested.
    * **503** ``unsafe_mid_turn_unknown`` if strict-mode and the
      mid-turn safety probe itself failed (fail-closed — Codex PR
      #2061 P0 #2).
    * **503** ``daemon_unavailable`` if the supervisor / tmux service
      can't be constructed, or if ``restart_session`` raises.
    """
    session = _find_session(config, name)
    force_restart = force or (body.force if body is not None else False)

    # Safety gate (Codex PR #2061 P0 #2 + rounds 6+7). Strict mode (the
    # default) fails *closed* — a probe that cannot answer "is this
    # agent currently working?" must not be treated as "agent is idle".
    #
    # The probe reads from the SAME source as the rest of the API
    # contract (config + tmux direct) instead of going through
    # :class:`TmuxSessionService.is_turn_active_strict`, which depends
    # on the legacy :class:`StateStore.list_sessions()` row set. In
    # production the pg facade owns session registration (via
    # :func:`pollypm.storage.pg_sessions.upsert_session` on the launch
    # path) so the legacy store is empty/stale and the in-service
    # strict probe returned ``False`` (fail-open) for actively-working
    # agents.
    #
    # Round 7: the probe OWNS window discovery directly via
    # ``TmuxClient.has_session`` / ``TmuxClient.list_windows`` so a
    # tmux outage raises :class:`TmuxProbeUnavailable` → 503
    # ``unsafe_mid_turn_unknown``. The previous round-6 wiring threaded
    # the fail-soft :func:`_list_storage_closet_windows` helper into the
    # probe, which returned ``{}`` for any tmux failure → probe saw
    # "window absent" → returned ``False`` (looks idle) → destructive
    # restart proceeded against an unobservable agent. The fail-soft
    # helper is still correct for the read-side GET paths above.
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

            mid_turn = _probe_strict_turn_active(
                config, name, TmuxClient(),
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
                name,
                account_name,
                failure_type="api_restart",
                force=force_restart,
            )
        except SessionLeaseConflictError as exc:
            logger.debug(
                "restart_session lease conflict for %s", name, exc_info=True,
            )
            raise _session_leased(name, str(exc)) from exc
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
    "/sessions/{name}/interrupt",
    response_model=ActionResult,
    summary="Send Escape to a live session/worker tmux pane",
    operation_id="interruptSession",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Configured session or task worker not found."},
        "409": {"description": "Pane is dead or missing a pane id."},
        "503": {"description": "tmux unavailable or target window missing."},
    },
)
def interrupt_session_endpoint(name: str, config: ConfigDep) -> ActionResult:
    """POST /api/v1/sessions/{name}/interrupt — send Escape to the pane.

    This is intentionally non-destructive: it sends the key the
    embedded agents advertise as their interruption path
    (``esc to interrupt``) instead of ``Ctrl-C``. Configured sessions
    resolve through ``config.sessions`` and their ``window_name``;
    per-task worker surfaces resolve only via the canonical
    ``task-<project>-<n>`` window name for registered projects.
    """
    window_name = _interrupt_window_name(config, name)
    storage_session = _storage_session_name(config)
    try:
        from pollypm.tmux.client import TmuxClient

        tmux = TmuxClient()
    except Exception as exc:  # noqa: BLE001
        raise _interrupt_failed(name, f"tmux client unavailable: {exc}") from exc
    pane_id = _find_interrupt_pane(
        tmux,
        name=name,
        storage_session=storage_session,
        window_name=window_name,
    )
    try:
        tmux.run("send-keys", "-t", pane_id, "Escape")
    except FileNotFoundError as exc:
        raise _interrupt_failed(name, f"tmux binary not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise _interrupt_failed(
            name, f"tmux command timed out after {exc.timeout}s",
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise _interrupt_failed(
            name,
            f"tmux send-keys failed with exit {exc.returncode}: {exc.stderr}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise _interrupt_failed(name, str(exc)) from exc
    return ActionResult(ok=True, message=f"sent Escape to {name}")


@router.post(
    "/sessions/{name}/pause",
    response_model=ActionResult,
    summary=(
        "Tag a session as paused (daemon loop chokepoints honor the marker; "
        "some manual send surfaces remain explicit actions — #2068)"
    ),
    operation_id="pauseSession",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Session not found."},
        # PR #2081 round 3 finding 2: a corrupt / permission-broken
        # marker fails closed in the recovery loops, so we refuse to
        # overwrite it from this endpoint. ``marker_unreadable`` is the
        # specific machine-readable code; ``daemon_unavailable`` covers
        # the no-base_dir case.
        "503": {
            "description": (
                "Pause marker is unreadable (``marker_unreadable``) or "
                "the daemon has no base_dir to write to "
                "(``daemon_unavailable``)."
            ),
        },
    },
)
def pause_session_endpoint(
    name: str,
    config: ConfigDep,
    body: SessionPauseMutationRequest | None = None,
) -> ActionResult:
    """POST /api/v1/sessions/{name}/pause — write the pause marker.

    **Partial enforcement.** The pause marker is HONORED by the
    recovery/relaunch, heartbeat per-session, heartbeat send, and
    task-assignment dispatch chokepoints wired under #2068; those
    loops emit a ``session.pause.skip`` audit event and yield. Some
    direct/manual cockpit and chat send surfaces remain explicit
    operator actions.
    The response ``message`` spells this out so operators know which
    loops they have quiesced and which still act.

    Writes ``<base_dir>/paused-sessions.json`` (atomic tmp+rename,
    serialised via ``fcntl.flock`` against concurrent pause/resume
    callers — see :func:`pollypm.session_paused.pause_marker_lock`)
    so ``GET /api/v1/sessions`` surfaces ``paused=true`` on the
    affected row. The ``status`` field continues to reflect actual
    runtime health (``healthy`` / ``stale`` / ``missing`` /
    ``unknown``) — a paused-but-missing session reports
    ``status="missing"`` AND ``paused=true``, not ``status="paused"``
    (Codex PR #2061 round 2). Idempotent: pausing an already-paused
    session returns 200 with no state change.
    """
    _find_session(config, name)  # 404 if unknown
    actor, reason = _pause_mutation_actor_reason(body)
    try:
        with _pause_marker_lock(config):
            # PR #2081 round 3 finding 2 — go through the discriminated
            # state reader instead of the best-effort ``load_paused_names``.
            # The best-effort reader collapses an unreadable marker to an
            # empty set; combined with our read-modify-write below, that
            # would silently overwrite a corrupt-but-existing marker with
            # just the newly requested name and lose every other paused
            # session. The discriminated reader lets us fail closed with
            # a typed 503 instead.
            state = _load_paused_state(config)
            if state.kind == "unreadable":
                marker_path = (
                    config.project.base_dir / "paused-sessions.json"
                    if getattr(config.project, "base_dir", None) is not None
                    else "<unknown>"
                )
                _emit_pause_operator_action(
                    config,
                    event=EVENT_SESSION_PAUSE_REFUSED,
                    session_name=name,
                    actor=actor,
                    reason=reason,
                    paused_count_after=None,
                    status="warn",
                    extra_metadata={
                        "operation": "pause",
                        "refused_reason": "marker_unreadable",
                        "marker_path": str(marker_path),
                        "marker_reason": state.reason,
                    },
                )
                raise _marker_unreadable(
                    name, str(marker_path), state.reason,
                )
            # ``absent`` and ``ok`` both have an empty-or-populated
            # ``names`` we can mutate; convert the frozenset to a
            # working set.
            names = set(state.names)
            if name in names:
                _emit_pause_operator_action(
                    config,
                    event=EVENT_SESSION_PAUSE_PAUSED,
                    session_name=name,
                    actor=actor,
                    reason=reason,
                    paused_count_after=len(names),
                )
                return ActionResult(
                    ok=True,
                    message=(
                        f"{name} already tagged paused — "
                        f"{_PAUSE_PARTIAL_ENFORCEMENT_NOTE}"
                    ),
                )
            names.add(name)
            _save_paused_names(config, names)
            paused_count_after = len(names)
    except APIError:
        raise
    except RuntimeError as exc:
        # ``save_paused_names`` raises ``RuntimeError`` when the
        # config has no ``project.base_dir`` (the marker has nowhere
        # to live). Translate to the 503 ``daemon_unavailable`` shape
        # the rest of this surface uses for "system can't act".
        logger.debug("pause marker has no path for %s", name, exc_info=True)
        raise _daemon_unavailable(name, str(exc)) from exc
    except OSError as exc:
        logger.debug("pause marker write failed for %s", name, exc_info=True)
        raise service_unavailable(
            f"Cannot write pause marker for {name!r}: {exc!s}",
            hint="Check filesystem permissions on ~/.pollypm.",
        ) from exc
    _emit_pause_operator_action(
        config,
        event=EVENT_SESSION_PAUSE_PAUSED,
        session_name=name,
        actor=actor,
        reason=reason,
        paused_count_after=paused_count_after,
    )
    return ActionResult(
        ok=True,
        message=f"tagged {name} paused — {_PAUSE_PARTIAL_ENFORCEMENT_NOTE}",
    )


@router.post(
    "/sessions/{name}/resume",
    response_model=ActionResult,
    summary=(
        "Clear the pause marker (recovery loops resume immediately; "
        "wired daemon-loop chokepoints resume immediately — #2068)"
    ),
    operation_id="resumeSession",
    responses={
        "401": {"description": "Missing or invalid bearer token."},
        "404": {"description": "Session not found."},
        # PR #2081 round 3 finding 2: resume cannot silently return
        # "already untagged" against an unreadable marker — the
        # recovery loops are failing closed, so the operator needs to
        # know the resume did NOT take effect.
        "503": {
            "description": (
                "Pause marker is unreadable (``marker_unreadable``) or "
                "the daemon has no base_dir to write to "
                "(``daemon_unavailable``)."
            ),
        },
    },
)
def resume_session_endpoint(
    name: str,
    config: ConfigDep,
    body: SessionPauseMutationRequest | None = None,
) -> ActionResult:
    """POST /api/v1/sessions/{name}/resume — clear the pause marker.

    Idempotent inverse of :func:`pause_session_endpoint`. The same
    partial-enforcement caveat applies: clearing the marker lifts
    the yield in the wired daemon-loop chokepoints immediately. Some
    direct/manual cockpit and chat send surfaces remain explicit
    operator actions.
    """
    _find_session(config, name)  # 404 if unknown
    actor, reason = _pause_mutation_actor_reason(body)
    try:
        with _pause_marker_lock(config):
            # PR #2081 round 3 finding 2 — same discriminated-reader
            # gate as the pause endpoint. ``load_paused_names`` would
            # collapse an unreadable marker to an empty set and the
            # idempotent branch below would return ``200 already
            # untagged`` while the recovery loops stayed quiesced.
            # Refuse with a 503 instead so the operator knows the
            # resume did NOT take effect.
            state = _load_paused_state(config)
            if state.kind == "unreadable":
                marker_path = (
                    config.project.base_dir / "paused-sessions.json"
                    if getattr(config.project, "base_dir", None) is not None
                    else "<unknown>"
                )
                _emit_pause_operator_action(
                    config,
                    event=EVENT_SESSION_PAUSE_REFUSED,
                    session_name=name,
                    actor=actor,
                    reason=reason,
                    paused_count_after=None,
                    status="warn",
                    extra_metadata={
                        "operation": "resume",
                        "refused_reason": "marker_unreadable",
                        "marker_path": str(marker_path),
                        "marker_reason": state.reason,
                    },
                )
                raise _marker_unreadable(
                    name, str(marker_path), state.reason,
                )
            names = set(state.names)
            if name not in names:
                _emit_pause_operator_action(
                    config,
                    event=EVENT_SESSION_PAUSE_RESUMED,
                    session_name=name,
                    actor=actor,
                    reason=reason,
                    paused_count_after=len(names),
                )
                return ActionResult(
                    ok=True,
                    message=(
                        f"{name} already untagged — "
                        f"{_PAUSE_PARTIAL_ENFORCEMENT_NOTE}"
                    ),
                )
            names.discard(name)
            _save_paused_names(config, names)
            paused_count_after = len(names)
    except APIError:
        raise
    except RuntimeError as exc:
        logger.debug("pause marker has no path for %s", name, exc_info=True)
        raise _daemon_unavailable(name, str(exc)) from exc
    except OSError as exc:
        logger.debug("pause marker write failed for %s", name, exc_info=True)
        raise service_unavailable(
            f"Cannot write pause marker for {name!r}: {exc!s}",
            hint="Check filesystem permissions on ~/.pollypm.",
        ) from exc
    _emit_pause_operator_action(
        config,
        event=EVENT_SESSION_PAUSE_RESUMED,
        session_name=name,
        actor=actor,
        reason=reason,
        paused_count_after=paused_count_after,
    )
    return ActionResult(
        ok=True,
        message=f"cleared pause tag on {name} — {_PAUSE_PARTIAL_ENFORCEMENT_NOTE}",
    )


__all__ = [
    "SessionConfigView",
    "SessionDetail",
    "SessionHealthSnapshot",
    "SessionInfo",
    "SessionPauseMutationRequest",
    "SessionsListResponse",
    "get_session_endpoint",
    "list_sessions_endpoint",
    "pause_session_endpoint",
    "restart_session_endpoint",
    "resume_session_endpoint",
    "router",
]
