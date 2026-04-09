from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, replace
from pathlib import Path

from pollypm.config import load_config
from pollypm.messaging import list_open_messages
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

    def _load_supervisor(self) -> Supervisor:
        supervisor = self.service.load_supervisor()
        supervisor.ensure_layout()
        return supervisor

    def _state_path(self) -> Path:
        config = load_config(self.config_path)
        config.project.base_dir.mkdir(parents=True, exist_ok=True)
        return config.project.base_dir / self._STATE_FILE

    def selected_key(self) -> str:
        data = self._load_state()
        value = data.get("selected")
        return str(value) if isinstance(value, str) and value else "polly"

    def set_selected_key(self, key: str) -> None:
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
        self._state_path().write_text(json.dumps(data, indent=2) + "\n")

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
        inbox_count = len(list_open_messages(config.project.root_dir))
        items = [
            CockpitItem("polly", "Polly", self._session_state("operator", launches, windows, alerts, spinner_index)),
            CockpitItem("inbox", f"Inbox ({inbox_count})", "mail" if inbox_count else "clear"),
        ]

        selected = self.selected_key()
        project_session_map = self._project_session_map(launches)
        for project_key, project in config.projects.items():
            session_name = project_session_map.get(project_key)
            if session_name is None:
                state = "idle"
            else:
                state = self._session_state(session_name, launches, windows, alerts, spinner_index)
            items.append(CockpitItem(f"project:{project_key}", project.name or project.key, state))
            # Unfold sub-items for the selected project
            if selected.startswith(f"project:{project_key}"):
                items.append(CockpitItem(f"project:{project_key}:settings", "  Settings", "sub"))
                items.append(CockpitItem(f"project:{project_key}:issues", "  Issues", "sub"))

        items.append(CockpitItem("settings", "Settings", "config"))
        return items

    def _project_session_map(self, launches) -> dict[str, str]:
        project_session_map: dict[str, str] = {}
        for launch in launches:
            if launch.session.role in {"operator-pm", "heartbeat-supervisor"}:
                continue
            project_session_map.setdefault(launch.session.project, launch.session.name)
        return project_session_map

    def _session_state(self, session_name: str, launches, windows, alerts, spinner_index: int) -> str:
        alert_count = sum(1 for alert in alerts if alert.session_name == session_name)
        if alert_count:
            return f"! {alert_count}"
        launch = next((item for item in launches if item.session.name == session_name), None)
        if launch is None:
            return "idle"
        window_map = {window.name: window for window in windows}
        window = window_map.get(launch.window_name)
        if window is None:
            return "idle"
        if window.pane_dead:
            return "dead"
        if launch.session.role == "worker":
            working = self._is_pane_working(window, launch.session.provider)
            if working:
                return ["\u25dc", "\u25dd", "\u25de", "\u25df"][spinner_index % 4] + " working"
            return "\u25cf live"
        if launch.session.role == "operator-pm":
            working = self._is_pane_working(window, launch.session.provider)
            if working:
                return ["\u25dc", "\u25dd", "\u25de", "\u25df"][spinner_index % 4] + " working"
            return "ready"
        if launch.session.role == "heartbeat-supervisor":
            return "watch"
        return "live"

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
        provider_value = provider.value if hasattr(provider, "value") else str(provider)
        if provider_value == "claude":
            return "\u276f" not in tail
        if provider_value == "codex":
            if "working" in lowered and "esc to interrupt" in lowered:
                return True
            # Codex idle: › prompt, or permission prompt, or "% left" usage line
            if "\u203a" in tail:
                return False
            if "press enter to confirm" in lowered:
                return False
            if "% left" in lowered:
                return False
            return bool(stripped)
        return False

    def ensure_cockpit_layout(self) -> None:
        config = load_config(self.config_path)
        target = f"{config.project.tmux_session}:{self._COCKPIT_WINDOW}"
        panes = self.tmux.list_panes(target)
        state = self._load_state()
        right_pane_id = state.get("right_pane_id")
        right_pane_present = isinstance(right_pane_id, str) and any(pane.pane_id == right_pane_id for pane in panes)
        if len(panes) < 2:
            right_pane_id = self.tmux.split_window(
                target,
                self._right_pane_command("polly"),
                horizontal=True,
                detached=True,
                percent=80,
            )
            left_pane = min(self.tmux.list_panes(target), key=self._pane_left)
            if hasattr(self.tmux, "resize_pane_width"):
                self.tmux.resize_pane_width(left_pane.pane_id, self._LEFT_PANE_WIDTH)
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
            if hasattr(self.tmux, "resize_pane_width"):
                self.tmux.resize_pane_width(left_pane.pane_id, self._LEFT_PANE_WIDTH)
            state["right_pane_id"] = max(panes, key=self._pane_left).pane_id
            self._write_state(state)

    def _pane_left(self, pane) -> int:
        return int(getattr(pane, "pane_left", 0))

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

    def create_worker_and_route(self, project_key: str) -> None:
        supervisor = self._load_supervisor()
        launches = supervisor.plan_launches()
        session_name = self._project_session_map(launches).get(project_key)
        storage_session = supervisor.storage_closet_session_name()
        storage_windows = {w.name for w in self.tmux.list_windows(storage_session)}

        if session_name is not None:
            launch = next(l for l in launches if l.session.name == session_name)
            if launch.window_name not in storage_windows:
                supervisor.launch_session(session_name)
        else:
            prompt = self.service.suggest_worker_prompt(project_key=project_key)
            self.service.create_and_launch_worker(project_key=project_key, prompt=prompt)
        self.route_selected(f"project:{project_key}")

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
        left_pane_id = self._left_pane_id(window_target)
        if left_pane_id is None:
            raise RuntimeError("Cockpit left pane is not available.")
        right_pane_id = self._right_pane_id(window_target)
        storage_session = supervisor.storage_closet_session_name()
        storage_windows = {window.name for window in self.tmux.list_windows(storage_session)}
        if launch.window_name not in storage_windows:
            # Session is not running -- try to relaunch it
            if launch.session.role in self.service._CONTROL_ROLES if hasattr(self.service, '_CONTROL_ROLES') else launch.session.role in {"operator-pm", "heartbeat-supervisor"}:
                try:
                    supervisor.launch_session(session_name)
                    # Successfully relaunched -- now join from storage
                    storage_windows = {w.name for w in self.tmux.list_windows(storage_session)}
                    if launch.window_name in storage_windows:
                        if right_pane_id is not None:
                            self.tmux.kill_pane(right_pane_id)
                        source = f"{storage_session}:{launch.window_name}.0"
                        self.tmux.join_pane(source, left_pane_id, horizontal=True)
                        panes = self.tmux.list_panes(window_target)
                        left_pane = min(panes, key=self._pane_left)
                        if hasattr(self.tmux, "resize_pane_width"):
                            self.tmux.resize_pane_width(left_pane.pane_id, self._LEFT_PANE_WIDTH)
                        right_pane = max(panes, key=self._pane_left)
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
                right_pane_id = self.tmux.split_window(
                    left_pane_id,
                    self._right_pane_command(fallback_kind, fallback_target),
                    horizontal=True,
                    detached=True,
                    percent=80,
                )
            else:
                self.tmux.respawn_pane(right_pane_id, self._right_pane_command(fallback_kind, fallback_target))
            left_pane = min(self.tmux.list_panes(window_target), key=self._pane_left)
            if hasattr(self.tmux, "resize_pane_width"):
                self.tmux.resize_pane_width(left_pane.pane_id, self._LEFT_PANE_WIDTH)
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
        if hasattr(self.tmux, "resize_pane_width"):
            self.tmux.resize_pane_width(left_pane.pane_id, self._LEFT_PANE_WIDTH)
        right_pane = max(panes, key=self._pane_left)
        state = self._load_state()
        state["mounted_session"] = session_name
        state["right_pane_id"] = right_pane.pane_id
        self._write_state(state)

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
        right_pane_id = self.tmux.split_window(
            left_pane_id,
            visible_launch.command,
            horizontal=True,
            detached=False,
            percent=80,
        )
        self.tmux.pipe_pane(right_pane_id, visible_launch.log_path)
        supervisor._stabilize_launch(visible_launch, right_pane_id)
        panes = self.tmux.list_panes(window_target)
        left_pane = min(panes, key=self._pane_left)
        if hasattr(self.tmux, "resize_pane_width"):
            self.tmux.resize_pane_width(left_pane.pane_id, self._LEFT_PANE_WIDTH)
        return max(self.tmux.list_panes(window_target), key=self._pane_left)

    def _park_mounted_session(self, supervisor: Supervisor, window_target: str) -> None:
        state = self._load_state()
        mounted_session = self._mounted_session_name(supervisor, window_target)
        if not isinstance(mounted_session, str) or not mounted_session:
            return
        right_pane_id = self._right_pane_id(window_target)
        if right_pane_id is None:
            return
        right_pane = max(self.tmux.list_panes(window_target), key=self._pane_left)
        if not self._is_live_provider_pane(right_pane):
            state.pop("mounted_session", None)
            self._write_state(state)
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
        return getattr(pane, "pane_current_command", "") in {"node", "claude", "codex"}

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
        panes = self.tmux.list_panes(window_target)
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
                percent=80,
            )
        else:
            self.tmux.respawn_pane(right_pane_id, self._right_pane_command(kind, project_key))
        left_pane = min(self.tmux.list_panes(window_target), key=self._pane_left)
        if hasattr(self.tmux, "resize_pane_width"):
            self.tmux.resize_pane_width(left_pane.pane_id, self._LEFT_PANE_WIDTH)
        state = self._load_state()
        state.pop("mounted_session", None)
        state["right_pane_id"] = self._right_pane_id(window_target)
        self._write_state(state)

    def _right_pane_command(self, kind: str, project_key: str | None = None) -> str:
        root = shlex.quote(str(self.config_path.parent.resolve()))
        import shutil
        pm_cmd = "pm" if shutil.which("pm") else "uv run pm"
        args = [pm_cmd, "cockpit-pane", kind]
        if project_key is not None:
            args.append(project_key)
        joined = " ".join(args)
        return f"sh -lc 'cd {root} && {joined}'"


