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

Pure classification / probe helpers live in
:mod:`pollypm.session_health` so the Phase 2 sessions-admin API
endpoint (``GET /api/v1/sessions``) consumes the same contract
without duplicating thresholds, naming, or probe logic (Codex PR #2061
round 5 blocker 3). The private ``_classify_status`` / ``_humanize_age``
/ ``_latest_heartbeat`` / ``_STALE_HEARTBEAT_SECONDS`` symbols remain
re-exported here for backwards compatibility with the test suite.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from pollypm.config import DEFAULT_CONFIG_PATH
from pollypm.session_health import (
    STALE_HEARTBEAT_SECONDS as _STALE_HEARTBEAT_SECONDS,
)
from pollypm.session_health import (
    STORAGE_CLOSET_SUFFIX as _STORAGE_CLOSET_SUFFIX,
)
from pollypm.session_health import (
    age_seconds as _age_seconds,
)
from pollypm.session_health import (
    classify_status as _classify_status,
)
from pollypm.session_health import (
    humanize_age as _humanize_age,
)
from pollypm.session_health import (
    latest_heartbeat as _latest_heartbeat,
)
from pollypm.session_health import (
    latest_session_runtime as _latest_session_runtime,
)
from pollypm.session_health import (
    list_storage_closet_windows as _list_windows,
)
from pollypm.session_health import (
    storage_session_name as _storage_session_name,
)

logger = logging.getLogger(__name__)


# Backwards-compat re-imports for callers (and tests) that bound to the
# private names before the helpers moved to :mod:`pollypm.session_health`.
# The shared module is the single source of truth — keep these aliased,
# do NOT re-implement the bodies here.
__all_shared__ = (
    "_STALE_HEARTBEAT_SECONDS",
    "_STORAGE_CLOSET_SUFFIX",
    "_age_seconds",
    "_classify_status",
    "_humanize_age",
    "_latest_heartbeat",
    "_latest_session_runtime",
    "_list_windows",
    "_storage_session_name",
)


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


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


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
    runtime = _latest_session_runtime(config, session.name)
    status = _classify_status(
        window_present=window is not None,
        age_seconds=age,
        runtime_status=getattr(runtime, "status", None),
        last_failure_type=getattr(runtime, "last_failure_type", None),
    )
    token_state = "ok" if (getattr(session, "auth_token", "") or "") else "missing"

    row: dict[str, Any] = {
        "name": session.name,
        "role": session.role,
        "project": session.project,
        "provider": str(_value(getattr(session, "provider", "")) or ""),
        "account": getattr(session, "account", "") or "",
        "window_name": window_name,
        "tmux_session": storage_session,
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
    "_STORAGE_CLOSET_SUFFIX",
    "_classify_status",
    "_humanize_age",
    "_latest_heartbeat",
    "_latest_session_runtime",
    "_list_windows",
    "_build_row",
]
