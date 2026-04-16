from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from pollypm.atomic_io import atomic_write_json
from pollypm.config import load_config
from pollypm.inbox_v2 import list_messages as list_v2_messages, read_message as read_v2_message
from pollypm.tz import format_time as _fmt_time
from pollypm.providers import get_provider
from pollypm.projects import ensure_project_scaffold
from pollypm.runtimes import get_runtime
from pollypm.service_api import PollyPMService
from pollypm.supervisor import Supervisor
from pollypm.task_backends import get_task_backend
from pollypm.session_services import create_tmux_client
from pollypm.worktrees import list_worktrees


_STATUS_ICONS = {
    "draft": "◌",
    "queued": "○",
    "in_progress": "⟳",
    "blocked": "⊘",
    "on_hold": "⏸",
    "review": "◉",
    "done": "✓",
    "cancelled": "✗",
}


def _render_work_service_issues(project: object) -> str:
    """Render tasks from the work service for a project."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = project.path / ".pollypm" / "state.db"
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    with SQLiteWorkService(db_path=db_path, project_path=project.path) as svc:
        counts = svc.state_counts(project=getattr(project, "key", None))
        tasks = svc.list_tasks(project=getattr(project, "key", None))

    name = getattr(project, "name", None) or getattr(project, "key", "Project")
    lines = [f"{name} · Tasks", ""]

    # Summary bar
    count_parts = []
    for status in ("queued", "in_progress", "review", "blocked", "on_hold", "draft"):
        n = counts.get(status, 0)
        if n:
            icon = _STATUS_ICONS.get(status, "·")
            count_parts.append(f"{icon} {n} {status.replace('_', ' ')}")
    if count_parts:
        lines.append(" · ".join(count_parts))
        lines.append("")

    # Active tasks (non-terminal) — sorted by status priority
    _so = {"in_progress": 0, "review": 1, "queued": 2, "blocked": 3, "on_hold": 4, "draft": 5}
    active = [t for t in tasks if t.work_status.value not in ("done", "cancelled")]
    active.sort(key=lambda t: _so.get(t.work_status.value, 9))
    if active:
        for t in active:
            icon = _STATUS_ICONS.get(t.work_status.value, "·")
            assignee = f" [{t.assignee}]" if t.assignee else ""
            lines.append(f"  {icon} #{t.task_number} {t.title}{assignee}")
        lines.append("")

    # Recently completed (last 10)
    completed = [t for t in tasks if t.work_status.value in ("done", "cancelled")]
    completed.sort(key=lambda t: t.updated_at or "", reverse=True)
    if completed:
        lines.append(f"─── completed ({len(completed)}) ───")
        for t in completed[:10]:
            icon = _STATUS_ICONS.get(t.work_status.value, "·")
            lines.append(f"  {icon} #{t.task_number} {t.title}")
        lines.append("")

    if not tasks:
        lines.append("No tasks found.")

    return "\n".join(lines)


def _render_project_dashboard(project: object, project_key: str, config_path, supervisor) -> str | None:
    """Render a project dashboard with task counts, active tasks, alerts, and sessions."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = project.path / ".pollypm" / "state.db"
    if not db_path.exists():
        return None

    with SQLiteWorkService(db_path=db_path, project_path=project.path) as svc:
        counts = svc.state_counts(project=project_key)
        tasks = svc.list_tasks(project=project_key)

    name = getattr(project, "name", None) or project_key
    lines = [f"{name}", ""]

    # Summary bar
    total = sum(counts.values())
    count_parts = []
    for status in ("queued", "in_progress", "review", "blocked", "on_hold", "draft"):
        n = counts.get(status, 0)
        if n:
            icon = _STATUS_ICONS.get(status, "·")
            count_parts.append(f"{icon} {n} {status.replace('_', ' ')}")
    done_count = counts.get("done", 0)
    if done_count:
        count_parts.append(f"✓ {done_count} done")
    if count_parts:
        lines.append(" · ".join(count_parts))
    else:
        lines.append("No tasks yet.")
    lines.append("")

    # Active tasks (non-terminal) — sorted by status priority
    _status_order = {"in_progress": 0, "review": 1, "queued": 2, "blocked": 3, "on_hold": 4, "draft": 5}
    active = [t for t in tasks if t.work_status.value not in ("done", "cancelled")]
    active.sort(key=lambda t: _status_order.get(t.work_status.value, 9))
    if active:
        lines.append("── Active ──")
        for t in active:
            icon = _STATUS_ICONS.get(t.work_status.value, "·")
            assignee = f" [{t.assignee}]" if t.assignee else ""
            node = f" @ {t.current_node_id}" if t.current_node_id else ""
            lines.append(f"  {icon} #{t.task_number} {t.title}{assignee}{node}")
        lines.append("")

    # Recently completed (last 5)
    completed = [t for t in tasks if t.work_status.value in ("done", "cancelled")]
    completed.sort(key=lambda t: t.updated_at or "", reverse=True)
    if completed:
        lines.append(f"── Completed ({len(completed)}) ──")
        for t in completed[:5]:
            icon = _STATUS_ICONS.get(t.work_status.value, "·")
            lines.append(f"  {icon} #{t.task_number} {t.title}")
        if len(completed) > 5:
            lines.append(f"  ... and {len(completed) - 5} more")
        lines.append("")

    # Alerts for this project
    try:
        project_alerts = [
            a for a in supervisor.store.open_alerts()
            if any(
                l.session.project == project_key and l.session.name == a.session_name
                for l in supervisor.plan_launches()
            ) and a.alert_type not in ("suspected_loop", "stabilize_failed", "needs_followup")
        ]
        if project_alerts:
            lines.append("── Alerts ──")
            for a in project_alerts:
                lines.append(f"  ⚠ {a.alert_type}: {a.message}")
            lines.append("")
    except Exception:
        pass

    if not active and not completed:
        lines.append("No active live lane is running for this project.")
        lines.append("Select the project in the left rail and press N to start a worker lane.")

    return "\n".join(lines)


@dataclass(slots=True)
class CockpitItem:
    key: str
    label: str
    state: str
    selectable: bool = True


