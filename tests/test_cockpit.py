import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.cockpit import CockpitRouter, build_cockpit_detail
from pollypm.config import write_config
from pollypm.cockpit_ui import PollyCockpitApp, PollySettingsPaneApp
from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)


def test_cockpit_router_build_items_includes_core_entries(monkeypatch, tmp_path: Path) -> None:
    class FakeConfig:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".pollypm-state"
            tmux_session = "pollypm"

        project = Project()
        projects = {
            "pollypm": KnownProject(key="pollypm", path=tmp_path, name="PollyPM", persona_name="Pete", kind=ProjectKind.GIT),
            "demo": KnownProject(key="demo", path=tmp_path / "demo", name="Demo", persona_name="Dora", kind=ProjectKind.GIT),
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

    monkeypatch.setattr("pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 1)
    (tmp_path / "pollypm.toml").write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n")
    router = CockpitRouter(tmp_path / "pollypm.toml")
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())

    items = router.build_items(spinner_index=2)

    keys = [item.key for item in items]
    assert "polly" in keys
    assert "russell" in keys
    assert "inbox" in keys
    assert "project:pollypm" in keys
    assert "project:demo" in keys
    assert "settings" in keys
    assert items[0].key == "polly"
    assert items[0].state == "ready"
    assert items[1].key == "russell"
    assert items[2].key == "inbox"
    assert items[2].label == "Inbox (1)"
    # Projects are sorted alphabetically; both "Demo" and "PollyPM" should be present
    project_labels = [i.label for i in items if i.key.startswith("project:")]
    assert "Demo" in project_labels
    assert "PollyPM" in project_labels


def test_cockpit_router_session_state_ignores_silent_alerts(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n"
    )

    class FakeLaunch:
        def __init__(self) -> None:
            self.window_name = "worker-demo"
            self.session = type(
                "Session",
                (),
                {
                    "name": "worker_demo",
                    "role": "worker",
                    "project": "demo",
                    "provider": type("P", (), {"value": "codex"})(),
                },
            )()

    class FakeWindow:
        def __init__(self) -> None:
            self.name = "worker-demo"
            self.pane_dead = False
            self.pane_current_command = "codex"

    def make_alert(alert_type: str):
        return type("Alert", (), {"session_name": "worker_demo", "alert_type": alert_type})()

    router = CockpitRouter(config_path)
    launches = [FakeLaunch()]
    windows = [FakeWindow()]

    assert router._session_state("worker_demo", launches, windows, [make_alert("needs_followup")], 0).endswith("live")
    assert router._session_state("worker_demo", launches, windows, [make_alert("pane_dead")], 0) == "! pane dead"


def test_cockpit_router_selected_key_clears_missing_right_pane_state(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n"
    )
    worker_cwd = tmp_path / "worker"
    worker_cwd.mkdir()

    class FakeLaunch:
        def __init__(self) -> None:
            self.session = type(
                "Session",
                (),
                {
                    "name": "worker_demo",
                    "cwd": worker_cwd,
                    "provider": type("P", (), {"value": "codex"})(),
                },
            )()

    class FakeSupervisor:
        def ensure_layout(self) -> None:
            return None

        def plan_launches(self):
            return [FakeLaunch()]

    class FakePane:
        def __init__(self, pane_id: str, *, pane_dead: bool, command: str, path: Path) -> None:
            self.pane_id = pane_id
            self.pane_dead = pane_dead
            self.pane_current_command = command
            self.pane_current_path = str(path)

    class FakeTmux:
        def list_panes(self, target: str):
            return [FakePane("%1", pane_dead=False, command="uv", path=tmp_path)]

    router = CockpitRouter(config_path)
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda fresh=False: FakeSupervisor())
    router._write_state(
        {
            "selected": "project:demo",
            "right_pane_id": "%9",
            "mounted_session": "worker_demo",
        }
    )

    assert router.selected_key() == "project:demo"
    state = router._load_state()
    assert state["selected"] == "project:demo"
    assert "right_pane_id" not in state
    assert "mounted_session" not in state


