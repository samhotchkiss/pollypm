import asyncio
from pathlib import Path

from pollypm.cockpit import CockpitRouter
from pollypm.cockpit_ui import PollyCockpitApp, PollySettingsPaneApp
from pollypm.models import ProviderKind
from pollypm.models import KnownProject, ProjectKind


def test_cockpit_router_build_items_includes_core_entries(monkeypatch, tmp_path: Path) -> None:
    class FakeConfig:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".pollypm"
            tmux_session = "pollypm"

        project = Project()
        projects = {
            "pollypm": KnownProject(key="pollypm", path=tmp_path, name="PollyPM", kind=ProjectKind.GIT),
            "demo": KnownProject(key="demo", path=tmp_path / "demo", name="Demo", kind=ProjectKind.GIT),
        }

    class FakeLaunch:
        def __init__(self, name: str, role: str, project: str, window_name: str) -> None:
            self.window_name = window_name
            self.session = type("Session", (), {"name": name, "role": role, "project": project, "provider": type("P", (), {"value": "claude"})()})()

    class FakeWindow:
        def __init__(self, name: str, pane_dead: bool = False) -> None:
            self.name = name
            self.pane_dead = pane_dead
            self.pane_id = f"%{name}"

    class FakeSupervisor:
        config = FakeConfig()

        def status(self):
            launches = [
                FakeLaunch("operator", "operator-pm", "pollypm", "pm-operator"),
                FakeLaunch("worker_demo", "worker", "demo", "worker-demo"),
            ]
            windows = [FakeWindow("pm-operator"), FakeWindow("worker-demo")]
            return launches, windows, [], [], []

    monkeypatch.setattr("pollypm.cockpit.list_open_messages", lambda root_dir: [object()])
    router = CockpitRouter(tmp_path / "pollypm.toml")
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())

    items = router.build_items(spinner_index=2)

    assert [item.key for item in items] == ["polly", "inbox", "project:pollypm", "project:demo", "settings"]
    assert items[0].state == "ready"
    assert items[1].label == "Inbox (1)"
    assert items[3].state.endswith("live")


