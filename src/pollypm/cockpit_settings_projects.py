"""Non-blocking project snapshot helpers for the cockpit settings screen.

Contract:
- Inputs: the loaded PollyPM config plus a relative-age formatter.
- Outputs: normalized project rows for the settings UI.
- Side effects: reads project-path metadata and does read-only SQLite
  queries with a very short timeout so busy project DBs do not stall UI
  mount.
- Invariants: callers get best-effort task totals; a locked DB surfaces
  as ``task_total_label='busy'`` instead of blocking the screen.
"""

from __future__ import annotations

from datetime import datetime as _dt
from pathlib import Path
import sqlite3


def collect_settings_projects(config, *, format_relative_age) -> list[dict]:
    """Return settings-project rows without blocking on busy work DBs."""

    rows: list[dict] = []
    for key, project in (getattr(config, "projects", {}) or {}).items():
        path = getattr(project, "path", None)
        persona = getattr(project, "persona_name", None)
        path_str = str(path) if path else ""
        tracked = bool(getattr(project, "tracked", False))
        path_exists = False
        task_total_label = "0"
        last_activity = ""
        try:
            if path is not None and path.exists():
                path_exists = True
                db_path = path / ".pollypm" / "state.db"
                if db_path.exists():
                    last_activity = _project_last_activity(
                        db_path, format_relative_age=format_relative_age
                    )
                    task_total = _project_task_total(db_path, project_key=key)
                    if task_total is None:
                        task_total_label = "busy"
                    else:
                        task_total_label = str(task_total)
        except OSError:
            path_exists = False
            task_total_label = "0"
        rows.append(
            {
                "key": key,
                "name": getattr(project, "name", None) or key,
                "persona": (
                    persona if isinstance(persona, str) and persona.strip() else "Polly"
                ),
                "path": path_str,
                "path_exists": path_exists,
                "tracked": tracked,
                "task_total": task_total_label,
                "task_total_label": task_total_label,
                "last_activity": last_activity,
                "project_obj": project,
            }
        )
    return rows


def _project_last_activity(db_path: Path, *, format_relative_age) -> str:
    try:
        mtime = db_path.stat().st_mtime
    except OSError:
        return ""
    return format_relative_age(_dt.fromtimestamp(mtime).isoformat())


def _project_task_total(db_path: Path, *, project_key: str) -> int | None:
    """Return a task count quickly, or ``None`` if the DB is busy."""

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            timeout=0.05,
        )
        conn.execute("PRAGMA busy_timeout=50")
        row = conn.execute(
            "SELECT COUNT(*) FROM work_tasks WHERE project = ?",
            (project_key,),
        ).fetchone()
        return int(row[0] or 0) if row is not None else 0
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            return None
        return 0
    except sqlite3.Error:
        return 0
    finally:
        if conn is not None:
            conn.close()


__all__ = ["collect_settings_projects"]