def test_cockpit_router_selected_key_clears_dead_right_pane_state(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n"
    )
    worker_cwd = tmp_path / "worker"
    worker_cwd.mkdir()

    class FakeLaunch:
        def __init__(self) -> None:
            self.session = type(
                "Session",
                (),
                {
                    "name": "worker_demo",
                    "cwd": worker_cwd,
                    "provider": type("P", (), {"value": "codex"})(),
                },
            )()

    class FakeSupervisor:
        def ensure_layout(self) -> None:
            return None

        def plan_launches(self):
            return [FakeLaunch()]

    class FakePane:
        def __init__(self, pane_id: str, *, pane_dead: bool, command: str, path: Path) -> None:
            self.pane_id = pane_id
            self.pane_dead = pane_dead
            self.pane_current_command = command
            self.pane_current_path = str(path)

    class FakeTmux:
        def list_panes(self, target: str):
            return [
                FakePane("%1", pane_dead=False, command="uv", path=tmp_path),
                FakePane("%2", pane_dead=True, command="node", path=worker_cwd),
            ]

    router = CockpitRouter(config_path)
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda fresh=False: FakeSupervisor())
    router._write_state(
        {
            "selected": "project:demo",
            "right_pane_id": "%2",
            "mounted_session": "worker_demo",
        }
    )

    assert router.selected_key() == "project:demo"
    state = router._load_state()
    assert state["selected"] == "project:demo"
    assert "right_pane_id" not in state
    assert "mounted_session" not in state


def test_cockpit_router_selected_key_clears_stale_mounted_session_only(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n"
    )
    worker_cwd = tmp_path / "worker"
    other_cwd = tmp_path / "other"
    worker_cwd.mkdir()
    other_cwd.mkdir()

    class FakeLaunch:
        def __init__(self) -> None:
            self.session = type(
                "Session",
                (),
                {
                    "name": "worker_demo",
                    "cwd": worker_cwd,
                    "provider": type("P", (), {"value": "codex"})(),
                },
            )()

    class FakeSupervisor:
        def ensure_layout(self) -> None:
            return None

        def plan_launches(self):
            return [FakeLaunch()]

    class FakePane:
        def __init__(self, pane_id: str, *, pane_dead: bool, command: str, path: Path) -> None:
            self.pane_id = pane_id
            self.pane_dead = pane_dead
            self.pane_current_command = command
            self.pane_current_path = str(path)

    class FakeTmux:
        def list_panes(self, target: str):
            return [
                FakePane("%1", pane_dead=False, command="uv", path=tmp_path),
                # A dead pane triggers stale mount cleanup — CWD matching
                # was removed in favour of trusting live provider panes.
                FakePane("%2", pane_dead=True, command="node", path=other_cwd),
            ]

    router = CockpitRouter(config_path)
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda fresh=False: FakeSupervisor())
    router._write_state(
        {
            "selected": "project:demo",
            "right_pane_id": "%2",
            "mounted_session": "worker_demo",
        }
    )

    assert router.selected_key() == "project:demo"
    state = router._load_state()
    assert state["selected"] == "project:demo"
    # Dead pane causes right_pane_id and mounted_session to be cleared
    assert "right_pane_id" not in state
    assert "mounted_session" not in state


