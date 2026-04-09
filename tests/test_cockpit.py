import asyncio
from pathlib import Path

from promptmaster.cockpit import CockpitRouter
from promptmaster.cockpit_ui import PollyCockpitApp, PollySettingsPaneApp
from promptmaster.models import ProviderKind
from promptmaster.models import KnownProject, ProjectKind


def test_cockpit_router_build_items_includes_core_entries(monkeypatch, tmp_path: Path) -> None:
    class FakeConfig:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".promptmaster"
            tmux_session = "pollypm"

        project = Project()
        projects = {
            "promptmaster": KnownProject(key="promptmaster", path=tmp_path, name="PollyPM", kind=ProjectKind.GIT),
            "demo": KnownProject(key="demo", path=tmp_path / "demo", name="Demo", kind=ProjectKind.GIT),
        }

    class FakeLaunch:
        def __init__(self, name: str, role: str, project: str, window_name: str) -> None:
            self.window_name = window_name
            self.session = type("Session", (), {"name": name, "role": role, "project": project})()

    class FakeWindow:
        def __init__(self, name: str, pane_dead: bool = False) -> None:
            self.name = name
            self.pane_dead = pane_dead

    class FakeSupervisor:
        config = FakeConfig()

        def status(self):
            launches = [
                FakeLaunch("operator", "operator-pm", "promptmaster", "pm-operator"),
                FakeLaunch("worker_demo", "worker", "demo", "worker-demo"),
            ]
            windows = [FakeWindow("pm-operator"), FakeWindow("worker-demo")]
            return launches, windows, [], [], []

    monkeypatch.setattr("promptmaster.cockpit.list_open_messages", lambda root_dir: [object()])
    router = CockpitRouter(tmp_path / "promptmaster.toml")
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())

    items = router.build_items(spinner_index=2)

    assert [item.key for item in items] == ["polly", "inbox", "project:promptmaster", "project:demo", "settings"]
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

    router = CockpitRouter(tmp_path / "promptmaster.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    config_path = tmp_path / "promptmaster.toml"
    config_path.write_text("[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\n")

    router.ensure_cockpit_layout()

    assert calls["split"][0] == "pollypm:PollyPM"
    assert "cockpit-pane inbox" in calls["split"][1]


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

    router = CockpitRouter(tmp_path / "promptmaster.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    config_path = tmp_path / "promptmaster.toml"
    config_path.write_text("[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\n")
    router._write_state({"right_pane_id": "%2"})

    router.ensure_cockpit_layout()

    assert calls["resize"] == ("%1", router._LEFT_PANE_WIDTH)


def test_cockpit_router_routes_idle_project_to_detail_pane(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    (tmp_path / "promptmaster.toml").write_text("[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\n")

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
            base_dir = tmp_path / ".promptmaster"
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

    router = CockpitRouter(tmp_path / "promptmaster.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())
    monkeypatch.setattr(router, "set_selected_key", lambda key: calls.setdefault("selected", key))
    monkeypatch.setattr(router, "ensure_cockpit_layout", lambda: None)
    monkeypatch.setattr(router, "_right_pane_id", lambda target: "%2")

    router.route_selected("project:demo")

    assert calls["selected"] == "project:demo"
    assert calls["respawn"][0] == "%2"
    assert "cockpit-pane project demo" in calls["respawn"][1]


def test_cockpit_router_prefers_visible_boot_over_storage_mount(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    (tmp_path / "promptmaster.toml").write_text("[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\n")

    class FakeLaunch:
        def __init__(self) -> None:
            self.window_name = "pm-operator"
            self.session = type("Session", (), {"name": "operator", "project": "promptmaster", "provider": type("P", (), {"value": "codex"})()})()

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

    class FakeTmux:
        def list_windows(self, target: str):
            return [FakeWindow("pm-operator")]

    router = CockpitRouter(tmp_path / "promptmaster.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())
    monkeypatch.setattr(router, "set_selected_key", lambda key: None)
    monkeypatch.setattr(router, "ensure_cockpit_layout", lambda: None)
    monkeypatch.setattr(router, "_park_mounted_session", lambda supervisor, target: None)
    monkeypatch.setattr(router, "_left_pane_id", lambda target: "%1")
    monkeypatch.setattr(router, "_right_pane_id", lambda target: "%2")
    monkeypatch.setattr(router, "_should_boot_visible", lambda launch: True)
    monkeypatch.setattr(
        router,
        "_launch_visible_session",
        lambda supervisor, launch, window_target, left_pane_id, right_pane_id: (
            calls.setdefault("visible", (window_target, left_pane_id, right_pane_id)),
            type("Pane", (), {"pane_id": "%9"})(),
        )[1],
    )
    monkeypatch.setattr(router, "_mark_ui_initialized", lambda session_name: calls.setdefault("marked", session_name))

    router.route_selected("polly")

    assert calls["visible"] == ("pollypm:PollyPM", "%1", "%2")
    assert calls["marked"] == "operator"


def test_cockpit_router_infers_mounted_session_from_live_right_pane(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    (tmp_path / "promptmaster.toml").write_text("[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\n")
    worker_cwd = tmp_path / ".promptmaster" / "worktrees" / "promptmaster-pa-worker_promptmaster"
    worker_cwd.mkdir(parents=True)

    class FakeLaunch:
        def __init__(self) -> None:
            self.window_name = "worker-promptmaster"
            self.session = type(
                "Session",
                (),
                {
                    "name": "worker_promptmaster",
                    "role": "worker",
                    "project": "promptmaster",
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

    router = CockpitRouter(tmp_path / "promptmaster.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())
    monkeypatch.setattr(router, "ensure_cockpit_layout", lambda: None)
    monkeypatch.setattr(router, "set_selected_key", lambda key: None)
    monkeypatch.setattr(router, "_park_mounted_session", lambda supervisor, target: calls.setdefault("park", True))
    monkeypatch.setattr(router, "_right_pane_id", lambda target: "%2")

    router.route_selected("project:promptmaster")

    assert "park" not in calls
    state = router._load_state()
    assert state["mounted_session"] == "worker_promptmaster"


def test_cockpit_router_project_click_does_not_launch_configured_but_unmounted_worker(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    (tmp_path / "promptmaster.toml").write_text("[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\n")

    class FakeLaunch:
        def __init__(self) -> None:
            self.window_name = "worker-promptmaster"
            self.session = type(
                "Session",
                (),
                {
                    "name": "worker_promptmaster",
                    "role": "worker",
                    "project": "promptmaster",
                    "provider": type("P", (), {"value": "codex"})(),
                    "cwd": tmp_path / ".promptmaster" / "worktrees" / "promptmaster-pa-worker_promptmaster",
                },
            )()

    class FakeConfig:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".promptmaster"
            tmux_session = "pollypm"

        project = Project()
        projects = {
            "promptmaster": KnownProject(key="promptmaster", path=tmp_path, name="PollyPM", kind=ProjectKind.GIT),
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

    router = CockpitRouter(tmp_path / "promptmaster.toml")
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

    router.route_selected("project:promptmaster")

    assert calls["selected"] == "project:promptmaster"
    assert calls["static"] == ("project", "promptmaster")
    assert "live" not in calls


def test_cockpit_router_visible_boot_preserves_ui_initialized_on_state_write(monkeypatch, tmp_path: Path) -> None:
    """Regression: _show_live_session must not overwrite ui_initialized_sessions saved by _mark_ui_initialized."""
    calls: dict[str, object] = {}
    (tmp_path / "promptmaster.toml").write_text("[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\n")

    class FakeLaunch:
        def __init__(self) -> None:
            self.window_name = "pm-operator"
            self.session = type(
                "Session",
                (),
                {
                    "name": "operator",
                    "role": "operator-pm",
                    "project": "promptmaster",
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

    class FakeWindow:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeTmux:
        def list_windows(self, target: str):
            return [FakeWindow("pm-operator")]

    router = CockpitRouter(tmp_path / "promptmaster.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())
    monkeypatch.setattr(router, "set_selected_key", lambda key: None)
    monkeypatch.setattr(router, "ensure_cockpit_layout", lambda: None)
    monkeypatch.setattr(router, "_park_mounted_session", lambda supervisor, target: None)
    monkeypatch.setattr(router, "_mounted_session_name", lambda supervisor, target: None)
    monkeypatch.setattr(router, "_left_pane_id", lambda target: "%1")
    monkeypatch.setattr(router, "_right_pane_id", lambda target: "%2")
    monkeypatch.setattr(router, "_should_boot_visible", lambda launch: True)
    monkeypatch.setattr(
        router,
        "_launch_visible_session",
        lambda supervisor, launch, window_target, left_pane_id, right_pane_id: (
            type("Pane", (), {"pane_id": "%9"})()
        ),
    )
    monkeypatch.setattr(router, "_mark_ui_initialized", lambda session_name: (
        calls.setdefault("marked", session_name),
        router._write_state({**router._load_state(), "ui_initialized_sessions": [session_name]}),
    ))

    router.route_selected("polly")

    state = router._load_state()
    assert state.get("mounted_session") == "operator"
    assert "operator" in state.get("ui_initialized_sessions", []), (
        "ui_initialized_sessions was overwritten by stale state"
    )


def test_cockpit_ui_arrow_and_enter_route_selected(tmp_path: Path) -> None:
    class FakeRouter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def ensure_cockpit_layout(self) -> None:
            return None

        def selected_key(self) -> str:
            return "polly"

        def build_items(self, *, spinner_index: int = 0):
            from promptmaster.cockpit import CockpitItem

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

    app = PollyCockpitApp(tmp_path / "promptmaster.toml")
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
            self.home = tmp_path / ".promptmaster" / "homes" / "claude_demo"

    class FakePromptMaster:
        controller_account = "claude_demo"
        failover_accounts = ["claude_demo"]
        open_permissions_by_default = True

    class FakeProject:
        workspace_root = tmp_path

    class FakeConfig:
        promptmaster = FakePromptMaster()
        project = FakeProject()

    calls: list[str] = []

    class FakeService:
        def list_account_statuses(self):
            return [FakeStatus()]

        def set_open_permissions_default(self, enabled: bool):
            calls.append(f"permissions:{enabled}")
            return enabled

    monkeypatch.setattr("promptmaster.cockpit_ui.load_config", lambda path: FakeConfig())

    app = PollySettingsPaneApp(tmp_path / "promptmaster.toml")
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
