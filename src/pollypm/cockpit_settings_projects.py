"""Non-blocking project snapshot helpers for the cockpit settings screen.

Contract:
- Inputs: the loaded PollyPM config plus a relative-age formatter.
- Outputs: normalized project rows for the settings UI.
- Side effects: reads project-path metadata and a fast, non-blocking
  task-count probe via the work-service facade so busy project DBs do
  not stall UI mount.
- Invariants: callers get best-effort task totals; a locked DB surfaces
  as ``task_total_label='busy'`` instead of blocking the screen.
"""

from __future__ import annotations

from datetime import datetime as _dt
from pathlib import Path

from pollypm.work.task_state import project_task_total_fast_count


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
                    task_total = project_task_total_fast_count(
                        db_path, project_key=key
                    )
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


__all__ = ["collect_settings_projects"]
