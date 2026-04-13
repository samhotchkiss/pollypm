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
from pollypm.tmux.client import TmuxClient
from pollypm.worktrees import list_worktrees


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
        self.tmux = TmuxClient()
        self._supervisor: Supervisor | None = None

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
        pane_path = getattr(pane, "pane_current_path", "")
        session_cwd = getattr(launch.session, "cwd", None)
        if not pane_path or session_cwd is None:
            return False
        try:
            return Path(pane_path).resolve() == Path(session_cwd).resolve()
        except Exception:  # noqa: BLE001
            return False

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
        user_inbox = len(list_v2_messages(config.project.root_dir, status="open", owner="user"))
        items = [
            CockpitItem("polly", "Polly", self._session_state("operator", launches, windows, alerts, spinner_index)),
            CockpitItem("inbox", f"Inbox ({user_inbox})", "mail" if user_inbox else "clear"),
        ]

        selected = self.selected_key()
        project_session_map = self._project_session_map(launches)
        for project_key, project in config.projects.items():
            session_name = project_session_map.get(project_key)
            if session_name is None:
                state = "idle"
            else:
                state = self._session_state(session_name, launches, windows, alerts, spinner_index)
            items.append(CockpitItem(f"project:{project_key}", project.display_label(), state))
            # Unfold sub-items for the selected project
            if selected.startswith(f"project:{project_key}"):
                items.append(CockpitItem(f"project:{project_key}:settings", "  Settings", "sub"))
                items.append(CockpitItem(f"project:{project_key}:issues", "  Issues", "sub"))

        items.append(CockpitItem("settings", "Settings", "config"))
        return items

    def _project_session_map(self, launches) -> dict[str, str]:
        project_session_map: dict[str, str] = {}
        for launch in launches:
            if launch.session.role in {"operator-pm", "heartbeat-supervisor", "triage"}:
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
        if launch.session.role in ("worker", "operator-pm"):
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

    def ensure_cockpit_layout(self) -> None:
        self._validate_state()
        config = load_config(self.config_path)
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
            self._show_live_session(supervisor, "operator", window_target)
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
            if sub_view in ("settings", "issues"):
                self._show_static_view(supervisor, window_target, sub_view, project_key)
                return
            launches = supervisor.plan_launches()
            session_name = self._project_session_map(launches).get(project_key)
            if session_name is not None and self._session_available_for_mount(supervisor, session_name, window_target):
                self._show_live_session(supervisor, session_name, window_target)
            else:
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
            # Session is not running — show a static view instead of
            # blocking the UI with a synchronous relaunch attempt.
            self._show_static_view(supervisor, window_target, "polly" if session_name == "operator" else "project")
            return
        if False:  # dead code — kept for reference
            if launch.session.role in {"operator-pm", "heartbeat-supervisor"}:
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
                        self.tmux.set_pane_history_limit(right_pane.pane_id, 500)
                        state = self._load_state()
                        state["mounted_session"] = session_name
                        state["right_pane_id"] = right_pane.pane_id
                        self._write_state(state)
                        return
                except Exception:  # noqa: BLE001
                    pass
            # Fall back to static detail view
            fallback_kind = "polly" if launch.session.role in {"operator-pm", "heartbeat-supervisor"} else "project"
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
        source = f"{storage_session}:{launch.window_name}.0"
        self.tmux.join_pane(source, left_pane_id, horizontal=True)
        panes = self.tmux.list_panes(window_target)
        left_pane = min(panes, key=self._pane_left)
        self._try_resize_rail(left_pane.pane_id)
        right_pane = max(panes, key=self._pane_left)
        self.tmux.set_pane_history_limit(right_pane.pane_id, 500)
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
        self.tmux.set_pane_history_limit(right_pane_id, 500)
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
        for launch in supervisor.plan_launches():
            session_cwd = str(Path(launch.session.cwd).resolve())
            pane_path = str(Path(right_pane.pane_current_path).resolve())
            if pane_path == session_cwd:
                state["mounted_session"] = launch.session.name
                state["right_pane_id"] = right_pane.pane_id
                self._write_state(state)
                return launch.session.name
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
        args = [pm_cmd, "cockpit-pane", kind]
        if project_key is not None:
            args.append(project_key)
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

    # ── Header ──
    project_count = len(config.projects)
    session_count = len(config.sessions)
    open_alerts = supervisor.store.open_alerts()
    user_inbox = len(list_v2_messages(config.project.root_dir, status="open", owner="user"))
    actionable_alerts = [a for a in open_alerts if a.alert_type not in (
        "suspected_loop", "stabilize_failed", "needs_followup",
    )]

    lines.append("  PollyPM Dashboard")
    lines.append("")
    status_parts = [f"{project_count} projects", f"{session_count} sessions"]
    if actionable_alerts:
        status_parts.append(f"{len(actionable_alerts)} alert(s)")
    if user_inbox:
        status_parts.append(f"{user_inbox} inbox")
    lines.append("  " + "  ·  ".join(status_parts))
    lines.append("")

    # ── Currently Working On ──
    lines.append("  ─── Active Work ───────────────────────────────────")
    lines.append("")
    all_runtimes = supervisor.store.list_session_runtimes()
    runtime_map = {rt.session_name: rt for rt in all_runtimes}
    launches = supervisor.plan_launches()
    has_active = False
    for launch in launches:
        if launch.session.role in ("heartbeat-supervisor",):
            continue
        rt = runtime_map.get(launch.session.name)
        status = rt.status if rt else "unknown"
        project = config.projects.get(launch.session.project)
        project_label = project.display_label() if project else launch.session.project

        if status in ("healthy", "needs_followup", "waiting_on_user"):
            icon = "●" if status == "healthy" else "◆" if status == "needs_followup" else "◇"
            role_label = "Polly" if launch.session.role == "operator-pm" else project_label
            reason = (rt.last_failure_message or "")[:60] if rt else ""
            # Get a useful snippet from the session's latest status reason
            status_reason = ""
            if rt and hasattr(rt, "updated_at") and rt.updated_at:
                try:
                    age = (now - datetime.fromisoformat(rt.updated_at)).total_seconds()
                    if age < 300:
                        status_reason = " (active)"
                    elif age < 3600:
                        status_reason = f" ({int(age // 60)}m ago)"
                except (ValueError, TypeError):
                    pass
            status_label = {"healthy": "working", "needs_followup": "in progress", "waiting_on_user": "waiting on you"}
            lines.append(f"  {icon} {role_label}: {status_label.get(status, status)}{status_reason}")
            has_active = True
    if not has_active:
        lines.append("  No active sessions.")
    lines.append("")

    # ── Agent Workqueue ──
    agent_items = list_v2_messages(config.project.root_dir, status="open", owner="polly")
    if agent_items:
        lines.append("  ─── Agent Workqueue ───────────────────────────────")
        lines.append("")
        import time
        spinners = ["◜", "◝", "◞", "◟"]
        spin_idx = int(time.time()) % 4
        for item in agent_items[:5]:
            # Show spinner if the operator is actively working (it might be on this item)
            op_rt = runtime_map.get("operator")
            if op_rt and op_rt.status in ("healthy", "needs_followup"):
                spin = spinners[spin_idx]
                lines.append(f"  {spin} {item.subject[:55]}")
            else:
                lines.append(f"  ◆ {item.subject[:55]}")
            lines.append(f"    from {item.sender} · {_fmt_time(item.created_at)}")
        if len(agent_items) > 5:
            lines.append(f"  ... and {len(agent_items) - 5} more")
        lines.append("")

    # ── Recent Activity (last 24h) ──
    lines.append("  ─── Last 24 Hours ─────────────────────────────────")
    lines.append("")
    recent = supervisor.store.recent_events(limit=200)
    cutoff = (now - timedelta(hours=24)).isoformat()
    day_events = [e for e in recent if e.created_at >= cutoff]

    # Summarize by type
    commits = [e for e in day_events if "commit" in e.message.lower()]
    recoveries = [e for e in day_events if e.event_type in ("recover", "recovery", "stabilize_failed")]
    sweeps = [e for e in day_events if e.event_type == "heartbeat"]
    sends = [e for e in day_events if e.event_type == "send_input"]

    summary_parts = []
    if sweeps:
        summary_parts.append(f"{len(sweeps)} heartbeat sweeps")
    if sends:
        summary_parts.append(f"{len(sends)} messages sent")
    if commits:
        summary_parts.append(f"{len(commits)} commits")
    if recoveries:
        summary_parts.append(f"{len(recoveries)} recoveries")
    if summary_parts:
        lines.append("  " + "  ·  ".join(summary_parts))
    else:
        lines.append("  No activity recorded.")
    lines.append("")

    # Show last few notable events
    notable = [e for e in day_events if e.event_type not in ("heartbeat", "token_ledger", "polly_followup")][:8]
    for event in notable:
        try:
            ts = datetime.fromisoformat(event.created_at)
            age = now - ts
            if age.total_seconds() < 3600:
                time_str = f"{int(age.total_seconds() // 60)}m ago"
            else:
                time_str = f"{int(age.total_seconds() // 3600)}h ago"
        except (ValueError, TypeError):
            time_str = "?"
        msg = event.message[:65]
        lines.append(f"  {time_str:>7}  {event.session_name}: {msg}")
    lines.append("")

    # ── Token Usage (30 days) ──
    lines.append("  ─── Token Usage (30 days) ──────────────────────────")
    lines.append("")
    daily = supervisor.store.daily_token_usage(days=30)
    if daily:
        values = [t for _, t in daily]
        total = sum(values)
        chart = _spark_bar(values, width=30)
        # Label the axis
        if len(daily) >= 2:
            lines.append(f"  {daily[0][0][-5:]}{'':>20}{daily[-1][0][-5:]}")
        lines.append(f"  {chart}")
        lines.append(f"  Total: {total:,} tokens across {len(daily)} days")
        # Today's usage
        today_str = now.strftime("%Y-%m-%d")
        today_tokens = next((t for d, t in daily if d == today_str), 0)
        if today_tokens:
            lines.append(f"  Today: {today_tokens:,} tokens")
    else:
        lines.append("  No token data yet.")
    lines.append("")

    # ── Alerts ──
    if actionable_alerts:
        lines.append("  ─── Alerts ────────────────────────────────────────")
        lines.append("")
        for alert in actionable_alerts[:5]:
            lines.append(f"  ▲ {alert.session_name}: {alert.message[:60]}")
        lines.append("")

    # ── Footer ──
    lines.append("  Click Polly to connect  ·  j/k navigate  ·  S settings")

    return "\n".join(lines)


def build_cockpit_detail(config_path: Path, kind: str, target: str | None = None) -> str:
    try:
        return _build_cockpit_detail_inner(config_path, kind, target)
    except Exception as exc:  # noqa: BLE001
        return f"Error loading {kind} view: {exc}"


def _build_cockpit_detail_inner(config_path: Path, kind: str, target: str | None = None) -> str:
    supervisor = Supervisor(load_config(config_path))
    supervisor.ensure_layout()
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
                if a.alert_type == "recovery_limit":
                    lines.append(f"  → Recovery paused. Run `pm reset` to clear, or investigate the session.")
                elif a.alert_type == "auth_broken":
                    lines.append(f"  → Run `pm relogin {a.session_name}` to fix authentication.")
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
