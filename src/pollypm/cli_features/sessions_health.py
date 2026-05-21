"""``pm sessions`` — one-shot session health summary (refs #2012).

Contract:
- Inputs: optional ``--health``, ``--json``, ``--config`` Typer flags.
- Outputs: one row per configured session printed to stdout, or one
  JSON object per line when ``--json`` is set.
- Side effects: reads ``pollypm.toml`` via :func:`pollypm.config.load_config`,
  shells out via the tmux client to ``list-windows`` for the storage-closet
  session, and queries the latest heartbeat row for each session through
  the backend-aware storage facades. Read-only — no state is mutated.
- Invariants: Lever-2 (#2012) trust signal ``session.auth_token`` is
  surfaced as ``TOKEN ok / missing`` so an operator can spot legacy
  sessions whose token never migrated in. Status thresholds:
  ``healthy`` = heartbeat <= 5 minutes old AND window alive;
  ``stale`` = heartbeat older than 5 minutes;
  ``missing`` = the configured ``window_name`` is not present in tmux;
  ``unknown`` = no heartbeat row has ever been recorded.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from pollypm.config import DEFAULT_CONFIG_PATH

logger = logging.getLogger(__name__)


# Heartbeats older than this are classified ``stale``. Five minutes
# matches the cockpit-inbox "stuck pane" threshold and the watchdog's
# escalation cadence — same number, same semantics, picked once.
_STALE_HEARTBEAT_SECONDS = 5 * 60

# Suffix appended to ``project.tmux_session`` to derive the storage-
# closet session name (mirrors
# :attr:`pollypm.supervisor.Supervisor._STORAGE_CLOSET_SESSION_SUFFIX`).
# Hard-coded here so the CLI command can resolve windows without
# spinning up a full Supervisor — heavyweight when all the user wants
# is a read-only summary.
_STORAGE_CLOSET_SUFFIX = "-storage-closet"


def _storage_session_name(tmux_session: str) -> str:
    return f"{tmux_session}{_STORAGE_CLOSET_SUFFIX}"


def _humanize_age(iso_timestamp: str | None) -> str:
    """Return a compact relative age (``"22s ago"`` / ``"12m ago"``).

    Returns ``"none"`` when ``iso_timestamp`` is ``None`` or unparseable
    so the column never flashes a misleading "now" for sessions that
    have never reported in.
    """
    if not iso_timestamp:
        return "none"
    try:
        when = datetime.fromisoformat(iso_timestamp)
    except (TypeError, ValueError):
        return "none"
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    total = int(max(0, (datetime.now(UTC) - when).total_seconds()))
    if total < 60:
        return f"{total}s ago"
    if total < 3600:
        return f"{total // 60}m ago"
    if total < 86400:
        return f"{total // 3600}h ago"
    return f"{total // 86400}d ago"


def _age_seconds(iso_timestamp: str | None) -> int | None:
    if not iso_timestamp:
        return None
    try:
        when = datetime.fromisoformat(iso_timestamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return int(max(0, (datetime.now(UTC) - when).total_seconds()))


def _classify_status(
    *,
    window_present: bool,
    age_seconds: int | None,
) -> str:
    """Return one of ``healthy`` / ``missing`` / ``stale`` / ``unknown``.

    ``missing`` wins over ``stale`` — when the tmux window is gone the
    pane is the more urgent problem; the heartbeat staleness is just a
    downstream symptom and reporting both would be noisy.
    """
    if not window_present:
        return "missing"
    if age_seconds is None:
        return "unknown"
    if age_seconds > _STALE_HEARTBEAT_SECONDS:
        return "stale"
    return "healthy"


def _latest_heartbeat(config, session_name: str):
    """Return the most-recent heartbeat record for ``session_name``.

    Backend-aware (#1737 / #1811): postgres installs read through the
    pg facade; sqlite installs fall back to the legacy ``StateStore``.
    Returns ``None`` when no heartbeat row exists OR the read fails —
    a read failure must not crash a read-only CLI summary.
    """
    from pollypm.storage._backend_dispatch import is_pg_backend

    try:
        if is_pg_backend(config):
            from pollypm.storage.pg_heartbeats import latest_heartbeat as _pg

            return _pg(session_name, config=config)
        from pollypm.store import get_store

        store = get_store(config)
        getter = getattr(store, "latest_heartbeat", None)
        if getter is None:
            return None
        return getter(session_name)
    except Exception:  # noqa: BLE001 — never crash a read-only summary
        logger.debug("latest_heartbeat lookup failed", exc_info=True)
        return None


def _list_windows(tmux_session_name: str) -> dict[str, Any]:
    """Return ``{window_name: TmuxWindow}`` for ``tmux_session_name``.

    Empty dict when the tmux server is unreachable, the session does
    not exist, or any other tmux probe fails — the caller treats every
    session as ``missing`` in that state, which is the right answer
    when tmux itself is down.
    """
    from pollypm.tmux.client import TmuxClient

    tmux = TmuxClient()
    try:
        if not tmux.has_session(tmux_session_name):
            return {}
        return {window.name: window for window in tmux.list_windows(tmux_session_name)}
    except Exception:  # noqa: BLE001
        logger.debug("tmux probe failed for %s", tmux_session_name, exc_info=True)
        return {}


def _log_mtime_iso(config, session_name: str) -> str | None:
    """Return the ISO mtime of ``<logs_dir>/<session_name>.log`` if any."""
    logs_dir = getattr(getattr(config, "project", None), "logs_dir", None)
    if not logs_dir:
        return None
    candidate = Path(logs_dir) / f"{session_name}.log"
    try:
        if not candidate.exists():
            return None
        stat = candidate.stat()
    except OSError:
        return None
    return datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()


def _build_row(
    *,
    config,
    session,
    storage_session: str,
    windows: dict[str, Any],
    health: bool,
) -> dict[str, Any]:
    window_name = session.window_name or session.name
    window = windows.get(window_name)
    heartbeat = _latest_heartbeat(config, session.name)
    hb_iso = getattr(heartbeat, "created_at", None) if heartbeat else None
    age = _age_seconds(hb_iso)
    status = _classify_status(window_present=window is not None, age_seconds=age)
    token_state = "ok" if (getattr(session, "auth_token", "") or "") else "missing"

    row: dict[str, Any] = {
        "name": session.name,
        "role": session.role,
        "project": session.project,
        "window": f"{storage_session}:{window_name}",
        "status": status,
        "last_heartbeat_age": _humanize_age(hb_iso),
        "last_heartbeat_iso": hb_iso,
        "token": token_state,
    }
    if health:
        row["pane_current_command"] = (
            getattr(window, "pane_current_command", None) if window else None
        )
        row["pid"] = getattr(window, "pane_pid", None) if window else None
        row["log_mtime"] = _log_mtime_iso(config, session.name)
    return row


def _format_row(row: dict[str, Any], *, health: bool) -> str:
    base = (
        f"{row['name']:<25} "
        f"{row['role']:<9} "
        f"{row['project']:<11} "
        f"{row['window']:<23} "
        f"{row['status']:<10} "
        f"{row['last_heartbeat_age']:<14} "
        f"{row['token']}"
    )
    if not health:
        return base
    cmd = row.get("pane_current_command") or "-"
    pid = row.get("pid")
    pid_str = str(pid) if pid is not None else "-"
    mtime = row.get("log_mtime") or "-"
    return f"{base}  cmd={cmd} pid={pid_str} log_mtime={mtime}"


def _header(*, health: bool) -> str:
    base = (
        f"{'NAME':<25} "
        f"{'ROLE':<9} "
        f"{'PROJECT':<11} "
        f"{'WINDOW':<23} "
        f"{'STATUS':<10} "
        f"{'LAST_HB':<14} "
        f"{'TOKEN'}"
    )
    if not health:
        return base
    return f"{base}  CMD/PID/LOG_MTIME"


def sessions_health(
    health: bool = typer.Option(
        False,
        "--health",
        help="Include diagnostic columns (pane_current_command, pid, log mtime).",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit one JSON object per line, machine-readable."
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
) -> None:
    """List configured sessions with their live runtime state.

    Read-only — safe to run outside the cockpit. Pairs with the
    watchdog/recovery alerting chain: when something looks off in the
    rail this command answers "is the pane actually there, when did it
    last heartbeat, and does it carry the Lever-2 auth token yet?"
    without spinning up a Supervisor.
    """
    from pollypm.config import load_config

    config = load_config(config_path)
    storage_session = _storage_session_name(config.project.tmux_session)
    windows = _list_windows(storage_session)

    sessions = sorted(
        (s for s in config.sessions.values() if getattr(s, "enabled", True)),
        key=lambda s: s.name,
    )

    rows = [
        _build_row(
            config=config,
            session=session,
            storage_session=storage_session,
            windows=windows,
            health=health,
        )
        for session in sessions
    ]

    if json_output:
        for row in rows:
            typer.echo(json.dumps(row, sort_keys=True))
        return

    if not rows:
        typer.echo("No sessions configured.")
        return

    typer.echo(_header(health=health))
    for row in rows:
        typer.echo(_format_row(row, health=health))


def register_sessions_health_command(app: typer.Typer) -> None:
    """Mount ``pm sessions`` on the root Typer app."""
    app.command(
        "sessions",
        help=(
            "List configured sessions with live tmux + heartbeat state. "
            "Add ``--health`` for diagnostic columns or ``--json`` for "
            "machine-readable output (one JSON object per line)."
        ),
    )(sessions_health)


__all__ = [
    "register_sessions_health_command",
    "sessions_health",
    "_STALE_HEARTBEAT_SECONDS",
    "_classify_status",
    "_humanize_age",
    "_build_row",
]