class CockpitRouter:
    _STATE_FILE = "cockpit_state.json"
    _COCKPIT_WINDOW = "PollyPM"
    _LEFT_PANE_WIDTH = 30

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.service = PollyPMService(config_path)
        self.tmux = create_tmux_client()
        self._supervisor: Supervisor | None = None
        # Per-project activity cache keyed by project key.
        # value: (db_mtime, git_mtime, is_active, has_working_task)
        # Skips re-opening SQLite on every 0.8s cockpit tick when nothing changed.
        self._project_activity_cache: dict[str, tuple[float, float, bool, bool]] = {}

    def _load_supervisor(self, *, fresh: bool = False) -> Supervisor:
        # Reload config if the file changed (picks up new projects, sessions, etc.)
        if not fresh and self._supervisor is not None:
            try:
                config_mtime = self.config_path.stat().st_mtime
                if not hasattr(self, "_config_mtime") or config_mtime != self._config_mtime:
                    fresh = True
                    self._config_mtime = config_mtime
            except OSError:
                pass
        if fresh or self._supervisor is None:
            if self._supervisor is not None:
                self._supervisor.store.close()
            self._supervisor = self.service.load_supervisor()
            self._supervisor.ensure_layout()
            try:
                self._config_mtime = self.config_path.stat().st_mtime
            except OSError:
                pass
            # Bump epoch so the cockpit TUI refreshes on next tick
            try:
                from pollypm.state_epoch import bump
                bump()
            except Exception:  # noqa: BLE001
                pass
        return self._supervisor

    def _state_path(self) -> Path:
        config = load_config(self.config_path)
        config.project.base_dir.mkdir(parents=True, exist_ok=True)
        return config.project.base_dir / self._STATE_FILE

    def selected_key(self) -> str:
        self._validate_state()
        data = self._load_state()
        value = data.get("selected")
        return str(value) if isinstance(value, str) and value else "polly"

    def set_selected_key(self, key: str) -> None:
        self._validate_state()
        data = self._load_state()
        data["selected"] = key
        self._write_state(data)

    def _load_state(self) -> dict[str, object]:
        path = self._state_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, data: dict[str, object]) -> None:
        atomic_write_json(self._state_path(), data)

    def _validate_state(self) -> None:
        """Clear stale entries from cockpit_state.json.

        Checks that right_pane_id points to a real pane and that
        mounted_session is actually alive. Prevents stale state from
        blocking heartbeat recovery or causing wrong session mounts.
        """
        state = self._load_state()
        dirty = False
        config = load_config(self.config_path)
        target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        panes = self._safe_list_panes(target)

        right_pane_id = state.get("right_pane_id")
        right_pane = None
        if isinstance(right_pane_id, str) and right_pane_id:
            right_pane = next((pane for pane in panes if pane.pane_id == right_pane_id), None)
            if right_pane is None or getattr(right_pane, "pane_dead", False):
                state.pop("right_pane_id", None)
                state.pop("mounted_session", None)
                dirty = True
                right_pane = None

        mounted = state.get("mounted_session")
        if isinstance(mounted, str) and mounted:
            release_lease = False
            try:
                supervisor = self._load_supervisor()
                launches = supervisor.plan_launches()
                launch = next((l for l in launches if l.session.name == mounted), None)
                if launch is None or not self._mounted_session_matches_pane(launch, right_pane):
                    state.pop("mounted_session", None)
                    dirty = True
                    release_lease = True
            except Exception:  # noqa: BLE001
                state.pop("mounted_session", None)
                dirty = True
                release_lease = True
            if release_lease:
                self._release_cockpit_lease(supervisor if "supervisor" in locals() else None, mounted)

        if dirty:
            self._write_state(state)

    def _safe_list_panes(self, target: str) -> list:
        try:
            return self.tmux.list_panes(target)
        except Exception:  # noqa: BLE001
            return []

    def _mounted_session_matches_pane(self, launch, pane) -> bool:
        if pane is None or getattr(pane, "pane_dead", False):
            return False
        if not self._is_live_provider_pane(pane):
            return False
        # A live provider pane is running — trust the state rather than
        # trying to match CWD.  CWD matching is unreliable because the
        # agent may cd elsewhere during its turn.  If state says this
        # session is mounted and the pane is alive, believe it.
        return True

    def _release_cockpit_lease(self, supervisor: Supervisor | None, session_name: str) -> None:
        if supervisor is None:
            try:
                supervisor = self._load_supervisor()
            except Exception:  # noqa: BLE001
                return
        try:
            supervisor.release_lease(session_name, expected_owner="cockpit")
        except Exception:  # noqa: BLE001
            pass

    def _ui_initialized_sessions(self) -> set[str]:
        data = self._load_state()
        value = data.get("ui_initialized_sessions")
        if not isinstance(value, list):
            return set()
        return {item for item in value if isinstance(item, str) and item}

    def _mark_ui_initialized(self, session_name: str) -> None:
        data = self._load_state()
        current = data.get("ui_initialized_sessions")
        items = [item for item in current if isinstance(item, str) and item] if isinstance(current, list) else []
        if session_name not in items:
            items.append(session_name)
        data["ui_initialized_sessions"] = items
        self._write_state(data)

    def build_items(self, *, spinner_index: int = 0) -> list[CockpitItem]:
        supervisor = self._load_supervisor()
        config = supervisor.config
        launches, windows, alerts, _leases, _errors = supervisor.status()
        user_msgs = list_v2_messages(config.project.root_dir, status="open", owner="user")
        unread = sum(1 for m in user_msgs if not m.read)
        inbox_label = f"Inbox ({unread})" if unread else f"Inbox ({len(user_msgs)})" if user_msgs else "Inbox"
        items = [
            CockpitItem("polly", "Polly", self._session_state("operator", launches, windows, alerts, spinner_index)),
            CockpitItem("russell", "Russell", self._session_state("reviewer", launches, windows, alerts, spinner_index)),
            CockpitItem("inbox", inbox_label, "mail" if unread else ("clear" if not user_msgs else "read")),
        ]

        selected = self.selected_key()
        project_session_map = self._project_session_map(launches)

        # Classify projects as active (task activity in last 24h or newly created)
        # vs inactive. SQLite opens are expensive at cockpit tick rate (0.8s) and
        # most projects don't change between ticks — cache by (db_mtime, git_mtime)
        # and only re-open on change. Uses a single SQL aggregation per project
        # (count + max(updated_at)) instead of loading all Task rows into memory.
        from datetime import UTC, datetime, timedelta
        import sqlite3
        now = datetime.now(UTC)
        cutoff_iso = (now - timedelta(hours=24)).isoformat()
        cutoff_ts = (now - timedelta(hours=24)).timestamp()
        active_projects: list[tuple[str, object]] = []
        inactive_projects: list[tuple[str, object]] = []
        project_has_active_task: dict[str, bool] = {}

        def _project_activity(
            project_key: str, project: object,
        ) -> tuple[bool, bool]:
            db_path = project.path / ".pollypm" / "state.db"
            git_dir = project.path / ".git"
            try:
                db_mtime = db_path.stat().st_mtime if db_path.exists() else 0.0
            except OSError:
                db_mtime = 0.0
            try:
                git_mtime = git_dir.stat().st_mtime if git_dir.exists() else 0.0
            except OSError:
                git_mtime = 0.0
            cached = self._project_activity_cache.get(project_key)
            if cached is not None and cached[0] == db_mtime and cached[1] == git_mtime:
                return cached[2], cached[3]

            is_active = False
            has_working_task = False
            if db_mtime > 0.0:
                # Batched single-query classification: one count for working
                # statuses, one max(updated_at) for recency. Avoids hydrating
                # every Task row through the full service.
                try:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                    try:
                        conn.row_factory = sqlite3.Row
                        row = conn.execute(
                            "SELECT "
                            "  SUM(CASE WHEN work_status IN ('in_progress','review') "
                            "           THEN 1 ELSE 0 END) AS working_count, "
                            "  MAX(updated_at) AS max_updated "
                            "FROM work_tasks WHERE project = ?",
                            (project_key,),
                        ).fetchone()
                    finally:
                        conn.close()
                    if row is not None:
                        working_count = row["working_count"] or 0
                        max_updated = row["max_updated"] or ""
                        if working_count > 0:
                            has_working_task = True
                            is_active = True
                        if max_updated and max_updated >= cutoff_iso:
                            is_active = True
                except (sqlite3.Error, OSError):
                    pass
            if not is_active and git_mtime > cutoff_ts:
                is_active = True
            self._project_activity_cache[project_key] = (
                db_mtime, git_mtime, is_active, has_working_task,
            )
            return is_active, has_working_task

        for project_key, project in config.projects.items():
            is_active, has_working_task = _project_activity(project_key, project)
            project_has_active_task[project_key] = has_working_task
            if is_active:
                active_projects.append((project_key, project))
            else:
                inactive_projects.append((project_key, project))

        # Evict cache entries for projects no longer in config to bound memory.
        live_keys = set(config.projects.keys())
        for stale_key in list(self._project_activity_cache.keys()):
            if stale_key not in live_keys:
                self._project_activity_cache.pop(stale_key, None)

        # Sort each group alphabetically by display label
        active_projects.sort(key=lambda x: x[1].display_label().lower())
        inactive_projects.sort(key=lambda x: x[1].display_label().lower())

        def _add_project_items(project_key: str, project: object) -> None:
            session_name = project_session_map.get(project_key)
            # Determine state/color: yellow for active task, idle otherwise
            if project_has_active_task.get(project_key):
                state = "◆ working"  # yellow indicator
            elif session_name is not None:
                state = self._session_state(session_name, launches, windows, alerts, spinner_index)
            else:
                state = "idle"
            items.append(CockpitItem(f"project:{project_key}", project.display_label(), state))
            if selected.startswith(f"project:{project_key}"):
                items.append(CockpitItem(f"project:{project_key}:dashboard", "  Dashboard", "sub"))
                persona = project.persona_name or "Polly"
                items.append(CockpitItem(f"project:{project_key}:session", f"  PM Chat ({persona})", "sub"))
                items.append(CockpitItem(f"project:{project_key}:issues", "  Tasks", "sub"))
                # Show active per-task worker sessions under the project
                try:
                    storage = supervisor.storage_closet_session_name()
                    task_prefix = f"task-{project_key}-"
                    for win in self.tmux.list_windows(storage):
                        if win.name.startswith(task_prefix):
                            task_num = win.name[len(task_prefix):]
                            task_label = f"  ⟳ Task #{task_num}"
                            items.append(CockpitItem(
                                f"project:{project_key}:task:{task_num}",
                                task_label,
                                "sub",
                            ))
                except Exception:  # noqa: BLE001
                    pass
                items.append(CockpitItem(f"project:{project_key}:settings", "  Settings", "sub"))

        for project_key, project in active_projects:
            _add_project_items(project_key, project)
        if active_projects and inactive_projects:
            items.append(CockpitItem("_separator", "", "separator", selectable=False))
        for project_key, project in inactive_projects:
            _add_project_items(project_key, project)

        items.append(CockpitItem("settings", "Settings", "config"))
        return items

    def _project_session_map(self, launches) -> dict[str, str]:
        project_session_map: dict[str, str] = {}
        for launch in launches:
            if launch.session.role in {"operator-pm", "heartbeat-supervisor", "triage", "reviewer"}:
                continue
            project_session_map.setdefault(launch.session.project, launch.session.name)
        return project_session_map

    # Alert types that are informational / auto-managed — don't show red triangle
    _SILENT_ALERT_TYPES = frozenset({
        "suspected_loop",      # auto-clears when snapshot changes
        "stabilize_failed",    # stale after successful recovery
        "needs_followup",      # informational, handled by heartbeat
    })

    def _session_state(self, session_name: str, launches, windows, alerts, spinner_index: int) -> str:
        actionable = [
            a for a in alerts
            if a.session_name == session_name and a.alert_type not in self._SILENT_ALERT_TYPES
        ]
        if actionable:
            # Include a short reason so the user knows what's wrong
            top = actionable[0]
            short_reason = top.alert_type.replace("_", " ")
            return f"! {short_reason}"
        launch = next((item for item in launches if item.session.name == session_name), None)
        if launch is None:
            return "idle"
        window_map = {window.name: window for window in windows}
        window = window_map.get(launch.window_name)
        # If the session is mounted in the cockpit, its storage window is gone.
        # Check the cockpit right pane instead.
        if window is None:
            state = self._load_state()
            if state.get("mounted_session") == session_name:
                window = self._mounted_window_proxy(launch, windows)
        if window is None:
            return "idle"
        if window.pane_dead:
            return "dead"
        spinners = ["\u25dc", "\u25dd", "\u25de", "\u25df"]
        if launch.session.role in ("worker", "operator-pm", "reviewer"):
            working = self._is_pane_working(window, launch.session.provider)
            if working:
                return spinners[spinner_index % 4] + " working"
            if launch.session.role == "worker":
                return "\u25cf live"
            return "ready"
        if launch.session.role == "heartbeat-supervisor":
            return "watch"
        if launch.session.role == "triage":
            return "triage"
        return "live"

    def _mounted_window_proxy(self, launch, windows):
        """Return a window-like object for a session mounted in the cockpit pane."""
        cockpit_windows = [w for w in windows if w.name == self._COCKPIT_WINDOW]
        if not cockpit_windows:
            return None
        # The cockpit window has multiple panes; the right pane is the mounted session.
        try:
            supervisor = self._load_supervisor()
            target = f"{supervisor.config.project.tmux_session}:{self._COCKPIT_WINDOW}"
            panes = self.tmux.list_panes(target)
            if len(panes) < 2:
                return None
            right_pane = max(panes, key=self._pane_left)
            # Return the cockpit window but with the right pane's info
            cockpit_win = cockpit_windows[0]
            from dataclasses import replace as dc_replace
            return dc_replace(
                cockpit_win,
                pane_id=right_pane.pane_id,
                pane_current_command=right_pane.pane_current_command,
                pane_dead=right_pane.pane_dead,
            )
        except Exception:  # noqa: BLE001
            return None

    def _is_pane_working(self, window, provider) -> bool:
        """Check if a session pane has an active turn (agent is working, not idle at prompt)."""
        try:
            pane_text = self.tmux.capture_pane(window.pane_id, lines=15)
        except Exception:  # noqa: BLE001
            return False
        stripped = pane_text.rstrip()
        if not stripped:
            return False
        tail = stripped[-200:]
        lowered = tail.lower()
        # Universal working indicator — both Claude and Codex show this during active turns
        if "esc to interrupt" in lowered:
            return True
        # Universal idle indicators — if any are present, the session is idle regardless of provider
        idle_markers = (
            "bypass permissions", "new task?", "/clear to save", "shift+tab to cycle",  # Claude
            "press enter to confirm", "% left",  # Codex
        )
        if any(marker in lowered for marker in idle_markers):
            return False
        # Provider-specific prompt detection
        provider_value = provider.value if hasattr(provider, "value") else str(provider)
        if provider_value == "claude":
            return "\u276f" not in tail
        if provider_value == "codex":
            # Codex idle: › prompt
            if "\u203a" in tail:
                return False
            return bool(stripped)
        return False

    def _cleanup_duplicate_windows(self, storage_session: str) -> None:
        """Kill duplicate windows in the storage closet, keeping only the first of each name."""
        try:
            windows = self.tmux.list_windows(storage_session)
        except Exception:  # noqa: BLE001
            return
        seen: dict[str, int] = {}  # name -> first index
        for window in windows:
            if window.name in seen:
                try:
                    self.tmux.kill_window(f"{storage_session}:{window.index}")
                except Exception:  # noqa: BLE001
                    pass
            else:
                seen[window.name] = window.index

    def ensure_cockpit_layout(self) -> None:
        self._validate_state()
        config = load_config(self.config_path)
        # Clean up duplicate windows in the storage closet before layout setup
        try:
            supervisor = self._load_supervisor()
            self._cleanup_duplicate_windows(supervisor.storage_closet_session_name())
        except Exception:  # noqa: BLE001
            pass
        target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        panes = self.tmux.list_panes(target)
        state = self._load_state()
        right_pane_id = state.get("right_pane_id")
        right_pane_present = isinstance(right_pane_id, str) and any(pane.pane_id == right_pane_id for pane in panes)
        if len(panes) == 1 and right_pane_present and panes[0].pane_id == right_pane_id:
            # The rail (left) pane died, only the worker (right) pane survived.
            # Park the worker back to storage and clear stale state so the
            # split below creates a fresh right pane from the cockpit pane.
            supervisor = self._load_supervisor()
            mounted = state.get("mounted_session")
            if isinstance(mounted, str) and mounted:
                launch = next(
                    (item for item in supervisor.plan_launches() if item.session.name == mounted),
                    None,
                )
                storage_session = supervisor.storage_closet_session_name()
                if launch is not None and self.tmux.has_session(storage_session):
                    try:
                        self.tmux.break_pane(panes[0].pane_id, storage_session, launch.window_name)
                    except Exception:  # noqa: BLE001
                        pass
            state.pop("right_pane_id", None)
            state.pop("mounted_session", None)
            self._write_state(state)
            try:
                panes = self.tmux.list_panes(target)
            except Exception:  # noqa: BLE001
                panes = []
            right_pane_id = None
            right_pane_present = False
        if len(panes) < 2:
            # Calculate right pane size so the rail starts at exactly _LEFT_PANE_WIDTH
            # columns — avoids the visible flash of a 50/50 split followed by resize.
            window_width = panes[0].pane_width if panes else 200
            right_size = max(window_width - self._LEFT_PANE_WIDTH - 1, 40)
            right_pane_id = self.tmux.split_window(
                target,
                self._right_pane_command("polly"),
                horizontal=True,
                detached=True,
                size=right_size,
            )
            state["right_pane_id"] = right_pane_id
            self._write_state(state)
            panes = self.tmux.list_panes(target)
        elif len(panes) > 2:
            for pane in panes:
                if pane.pane_id == panes[0].pane_id:
                    continue
                try:
                    self.tmux.kill_pane(pane.pane_id)
                except Exception:  # noqa: BLE001
                    pass
            panes = self.tmux.list_panes(target)
        if len(panes) >= 2 and (not right_pane_present or len(panes) != 2):
            self._normalize_layout(target, panes)
            panes = self.tmux.list_panes(target)
        if len(panes) >= 2:
            self._normalize_layout(target, panes)
            panes = self.tmux.list_panes(target)
            left_pane = min(panes, key=self._pane_left)
            state["right_pane_id"] = max(panes, key=self._pane_left).pane_id
            self._write_state(state)
            self._try_resize_rail(left_pane.pane_id)

    def _pane_left(self, pane) -> int:
        return int(getattr(pane, "pane_left", 0))

    def _try_resize_rail(self, pane_id: str) -> None:
        """Best-effort resize of the rail pane. Never raises."""
        try:
            self.tmux.resize_pane_width(pane_id, self._LEFT_PANE_WIDTH)
        except Exception:  # noqa: BLE001
            pass

    def _right_pane_size(self, window_target: str) -> int | None:
        """Calculate the exact right-pane size so the rail starts at _LEFT_PANE_WIDTH columns."""
        try:
            panes = self.tmux.list_panes(window_target)
            if panes:
                window_width = max(p.pane_width for p in panes)
                return max(window_width - self._LEFT_PANE_WIDTH - 1, 40)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _normalize_layout(self, target: str, panes) -> None:
        if len(panes) != 2:
            return
        left_pane = min(panes, key=self._pane_left)
        right_pane = max(panes, key=self._pane_left)
        left_command = getattr(left_pane, "pane_current_command", "")
        right_command = getattr(right_pane, "pane_current_command", "")
        if left_command == "uv":
            return
        if right_command == "uv":
            self.tmux.swap_pane(right_pane.pane_id, left_pane.pane_id)

    def _right_pane_id(self, target: str) -> str | None:
        panes = self.tmux.list_panes(target)
        if len(panes) < 2:
            return None
        return max(panes, key=self._pane_left).pane_id

    def _left_pane_id(self, target: str) -> str | None:
        panes = self.tmux.list_panes(target)
        if not panes:
            return None
        return min(panes, key=self._pane_left).pane_id

    def route_selected(self, key: str) -> None:
        supervisor = self._load_supervisor()
        window_target = f"{supervisor.config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        self.ensure_cockpit_layout()
        right_pane = self._right_pane_id(window_target)
        if right_pane is None:
            raise RuntimeError("Cockpit right pane is not available.")

        self.set_selected_key(key)
        if key == "polly":
            # Launch operator session if not running
            launches = supervisor.plan_launches()
            storage_session = supervisor.storage_closet_session_name()
            op_launch = next((l for l in launches if l.session.name == "operator"), None)
            if op_launch is not None:
                storage_windows = {w.name for w in self.tmux.list_windows(storage_session)}
                if op_launch.window_name not in storage_windows:
                    try:
                        supervisor.launch_session("operator")
                    except Exception:
                        pass
            try:
                self._show_live_session(supervisor, "operator", window_target)
            except Exception:
                self._show_static_view(supervisor, window_target, "polly")
            return
        if key == "russell":
            # Launch reviewer session if not running
            launches = supervisor.plan_launches()
            storage_session = supervisor.storage_closet_session_name()
            rev_launch = next((l for l in launches if l.session.name == "reviewer"), None)
            if rev_launch is not None:
                storage_windows = {w.name for w in self.tmux.list_windows(storage_session)}
                if rev_launch.window_name not in storage_windows:
                    try:
                        supervisor.launch_session("reviewer")
                    except Exception:
                        pass
            try:
                self._show_live_session(supervisor, "reviewer", window_target)
            except Exception:
                self._show_static_view(supervisor, window_target, "polly")
            return
        if key == "inbox":
            self._show_static_view(supervisor, window_target, "inbox")
            return
        if key == "settings":
            self._show_static_view(supervisor, window_target, "settings")
            return
        if key.startswith("project:"):
            parts = key.split(":")
            project_key = parts[1]
            sub_view = parts[2] if len(parts) > 2 else None
            if sub_view is None or sub_view == "dashboard":
                # Clicking project parent or Dashboard — show dashboard,
                # and set selection to the dashboard sub-item
                self.set_selected_key(f"project:{project_key}:dashboard")
                self._show_static_view(supervisor, window_target, "project", project_key)
                return
            if sub_view in ("settings", "issues"):
                self._show_static_view(supervisor, window_target, sub_view, project_key)
                return
            if sub_view == "task" and len(parts) > 3:
                # Per-task worker session — mount it from storage closet
                task_num = parts[3]
                window_name = f"task-{project_key}-{task_num}"
                storage = supervisor.storage_closet_session_name()
                try:
                    storage_windows = self.tmux.list_windows(storage)
                    target_win = next((w for w in storage_windows if w.name == window_name), None)
                    if target_win is not None:
                        self._park_mounted_session(supervisor, window_target)
                        self._cleanup_extra_panes(window_target)
                        left_pane = self._left_pane_id(window_target)
                        right_pane_id = self._right_pane_id(window_target)
                        if right_pane_id is not None:
                            self.tmux.kill_pane(right_pane_id)
                        source = f"{storage}:{target_win.index}.0"
                        self.tmux.join_pane(source, left_pane, horizontal=True)
                        panes = self.tmux.list_panes(window_target)
                        left_p = min(panes, key=self._pane_left)
                        self._try_resize_rail(left_p.pane_id)
                        right_p = max(panes, key=self._pane_left)
                        self.tmux.set_pane_history_limit(right_p.pane_id, 200)
                        state = self._load_state()
                        state["mounted_session"] = window_name
                        state["right_pane_id"] = right_p.pane_id
                        self._write_state(state)
                        return
                except Exception:  # noqa: BLE001
                    pass
                # Fallback to dashboard
                self.set_selected_key(f"project:{project_key}:dashboard")
                self._show_static_view(supervisor, window_target, "project", project_key)
                return
            if sub_view == "session":
                # PM Chat — mount live session, spawning if needed
                launches = supervisor.plan_launches()
                session_name = self._project_session_map(launches).get(project_key)
                if session_name is not None:
                    if not self._session_available_for_mount(supervisor, session_name, window_target):
                        # Session exists in config but not running — launch it
                        try:
                            supervisor.launch_session(session_name)
                        except Exception:
                            pass
                    try:
                        self._show_live_session(supervisor, session_name, window_target)
                    except Exception:
                        # Mount failed (e.g. duplicate windows) — fall back to dashboard
                        self.set_selected_key(f"project:{project_key}:dashboard")
                        self._show_static_view(supervisor, window_target, "project", project_key)
                else:
                    # No session configured — try to create one
                    try:
                        self.create_worker_and_route(project_key)
                    except Exception:
                        # Creation failed (e.g. no git commits) — fall back to dashboard
                        self.set_selected_key(f"project:{project_key}:dashboard")
                        self._show_static_view(supervisor, window_target, "project", project_key)
                return
            # Unknown sub_view — fall back to dashboard
            self.set_selected_key(f"project:{project_key}:dashboard")
            self._show_static_view(supervisor, window_target, "project", project_key)
            return
        raise RuntimeError(f"Unknown cockpit item: {key}")

    def focus_right_pane(self) -> None:
        config = load_config(self.config_path)
        window_target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        self.ensure_cockpit_layout()
        right_pane = self._right_pane_id(window_target)
        if right_pane is not None:
            self.tmux.select_pane(right_pane)

    def create_worker_and_route(
        self,
        project_key: str,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        supervisor = self._load_supervisor()
        launches = supervisor.plan_launches()
        session_name = self._project_session_map(launches).get(project_key)
        storage_session = supervisor.storage_closet_session_name()
        storage_windows = {w.name for w in self.tmux.list_windows(storage_session)}

        target: str | None = None
        if session_name is not None:
            launch = next(l for l in launches if l.session.name == session_name)
            if launch.window_name not in storage_windows:
                _launch, target = supervisor.create_session_window(session_name, on_status=on_status)
        else:
            prompt = self.service.suggest_worker_prompt(project_key=project_key)
            self.service.create_and_launch_worker(
                project_key=project_key, prompt=prompt, on_status=on_status, skip_stabilize=True,
            )
            # Re-read launches to pick up the newly created session
            supervisor = self._load_supervisor(fresh=True)
            launches = supervisor.plan_launches()
            session_name = self._project_session_map(launches).get(project_key)
            if session_name is not None:
                launch = next(l for l in launches if l.session.name == session_name)
                tmux_session = supervisor._tmux_session_for_launch(launch)
                window_map = supervisor._window_map()
                if launch.window_name in window_map:
                    target_key = f"{tmux_session}:{launch.window_name}"
                    # Window exists but hasn't been stabilized yet
                    target = target_key

        # Route immediately so the user sees the session booting live
        self.route_selected(f"project:{project_key}")

        # Stabilize in the background (dismisses prompts, waits for ready)
        if target is not None and session_name is not None:
            launch = next(l for l in supervisor.plan_launches() if l.session.name == session_name)
            supervisor._stabilize_launch(launch, target, on_status=on_status)

    def _show_live_session(self, supervisor: Supervisor, session_name: str, window_target: str) -> None:
        mounted_session = self._mounted_session_name(supervisor, window_target)
        launch = next(item for item in supervisor.plan_launches() if item.session.name == session_name)
        if isinstance(mounted_session, str) and mounted_session == session_name:
            right_pane_id = self._right_pane_id(window_target)
            if right_pane_id is not None:
                panes = self.tmux.list_panes(window_target)
                right_pane = max(panes, key=self._pane_left)
                if self._is_live_provider_pane(right_pane):
                    return
        self._park_mounted_session(supervisor, window_target)
        self._cleanup_extra_panes(window_target)
        left_pane_id = self._left_pane_id(window_target)
        if left_pane_id is None:
            raise RuntimeError("Cockpit left pane is not available.")
        right_pane_id = self._right_pane_id(window_target)
        storage_session = supervisor.storage_closet_session_name()
        storage_windows = {window.name for window in self.tmux.list_windows(storage_session)}
        if launch.window_name not in storage_windows:
            # Control sessions (operator, reviewer) should be respawned
            # when missing — the user clicking on Polly expects to talk to
            # Polly, not see a placeholder.
            if launch.session.role in {"operator-pm", "reviewer"}:
                try:
                    supervisor.launch_session(session_name)
                    storage_windows = {w.name for w in self.tmux.list_windows(storage_session)}
                    if launch.window_name in storage_windows:
                        if right_pane_id is not None:
                            self.tmux.kill_pane(right_pane_id)
                        source = f"{storage_session}:{launch.window_name}.0"
                        self.tmux.join_pane(source, left_pane_id, horizontal=True)
                        panes = self.tmux.list_panes(window_target)
                        left_pane = min(panes, key=self._pane_left)
                        self._try_resize_rail(left_pane.pane_id)
                        right_pane = max(panes, key=self._pane_left)
                        self.tmux.set_pane_history_limit(right_pane.pane_id, 200)
                        state = self._load_state()
                        state["mounted_session"] = session_name
                        state["right_pane_id"] = right_pane.pane_id
                        self._write_state(state)
                        return
                except Exception:  # noqa: BLE001
                    pass  # Fall through to static view if relaunch fails
            # Non-control sessions or failed relaunch — show static view
            fallback_kind = "polly" if session_name == "operator" else "project"
            fallback_target = launch.session.project if fallback_kind == "project" else None
            if right_pane_id is None:
                right_size = self._right_pane_size(window_target)
                right_pane_id = self.tmux.split_window(
                    left_pane_id,
                    self._right_pane_command(fallback_kind, fallback_target),
                    horizontal=True,
                    detached=True,
                    size=right_size,
                )
            else:
                self.tmux.respawn_pane(right_pane_id, self._right_pane_command(fallback_kind, fallback_target))
            state = self._load_state()
            state.pop("mounted_session", None)
            state["right_pane_id"] = self._right_pane_id(window_target)
            self._write_state(state)
            return
        if right_pane_id is not None:
            self.tmux.kill_pane(right_pane_id)
        # Use window index to avoid ambiguity with duplicate window names
        storage_windows = self.tmux.list_windows(storage_session)
        target_window = next(
            (w for w in storage_windows if w.name == launch.window_name),
            None,
        )
        if target_window is None:
            self._show_static_view(supervisor, window_target, "polly" if session_name == "operator" else "project")
            return
        source = f"{storage_session}:{target_window.index}.0"
        self.tmux.join_pane(source, left_pane_id, horizontal=True)
        panes = self.tmux.list_panes(window_target)
        left_pane = min(panes, key=self._pane_left)
        self._try_resize_rail(left_pane.pane_id)
        right_pane = max(panes, key=self._pane_left)
        self.tmux.set_pane_history_limit(right_pane.pane_id, 200)
        state = self._load_state()
        state["mounted_session"] = session_name
        state["right_pane_id"] = right_pane.pane_id
        self._write_state(state)
        # Auto-claim a cockpit lease so the heartbeat won't send
        # nudges while a human is viewing/typing in this session.
        try:
            supervisor.claim_lease(session_name, "cockpit", "mounted in cockpit")
        except Exception:  # noqa: BLE001
            pass  # Lease may conflict — best effort

    def _should_boot_visible(self, launch) -> bool:
        if launch.session.name in self._ui_initialized_sessions():
            return False
        return launch.session.provider.value in {"claude", "codex"}

    def _launch_visible_session(self, supervisor: Supervisor, launch, window_target: str, left_pane_id: str, right_pane_id: str | None):
        storage_session = supervisor.storage_closet_session_name()
        for window in self.tmux.list_windows(storage_session):
            if window.name == launch.window_name:
                self.tmux.kill_window(f"{storage_session}:{window.index}")
                break
        visible_launch = launch
        if launch.session.provider.value == "codex" and launch.initial_input:
            provider = get_provider(launch.session.provider, root_dir=supervisor.config.project.root_dir)
            runtime = get_runtime(launch.account.runtime, root_dir=supervisor.config.project.root_dir)
            visible_command = provider.build_launch_command(launch.session, launch.account)
            visible_launch = replace(
                launch,
                command=runtime.wrap_command(visible_command, launch.account, supervisor.config.project),
                initial_input=visible_command.initial_input,
                resume_marker=visible_command.resume_marker,
                fresh_launch_marker=visible_command.fresh_launch_marker,
            )
        if right_pane_id is not None:
            self.tmux.kill_pane(right_pane_id)
        right_size = self._right_pane_size(window_target)
        right_pane_id = self.tmux.split_window(
            left_pane_id,
            visible_launch.command,
            horizontal=True,
            detached=False,
            size=right_size,
        )
        self.tmux.set_pane_history_limit(right_pane_id, 200)
        self.tmux.pipe_pane(right_pane_id, visible_launch.log_path)
        supervisor._stabilize_launch(visible_launch, right_pane_id)
        return max(self.tmux.list_panes(window_target), key=self._pane_left)

    def _park_mounted_session(self, supervisor: Supervisor, window_target: str) -> None:
        state = self._load_state()
        mounted_session = self._mounted_session_name(supervisor, window_target)
        if not isinstance(mounted_session, str) or not mounted_session:
            return
        right_pane_id = self._right_pane_id(window_target)
        if right_pane_id is None:
            state.pop("mounted_session", None)
            self._write_state(state)
            self._release_cockpit_lease(supervisor, mounted_session)
            return
        right_pane = max(self.tmux.list_panes(window_target), key=self._pane_left)
        if not self._is_live_provider_pane(right_pane):
            state.pop("mounted_session", None)
            self._write_state(state)
            self._release_cockpit_lease(supervisor, mounted_session)
            return
        launch = next(item for item in supervisor.plan_launches() if item.session.name == mounted_session)
        storage_session = supervisor.storage_closet_session_name()
        before = {(window.index, window.name) for window in self.tmux.list_windows(storage_session)}
        self.tmux.break_pane(right_pane_id, storage_session, launch.window_name)
        after = self.tmux.list_windows(storage_session)
        created = [window for window in after if (window.index, window.name) not in before]
        if created:
            self.tmux.rename_window(f"{storage_session}:{created[-1].index}", launch.window_name)
        else:
            for window in after:
                if window.name == self._COCKPIT_WINDOW:
                    self.tmux.rename_window(f"{storage_session}:{window.index}", launch.window_name)
                    break
        state.pop("mounted_session", None)
        self._write_state(state)
        self._release_cockpit_lease(supervisor, mounted_session)

    # Roles that should NEVER be auto-detected as mounted via CWD fallback.
    # These are background roles — if the user is looking at a pane, it's
    # not the heartbeat.  Guessing wrong here causes cascading mis-parks.
    _NEVER_MOUNT_ROLES = frozenset({"heartbeat-supervisor", "triage"})

    # When CWD is ambiguous (multiple sessions share the same cwd), prefer
    # the session the user is most likely interacting with.
    _MOUNT_PRIORITY = {"operator-pm": 0, "reviewer": 1, "worker": 2}

    def _mounted_session_name(self, supervisor: Supervisor, window_target: str) -> str | None:
        state = self._load_state()
        mounted_session = state.get("mounted_session")
        if isinstance(mounted_session, str) and mounted_session:
            return mounted_session
        if not hasattr(self.tmux, "list_panes"):
            return None
        right_pane_id = self._right_pane_id(window_target)
        if right_pane_id is None:
            return None
        panes = self.tmux.list_panes(window_target)
        if len(panes) < 2:
            return None
        right_pane = max(panes, key=self._pane_left)
        if not self._is_live_provider_pane(right_pane):
            return None
        # CWD fallback: find the best matching session, but NEVER guess
        # heartbeat or triage — those are background roles that should
        # never be mounted in the cockpit.  When multiple sessions share
        # a CWD, prefer operator > reviewer > worker.
        pane_path = str(Path(right_pane.pane_current_path).resolve())
        best_match: tuple[int, str] | None = None  # (priority, session_name)
        for launch in supervisor.plan_launches():
            if launch.session.role in self._NEVER_MOUNT_ROLES:
                continue
            session_cwd = str(Path(launch.session.cwd).resolve())
            if pane_path == session_cwd:
                priority = self._MOUNT_PRIORITY.get(launch.session.role, 5)
                if best_match is None or priority < best_match[0]:
                    best_match = (priority, launch.session.name)
        if best_match is not None:
            state["mounted_session"] = best_match[1]
            state["right_pane_id"] = right_pane.pane_id
            self._write_state(state)
            return best_match[1]
        return None

    def _is_live_provider_pane(self, pane) -> bool:
        cmd = getattr(pane, "pane_current_command", "")
        # Claude Code may report the version string (e.g. "2.1.98") as the
        # current command instead of "claude" or "node".
        if cmd in {"node", "claude", "codex"}:
            return True
        # Treat any version-like string (digits and dots) as a live Claude pane.
        if cmd and all(c.isdigit() or c == "." for c in cmd):
            return True
        return False

    def _session_available_for_mount(self, supervisor: Supervisor, session_name: str, window_target: str) -> bool:
        """Return True only if the session is already running (mounted or in storage)."""
        mounted = self._mounted_session_name(supervisor, window_target)
        if mounted == session_name:
            return True
        launch = next((item for item in supervisor.plan_launches() if item.session.name == session_name), None)
        if launch is None:
            return False
        storage_session = supervisor.storage_closet_session_name()
        storage_windows = {window.name for window in self.tmux.list_windows(storage_session)}
        return launch.window_name in storage_windows

    def _show_static_view(
        self,
        supervisor: Supervisor,
        window_target: str,
        kind: str,
        project_key: str | None = None,
    ) -> None:
        self._park_mounted_session(supervisor, window_target)
        self._cleanup_extra_panes(window_target)
        left_pane_id = self._left_pane_id(window_target)
        if left_pane_id is None:
            raise RuntimeError("Cockpit left pane is not available.")
        right_pane_id = self._right_pane_id(window_target)
        if right_pane_id is None:
            right_pane_id = self.tmux.split_window(
                left_pane_id,
                self._right_pane_command(kind, project_key),
                horizontal=True,
                detached=True,
                size=self._right_pane_size(window_target),
            )
        else:
            self.tmux.respawn_pane(right_pane_id, self._right_pane_command(kind, project_key))
        state = self._load_state()
        state.pop("mounted_session", None)
        state["right_pane_id"] = self._right_pane_id(window_target)
        self._write_state(state)

    def _cleanup_extra_panes(self, window_target: str) -> None:
        """Kill any extra panes beyond the expected 2 (rail + right)."""
        try:
            panes = self.tmux.list_panes(window_target)
        except Exception:  # noqa: BLE001
            return
        if len(panes) <= 2:
            return
        left_pane = min(panes, key=self._pane_left)
        # Keep the leftmost (rail) and rightmost (content) panes, kill the rest
        right_pane = max(panes, key=self._pane_left)
        for pane in panes:
            if pane.pane_id not in {left_pane.pane_id, right_pane.pane_id}:
                try:
                    self.tmux.kill_pane(pane.pane_id)
                except Exception:  # noqa: BLE001
                    pass

    def _right_pane_command(self, kind: str, project_key: str | None = None) -> str:
        root = shlex.quote(str(self.config_path.parent.resolve()))
        import shutil
        pm_cmd = "pm" if shutil.which("pm") else "uv run pm"
        args = [pm_cmd, "cockpit-pane", shlex.quote(kind)]
        if project_key is not None:
            args.append(shlex.quote(project_key))
        joined = " ".join(args)
        return f"sh -lc 'cd {root} && {joined}'"


def _spark_bar(values: list[int], width: int = 30) -> str:
    """Render a mini spark-line bar chart using Unicode block characters."""
    if not values:
        return ""
    max_val = max(values) or 1
    blocks = " ▁▂▃▄▅▆▇█"
    return "".join(blocks[min(8, int(v / max_val * 8))] for v in values)


def _build_dashboard(supervisor, config) -> str:
    from datetime import UTC, datetime, timedelta

    lines: list[str] = []
    now = datetime.now(UTC)

    def _age(ts_str: str) -> str:
        try:
            dt = datetime.fromisoformat(ts_str)
            secs = (now - dt).total_seconds()
            if secs < 60:
                return "just now"
            if secs < 3600:
                return f"{int(secs // 60)}m ago"
            if secs < 86400:
                return f"{int(secs // 3600)}h ago"
            return f"{int(secs // 86400)}d ago"
        except (ValueError, TypeError):
            return ""

    # ── Gather task data across all projects ──
    from pollypm.work.sqlite_service import SQLiteWorkService
    all_active: list[tuple[str, object]] = []  # in_progress
    all_review: list[tuple[str, object]] = []  # waiting for review
    all_queued: list[tuple[str, object]] = []  # ready for pickup
    all_blocked: list[tuple[str, object]] = []
    all_done: list[tuple[str, object]] = []
    total_counts: dict[str, int] = {}
    for pk, proj in config.projects.items():
        db_path = proj.path / ".pollypm" / "state.db"
        if not db_path.exists():
            continue
        try:
            with SQLiteWorkService(db_path=db_path, project_path=proj.path) as svc:
                tasks = svc.list_tasks(project=pk)
                counts = svc.state_counts(project=pk)
                for s, n in counts.items():
                    total_counts[s] = total_counts.get(s, 0) + n
                for t in tasks:
                    sv = t.work_status.value
                    if sv == "in_progress":
                        all_active.append((pk, t))
                    elif sv == "review":
                        all_review.append((pk, t))
                    elif sv == "queued":
                        all_queued.append((pk, t))
                    elif sv == "blocked":
                        all_blocked.append((pk, t))
                    elif sv == "done":
                        all_done.append((pk, t))
        except Exception:  # noqa: BLE001
            pass
    all_done.sort(key=lambda x: x[1].updated_at or "", reverse=True)

    # ── Gather system data ──
    open_alerts = supervisor.store.open_alerts()
    user_inbox = len(list_v2_messages(config.project.root_dir, status="open", owner="user"))
    actionable_alerts = [a for a in open_alerts if a.alert_type not in (
        "suspected_loop", "stabilize_failed", "needs_followup",
    )]
    recent = supervisor.store.recent_events(limit=300)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    day_events = [e for e in recent if e.created_at >= cutoff_24h]

    # ── Header ──
    lines.append("  PollyPM")
    lines.append("")

    # Status line: what needs YOUR attention right now (actionable items only)
    attention: list[str] = []
    if all_review:
        attention.append(f"◉ {len(all_review)} awaiting review")
    if user_inbox:
        attention.append(f"✉ {user_inbox} inbox")
    if actionable_alerts:
        attention.append(f"▲ {len(actionable_alerts)} alert{'s' if len(actionable_alerts) != 1 else ''}")
    if attention:
        lines.append("  " + "  ·  ".join(attention))
        lines.append("")

    # Task count summary
    count_parts = []
    for status in ("in_progress", "review", "queued", "blocked"):
        n = total_counts.get(status, 0)
        if n:
            icon = _STATUS_ICONS.get(status, "·")
            count_parts.append(f"{icon} {n} {status.replace('_', ' ')}")
    done_n = total_counts.get("done", 0)
    if done_n:
        count_parts.append(f"✓ {done_n} done")
    if count_parts:
        lines.append("  " + " · ".join(count_parts))
    lines.append("")

    # ── What's happening right now ──
    if all_active or all_review:
        lines.append("  ─── Now ───────────────────────────────────────────")
        lines.append("")
        for pk, t in all_active:
            project = config.projects.get(pk)
            proj_label = project.display_label() if project else pk
            assignee = f" [{t.assignee}]" if t.assignee else ""
            node = t.current_node_id or ""
            age = _age(t.updated_at) if t.updated_at else ""
            lines.append(f"  ⟳ {t.title}")
            lines.append(f"    {proj_label}{assignee} · {node} · {age}")
            lines.append("")
        for pk, t in all_review:
            project = config.projects.get(pk)
            proj_label = project.display_label() if project else pk
            age = _age(t.updated_at) if t.updated_at else ""
            lines.append(f"  ◉ {t.title}")
            lines.append(f"    {proj_label} · waiting for Russell · {age}")
            lines.append("")

    # ── Queued (ready for pickup) ──
    if all_queued:
        lines.append("  ─── Ready ─────────────────────────────────────────")
        lines.append("")
        for pk, t in all_queued[:5]:
            project = config.projects.get(pk)
            proj_label = project.display_label() if project else pk
            lines.append(f"  ○ {t.title}  ({proj_label})")
        if len(all_queued) > 5:
            lines.append(f"    + {len(all_queued) - 5} more queued")
        lines.append("")

    # ── Recently completed ──
    if all_done:
        lines.append("  ─── Done ──────────────────────────────────────────")
        lines.append("")
        for pk, t in all_done[:8]:
            project = config.projects.get(pk)
            proj_label = project.display_label() if project else pk
            age = _age(t.updated_at) if t.updated_at else ""
            lines.append(f"  ✓ {t.title}  ({proj_label})  {age}")
        if len(all_done) > 8:
            lines.append(f"    + {len(all_done) - 8} more completed")
        lines.append("")

    # ── System activity ──
    lines.append("  ─── Activity ──────────────────────────────────────")
    lines.append("")
    commits = [e for e in day_events if "commit" in e.message.lower()]
    recoveries = [e for e in day_events if e.event_type in ("recover", "recovery", "stabilize_failed")]
    sends = [e for e in day_events if e.event_type == "send_input"]
    task_events = [e for e in day_events if e.event_type in ("launch", "send_input", "auth_sync")]
    activity_parts = []
    if commits:
        activity_parts.append(f"{len(commits)} commits")
    if sends:
        activity_parts.append(f"{len(sends)} messages")
    if recoveries:
        activity_parts.append(f"{len(recoveries)} recoveries")
    if activity_parts:
        lines.append("  Today: " + " · ".join(activity_parts))
    else:
        lines.append("  No notable activity today.")
    lines.append("")

    # Show last few notable events with timestamps
    notable = [e for e in day_events if e.event_type not in ("heartbeat", "token_ledger", "polly_followup")][:6]
    for event in notable:
        age = _age(event.created_at)
        session = event.session_name
        msg = event.message[:55]
        lines.append(f"  {age:>8}  {session}: {msg}")
    if notable:
        lines.append("")

    # ── Alerts (if any) ──
    if actionable_alerts:
        lines.append("  ─── Alerts ────────────────────────────────────────")
        lines.append("")
        for alert in actionable_alerts[:5]:
            lines.append(f"  ▲ {alert.session_name}: {alert.message[:55]}")
        lines.append("")

    # ── Footer ──
    project_count = len(config.projects)
    lines.append(f"  {project_count} projects  ·  j/k navigate  ·  S settings")

    return "\n".join(lines)


def build_cockpit_detail(config_path: Path, kind: str, target: str | None = None) -> str:
    try:
        return _build_cockpit_detail_inner(config_path, kind, target)
    except Exception as exc:  # noqa: BLE001
        return f"Error loading {kind} view: {exc}"


def _build_cockpit_detail_inner(config_path: Path, kind: str, target: str | None = None) -> str:
    supervisor = Supervisor(load_config(config_path))
    try:
        supervisor.ensure_layout()
        return _build_cockpit_detail_dispatch(supervisor, config_path, kind, target)
    finally:
        try:
            supervisor.store.close()
        except Exception:  # noqa: BLE001
            pass


def _build_cockpit_detail_dispatch(supervisor, config_path: Path, kind: str, target: str | None = None) -> str:
    config = supervisor.config
    if kind in ("polly", "dashboard"):
        return _build_dashboard(supervisor, config)

    if kind == "inbox":
        from pollypm.inbox_processor import list_decisions
        messages = list_v2_messages(config.project.root_dir, status="open")
        archived = list_v2_messages(config.project.root_dir, status="closed")
        decisions = list_decisions(config.project.root_dir, limit=5)

        lines = ["Inbox"]
        if not messages and not decisions:
            lines.extend(["", "No open messages or recent decisions."])
        else:
            if messages:
                lines.extend(["", f"Open ({len(messages)}):"])
                for msg in messages[:10]:
                    prefix = ""
                    if "[Escalation]" in msg.subject:
                        prefix = "▲ "
                    elif "[Decision]" in msg.subject:
                        prefix = "◆ "
                    lines.append(f"  {prefix}{msg.subject}")
                    lines.append(f"    from {msg.sender} · {msg.created_at[:16]}")
                    # Show first line of body from entries
                    try:
                        _ctx, _hist, entries = read_v2_message(config.project.root_dir, msg.id)
                        first_line = entries[0].body.strip().split("\n")[0][:70] if entries else ""
                    except Exception:  # noqa: BLE001
                        first_line = ""
                    if first_line:
                        lines.append(f"    {first_line}")
                    lines.append("")

            if decisions:
                lines.extend(["", f"Recent Decisions ({len(decisions)}):"])
                for dec in decisions[:5]:
                    tier = dec.get("tier", 2)
                    icon = "◆" if tier <= 2 else "▲"
                    lines.append(f"  {icon} {dec.get('subject', '?')[:60]}")
                    if dec.get("decision"):
                        lines.append(f"    {dec['decision'][:65]}")
                    lines.append(f"    from {dec.get('original_sender', '?')} · {dec.get('timestamp', '')[:16]}")
                    lines.append("")

        if archived:
            lines.extend([f"Archived: {len(archived)} message(s)", "  View with: pm mail --archived"])

        lines.extend([
            "",
            "Read: pm mail <message-id>",
            "Archive: pm mail --close <filename>",
            "Decisions: pm decisions",
        ])
        return "\n".join(lines)

    if kind == "settings":
        recent_usage = supervisor.store.recent_token_usage(limit=5)
        lines = [
            "Settings",
            "",
            f"Workspace root: {config.project.workspace_root}",
            f"Control account: {config.pollypm.controller_account}",
            f"Failover order: {', '.join(config.pollypm.failover_accounts) or 'none'}",
            f"Open permissions by default: {'on' if config.pollypm.open_permissions_by_default else 'off'}",
            "",
            "This pane is read-only for now.",
            "Use Polly or the legacy `pm ui` surface for deeper account/runtime changes.",
        ]
        if recent_usage:
            lines.extend(["", "Recent token usage:"])
            for row in recent_usage[:4]:
                lines.append(
                    f"- {row.project_key} · {row.account_name} · {row.model_name} · {row.tokens_used} tokens"
                )
        return "\n".join(lines)

    if kind == "project" and target:
        project = config.projects.get(target)
        if project is None:
            return f"Project '{target}' not found in config.\n\nIt may not have been saved. Try `pm add-project <path>` or check ~/.pollypm/pollypm.toml."
        ensure_project_scaffold(project.path)

        # Try work service dashboard first
        try:
            dashboard = _render_project_dashboard(project, target, config_path, supervisor)
            if dashboard:
                return dashboard
        except Exception:
            pass

        # Fallback to basic project info
        task_backend = get_task_backend(project.path)
        issues_root = task_backend.issues_root()
        state_counts = task_backend.state_counts() if task_backend.exists() else {}
        worktrees = [item for item in list_worktrees(config_path, target) if item.status == "active"]
        lines = [
            f"{project.name or project.key}",
            "",
            f"Path: {project.path}",
            f"Kind: {project.kind.value}",
            f"Tracked: {'yes' if project.tracked else 'no'}",
            f"Issue tracker: {issues_root if task_backend.exists() else 'not initialized'}",
            f"Active worktrees: {len(worktrees)}",
            "",
            "No active live lane is running for this project.",
            "Select the project in the left rail and press N to start a worker lane.",
        ]
        # Show alerts for this project's sessions
        project_alerts = [
            a for a in supervisor.store.open_alerts()
            if any(
                l.session.project == target and l.session.name == a.session_name
                for l in supervisor.plan_launches()
            ) and a.alert_type not in ("suspected_loop", "stabilize_failed", "needs_followup")
        ]
        if project_alerts:
            lines.extend(["", "⚠ Alerts:"])
            for a in project_alerts:
                lines.append(f"  {a.severity} {a.alert_type}: {a.message}")
            lines.append("")
        if state_counts:
            lines.extend(["Task states:"])
            for state, count in state_counts.items():
                if count:
                    lines.append(f"- {state}: {count}")
        return "\n".join(lines)

    if kind == "issues" and target:
        project = config.projects.get(target)
        if not project:
            return f"Project '{target}' not found."
        # Try the work service first, fall back to file-based backend
        try:
            return _render_work_service_issues(project)
        except Exception:
            pass
        task_backend = get_task_backend(project.path)
        if not task_backend.exists():
            return f"{project.name or project.key} · Issues\n\nNo issue tracker initialized.\nUse `pm init-tracker {target}` to create one."
        state_counts = task_backend.state_counts()
        lines = [f"{project.name or project.key} · Issues", ""]
        for state_name in ["01-ready", "02-in-progress", "03-needs-review", "04-in-review", "05-completed"]:
            count = state_counts.get(state_name, 0)
            if count:
                tasks = task_backend.list_tasks(states=[state_name])
                lines.append(f"─── {state_name} ({count}) ───")
                for task in tasks[:8]:
                    lines.append(f"  {task.task_id}: {task.title}")
                lines.append("")
        if not any(state_counts.values()):
            lines.append("No issues found.")
        return "\n".join(lines)

    return "PollyPM\n\nSelect Polly, Inbox, a project, or Settings from the left rail."