def test_cockpit_router_ensure_layout_splits_when_missing_right_pane(tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    class FakeTmux:
        def list_panes(self, target: str):
            calls.setdefault("list_targets", []).append(target)
            if "split" not in calls:
                return [type("Pane", (), {"pane_id": "%1", "active": True})()]
            return [
                type("Pane", (), {"pane_id": "%1", "active": True})(),
                type("Pane", (), {"pane_id": "%2", "active": False})(),
            ]

        def split_window(self, target: str, command: str, *, horizontal: bool = True, detached: bool = True, percent: int | None = None):
            calls["split"] = (target, command, horizontal, detached, percent)
            return "%2"

        def select_pane(self, target: str):
            calls["selected"] = target

        def run(self, *args, **kwargs):
            calls.setdefault("run", []).append(args)

    router = CockpitRouter(tmp_path / "pollypm.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n")

    router.ensure_cockpit_layout()

    assert calls["split"][0] == "pollypm:PollyPM"
    assert "cockpit-pane polly" in calls["split"][1]


def test_cockpit_router_ensure_layout_resizes_existing_left_pane(tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    class FakePane:
        def __init__(self, pane_id: str, pane_left: int, command: str) -> None:
            self.pane_id = pane_id
            self.pane_left = pane_left
            self.pane_current_command = command

    class FakeTmux:
        def list_panes(self, target: str):
            return [
                FakePane("%1", 0, "bash"),
                FakePane("%2", 101, "zsh"),
            ]

        def resize_pane_width(self, target: str, width: int):
            calls["resize"] = (target, width)

        def run(self, *args, **kwargs):
            calls.setdefault("run", []).append(args)

        def swap_pane(self, source: str, target: str):
            calls["swap"] = (source, target)

    router = CockpitRouter(tmp_path / "pollypm.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n")
    router._write_state({"right_pane_id": "%2"})

    router.ensure_cockpit_layout()

    assert calls["resize"] == ("%1", router._LEFT_PANE_WIDTH)


def test_cockpit_router_routes_idle_project_to_detail_pane(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    (tmp_path / "pollypm.toml").write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n")

    class FakeTmux:
        def list_panes(self, target: str):
            return [
                type("Pane", (), {"pane_id": "%1", "active": True})(),
                type("Pane", (), {"pane_id": "%2", "active": False})(),
            ]

        def respawn_pane(self, target: str, command: str):
            calls["respawn"] = (target, command)

    class FakeConfig:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".pollypm"
            tmux_session = "pollypm"

        project = Project()
        projects = {
            "demo": KnownProject(key="demo", path=tmp_path / "demo", name="Demo", kind=ProjectKind.GIT),
        }

    class FakeSupervisor:
        config = FakeConfig()

        def ensure_layout(self):
            return None

        def plan_launches(self):
            return []

    router = CockpitRouter(tmp_path / "pollypm.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())
    monkeypatch.setattr(router, "set_selected_key", lambda key: calls.setdefault("selected", key))
    monkeypatch.setattr(router, "ensure_cockpit_layout", lambda: None)
    monkeypatch.setattr(router, "_right_pane_id", lambda target: "%2")

    router.route_selected("project:demo")

    assert calls["selected"] == "project:demo"
    assert calls["respawn"][0] == "%2"
    assert "cockpit-pane project demo" in calls["respawn"][1]


def test_cockpit_router_joins_session_from_storage(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    (tmp_path / "pollypm.toml").write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n")

    class FakeLaunch:
        def __init__(self) -> None:
            self.window_name = "pm-operator"
            self.session = type("Session", (), {"name": "operator", "project": "pollypm", "provider": type("P", (), {"value": "codex"})()})()

    class FakeSupervisor:
        class Config:
            class Project:
                tmux_session = "pollypm"

            project = Project()

        config = Config()

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

        def plan_launches(self):
            return [FakeLaunch()]

    class FakeWindow:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakePane:
        def __init__(self, pane_id, pane_left):
            self.pane_id = pane_id
            self.pane_left = pane_left
            self.pane_current_command = "bash"
            self.pane_width = 30

    class FakeTmux:
        def list_windows(self, target: str):
            return [FakeWindow("pm-operator")]

        def kill_pane(self, target: str):
            calls["killed"] = target

        def join_pane(self, source: str, target: str, *, horizontal: bool = True):
            calls["joined"] = (source, target)

        def list_panes(self, target: str):
            return [FakePane("%1", 0), FakePane("%9", 31)]

        def resize_pane_width(self, target: str, width: int):
            pass

    router = CockpitRouter(tmp_path / "pollypm.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())
    monkeypatch.setattr(router, "set_selected_key", lambda key: None)
    monkeypatch.setattr(router, "ensure_cockpit_layout", lambda: None)
    monkeypatch.setattr(router, "_park_mounted_session", lambda supervisor, target: None)
    monkeypatch.setattr(router, "_mounted_session_name", lambda supervisor, target: None)
    monkeypatch.setattr(router, "_left_pane_id", lambda target: "%1")
    monkeypatch.setattr(router, "_right_pane_id", lambda target: "%2")

    router.route_selected("polly")

    assert calls["killed"] == "%2"
    assert calls["joined"] == ("pollypm-storage-closet:pm-operator.0", "%1")


def test_cockpit_router_infers_mounted_session_from_live_right_pane(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    (tmp_path / "pollypm.toml").write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n")
    worker_cwd = tmp_path / ".pollypm" / "worktrees" / "pollypm-pa-worker_pollypm"
    worker_cwd.mkdir(parents=True)

    class FakeLaunch:
        def __init__(self) -> None:
            self.window_name = "worker-pollypm"
            self.session = type(
                "Session",
                (),
                {
                    "name": "worker_pollypm",
                    "role": "worker",
                    "project": "pollypm",
                    "provider": type("P", (), {"value": "codex"})(),
                    "cwd": worker_cwd,
                },
            )()

    class FakePane:
        def __init__(self, pane_id: str, pane_left: int, command: str, path: Path) -> None:
            self.pane_id = pane_id
            self.pane_left = pane_left
            self.pane_current_command = command
            self.pane_current_path = str(path)

    class FakeSupervisor:
        class Config:
            class Project:
                tmux_session = "pollypm"

            project = Project()

        config = Config()

        def plan_launches(self):
            return [FakeLaunch()]

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

    class FakeTmux:
        def list_panes(self, target: str):
            return [
                FakePane("%1", 0, "uv", tmp_path),
                FakePane("%2", 30, "node", worker_cwd),
            ]

        def list_windows(self, target: str):
            return []

        def respawn_pane(self, target: str, command: str):
            calls["respawn"] = (target, command)

        def split_window(self, target: str, command: str, *, horizontal: bool = True, detached: bool = True, percent: int | None = None):
            return "%3"

        def resize_pane_width(self, target: str, width: int):
            pass

    router = CockpitRouter(tmp_path / "pollypm.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())
    monkeypatch.setattr(router, "ensure_cockpit_layout", lambda: None)
    monkeypatch.setattr(router, "set_selected_key", lambda key: None)
    monkeypatch.setattr(router, "_park_mounted_session", lambda supervisor, target: calls.setdefault("park", True))
    monkeypatch.setattr(router, "_right_pane_id", lambda target: "%2")

    router.route_selected("project:pollypm")

    assert "park" not in calls
    state = router._load_state()
    assert state["mounted_session"] == "worker_pollypm"


def test_cockpit_router_project_click_does_not_launch_configured_but_unmounted_worker(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    (tmp_path / "pollypm.toml").write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n")

    class FakeLaunch:
        def __init__(self) -> None:
            self.window_name = "worker-pollypm"
            self.session = type(
                "Session",
                (),
                {
                    "name": "worker_pollypm",
                    "role": "worker",
                    "project": "pollypm",
                    "provider": type("P", (), {"value": "codex"})(),
                    "cwd": tmp_path / ".pollypm" / "worktrees" / "pollypm-pa-worker_pollypm",
                },
            )()

    class FakeConfig:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".pollypm"
            tmux_session = "pollypm"

        project = Project()
        projects = {
            "pollypm": KnownProject(key="pollypm", path=tmp_path, name="PollyPM", kind=ProjectKind.GIT),
        }

    class FakeSupervisor:
        config = FakeConfig()

        def ensure_layout(self):
            return None

        def plan_launches(self):
            return [FakeLaunch()]

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

    class FakeTmux:
        def list_windows(self, target: str):
            return []

        def list_panes(self, target: str):
            return [
                type("Pane", (), {"pane_id": "%1", "pane_left": 0, "pane_current_command": "uv", "pane_current_path": str(tmp_path)})(),
                type("Pane", (), {"pane_id": "%2", "pane_left": 30, "pane_current_command": "bash", "pane_current_path": str(tmp_path)})(),
            ]

    router = CockpitRouter(tmp_path / "pollypm.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())
    monkeypatch.setattr(router, "set_selected_key", lambda key: calls.setdefault("selected", key))
    monkeypatch.setattr(router, "ensure_cockpit_layout", lambda: None)
    monkeypatch.setattr(router, "_right_pane_id", lambda target: "%2")
    monkeypatch.setattr(router, "_left_pane_id", lambda target: "%1")
    monkeypatch.setattr(router, "_show_live_session", lambda supervisor, session_name, target: calls.setdefault("live", session_name))
    monkeypatch.setattr(
        router,
        "_show_static_view",
        lambda supervisor, target, kind, project_key=None: calls.setdefault("static", (kind, project_key)),
    )

    router.route_selected("project:pollypm")

    assert calls["selected"] == "project:pollypm"
    assert calls["static"] == ("project", "pollypm")
    assert "live" not in calls


def test_cockpit_router_falls_back_to_static_when_session_not_in_storage(monkeypatch, tmp_path: Path) -> None:
    """When a configured session is not running in storage, show the static project detail instead of auto-launching."""
    calls: dict[str, object] = {}
    (tmp_path / "pollypm.toml").write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n")

    class FakeLaunch:
        def __init__(self) -> None:
            self.window_name = "pm-operator"
            self.session = type(
                "Session",
                (),
                {
                    "name": "operator",
                    "role": "operator-pm",
                    "project": "pollypm",
                    "provider": type("P", (), {"value": "claude"})(),
                },
            )()

    class FakeSupervisor:
        class Config:
            class Project:
                tmux_session = "pollypm"

            project = Project()

        config = Config()

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

        def plan_launches(self):
            return [FakeLaunch()]

    class FakeTmux:
        def list_windows(self, target: str):
            return []  # Session NOT in storage

        def respawn_pane(self, target: str, command: str):
            calls["respawn"] = (target, command)

        def list_panes(self, target: str):
            return [
                type("Pane", (), {"pane_id": "%1", "pane_left": 0, "pane_width": 30})(),
                type("Pane", (), {"pane_id": "%2", "pane_left": 31, "pane_width": 49})(),
            ]

        def resize_pane_width(self, target: str, width: int):
            pass

    router = CockpitRouter(tmp_path / "pollypm.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())
    monkeypatch.setattr(router, "set_selected_key", lambda key: None)
    monkeypatch.setattr(router, "ensure_cockpit_layout", lambda: None)
    monkeypatch.setattr(router, "_park_mounted_session", lambda supervisor, target: None)
    monkeypatch.setattr(router, "_mounted_session_name", lambda supervisor, target: None)
    monkeypatch.setattr(router, "_left_pane_id", lambda target: "%1")
    monkeypatch.setattr(router, "_right_pane_id", lambda target: "%2")

    router.route_selected("polly")

    # Should fall back to static detail, not auto-launch
    assert "respawn" in calls
    state = router._load_state()
    assert state.get("mounted_session") is None


def test_cockpit_ui_arrow_and_enter_route_selected(tmp_path: Path) -> None:
    class FakeRouter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def ensure_cockpit_layout(self) -> None:
            return None

        def selected_key(self) -> str:
            return "polly"

        def _load_state(self) -> dict:
            return {}

        def build_items(self, *, spinner_index: int = 0):
            from pollypm.cockpit import CockpitItem

            return [
                CockpitItem("polly", "Polly", "ready"),
                CockpitItem("inbox", "Inbox (0)", "clear"),
                CockpitItem("project:demo", "Demo", "idle"),
                CockpitItem("settings", "Settings", "config"),
            ]

        def route_selected(self, key: str) -> None:
            self.calls.append(key)

        def create_worker_and_route(self, project_key: str) -> None:
            self.calls.append(f"new:{project_key}")

    app = PollyCockpitApp(tmp_path / "pollypm.toml")
    app.router = FakeRouter()  # type: ignore[assignment]

    async def exercise() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app._selected_row_key() == "polly"
            await pilot.press("down")
            await pilot.pause()
            assert app._selected_row_key() == "inbox"
            await pilot.press("enter")
            await pilot.pause()
            assert app.router.calls == ["inbox"]

    asyncio.run(exercise())


def test_settings_pane_renders_accounts_and_toggles_permissions(monkeypatch, tmp_path: Path) -> None:
    class FakeStatus:
        def __init__(self) -> None:
            self.key = "claude_demo"
            self.email = "demo@example.com"
            self.provider = ProviderKind.CLAUDE
            self.logged_in = True
            self.plan = "max"
            self.health = "healthy"
            self.usage_summary = "90% left"
            self.reason = ""
            self.available_at = None
            self.access_expires_at = None
            self.usage_updated_at = None
            self.usage_raw_text = "Current week\n10% used"
            self.isolation_status = "host-profile"
            self.isolation_summary = ""
            self.isolation_recommendation = ""
            self.auth_storage = "file"
            self.profile_root = str(tmp_path / ".claude")
            self.home = tmp_path / ".pollypm" / "homes" / "claude_demo"

    class FakePollyPM:
        controller_account = "claude_demo"
        failover_accounts = ["claude_demo"]
        open_permissions_by_default = True

    class FakeProject:
        workspace_root = tmp_path

    class FakeConfig:
        pollypm = FakePollyPM()
        project = FakeProject()

    calls: list[str] = []

    class FakeService:
        def list_account_statuses(self):
            return [FakeStatus()]

        def set_open_permissions_default(self, enabled: bool):
            calls.append(f"permissions:{enabled}")
            return enabled

    monkeypatch.setattr("pollypm.cockpit_ui.load_config", lambda path: FakeConfig())

    app = PollySettingsPaneApp(tmp_path / "pollypm.toml")
    app.service = FakeService()  # type: ignore[assignment]

    async def exercise() -> None:
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app.accounts.row_count == 1
            assert "demo@example.com" in str(app.detail.render())
            await pilot.press("b")
            await pilot.pause()
            assert calls == ["permissions:False"]

    asyncio.run(exercise())
