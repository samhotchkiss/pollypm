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


__all__ = [
    "STALE_HEARTBEAT_SECONDS",
    "STORAGE_CLOSET_SUFFIX",
    "age_seconds",
    "classify_status",
    "humanize_age",
    "latest_heartbeat",
    "list_storage_closet_windows",
    "parse_iso",
    "storage_session_name",
]
