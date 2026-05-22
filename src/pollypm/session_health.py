"""Shared session-health classification + heartbeat/tmux probe helpers.

Single source of truth for the ``pm sessions`` health contract. Both
:mod:`pollypm.cli_features.sessions_health` (the CLI) and
:mod:`pollypm.web_api.routes.sessions_admin` (the Phase 2 API) import
from here so the two surfaces cannot drift on:

* the ``healthy`` / ``stale`` / ``missing`` / ``unknown`` classification
  thresholds (Codex PR #2061 round 5 blocker 3),
* the storage-closet tmux-session naming,
* the heartbeat-row lookup (pg-facade-backed, fail-soft on outage),
* the tmux-window probe (fail-soft on outage).

Pure module — no Typer / FastAPI / Pydantic imports — so it is cheap to
unit-test in isolation and safe to import from any layer.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# Heartbeats older than this are classified ``stale``. Five minutes
# matches the cockpit-inbox "stuck pane" threshold and the watchdog's
# escalation cadence — same number, same semantics, picked once.
STALE_HEARTBEAT_SECONDS = 5 * 60

# Suffix appended to ``project.tmux_session`` to derive the storage-
# closet session name. Hard-coded here (rather than importing from
# :mod:`pollypm.supervisor`) so the read-only summary path stays free
# of the heavyweight Supervisor construction. Mirror of
# :attr:`pollypm.supervisor.Supervisor._STORAGE_CLOSET_SESSION_SUFFIX`.
STORAGE_CLOSET_SUFFIX = "-storage-closet"


def storage_session_name(tmux_session: str) -> str:
    """Return ``<tmux_session>-storage-closet`` for ``tmux_session``."""
    return f"{tmux_session}{STORAGE_CLOSET_SUFFIX}"


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; ``None`` on missing or unparseable input."""
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


def age_seconds(iso_timestamp: str | None) -> int | None:
    """Return seconds since ``iso_timestamp`` (clamped to >= 0), or ``None``."""
    parsed = parse_iso(iso_timestamp)
    if parsed is None:
        return None
    return int(max(0, (datetime.now(UTC) - parsed).total_seconds()))


def humanize_age(iso_timestamp: str | None) -> str:
    """Return a compact relative age (``"22s ago"`` / ``"12m ago"``).

    Returns ``"none"`` when ``iso_timestamp`` is ``None`` or unparseable
    so the column never flashes a misleading "now" for sessions that
    have never reported in.
    """
    secs = age_seconds(iso_timestamp)
    if secs is None:
        return "none"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def classify_status(
    *,
    window_present: bool,
    age_seconds: int | None,
) -> str:
    """Return one of ``healthy`` / ``stale`` / ``missing`` / ``unknown``.

    ``missing`` wins over ``stale`` — when the tmux window is gone the
    pane is the more urgent problem and reporting both would be noisy.

    The pause marker is intentionally NOT consulted here (Codex PR #2061
    round 2). Pause is informational only — folding it into ``status``
    would mask the real runtime state.
    """
    if not window_present:
        return "missing"
    if age_seconds is None:
        return "unknown"
    if age_seconds > STALE_HEARTBEAT_SECONDS:
        return "stale"
    return "healthy"


def latest_heartbeat(config: Any, session_name: str) -> Any | None:
    """Fetch the most-recent heartbeat record; ``None`` on any failure.

    Read failures must never crash a read-only surface. The caller
    treats ``None`` as ``unknown`` (no heartbeat row yet) and that is
    a legitimate live state for a freshly-booted session.
    """
    try:
        from pollypm.storage.pg_heartbeats import latest_heartbeat as _pg
    except Exception:  # noqa: BLE001
        logger.debug("pg_heartbeats import failed", exc_info=True)
        return None
    try:
        return _pg(session_name, config=config)
    except Exception:  # noqa: BLE001
        logger.debug(
            "latest_heartbeat lookup failed for %s",
            session_name,
            exc_info=True,
        )
        return None


def list_storage_closet_windows(tmux_session: str) -> dict[str, Any]:
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


class TmuxProbeUnavailable(RuntimeError):
    """Raised by :func:`probe_strict_turn_active` when the probe cannot answer.

    The destructive ``POST /api/v1/sessions/{name}/restart`` strict
    safety gate must fail *closed* — a transient tmux outage must not
    look like "agent is idle" and let the restart tear down a working
    pane (Codex PR #2061 round 3 + round 6). Callers map this to
    ``503 unsafe_mid_turn_unknown``.

    Aliases :class:`pollypm.session_services.tmux.TmuxProbeUnavailable`
    so the route layer can catch a single exception type regardless of
    where the probe is dispatched. The session-services exception type
    stays alive for in-service probes; the route now prefers the
    config/tmux-direct probe defined here.
    """