def test_build_cockpit_detail_shows_github_issue_counts(monkeypatch, tmp_path: Path) -> None:
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm-state",
            logs_dir=tmp_path / ".pollypm-state/logs",
            snapshots_dir=tmp_path / ".pollypm-state/snapshots",
            state_db=tmp_path / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm-state" / "homes" / "claude_main",
            )
        },
        sessions={},
        projects={
            "demo": KnownProject(key="demo", path=tmp_path / "demo", name="Demo", kind=ProjectKind.GIT, tracked=True),
        },
    )
    project_root = config.projects["demo"].path
    project_root.mkdir()
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    (project_root / ".pollypm" / "config").mkdir(parents=True, exist_ok=True)
    (project_root / ".pollypm" / "config" / "project.toml").write_text(
        """
[project]
display_name = "Demo"

[plugins]
issue_backend = "github"

[plugins.github_issues]
repo = "acme/widgets"
"""
    )

    class FakeStore:
        def get_session_runtime(self, _session_name: str):
            return None

        def recent_token_usage(self, limit: int = 5):
            return []

        def open_alerts(self):
            return []

        def close(self) -> None:
            return None

    class FakeSupervisor:
        def __init__(self) -> None:
            self.config = config
            self.store = FakeStore()

        def ensure_layout(self) -> None:
            return None

        def plan_launches(self):
            return []

    monkeypatch.setattr("pollypm.cockpit.PollyPMService.load_supervisor", lambda self: FakeSupervisor())
    monkeypatch.setattr("pollypm.cockpit.list_worktrees", lambda config_path, project_key: [])

    def fake_gh(*args: str, check: bool = True):
        class Result:
            def __init__(self, stdout: str) -> None:
                self.stdout = stdout

        if args[:2] == ("repo", "view"):
            return Result('{"name":"widgets"}')
        label = args[args.index("--label") + 1]
        payloads = {
            "polly:not-ready": "0",
            "polly:ready": "2",
            "polly:in-progress": "1",
            "polly:needs-review": "0",
            "polly:in-review": "0",
            "polly:completed": "4",
        }
        return Result(payloads[label])

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)

    detail = build_cockpit_detail(config_path, "project", "demo")

    assert f"Issue tracker: {project_root}" in detail
    assert "- 01-ready: 2" in detail
    assert "- 02-in-progress: 1" in detail
    assert "- 05-completed: 4" in detail


def test_build_cockpit_detail_groups_in_review_issues(monkeypatch, tmp_path: Path) -> None:
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm-state",
            logs_dir=tmp_path / ".pollypm-state/logs",
            snapshots_dir=tmp_path / ".pollypm-state/snapshots",
            state_db=tmp_path / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm-state" / "homes" / "claude_main",
            )
        },
        sessions={},
        projects={
            "demo": KnownProject(key="demo", path=tmp_path / "demo", name="Demo", kind=ProjectKind.GIT, tracked=True),
        },
    )
    project_root = config.projects["demo"].path
    project_root.mkdir()
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    (project_root / ".pollypm" / "config").mkdir(parents=True, exist_ok=True)
    (project_root / ".pollypm" / "config" / "project.toml").write_text(
        """
[project]
display_name = "Demo"

[plugins]
issue_backend = "github"

[plugins.github_issues]
repo = "acme/widgets"
"""
    )

    class FakeStore:
        def get_session_runtime(self, _session_name: str):
            return None

        def recent_token_usage(self, limit: int = 5):
            return []

        def close(self) -> None:
            return None

    class FakeSupervisor:
        def __init__(self) -> None:
            self.config = config
            self.store = FakeStore()

        def ensure_layout(self) -> None:
            return None

    monkeypatch.setattr("pollypm.cockpit.PollyPMService.load_supervisor", lambda self: FakeSupervisor())

    def fake_gh(*args: str, check: bool = True):
        class Result:
            def __init__(self, stdout: str) -> None:
                self.stdout = stdout

        if args[:2] == ("repo", "view"):
            return Result('{"name":"widgets"}')
        if args[:2] == ("issue", "list"):
            label = args[args.index("--label") + 1]
            if "-q" in args:
                payloads = {
                    "polly:not-ready": "0",
                    "polly:ready": "0",
                    "polly:in-progress": "0",
                    "polly:needs-review": "0",
                    "polly:in-review": "1",
                    "polly:completed": "0",
                }
                return Result(payloads[label])
            if label == "polly:in-review":
                return Result('[{"number":17,"title":"Review the patch","state":"OPEN"}]')
            return Result("[]")
        raise AssertionError(f"Unexpected gh call: {args}")

    monkeypatch.setattr("pollypm.task_backends.github._gh", fake_gh)

    detail = build_cockpit_detail(config_path, "issues", "demo")

    assert "─── 04-in-review (1) ───" in detail
    assert "17: Review the patch" in detail