def build_cockpit_detail(config_path: Path, kind: str, target: str | None = None) -> str:
    try:
        return _build_cockpit_detail_inner(config_path, kind, target)
    except Exception as exc:  # noqa: BLE001
        return f"Error loading {kind} view: {exc}"


def _build_cockpit_detail_inner(config_path: Path, kind: str, target: str | None = None) -> str:
    supervisor = Supervisor(load_config(config_path))
    supervisor.ensure_layout()
    config = supervisor.config
    if kind == "polly":
        return (
            "Polly\n\n"
            "Polly is your AI project manager. She runs as an interactive\n"
            "Claude session and can start, steer, and review worker sessions.\n\n"
            "The Polly session is not currently running.\n"
            "Use `pm up` to restart all sessions."
        )

    if kind == "inbox":
        messages = list_open_messages(config.project.root_dir)
        if not messages:
            return "Inbox\n\nNo open messages."
        lines = ["Inbox", "", "Open messages:"]
        for message in messages[:12]:
            lines.append(f"- {message.subject} · from {message.sender}")
        lines.extend(
            [
                "",
                "Reply flow",
                "Reply to Polly. Polly keeps the thread, decides whether to resolve it, continue the conversation, or route a distilled action to a worker lane.",
            ]
        )
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
        project = config.projects[target]
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
            f"Issue tracker: {issues_root if issues_root.exists() else 'not initialized'}",
            f"Active worktrees: {len(worktrees)}",
            "",
            "No active live lane is running for this project.",
            "Select the project in the left rail and press N to start a worker lane.",
        ]
        if state_counts:
            lines.extend(["", "Task states:"])
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
        for state_name in ["01-ready", "02-in-progress", "03-needs-review", "04-done", "05-completed"]:
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