def probe_strict_turn_active(
    config: Any,
    session_name: str,
    tmux_client: Any,
) -> bool:
    """Return True iff the configured session has a live mid-turn pane.

    Single-sourced strict probe for the destructive restart safety gate.
    Reads from the **same** contract surfaces the rest of the sessions-
    admin API exposes:

    * ``config.sessions[name]`` — the canonical configured-session
      registry (mirrors what ``GET /api/v1/sessions`` walks);
    * direct :class:`pollypm.tmux.client.TmuxClient` ``has_session`` /
      ``list_windows`` calls — owned by the probe itself, NOT the
      fail-soft :func:`list_storage_closet_windows` helper — so a tmux
      outage raises :class:`TmuxProbeUnavailable` instead of looking
      identical to "window genuinely absent" (Codex PR #2061 round 7);
    * direct :class:`pollypm.tmux.client.TmuxClient` ``list_panes`` /
      ``capture_pane`` calls against the resolved pane.

    Pointedly NOT routed through :class:`TmuxSessionService` /
    :class:`pollypm.storage.state.StateStore`. Codex PR #2061 round 6
    caught that production session registration is now in pg (via
    :func:`pollypm.storage.pg_sessions.upsert_session`, called from the
    Supervisor launch path), so the legacy ``StateStore.list_sessions``
    (which the in-service strict probe consults) can be empty or stale
    even when the configured session and tmux pane exist. With that
    split source the in-service probe returned ``False`` (looks-idle)
    on healthy production setups and the restart proceeded to destroy
    a working pane — fail-open in the worst possible way.

    Round 7 followup: the probe NO LONGER accepts a pre-computed
    ``windows`` dict. Threading the fail-soft
    :func:`list_storage_closet_windows` result in let a tmux outage
    (``{}`` returned for ``CalledProcessError`` / ``FileNotFoundError``
    / timeout) look identical to "window genuinely absent" → probe
    returned ``False`` (looks idle) → strict restart proceeded. The
    probe now owns window discovery directly so it can distinguish:

    * tmux call **succeeds, no matching window** → return ``False``
      (nothing to interrupt; the relaunch can safely create a fresh
      window).
    * tmux call **raises** → propagate as ``TmuxProbeUnavailable`` →
      route maps to ``503 unsafe_mid_turn_unknown`` (fail-closed).

    The fail-soft :func:`list_storage_closet_windows` helper is still
    correct for the read-side GET detail/list paths — losing tmux
    there should degrade gracefully, not 503 a read-only summary. Only
    the destructive restart safety gate needs the strict variant.

    Returns
    -------
    bool
        ``True`` if the pane is live and shows a recognised active-turn
        marker (Claude Code's ``working`` / ``⏺`` or Codex's
        ``working (...)`` + ``esc to interrupt``).
        ``False`` if:
        * the session isn't configured (caller's 404 lookup already
          ran, but be lenient — nothing to "be mid-turn" on),
        * tmux reliably reports the storage-closet session is absent
          (no tmux server / session not yet booted) → nothing to
          interrupt,
        * the tmux window is absent → nothing to interrupt,
        * the pane is dead → nothing to interrupt,
        * or the pane text shows no active-turn marker.

    Raises
    ------
    TmuxProbeUnavailable
        Any failure of the underlying ``TmuxClient`` calls
        (``has_session`` / ``list_windows`` / ``list_panes`` /
        ``capture_pane``) — propagated so the route maps to
        ``503 unsafe_mid_turn_unknown`` instead of silently returning
        ``False``.
    """
    sessions = getattr(config, "sessions", None) or {}
    session = sessions.get(session_name)
    if session is None:
        # Lookup by ``name`` attribute too — config may be keyed by
        # role / window / etc. in test fixtures.
        for candidate in sessions.values():
            if getattr(candidate, "name", "") == session_name:
                session = candidate
                break
    if session is None:
        return False

    window_name = getattr(session, "window_name", None) or session_name
    tmux_session = getattr(getattr(config, "project", None), "tmux_session", "") or ""
    storage_session = storage_session_name(tmux_session) if tmux_session else ""
    if not storage_session:
        return False

    # Owned window discovery (Codex PR #2061 round 7). ANY exception
    # from the underlying tmux calls is "unknown" — raise
    # ``TmuxProbeUnavailable`` so the route maps to 503
    # ``unsafe_mid_turn_unknown`` instead of treating tmux outage as
    # "window absent → safe to restart".
    try:
        session_present = tmux_client.has_session(storage_session)
    except Exception as exc:  # noqa: BLE001
        raise TmuxProbeUnavailable(
            f"tmux.has_session failed: {exc!s}",
        ) from exc
    if not session_present:
        # tmux is up and reliably reports the storage-closet session
        # isn't there — definitive "nothing to interrupt".
        return False

    try:
        windows_list = tmux_client.list_windows(storage_session)
    except Exception as exc:  # noqa: BLE001
        raise TmuxProbeUnavailable(
            f"tmux.list_windows failed: {exc!s}",
        ) from exc
    windows = {getattr(w, "name", ""): w for w in windows_list}
    window = windows.get(window_name)
    if window is None:
        # tmux reliably reported no window with this name → nothing to
        # interrupt. The route may still proceed to relaunch into a
        # fresh window; the strict gate does not block that.
        return False
    pane_id = getattr(window, "pane_id", None)
    if pane_id is None:
        return False
    if getattr(window, "pane_dead", False):
        return False

    target = f"{getattr(window, 'session', storage_session)}:{window_name}"
    try:
        panes = tmux_client.list_panes(target)
    except Exception as exc:  # noqa: BLE001
        raise TmuxProbeUnavailable(
            f"tmux.list_panes failed: {exc!s}",
        ) from exc
    active_pane_id = pane_id
    for pane in panes:
        if getattr(pane, "active", False):
            active_pane_id = getattr(pane, "pane_id", pane_id)
            break

    try:
        text = tmux_client.capture_pane(active_pane_id, lines=200)
    except Exception as exc:  # noqa: BLE001
        raise TmuxProbeUnavailable(
            f"tmux.capture_pane failed: {exc!s}",
        ) from exc

    lowered = text.lower()
    if "⏺" in text or "working" in lowered:
        return True
    if "working (" in lowered and "esc to interrupt" in lowered:
        return True
    return False


__all__ = [
    "STALE_HEARTBEAT_SECONDS",
    "STORAGE_CLOSET_SUFFIX",
    "TmuxProbeUnavailable",
    "age_seconds",
    "classify_status",
    "humanize_age",
    "latest_heartbeat",
    "list_storage_closet_windows",
    "parse_iso",
    "probe_strict_turn_active",
    "storage_session_name",
]