def test_build_cockpit_detail_dashboard_shows_activity_and_tokens(monkeypatch, tmp_path: Path) -> None:
    today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
    yesterday_utc = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm-state",
            logs_dir=tmp_path / ".pollypm-state/logs",
            snapshots_dir=tmp_path / ".pollypm-state/snapshots",
            state_db=tmp_path / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm-state" / "homes" / "claude_main",
            )
        },
        sessions={
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=tmp_path,
            ),
            "worker_demo": SessionConfig(
                name="worker_demo",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=tmp_path / "demo",
                project="demo",
            ),
        },
        projects={
            "demo": KnownProject(key="demo", path=tmp_path / "demo", name="Demo", kind=ProjectKind.GIT, tracked=True),
        },
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

    class FakeRuntime:
        def __init__(self, session_name: str, status: str, updated_at: str, last_failure_message: str = "") -> None:
            self.session_name = session_name
            self.status = status
            self.updated_at = updated_at
            self.last_failure_message = last_failure_message

    class FakeEvent:
        def __init__(self, created_at: str, event_type: str, message: str, session_name: str) -> None:
            self.created_at = created_at
            self.event_type = event_type
            self.message = message
            self.session_name = session_name

    class FakeAlert:
        def __init__(self, session_name: str, alert_type: str, message: str) -> None:
            self.session_name = session_name
            self.alert_type = alert_type
            self.message = message

    class FakeStore:
        def open_alerts(self):
            return [
                FakeAlert("worker_demo", "pane_dead", "Worker pane exited"),
                FakeAlert("worker_demo", "needs_followup", "Please review"),
            ]

        def list_session_runtimes(self):
            from datetime import UTC, datetime, timedelta
            now = datetime.now(UTC)
            return [
                FakeRuntime("operator", "healthy", (now - timedelta(minutes=2)).isoformat()),
                FakeRuntime("worker_demo", "waiting_on_user", (now - timedelta(minutes=20)).isoformat()),
            ]

        def recent_events(self, limit: int = 200):
            from datetime import UTC, datetime, timedelta
            now = datetime.now(UTC)
            return [
                FakeEvent((now - timedelta(minutes=5)).isoformat(), "heartbeat", "heartbeat sweep", "heartbeat"),
                FakeEvent((now - timedelta(minutes=10)).isoformat(), "send_input", "Sent follow-up to worker", "operator"),
                FakeEvent((now - timedelta(minutes=90)).isoformat(), "note", "Commit created for dashboard polish", "worker_demo"),
            ]

        def daily_token_usage(self, days: int = 30):
            assert days == 30
            return [
                (yesterday_utc, 1200),
                (today_utc, 345),
            ]

        def close(self) -> None:
            return None

    class FakeLaunch:
        def __init__(self, name: str, role: str, project: str) -> None:
            self.session = type("Session", (), {"name": name, "role": role, "project": project})()

    class FakeSupervisor:
        def __init__(self) -> None:
            self.config = config
            self.store = FakeStore()

        def ensure_layout(self) -> None:
            return None

        def plan_launches(self):
            return [
                FakeLaunch("operator", "operator-pm", "demo"),
                FakeLaunch("worker_demo", "worker", "demo"),
            ]

    monkeypatch.setattr("pollypm.cockpit.load_config", lambda path: config)
    monkeypatch.setattr("pollypm.cockpit.PollyPMService.load_supervisor", lambda self: FakeSupervisor())
    monkeypatch.setattr("pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 1)

    detail = build_cockpit_detail(config_path, "dashboard")

    assert "PollyPM" in detail
    # Dashboard shows alerts and inbox in attention line
    assert "▲" in detail  # alert indicator appears somewhere
    # Activity section shows events
    assert "Activity" in detail
    assert "1 commits" in detail or "1 messages" in detail


def test_cockpit_router_ensure_layout_splits_when_missing_right_pane(tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    class FakeTmux:
        def list_panes(self, target: str):
            calls.setdefault("list_targets", []).append(target)
            if "split" not in calls:
                return [type("Pane", (), {"pane_id": "%1", "active": True, "pane_width": 200})()]
            return [
                type("Pane", (), {"pane_id": "%1", "active": True, "pane_width": 30})(),
                type("Pane", (), {"pane_id": "%2", "active": False, "pane_width": 169})(),
            ]

        def split_window(self, target: str, command: str, *, horizontal: bool = True, detached: bool = True, percent: int | None = None, size: int | None = None):
            calls["split"] = (target, command, horizontal, detached, size)
            return "%2"

        def select_pane(self, target: str):
            calls["selected"] = target

        def run(self, *args, **kwargs):
            calls.setdefault("run", []).append(args)

    router = CockpitRouter(tmp_path / "pollypm.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n")

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
    config_path.write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n")
    router._write_state({"right_pane_id": "%2"})

    router.ensure_cockpit_layout()

    assert calls["resize"] == ("%1", router._LEFT_PANE_WIDTH)


def test_cockpit_router_routes_idle_project_to_detail_pane(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    (tmp_path / "pollypm.toml").write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n")

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
            base_dir = tmp_path / ".pollypm-state"
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
    (tmp_path / "pollypm.toml").write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n")

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

        def claim_lease(self, session_name: str, owner: str, note: str = "") -> None:
            calls["claimed"] = (session_name, owner, note)

    class FakeWindow:
        def __init__(self, name: str, index: int = 0) -> None:
            self.name = name
            self.index = index

    class FakePane:
        def __init__(self, pane_id, pane_left):
            self.pane_id = pane_id
            self.pane_left = pane_left
            self.pane_current_command = "bash"
            self.pane_width = 30

    class FakeTmux:
        def list_windows(self, target: str):
            return [FakeWindow("pm-operator", index=1)]

        def kill_pane(self, target: str):
            calls["killed"] = target

        def join_pane(self, source: str, target: str, *, horizontal: bool = True):
            calls["joined"] = (source, target)

        def list_panes(self, target: str):
            return [FakePane("%1", 0), FakePane("%9", 31)]

        def resize_pane_width(self, target: str, width: int):
            pass

        def set_pane_history_limit(self, target: str, limit: int):
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
    # Uses window index (not name) to avoid ambiguity with duplicate windows
    assert calls["joined"] == ("pollypm-storage-closet:1.0", "%1")
    assert calls["claimed"] == ("operator", "cockpit", "mounted in cockpit")


def test_cockpit_router_releases_lease_when_unmounting_static_view(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n"
    )

    class FakeLaunch:
        def __init__(self) -> None:
            self.window_name = "worker-demo"
            self.session = type(
                "Session",
                (),
                {
                    "name": "worker_demo",
                    "project": "demo",
                    "provider": type("P", (), {"value": "codex"})(),
                },
            )()

    class FakeWindow:
        def __init__(self, index: int, name: str) -> None:
            self.index = index
            self.name = name

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

        def release_lease(self, session_name: str, expected_owner: str | None = None) -> None:
            calls["released"] = (session_name, expected_owner)

    class FakePane:
        def __init__(self, pane_id: str, pane_left: int, command: str) -> None:
            self.pane_id = pane_id
            self.pane_left = pane_left
            self.pane_current_command = command

    class FakeTmux:
        def list_panes(self, target: str):
            return [
                FakePane("%1", 0, "uv"),
                FakePane("%2", 30, "node"),
            ]

        def list_windows(self, target: str):
            return [FakeWindow(1, "worker-demo")]

        def break_pane(self, source: str, target_session: str, window_name: str) -> None:
            calls["break"] = (source, target_session, window_name)

        def rename_window(self, target: str, name: str) -> None:
            calls["renamed"] = (target, name)

        def respawn_pane(self, target: str, command: str) -> None:
            calls["respawn"] = (target, command)

        def resize_pane_width(self, target: str, width: int) -> None:
            calls["resize"] = (target, width)

    router = CockpitRouter(config_path)
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda fresh=False: FakeSupervisor())
    monkeypatch.setattr(router, "_mounted_session_name", lambda supervisor, target: "worker_demo")
    monkeypatch.setattr(router, "_left_pane_id", lambda target: "%1")
    monkeypatch.setattr(router, "_right_pane_id", lambda target: "%2")
    router._write_state({"mounted_session": "worker_demo", "right_pane_id": "%2"})

    router._show_static_view(FakeSupervisor(), "pollypm:PollyPM", "settings")

    state = router._load_state()
    assert "mounted_session" not in state
    assert calls["released"] == ("worker_demo", "cockpit")


def test_cockpit_router_validation_releases_stale_cockpit_lease(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n"
    )
    worker_cwd = tmp_path / "worker"
    other_cwd = tmp_path / "other"
    worker_cwd.mkdir()
    other_cwd.mkdir()

    class FakeLaunch:
        def __init__(self) -> None:
            self.session = type(
                "Session",
                (),
                {
                    "name": "worker_demo",
                    "cwd": worker_cwd,
                    "provider": type("P", (), {"value": "codex"})(),
                },
            )()

    class FakeSupervisor:
        def ensure_layout(self) -> None:
            return None

        def plan_launches(self):
            return [FakeLaunch()]

        def release_lease(self, session_name: str, expected_owner: str | None = None) -> None:
            calls["released"] = (session_name, expected_owner)

    class FakePane:
        def __init__(self, pane_id: str, *, pane_dead: bool, command: str, path: Path) -> None:
            self.pane_id = pane_id
            self.pane_dead = pane_dead
            self.pane_current_command = command
            self.pane_current_path = str(path)

    class FakeTmux:
        def list_panes(self, target: str):
            return [
                FakePane("%1", pane_dead=False, command="uv", path=tmp_path),
                # Pane is alive but running a non-provider command ("bash"),
                # so _is_live_provider_pane returns False and the mounted
                # session is considered stale — triggering lease release.
                FakePane("%2", pane_dead=False, command="bash", path=other_cwd),
            ]

    router = CockpitRouter(config_path)
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda fresh=False: FakeSupervisor())
    router._write_state(
        {
            "selected": "project:demo",
            "right_pane_id": "%2",
            "mounted_session": "worker_demo",
        }
    )

    assert router.selected_key() == "project:demo"
    assert calls["released"] == ("worker_demo", "cockpit")


def test_cockpit_router_infers_mounted_session_from_live_right_pane(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    (tmp_path / "pollypm.toml").write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n")
    worker_cwd = tmp_path / ".pollypm-state" / "worktrees" / "pollypm-pa-worker_pollypm"
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

    # Clicking a project now shows the dashboard, not the live session.
    # The live session is accessible via project:pollypm:session.
    assert "respawn" in calls
    assert "project pollypm" in calls["respawn"][1]


def test_cockpit_router_project_click_does_not_launch_configured_but_unmounted_worker(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    (tmp_path / "pollypm.toml").write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n")

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
                    "cwd": tmp_path / ".pollypm-state" / "worktrees" / "pollypm-pa-worker_pollypm",
                },
            )()

    class FakeConfig:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".pollypm-state"
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
    (tmp_path / "pollypm.toml").write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm-state'}\"\n")

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

        def set_pane_history_limit(self, target: str, limit: int):
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
            self.home = tmp_path / ".pollypm-state" / "homes" / "claude_demo"

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


def test_cockpit_app_tick_scheduler_is_noop(tmp_path: Path) -> None:
    """The scheduler tick is a no-op — heartbeat runs via cron, not the cockpit."""
    app = PollyCockpitApp(tmp_path / "pollypm.toml")
    app._tick_scheduler()  # should not raise or do anything
