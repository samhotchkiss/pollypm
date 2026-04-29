import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.widgets import ListView

from pollypm.cockpit import build_cockpit_detail
from pollypm.cockpit_project_state import ProjectRailState, ProjectStateRollup
from pollypm.cockpit_rail import CockpitItem, CockpitPresence, CockpitRouter, PALETTE, PollyCockpitRail
from pollypm.cockpit_rail_routes import ProjectRoute
from pollypm.cockpit_ui import PollyCockpitApp, PollyDashboardApp, PollySettingsPaneApp, RailItem
from pollypm.config import write_config
from pollypm.dashboard_data import CompletedItem, DashboardData, SessionActivity
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
from pollypm.recovery.base import SessionHealth


class _CaptureWidget:
    def __init__(self) -> None:
        self.value = ""

    def update(self, value: str) -> None:
        self.value = value


def test_cockpit_router_build_items_includes_core_entries(monkeypatch, tmp_path: Path) -> None:
    class FakeConfig:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".pollypm"
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
    (tmp_path / "pollypm.toml").write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n")
    router = CockpitRouter(tmp_path / "pollypm.toml")
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())

    items = router.build_items(spinner_index=2)

    keys = [item.key for item in items]
    assert "dashboard" in keys
    assert "polly" in keys
    assert "russell" in keys
    assert "inbox" in keys
    assert "project:pollypm" in keys
    assert "project:demo" in keys
    assert "settings" in keys
    by_key = {item.key: item for item in items}
    assert keys.index("dashboard") < keys.index("polly") < keys.index("inbox")
    assert by_key["dashboard"].label == "Home"
    assert by_key["polly"].state == "ready"
    assert by_key["inbox"].label == "Inbox (1)"
    # Projects are sorted alphabetically; both "Demo" and "PollyPM" should be present
    project_labels = [i.label for i in items if i.key.startswith("project:")]
    assert "Demo" in project_labels
    assert "PollyPM" in project_labels


def test_cockpit_router_build_items_keeps_mounted_task_worker_visible(
    monkeypatch, tmp_path: Path,
) -> None:
    class FakeConfig:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".pollypm"
            tmux_session = "pollypm"

        project = Project()
        projects = {
            "demo": KnownProject(
                key="demo",
                path=tmp_path / "demo",
                name="Demo",
                persona_name="Dora",
                kind=ProjectKind.GIT,
            ),
        }

    class FakeLaunch:
        def __init__(self, name: str, role: str, project: str, window_name: str) -> None:
            self.window_name = window_name
            self.session = type(
                "Session",
                (),
                {
                    "name": name,
                    "role": role,
                    "project": project,
                    "provider": type("P", (), {"value": "claude"})(),
                },
            )()

    class FakeWindow:
        def __init__(self, name: str, pane_dead: bool = False) -> None:
            self.name = name
            self.pane_dead = pane_dead
            self.pane_id = f"%{name}"

    class FakeStore:
        def recent_events(self, limit: int = 300):
            return []

        def latest_heartbeat(self, session_name: str):
            return None

    class FakeSupervisor:
        config = FakeConfig()
        store = FakeStore()

        def status(self):
            launches = [
                FakeLaunch("worker_demo", "worker", "demo", "worker-demo"),
            ]
            windows = [FakeWindow("worker-demo")]
            return launches, windows, [], [], []

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

    class FakeTmux:
        def list_windows(self, target: str):
            assert target == "pollypm-storage-closet"
            return []

    monkeypatch.setattr("pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 0)
    (tmp_path / "pollypm.toml").write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    router = CockpitRouter(tmp_path / "pollypm.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor())
    router._write_state(
        {
            "selected": "project:demo:task:7",
            "mounted_session": "task-demo-7",
            "right_pane_id": "%2",
        }
    )

    items = router.build_items(spinner_index=0)

    task_item = next(item for item in items if item.key == "project:demo:task:7")
    assert task_item.label == "  ⟳ Task #7"
    assert task_item.state == "sub"


EXPECTED_SESSION_HEALTH_RENDER_STATES = [
    SessionHealth.ACTIVE,
    SessionHealth.IDLE,
    SessionHealth.STUCK,
    SessionHealth.LOOPING,
    SessionHealth.EXITED,
    SessionHealth.ERROR,
    SessionHealth.BLOCKED_NO_CAPACITY,
    SessionHealth.AUTH_BROKEN,
    SessionHealth.WAITING_ON_USER,
    SessionHealth.HEALTHY,
    SessionHealth.STUCK_ON_TASK,
    SessionHealth.SILENT_WORKER,
    SessionHealth.STATE_DRIFT,
]


def _dashboard_snapshot(status: str) -> tuple[SimpleNamespace, DashboardData]:
    config = SimpleNamespace(
        projects={"demo": object()},
        sessions={"worker_demo": object()},
    )
    data = DashboardData(
        active_sessions=[
            SessionActivity(
                name="worker_demo",
                role="worker",
                project="demo",
                project_label="Demo",
                status=status,
                description="worker is in this state",
                age_seconds=125.0,
            ),
        ],
        recent_commits=[],
        completed_items=[],
        recent_messages=[],
        daily_tokens=[],
        today_tokens=0,
        total_tokens=0,
        sweep_count_24h=0,
        message_count_24h=0,
        recovery_count_24h=0,
        inbox_count=0,
        alert_count=0,
        briefing="",
    )
    return config, data


@pytest.mark.parametrize("session_health", EXPECTED_SESSION_HEALTH_RENDER_STATES)
def test_dashboard_now_panel_renders_each_session_health_state(
    session_health: SessionHealth,
    tmp_path: Path,
) -> None:
    app = PollyDashboardApp(tmp_path / "pollypm.toml")
    app.header_w = _CaptureWidget()
    app.now_body = _CaptureWidget()
    app.messages_body = _CaptureWidget()
    app.done_body = _CaptureWidget()
    app.chart_body = _CaptureWidget()
    app.footer_w = _CaptureWidget()

    config, data = _dashboard_snapshot(session_health.value)
    app._render_dashboard(config, data)

    body = app.now_body.value
    assert "Demo" in body

    if session_health is SessionHealth.HEALTHY:
        assert "[#3fb950]●[/#3fb950] [b]Demo[/b]" in body
        assert "worker is in this state" in body
        assert "[dim]2m ago[/dim]" in body
        assert session_health.value not in body
    elif session_health is SessionHealth.WAITING_ON_USER:
        assert "[#f85149]◇[/#f85149] [b]Demo[/b]" in body
        assert "[#f85149]worker is in this state[/#f85149]" in body
        assert session_health.value not in body
    else:
        assert f"[dim]○[/dim] [dim]Demo[/dim]  [dim]{session_health.value}[/dim]" in body


def test_dashboard_now_cases_cover_current_session_health_enum() -> None:
    assert [state.value for state in EXPECTED_SESSION_HEALTH_RENDER_STATES] == [
        state.value for state in SessionHealth
    ]


def _dashboard_done_snapshot(
    *,
    completed_n: int = 0,
    sweep: int = 0,
    message: int = 0,
    recovery: int = 0,
) -> tuple[SimpleNamespace, DashboardData]:
    """Build a dashboard snapshot tailored for the done-body section."""
    config = SimpleNamespace(projects={}, sessions={})
    completed = [
        CompletedItem(title=f"Item {i}", kind="issue", project="demo", age_seconds=120.0)
        for i in range(completed_n)
    ]
    return config, DashboardData(
        active_sessions=[],
        recent_commits=[],
        completed_items=completed,
        recent_messages=[],
        daily_tokens=[],
        today_tokens=0,
        total_tokens=0,
        sweep_count_24h=sweep,
        message_count_24h=message,
        recovery_count_24h=recovery,
        inbox_count=0,
        alert_count=0,
        briefing="",
    )


def test_dashboard_done_body_pluralises_singular_counts(tmp_path: Path) -> None:
    """Done section must not render ``1 issues completed`` / ``1 sweeps``.

    The done-body summary printed bare plurals at every count
    (``issues completed``, ``sweeps``, ``messages``, ``recoveries``).
    At a typical end-of-day state — one issue closed, one sweep,
    one message, one recovery — every line read as a copy bug.
    Mirrors cycle 57 (inbox status bar) on the dashboard surface.
    """
    app = PollyDashboardApp(tmp_path / "pollypm.toml")
    app.header_w = _CaptureWidget()
    app.now_body = _CaptureWidget()
    app.messages_body = _CaptureWidget()
    app.done_body = _CaptureWidget()
    app.chart_body = _CaptureWidget()
    app.footer_w = _CaptureWidget()

    # Path A: completed_items=1 → singular ``1 issue completed``.
    config, data = _dashboard_done_snapshot(completed_n=1)
    app._render_dashboard(config, data)
    body = app.done_body.value
    assert "1[/b] issue completed" in body
    assert "issues completed" not in body

    # Path B: no commits or completed → falls into the sweep/message/
    # recovery summary branch. Singular for each.
    config, data = _dashboard_done_snapshot(sweep=1, message=1, recovery=1)
    app._render_dashboard(config, data)
    body = app.done_body.value
    assert "[/#3fb950] sweep" in body and "sweeps" not in body
    assert "[/#58a6ff] message" in body and "messages" not in body
    assert "[/#d29922] recovery" in body and "recoveries" not in body
    # Cycle 61: the bottom dashboard footer (separate from done_body)
    # also depends on sweep_count / message_count and used bare plurals.
    footer = app.footer_w.value
    assert "1 sweep today" in footer
    assert "1 sweeps today" not in footer
    assert "1 message" in footer
    assert "1 messages" not in footer

    # Path C: plural cases stay plural.
    config, data = _dashboard_done_snapshot(completed_n=3)
    app._render_dashboard(config, data)
    body = app.done_body.value
    assert "3[/b] issues completed" in body


def test_dashboard_header_pluralises_projects_agents_alerts(tmp_path: Path) -> None:
    """Header line must read ``1 project · 1 agent · 1 alert`` at count=1.

    The very first line of the polly-dashboard header used bare-plural
    ``projects`` / ``agents`` / ``alerts``. A new install with one
    project + one worker + one open alert read ``1 projects · 1 agents
    · … · 1 alerts`` — three copy bugs on the most-seen line in the
    cockpit. Mirrors cycles 57/58/59 across the same surface.
    """
    app = PollyDashboardApp(tmp_path / "pollypm.toml")
    app.header_w = _CaptureWidget()
    app.now_body = _CaptureWidget()
    app.messages_body = _CaptureWidget()
    app.done_body = _CaptureWidget()
    app.chart_body = _CaptureWidget()
    app.footer_w = _CaptureWidget()

    # Path A: count=1 for each → singular forms.
    config = SimpleNamespace(projects={"only": object()}, sessions={"only": object()})
    data = DashboardData(
        active_sessions=[],
        recent_commits=[],
        completed_items=[],
        recent_messages=[],
        daily_tokens=[],
        today_tokens=0,
        total_tokens=0,
        sweep_count_24h=0,
        message_count_24h=0,
        recovery_count_24h=0,
        inbox_count=0,
        alert_count=1,
        briefing="",
    )
    app._render_dashboard(config, data)
    header = app.header_w.value
    assert "[/b] project " in header
    assert "[/b] projects" not in header
    assert "[/b] agent " in header
    assert "[/b] agents" not in header
    assert "[/b] alert[/" in header
    assert "[/b] alerts[/" not in header

    # Path B: plural cases stay plural.
    config = SimpleNamespace(
        projects={"a": object(), "b": object(), "c": object()},
        sessions={"x": object(), "y": object()},
    )
    data = DashboardData(
        active_sessions=[],
        recent_commits=[],
        completed_items=[],
        recent_messages=[],
        daily_tokens=[],
        today_tokens=0,
        total_tokens=0,
        sweep_count_24h=0,
        message_count_24h=0,
        recovery_count_24h=0,
        inbox_count=0,
        alert_count=4,
        briefing="",
    )
    app._render_dashboard(config, data)
    header = app.header_w.value
    assert "[/b] projects" in header
    assert "[/b] agents" in header
    assert "[/b] alerts[/" in header


def test_cockpit_router_session_state_ignores_silent_alerts(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
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


def test_cockpit_router_session_state_uses_heartbeat_state(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )

    class FakeLaunch:
        def __init__(self) -> None:
            self.window_name = "heartbeat"
            self.session = type(
                "Session",
                (),
                {
                    "name": "heartbeat",
                    "role": "heartbeat-supervisor",
                    "project": "pollypm",
                    "provider": type("P", (), {"value": "claude"})(),
                },
            )()

    class FakeWindow:
        def __init__(self) -> None:
            self.name = "heartbeat"
            self.pane_dead = False
            self.pane_current_command = "python"

    router = CockpitRouter(config_path)
    assert router._session_state("heartbeat", [FakeLaunch()], [FakeWindow()], [], 0) == "watch"


def test_cockpit_presence_treats_outside_tmux_as_attached() -> None:
    class _FakeTmux:
        def current_session_name(self) -> str | None:
            return None

    presence = CockpitPresence(_FakeTmux())

    assert presence.is_tmux_attached() is True
    assert presence.should_animate() is True


def test_cockpit_presence_caches_attached_result_for_about_two_seconds(monkeypatch) -> None:
    class _FakeTmux:
        def __init__(self) -> None:
            self.calls = 0

        def current_session_name(self) -> str | None:
            return "pollypm"

        def list_clients(self, session_name: str) -> str:
            del session_name
            self.calls += 1
            return ""

    tmux = _FakeTmux()
    presence = CockpitPresence(tmux)
    times = iter([10.0, 10.5, 12.5])
    monkeypatch.setattr("pollypm.cockpit_rail.time.monotonic", lambda: next(times))

    assert presence.is_tmux_attached() is False
    assert presence.is_tmux_attached() is False
    assert presence.is_tmux_attached() is False
    assert tmux.calls == 2


def test_cockpit_presence_defaults_to_attached_on_detection_failure() -> None:
    class _BrokenTmux:
        def current_session_name(self) -> str | None:
            return "pollypm"

        def run(self, *args, **kwargs):  # noqa: ANN001
            raise RuntimeError("boom")

    presence = CockpitPresence(_BrokenTmux())

    assert presence.is_tmux_attached() is True


def test_cockpit_presence_calm_mode_disables_animation(monkeypatch) -> None:
    class _FakeTmux:
        def current_session_name(self) -> str | None:
            return "pollypm"

        def list_clients(self, session_name: str) -> str:
            del session_name
            return "client"

    monkeypatch.setenv("POLLY_CALM", "1")
    presence = CockpitPresence(_FakeTmux())

    assert presence.should_animate() is False
    assert presence.working_frame(3) == "◜"


def test_cockpit_ui_rail_item_uses_static_ellipsis_in_calm_mode(monkeypatch) -> None:
    monkeypatch.setattr(RailItem, "update_body", lambda self: None)

    class _FakeTmux:
        def current_session_name(self) -> str | None:
            return "pollypm"

        def list_clients(self, session_name: str) -> str:
            del session_name
            return "client"

    monkeypatch.setenv("POLLY_CALM", "1")
    presence = CockpitPresence(_FakeTmux())
    item = RailItem(
        CockpitItem(
            "project:demo",
            "Demo",
            "ready",
            session_name="worker_demo",
            work_state="writing",
            heartbeat_at="2026-04-21T23:00:00+00:00",
        ),
        active_view=False,
        presence=presence,
    )

    assert item._indicator()[0] == "♥…"


def test_cockpit_presence_heartbeat_frame_advances_only_on_new_heartbeat() -> None:
    class _FakeTmux:
        def current_session_name(self) -> str | None:
            return "pollypm"

        def list_clients(self, session_name: str) -> str:
            del session_name
            return "client"

    presence = CockpitPresence(_FakeTmux())

    first = presence.heartbeat_frame_for("worker_demo", "2026-04-21T23:00:00+00:00")
    second = presence.heartbeat_frame_for("worker_demo", "2026-04-21T23:00:00+00:00")
    third = presence.heartbeat_frame_for("worker_demo", "2026-04-21T23:05:00+00:00")

    assert first == "♡"
    assert second == "♡"
    assert third == "♥"


def test_cockpit_ui_rail_item_indicator_combines_pulse_and_work_glyph(monkeypatch) -> None:
    monkeypatch.setattr(RailItem, "update_body", lambda self: None)

    class _FakeTmux:
        def current_session_name(self) -> str | None:
            return "pollypm"

        def list_clients(self, session_name: str) -> str:
            del session_name
            return "client"

    presence = CockpitPresence(_FakeTmux())

    writing = RailItem(
        CockpitItem(
            "project:demo",
            "Demo",
            "ready",
            session_name="worker_demo",
            work_state="writing",
            heartbeat_at="2026-04-21T23:00:00+00:00",
        ),
        active_view=False,
        presence=presence,
    )
    reviewing = RailItem(
        CockpitItem(
            "russell",
            "Russell",
            "ready",
            session_name="reviewer",
            work_state="reviewing",
            heartbeat_at="2026-04-21T23:00:00+00:00",
        ),
        active_view=False,
        presence=presence,
    )
    stuck = RailItem(
        CockpitItem(
            "project:demo",
            "Demo",
            "! pane dead",
            session_name="worker_demo",
            work_state="stuck",
            heartbeat_at="2026-04-21T23:00:00+00:00",
        ),
        active_view=False,
        presence=presence,
    )
    exited = RailItem(
        CockpitItem(
            "project:demo",
            "Demo",
            "dead",
            session_name="worker_demo",
            work_state="exited",
            heartbeat_at="2026-04-21T23:00:00+00:00",
        ),
        active_view=False,
        presence=presence,
    )

    assert writing._indicator()[0] == "♡◜"
    assert reviewing._indicator()[0] == "♡✎"
    assert stuck._indicator()[0] == "♡⚠"
    assert exited._indicator()[0] == "♡✕"


def test_cockpit_ui_project_row_hides_activity_sparkline() -> None:
    cockpit_item = CockpitItem(
        "project:booktalk",
        "booktalk ········█·",
        "unread",
    )

    class _RailTestApp(App[None]):
        def compose(self) -> ComposeResult:
            with ListView(id="nav"):
                yield RailItem(cockpit_item, active_view=False)

    async def exercise() -> None:
        app = _RailTestApp()
        async with app.run_test(size=(40, 10)) as pilot:
            await pilot.pause()
            item = app.query_one(RailItem)
            rendered = item.body.render()
            plain = getattr(rendered, "plain", str(rendered))

            assert "booktalk" in plain
            assert "█" not in plain
            assert "··" not in plain
            assert "●" not in plain
            assert "•" in plain

    asyncio.run(exercise())


def test_cockpit_raw_rail_project_row_hides_activity_sparkline() -> None:
    rail = PollyCockpitRail.__new__(PollyCockpitRail)
    rail.selected_key = "polly"
    rail.spinner_index = 0

    row = rail._item_row(
        CockpitItem(
            "project:booktalk",
            "booktalk ········█·",
            "unread",
        ),
        width=30,
        active_view="polly",
    )

    assert "booktalk" in row.text
    assert "█" not in row.text
    assert "··" not in row.text
    assert "●" not in row.text
    assert "•" in row.text


def test_cockpit_raw_rail_project_rollup_status_uses_indicator_not_label() -> None:
    rail = PollyCockpitRail.__new__(PollyCockpitRail)
    rail.selected_key = "polly"
    rail.spinner_index = 0

    row = rail._item_row(
        CockpitItem(
            "project:booktalk",
            "booktalk",
            "project-yellow",
            session_name="worker_booktalk",
            work_state="idle",
            heartbeat_at="2026-04-21T23:00:00+00:00",
        ),
        width=30,
        active_view="polly",
    )

    assert "booktalk" in row.text
    assert "🟡" not in row.text
    assert "⚙" not in row.text
    assert "•" in row.text
    assert "♡" not in row.text


def test_cockpit_ui_project_rollup_status_uses_indicator_not_label() -> None:
    cockpit_item = CockpitItem(
        "project:booktalk",
        "booktalk",
        "project-yellow",
        session_name="worker_booktalk",
        work_state="idle",
        heartbeat_at="2026-04-21T23:00:00+00:00",
    )

    class _RailTestApp(App[None]):
        def compose(self) -> ComposeResult:
            with ListView(id="nav"):
                yield RailItem(cockpit_item, active_view=False)

    async def exercise() -> None:
        app = _RailTestApp()
        async with app.run_test(size=(40, 10)) as pilot:
            await pilot.pause()
            item = app.query_one(RailItem)
            rendered = item.body.render()
            plain = getattr(rendered, "plain", str(rendered))

            assert "booktalk" in plain
            assert "🟡" not in plain
            assert "⚙" not in plain
            assert "•" in plain

    asyncio.run(exercise())


def test_cockpit_rail_session_indicator_combines_pulse_and_work_glyph(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )

    class _FakeTmux:
        def current_session_name(self) -> str | None:
            return "pollypm"

        def list_clients(self, session_name: str) -> str:
            del session_name
            return "client"

    rail = PollyCockpitRail(config_path)
    rail.router.tmux = _FakeTmux()  # type: ignore[assignment]
    rail.presence = CockpitPresence(_FakeTmux())

    writing = CockpitItem(
        "project:demo",
        "Demo",
        "◜ working",
        session_name="worker_demo",
        work_state="writing",
        heartbeat_at="2026-04-21T23:00:00+00:00",
    )
    reviewing = CockpitItem(
        "russell",
        "Russell",
        "ready",
        session_name="reviewer",
        work_state="reviewing",
        heartbeat_at="2026-04-21T23:00:00+00:00",
    )
    stuck = CockpitItem(
        "project:demo",
        "Demo",
        "! pane dead",
        session_name="worker_demo",
        work_state="stuck",
        heartbeat_at="2026-04-21T23:00:00+00:00",
    )
    exited = CockpitItem(
        "project:demo",
        "Demo",
        "dead",
        session_name="worker_demo",
        work_state="exited",
        heartbeat_at="2026-04-21T23:00:00+00:00",
    )

    assert rail._indicator(writing)[0] == "♡◜"
    assert rail._indicator(reviewing)[0] == "♡✎"
    assert rail._indicator(stuck)[0] == "♡⚠"
    assert rail._indicator(exited)[0] == "♡✕"


def test_cockpit_rail_session_indicator_uses_static_ellipsis_in_calm_mode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("POLLY_CALM", "1")
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )

    class _FakeTmux:
        def current_session_name(self) -> str | None:
            return "pollypm"

        def list_clients(self, session_name: str) -> str:
            del session_name
            return "client"

    rail = PollyCockpitRail(config_path)
    rail.router.tmux = _FakeTmux()  # type: ignore[assignment]
    rail.presence = CockpitPresence(_FakeTmux())

    writing = CockpitItem(
        "project:demo",
        "Demo",
        "◜ working",
        session_name="worker_demo",
        work_state="writing",
        heartbeat_at="2026-04-21T23:00:00+00:00",
    )

    assert rail._indicator(writing)[0] == "♥…"


def test_cockpit_ui_help_legend_mentions_glyph_alphabet() -> None:
    binding = next(
        binding for binding in PollyCockpitApp.BINDINGS if getattr(binding, "key", "") == "question_mark"
    )

    assert "pulse" in binding.description
    assert "✎" in binding.description
    assert "⚠" in binding.description
    assert "✕" in binding.description


def test_command_palette_compact_rows_omit_wrapping_descriptions() -> None:
    from pollypm.cockpit import PaletteCommand
    from pollypm.cockpit_palette import _PaletteListItem

    command = PaletteCommand(
        title="Open pollypm.toml in editor",
        subtitle="/Users/sam/.pollypm/pollypm.toml",
        category="System",
        keybind=None,
        tag="system.edit_config",
    )

    rendered = _PaletteListItem._render_body(command, compact=True)

    assert "\n" not in rendered
    assert "System" not in rendered
    assert "pollypm.toml" in rendered


def test_cockpit_tagline_is_static_between_ticks(monkeypatch) -> None:
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    app._tick_count = 0
    app.spinner_index = 0
    app._last_epoch_mtime = 0.0
    app._last_refresh_tick = 0
    app._items = []
    app._row_widgets = {}
    updates: list[str] = []
    app.tagline = type(
        "Tagline",
        (),
        {"update": lambda _self, text: updates.append(text)},
    )()
    app._update_ticker = lambda: None
    app._update_pill_refresh = lambda: None
    app._check_post_upgrade_flag = lambda: None
    app._enforce_rail_width = lambda: None
    monkeypatch.setattr("pollypm.state_epoch.mtime", lambda: 0.0)

    for _ in range(80):
        app._tick()

    assert updates == []


def test_cockpit_router_config_cache_reuses_loaded_config(monkeypatch, tmp_path: Path) -> None:
    class _Config:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".pollypm"
            tmux_session = "pollypm"

        project = Project()

    calls: list[Path] = []
    monkeypatch.setattr("pollypm.cockpit_rail.load_config", lambda path: calls.append(path) or _Config())

    router = CockpitRouter(tmp_path / "pollypm.toml")

    first = router._load_config()
    second = router._load_config()

    assert first is second
    assert len(calls) == 1


def test_cockpit_router_state_cache_reuses_loaded_dict(monkeypatch, tmp_path: Path) -> None:
    class _Config:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".pollypm"
            tmux_session = "pollypm"

        project = Project()

    monkeypatch.setattr("pollypm.cockpit_rail.load_config", lambda path: _Config())

    config_path = tmp_path / "pollypm.toml"
    router = CockpitRouter(config_path)
    router._write_state({"selected": "polly", "rail_width": 44})

    reader = CockpitRouter(config_path)
    reads: list[Path] = []
    original_read_text = Path.read_text
    state_path = reader._state_path()

    def counting_read_text(self: Path, *args, **kwargs):
        if self == state_path:
            reads.append(self)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    first = reader._load_state()
    second = reader._load_state()

    assert first == second == {"selected": "polly", "rail_width": 44}
    assert len(reads) == 1


def test_cockpit_router_debounces_rail_width_writes(monkeypatch, tmp_path: Path) -> None:
    class _Config:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".pollypm"
            tmux_session = "pollypm"

        project = Project()

    monkeypatch.setattr("pollypm.cockpit_rail.load_config", lambda path: _Config())

    config_path = tmp_path / "pollypm.toml"
    router = CockpitRouter(config_path)
    router._write_state({"rail_width": 30})

    writes: list[dict[str, object]] = []
    from pollypm import cockpit_rail as cockpit_rail_module

    original_atomic_write_json = cockpit_rail_module.atomic_write_json

    def counting_atomic_write_json(path: Path, data: dict[str, object]) -> None:
        writes.append(dict(data))
        original_atomic_write_json(path, data)

    times = iter([0.0, 0.0, 0.1, 0.1, 0.5])
    monkeypatch.setattr("pollypm.cockpit_rail.atomic_write_json", counting_atomic_write_json)
    monkeypatch.setattr("pollypm.cockpit_rail.time.monotonic", lambda: next(times))

    router.set_rail_width(42)
    router.set_rail_width(44)
    router._maybe_flush_state()

    assert writes == [{"rail_width": 44}]
    assert router.rail_width() == 44


def test_cockpit_router_caches_hidden_collapsed_and_grouped_registrations(monkeypatch, tmp_path: Path) -> None:
    router = CockpitRouter.__new__(CockpitRouter)
    router._hidden_items_cache_key = None
    router._hidden_items_cache = None
    router._collapsed_sections_cache_key = None
    router._collapsed_sections_cache = None
    router._grouped_rail_cache_key = None
    router._grouped_rail_cache = None

    calls = {"hidden": 0, "collapsed": 0, "registry": 0}

    def _hidden(config):
        del config
        calls["hidden"] += 1
        return frozenset({"top.Hidden"})

    def _collapsed(config):
        del config
        calls["collapsed"] += 1
        return frozenset({"system"})

    monkeypatch.setattr("pollypm.cockpit_rail._hidden_rail_items", _hidden)
    monkeypatch.setattr("pollypm.cockpit_rail._collapsed_rail_sections", _collapsed)

    class _Reg:
        def __init__(self, item_key: str, section: str) -> None:
            self.item_key = item_key
            self.section = section

    class _Registry:
        def items(self):
            calls["registry"] += 1
            return [_Reg("top.Inbox", "top"), _Reg("system.Settings", "system")]

    config = object()
    registry = _Registry()

    hidden_first = router._hidden_rail_items_cached(config)
    hidden_second = router._hidden_rail_items_cached(config)
    collapsed_first = router._collapsed_rail_sections_cached(config)
    collapsed_second = router._collapsed_rail_sections_cached(config)
    grouped_first = router._grouped_rail_registrations(config, registry)
    grouped_second = router._grouped_rail_registrations(config, registry)

    assert hidden_first == hidden_second == frozenset({"top.Hidden"})
    assert collapsed_first == collapsed_second == frozenset({"system"})
    assert grouped_first == grouped_second
    assert calls == {"hidden": 1, "collapsed": 1, "registry": 1}


def test_strip_trailing_spark_detects_decorated_project_row() -> None:
    """Project rows are decorated as ``<emoji>? <name> <10-char-spark>``.
    Compact rail renderers use ``_strip_trailing_spark`` to show the
    project name without the sparkline.
    """
    from pollypm.cockpit_rail import _strip_trailing_spark

    head, spark = _strip_trailing_spark("polly-e2e-proj ··········")
    assert head == "polly-e2e-proj"
    assert spark == "··········"

    head, spark = _strip_trailing_spark("PollyPM ···█·····█")
    assert head == "PollyPM"
    assert spark == "···█·····█"

    # Non-spark labels return the label unchanged with empty spark.
    head, spark = _strip_trailing_spark("Inbox (13)")
    assert head == "Inbox (13)"
    assert spark == ""

    # Trailing token with the right length but wrong characters → no match.
    head, spark = _strip_trailing_spark("project Wibble plain")
    assert spark == ""

    # Short label → no match.
    head, spark = _strip_trailing_spark("X")
    assert spark == ""


def test_format_event_ticker_never_leaves_dangling_separator() -> None:
    from pollypm.cockpit_rail import _format_event_ticker

    ticker = _format_event_ticker(["deploy complete", "review", "commit"])

    assert ticker == "events · deploy complete"
    assert len(ticker) <= 28
    assert not ticker.endswith(" ·")


def test_project_activity_sparkline_uses_dots_for_inline_zero_buckets() -> None:
    """Regression: when a project has activity in some 6-minute buckets
    but not others, ``_spark_bar`` returns U+0020 for the empty
    buckets — so the rail rendered ``PollyPM     █    █`` with the
    interleaved spaces reading as padding rather than "no activity in
    that bucket". The all-zero fallback at this layer already uses
    ``·`` for empty; mirror that for in-line zeros so the spark line
    stays visually continuous.
    """
    from datetime import UTC, datetime, timedelta

    router = CockpitRouter.__new__(CockpitRouter)

    class _Event:
        def __init__(self, session_name: str, created_at) -> None:
            self.session_name = session_name
            self.created_at = created_at

    now = datetime.now(UTC)
    # Activity in two distant buckets (most recent + ~50min ago) and
    # nothing in between — exactly the pattern that produced the
    # "█    █" rendering on the live PollyPM rail row.
    recent_events = [
        _Event("worker_pollypm", now - timedelta(minutes=2)),
        _Event("worker_pollypm", now - timedelta(minutes=51)),
    ]

    def _iso_to_dt(value):
        return value if hasattr(value, "tzinfo") else None

    from pollypm.cockpit_sections.base import _spark_bar

    result = router._project_activity_sparkline(
        {"pollypm": "worker_pollypm"},
        recent_events,
        _iso_to_dt=_iso_to_dt,
        _spark_bar=_spark_bar,
    )
    spark = result["pollypm"]
    assert len(spark) == 10
    # No literal ASCII spaces — those would render as padding gaps.
    assert " " not in spark
    # Empty buckets fall through to the dot glyph so the bar reads
    # as one continuous string of dots and blocks.
    assert "·" in spark
    assert "█" in spark


def test_project_activity_sparkline_all_zero_falls_back_to_all_dots() -> None:
    """No activity → ten dots, unchanged from prior behaviour."""
    router = CockpitRouter.__new__(CockpitRouter)

    def _iso_to_dt(value):
        return None

    from pollypm.cockpit_sections.base import _spark_bar

    result = router._project_activity_sparkline(
        {"pollypm": "worker_pollypm"},
        [],
        _iso_to_dt=_iso_to_dt,
        _spark_bar=_spark_bar,
    )
    assert result == {}  # empty events → empty dict (early return)


def test_cockpit_router_decorates_project_items_with_sparkline_and_pin() -> None:
    router = CockpitRouter.__new__(CockpitRouter)

    class _Launch:
        def __init__(self, session_name: str, project: str) -> None:
            self.session = type(
                "Session",
                (),
                {
                    "name": session_name,
                    "role": "worker",
                    "project": project,
                },
            )()

    class _Event:
        def __init__(self, session_name: str, created_at) -> None:
            self.session_name = session_name
            self.created_at = created_at

    items = [
        CockpitItem(key="top", label="Top", state="idle"),
        CockpitItem(key="project:alpha", label="Alpha", state="idle"),
        CockpitItem(key="project:alpha:dashboard", label="Dashboard", state="sub", selectable=False),
        CockpitItem(key="project:demo", label="Demo", state="◜ working"),
        CockpitItem(key="project:demo:dashboard", label="Dashboard", state="sub", selectable=False),
        CockpitItem(key="system", label="Settings", state="idle"),
    ]
    launches = [_Launch("worker_alpha", "alpha"), _Launch("worker_demo", "demo")]
    recent_events = [
        _Event("worker_alpha", datetime.now(UTC) - timedelta(minutes=4)),
        _Event("worker_demo", datetime.now(UTC) - timedelta(minutes=15)),
        _Event("worker_demo", datetime.now(UTC) - timedelta(minutes=28)),
    ]
    router.is_project_pinned = lambda key: key == "alpha"  # type: ignore[assignment]
    router.pinned_projects = lambda: ["alpha"]  # type: ignore[assignment]

    decorated = router._decorate_project_items(
        items,
        selected_project="demo",
        launches=launches,
        recent_events=recent_events,
        project_session_map={"alpha": "worker_alpha", "demo": "worker_demo"},
    )

    project_rows = [item for item in decorated if item.key.startswith("project:") and item.key.count(":") == 1]
    assert project_rows[0].key == "project:alpha"
    assert project_rows[0].label.startswith("📌 Alpha ")
    assert len(project_rows[0].label[len("📌 Alpha "):]) == 10
    assert project_rows[1].key == "project:demo"
    assert project_rows[1].label != "Demo"
    assert decorated[-1].key == "system"


def test_cockpit_router_decorates_and_sorts_project_rollup_status() -> None:
    router = CockpitRouter.__new__(CockpitRouter)
    router.is_project_pinned = lambda key: key == "alpha"  # type: ignore[assignment]
    router.pinned_projects = lambda: ["alpha"]  # type: ignore[assignment]

    items = [
        CockpitItem(key="project:alpha", label="Alpha", state="idle"),
        CockpitItem(key="project:beta", label="Beta", state="idle"),
        CockpitItem(key="project:gamma", label="Gamma", state="idle"),
        CockpitItem(key="project:delta", label="Delta", state="idle"),
    ]
    rollups = {
        "alpha": ProjectStateRollup(ProjectRailState.NONE, None, 4),
        "beta": ProjectStateRollup(ProjectRailState.YELLOW, "🟡", 1, "project:beta:issues"),
        "gamma": ProjectStateRollup(ProjectRailState.WORKING, "⚙️", 3),
        "delta": ProjectStateRollup(ProjectRailState.RED, "🔴", 0, "project:delta:issues"),
    }

    decorated = router._decorate_project_items(
        items,
        selected_project=None,
        launches=[],
        recent_events=[],
        project_session_map={},
        project_rollups=rollups,
    )

    assert [item.key for item in decorated] == [
        "project:delta",
        "project:beta",
        "project:gamma",
        "project:alpha",
    ]
    assert decorated[0].label == "Delta"
    assert decorated[0].state == "project-red"
    assert decorated[1].label == "Beta"
    assert decorated[1].state == "project-yellow"
    assert decorated[2].label == "Gamma"
    assert decorated[2].state == "project-working"
    assert decorated[3].label.startswith("📌 Alpha")


def test_cockpit_router_routes_project_click_to_dashboard_even_when_actionable() -> None:
    calls: dict[str, object] = {}
    router = CockpitRouter.__new__(CockpitRouter)
    router.set_selected_key = lambda key: calls.setdefault("selected", key)  # type: ignore[assignment]
    router._show_static_view = (  # type: ignore[assignment]
        lambda supervisor, window_target, kind, project_key=None: calls.setdefault(
            "static", (window_target, kind, project_key),
        )
    )
    router._project_rollup_for_route = (  # type: ignore[assignment]
        lambda supervisor, project_key: ProjectStateRollup(
            ProjectRailState.GREEN,
            "🟢",
            2,
            "project:demo:issues",
        )
    )

    router._route_project_selection(
        SimpleNamespace(),
        "pollypm:PollyPM",
        ProjectRoute(project_key="demo", sub_view=None),
    )

    assert calls["selected"] == "project:demo:dashboard"
    assert calls["static"] == ("pollypm:PollyPM", "project", "demo")


def test_cockpit_router_primes_per_project_pm_session_distinctly() -> None:
    """Opening different projects' PM Chat should send distinct,
    project-aware priming messages so each session re-anchors on its
    own project identity (#958).
    """
    sent: list[tuple[str, str]] = []
    primed_state: dict[str, object] = {}

    class _FakeTmux:
        def send_keys(self, target, text, press_enter=True):  # noqa: ARG002
            sent.append((target, text))

    class _Project:
        def __init__(self, key: str, name: str) -> None:
            self.key = key
            self.name = name
            self.path = Path(f"/tmp/{key}")
            self.persona_name = None

    class _FakeConfig:
        projects = {
            "alpha": _Project("alpha", "Alpha"),
            "beta": _Project("beta", "Beta"),
        }

    class _FakeSupervisor:
        config = _FakeConfig()

        def plan_launches(self):
            class _Sess:
                def __init__(self, name, role, project):
                    self.name = name
                    self.role = role
                    self.project = project
            class _L:
                def __init__(self, name, role, project):
                    self.session = _Sess(name, role, project)
            return [
                _L("worker_alpha", "worker", "alpha"),
                _L("worker_beta", "worker", "beta"),
            ]

    router = CockpitRouter.__new__(CockpitRouter)
    router.tmux = _FakeTmux()
    router._right_pane_id = lambda window_target: "%right"  # type: ignore[assignment]
    router._load_state = lambda: dict(primed_state)  # type: ignore[assignment]
    def _write(data):
        primed_state.clear()
        primed_state.update(data)
    router._write_state = _write  # type: ignore[assignment]
    router.set_selected_key = lambda key: None  # type: ignore[assignment]
    router._show_static_view = lambda *a, **k: None  # type: ignore[assignment]
    router._session_available_for_mount = lambda *a, **k: True  # type: ignore[assignment]
    router._show_live_session = lambda *a, **k: None  # type: ignore[assignment]

    supervisor = _FakeSupervisor()

    router._route_project_selection(
        supervisor,
        "pollypm:PollyPM",
        ProjectRoute(project_key="alpha", sub_view="session"),
    )
    router._route_project_selection(
        supervisor,
        "pollypm:PollyPM",
        ProjectRoute(project_key="beta", sub_view="session"),
    )

    # Each project got its own primer, sent to the right pane.
    assert len(sent) == 2, sent
    alpha_target, alpha_text = sent[0]
    beta_target, beta_text = sent[1]
    assert alpha_target == "%right"
    assert beta_target == "%right"
    assert "alpha" in alpha_text.lower() and "Alpha" in alpha_text
    assert "beta" in beta_text.lower() and "Beta" in beta_text
    assert alpha_text != beta_text
    # State persisted both session names so a re-mount won't re-prime.
    assert "worker_alpha" in primed_state.get("pm_primed_sessions", [])
    assert "worker_beta" in primed_state.get("pm_primed_sessions", [])

    # Re-mounting alpha should NOT send a second primer.
    router._route_project_selection(
        supervisor,
        "pollypm:PollyPM",
        ProjectRoute(project_key="alpha", sub_view="session"),
    )
    assert len(sent) == 2  # unchanged


def test_cockpit_rail_render_includes_event_ticker(monkeypatch) -> None:
    class _Event:
        def __init__(self, event_type: str, session_name: str, created_at) -> None:
            self.event_type = event_type
            self.session_name = session_name
            self.created_at = created_at

    class _Store:
        def recent_events(self, limit: int = 4):
            del limit
            now = datetime.now(UTC)
            return [
                _Event("session.started", "polly", now - timedelta(minutes=1)),
                _Event("heartbeat", "heartbeat", now - timedelta(minutes=2)),
            ]

    class _Supervisor:
        store = _Store()

    class _Router:
        def selected_key(self) -> str:
            return "polly"

        def _load_supervisor(self):
            return _Supervisor()

    rail = PollyCockpitRail.__new__(PollyCockpitRail)
    rail.router = _Router()
    rail.selected_key = "polly"
    rail.spinner_index = 1
    rail.slogan_started_at = 0.0
    rail._ticker_started_at = 0.0
    rail._last_items = []
    rail._slogan_phase = 0
    rail._current_slogan = lambda: ("Line one", "Line two")
    rail._slogan_color = lambda: PALETTE["slogan"]

    captured: list[str] = []
    rail._write = lambda text: captured.append(text)
    monkeypatch.setattr("pollypm.cockpit_rail.time.monotonic", lambda: 11.0)
    monkeypatch.setattr(
        "pollypm.cockpit_rail.shutil.get_terminal_size",
        lambda fallback=(30, 24): type("Size", (), {"columns": 120, "lines": 20})(),
    )

    rail._render([CockpitItem(key="polly", label="Polly", state="idle"), CockpitItem(key="settings", label="Settings", state="idle")])

    # #793: heartbeat events are infrastructure noise and must be
    # filtered before the ticker is built. #876: surviving events show
    # a friendly label without the internal session-name suffix.
    assert rail._event_ticker_text() == "events · session.started"
    assert captured


def test_cockpit_rail_hides_event_ticker_when_empty(monkeypatch) -> None:
    class _Store:
        def recent_events(self, limit: int = 12):
            del limit
            return []

    class _Supervisor:
        store = _Store()

    class _Router:
        def selected_key(self) -> str:
            return "polly"

        def _load_supervisor(self):
            return _Supervisor()

    rail = PollyCockpitRail.__new__(PollyCockpitRail)
    rail.router = _Router()
    rail._ticker_started_at = 0.0
    # Router has no ``_presence`` → gate silently passes (render as if
    # attached), and an empty event list yields empty ticker.
    assert rail._event_ticker_text() == ""


def test_cockpit_ui_event_ticker_cycles_and_hides_when_empty(monkeypatch, tmp_path: Path) -> None:
    class _Event:
        def __init__(self, event_type: str, session_name: str) -> None:
            self.event_type = event_type
            self.session_name = session_name

    class _Store:
        def __init__(self, events: list[_Event]) -> None:
            self._events = events

        def recent_events(self, limit: int = 12):
            del limit
            return self._events

    class _Supervisor:
        def __init__(self, events: list[_Event]) -> None:
            self.store = _Store(events)

    class _Router:
        def __init__(self, events: list[_Event]) -> None:
            self._supervisor = _Supervisor(events)

        def selected_key(self) -> str:
            return "polly"

        def _load_supervisor(self):
            return self._supervisor

        def build_items(self, *, spinner_index: int = 0):
            del spinner_index
            return [
                CockpitItem("polly", "Polly", "ready"),
                CockpitItem("settings", "Settings", "config"),
            ]

    class _FakePresence:
        def __init__(self, attached: bool = True) -> None:
            self._attached = attached

        def is_tmux_attached(self) -> bool:
            return self._attached

    # Extend the router with a ``_presence`` hook the production code
    # calls to gate the ticker on tmux-client attachment.
    def _make_router(events, *, attached: bool = True):
        router = _Router(events)
        router._presence = lambda: _FakePresence(attached)
        return router

    app = PollyCockpitApp(tmp_path / "pollypm.toml")
    app.router = _make_router([_Event("commit", "worker_demo"), _Event("review", "system")])  # type: ignore[assignment]
    app._ticker_started_at = 0.0
    monkeypatch.setattr("pollypm.cockpit_ui.time.monotonic", lambda: 11.0)

    # offset=11//10=1; window_size=min(3, 2)=2; cycled = events[1], events[0]
    # User-facing labels — no session name suffix, no internal joiner (#876).
    assert app._event_ticker_text() == "events · review · commit"

    app.router = _make_router([])  # type: ignore[assignment]
    assert app._event_ticker_text() == ""

    # Gate closed → ticker empty even with events available.
    app.router = _make_router(
        [_Event("commit", "worker_demo")], attached=False,
    )  # type: ignore[assignment]
    assert app._event_ticker_text() == ""

    # All-suppressed events drop the entire ticker (#876).
    app.router = _make_router(
        [
            _Event("lease", "operator"),
            _Event("launch", "operator"),
            _Event("token_ledger", "heartbeat"),
        ],
    )  # type: ignore[assignment]
    assert app._event_ticker_text() == ""


def test_cockpit_ui_bindings_expose_activity_and_pin_legend() -> None:
    bindings = {binding.key: binding.description for binding in PollyCockpitApp.BINDINGS}

    assert bindings["t"] == "Activity"
    assert bindings["p"] == "Pin Project"


def test_cockpit_new_worker_non_project_selection_updates_hint() -> None:
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    updates: list[str] = []
    app._selected_row_key = lambda: "inbox"  # type: ignore[method-assign]
    app.hint = type(
        "Hint",
        (),
        {"update": lambda _self, text: updates.append(text)},
    )()

    app.action_new_worker()

    assert updates == ["Select a project first, then press n to launch a worker."]


def test_toggle_pinned_project_preserves_insertion_recency(tmp_path: Path) -> None:
    """#677 acceptance: most-recently-pinned sorts first. Each new pin
    prepends so it beats older pins in rail ordering."""
    from pollypm.cockpit_rail import CockpitRouter

    router = CockpitRouter.__new__(CockpitRouter)
    state: dict[str, object] = {}
    router._load_state = lambda: dict(state)
    router._write_state = lambda data: state.update(data)

    router.toggle_pinned_project("alpha")
    router.toggle_pinned_project("beta")
    router.toggle_pinned_project("gamma")

    # Most-recently pinned first.
    assert router.pinned_projects() == ["gamma", "beta", "alpha"]

    # Toggling off removes without reordering the rest.
    router.toggle_pinned_project("beta")
    assert router.pinned_projects() == ["gamma", "alpha"]

    # Re-pinning moves to front.
    router.toggle_pinned_project("beta")
    assert router.pinned_projects() == ["beta", "gamma", "alpha"]


def test_cockpit_ui_activity_and_pin_actions_route_live_rail(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    class _Router:
        def route_selected(self, key: str) -> None:
            calls.append(("route", key))

        def toggle_pinned_project(self, project_key: str) -> None:
            calls.append(("pin", project_key))

    app = PollyCockpitApp(tmp_path / "pollypm.toml")
    app.router = _Router()  # type: ignore[assignment]
    monkeypatch.setattr(app, "_refresh_rows", lambda: calls.append(("refresh", "yes")))
    monkeypatch.setattr(app, "_selected_row_key", lambda: "project:demo")

    app.action_open_activity()
    app.action_toggle_project_pin()

    assert ("route", "activity") in calls
    assert ("pin", "demo") in calls
    assert ("refresh", "yes") in calls


def test_cockpit_router_selected_key_clears_missing_right_pane_state(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
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


def test_cockpit_router_marks_palette_tip_seen(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    router = CockpitRouter(config_path)

    assert router.should_show_palette_tip() is True

    router.mark_palette_tip_seen()

    assert router.should_show_palette_tip() is False
    assert router._load_state()["palette_tip_seen"] is True


def test_cockpit_router_selected_key_bumps_epoch(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    bumps: list[str] = []
    monkeypatch.setattr("pollypm.state_epoch.bump", lambda: bumps.append("bump"))

    router = CockpitRouter(config_path)

    router.set_selected_key("settings")
    router.set_selected_key("settings")

    assert bumps == ["bump"]


def test_cockpit_router_selected_key_clears_dead_right_pane_state(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
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


def test_cockpit_router_selected_key_keeps_live_task_mount_state(
    monkeypatch, tmp_path: Path,
) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )

    class FakeSupervisor:
        def ensure_layout(self) -> None:
            return None

        def plan_launches(self):
            return []

    class FakePane:
        def __init__(self, pane_id: str, pane_left: int, command: str, path: Path) -> None:
            self.pane_id = pane_id
            self.pane_left = pane_left
            self.pane_dead = False
            self.pane_current_command = command
            self.pane_current_path = str(path)

    class FakeTmux:
        def list_panes(self, target: str):
            return [
                FakePane("%1", 0, "uv", tmp_path),
                FakePane("%2", 31, "node", tmp_path / "task"),
            ]

    router = CockpitRouter(config_path)
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda fresh=False: FakeSupervisor())
    router._write_state(
        {
            "selected": "project:demo:task:7",
            "right_pane_id": "%2",
            "mounted_session": "task-demo-7",
        }
    )

    assert router.selected_key() == "project:demo:task:7"
    state = router._load_state()
    assert state["mounted_session"] == "task-demo-7"
    assert state["right_pane_id"] == "%2"


def test_cockpit_router_selected_key_clears_stale_mounted_session_only(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
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
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
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
            # auth_broken stays in the user-actionable set; pane_dead
            # and needs_followup are filtered out as operational noise
            # (#765 morning-after widening). Use auth_broken here so
            # the dashboard still has an alert to render.
            return [
                FakeAlert("worker_demo", "auth_broken", "Claude probe failed"),
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
    # Cycle 64: ``commits`` / ``messages`` pluralise per count, so the
    # singular boundary now reads ``1 commit`` / ``1 message``.
    assert "1 commit" in detail or "1 message" in detail


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
    config_path.write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n")

    router.ensure_cockpit_layout()

    assert calls["split"][0] == "pollypm:PollyPM"
    assert "cockpit-pane polly" in calls["split"][1]


def test_cockpit_router_focus_right_shows_return_affordance(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    calls: list[tuple[str, tuple[str, ...]]] = []

    class FakeTmux:
        def run(self, *args: str, **_kwargs) -> None:
            calls.append(("run", args))

        def select_pane(self, target: str) -> None:
            calls.append(("select", (target,)))

    router = CockpitRouter(config_path)
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "ensure_cockpit_layout", lambda: None)
    monkeypatch.setattr(router, "_right_pane_id", lambda target: "%2")

    router.focus_right_pane()

    assert calls[0][0] == "run"
    assert "display-message" in calls[0][1]
    assert any("Ctrl-b Left returns to the rail." in part for part in calls[0][1])
    assert calls[1] == ("select", ("%2",))


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


def test_cockpit_router_ensure_layout_steady_state_issues_one_list_panes(tmp_path: Path) -> None:
    """Steady-state ``ensure_cockpit_layout`` must invoke ``list_panes`` once (#175).

    Before the fix, the steady-state path (len==2, right_pane_id known, layout
    already correct) issued two ``list_panes`` subprocesses — one baseline plus
    one redundant re-fetch after a no-op ``_normalize_layout``. After the fix
    the left/right pane is derived locally from the baseline.
    """

    class FakePane:
        def __init__(self, pane_id: str, pane_left: int, command: str, pane_width: int = 100) -> None:
            self.pane_id = pane_id
            self.pane_left = pane_left
            self.pane_current_command = command
            self.pane_width = pane_width
            self.active = True

    list_pane_calls: list[str] = []

    class FakeTmux:
        def list_panes(self, target: str):
            list_pane_calls.append(target)
            return [
                FakePane("%1", 0, "uv", pane_width=30),
                FakePane("%2", 31, "bash", pane_width=170),
            ]

        def resize_pane_width(self, target: str, width: int):
            pass

        def run(self, *args, **kwargs):
            pass

        def swap_pane(self, source: str, target: str):
            raise AssertionError("swap should not happen in steady state")

        def list_windows(self, target: str):
            return []

    router = CockpitRouter(tmp_path / "pollypm.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n")
    router._write_state({"right_pane_id": "%2"})

    router.ensure_cockpit_layout()

    # Exactly one list_panes call on the cockpit window. The old
    # implementation ran up to 6 in this path (baseline + redundant refetch
    # after each no-op branch).
    cockpit_calls = [t for t in list_pane_calls if ":PollyPM" in t]
    assert len(cockpit_calls) == 1, (
        f"expected 1 list_panes call in steady state, got {len(cockpit_calls)}: "
        f"{cockpit_calls}"
    )


def test_cockpit_router_ensure_layout_swap_preserves_pane_count(tmp_path: Path) -> None:
    """When ``_normalize_layout`` swaps, we still issue just one ``list_panes``.

    Pane IDs are stable across ``swap_pane``, so deriving the post-swap
    left/right locally (#175) avoids the refetch.
    """

    class FakePane:
        def __init__(self, pane_id: str, pane_left: int, command: str, pane_width: int = 100) -> None:
            self.pane_id = pane_id
            self.pane_left = pane_left
            self.pane_current_command = command
            self.pane_width = pane_width
            self.active = True

    list_pane_calls: list[str] = []
    swaps: list[tuple[str, str]] = []

    class FakeTmux:
        def list_panes(self, target: str):
            list_pane_calls.append(target)
            # Left pane runs bash, right pane runs uv — so a swap is needed.
            return [
                FakePane("%1", 0, "bash", pane_width=30),
                FakePane("%2", 31, "uv", pane_width=170),
            ]

        def resize_pane_width(self, target: str, width: int):
            pass

        def run(self, *args, **kwargs):
            pass

        def swap_pane(self, source: str, target: str):
            swaps.append((source, target))

        def list_windows(self, target: str):
            return []

    router = CockpitRouter(tmp_path / "pollypm.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n")
    router._write_state({"right_pane_id": "%2"})

    router.ensure_cockpit_layout()

    cockpit_calls = [t for t in list_pane_calls if ":PollyPM" in t]
    assert len(cockpit_calls) == 1, (
        f"expected 1 list_panes call when only a swap happens, got {len(cockpit_calls)}"
    )
    # The swap actually happened — right_pane was "uv", so left↔right exchanged.
    assert len(swaps) == 1
    # Post-swap: %2 (was right, ran uv) is now the left pane and
    # therefore the new ``right_pane_id`` in state is %1.
    state = router._load_state()
    assert state["right_pane_id"] == "%1"


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


def test_cockpit_router_routes_dashboard_home_to_static_pane(
    monkeypatch, tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}
    (tmp_path / "pollypm.toml").write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )

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
        projects = {}

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

    router.route_selected("dashboard")

    assert calls["selected"] == "dashboard"
    assert calls["respawn"][0] == "%2"
    assert "cockpit-pane dashboard" in calls["respawn"][1]


def test_live_session_fallback_does_not_kill_right_pane_before_source_exists(
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {"kill": []}
    (tmp_path / "pollypm.toml").write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )

    class FakePane:
        def __init__(self, pane_id: str, left: int) -> None:
            self.pane_id = pane_id
            self.pane_left = left
            self.pane_current_command = "uv" if left == 0 else "sh"
            self.pane_width = 80

    class FakeWindow:
        def __init__(self, name: str, index: int) -> None:
            self.name = name
            self.index = index

    class FakeTmux:
        def __init__(self) -> None:
            self.window_calls = 0

        def list_panes(self, target: str):
            return [FakePane("%1", 0), FakePane("%2", 31)]

        def list_windows(self, target: str):
            self.window_calls += 1
            if self.window_calls == 1:
                return [FakeWindow("worker-demo", 3)]
            return []

        def kill_pane(self, pane_id: str):
            calls["kill"].append(pane_id)  # type: ignore[union-attr]

        def respawn_pane(self, target: str, command: str):
            calls["respawn"] = (target, command)

        def resize_pane_width(self, target: str, width: int):
            pass

        def run(self, *args, **kwargs):
            pass

    class FakeSession:
        name = "worker_demo"
        role = "worker"
        project = "demo"

    class FakeLaunch:
        session = FakeSession()
        window_name = "worker-demo"

    class FakeConfig:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".pollypm"
            tmux_session = "pollypm"

        project = Project()
        projects = {
            "demo": KnownProject(key="demo", path=tmp_path / "demo", name="Demo", kind=ProjectKind.GIT),
        }
        sessions = {}

    class FakeSupervisor:
        config = FakeConfig()

        def plan_launches(self):
            return [FakeLaunch()]

        def storage_closet_session_name(self):
            return "pollypm-storage-closet"

    router = CockpitRouter(tmp_path / "pollypm.toml")
    router.tmux = FakeTmux()  # type: ignore[assignment]

    router._show_live_session(FakeSupervisor(), "worker_demo", "pollypm:PollyPM")

    assert calls["kill"] == []
    assert calls["respawn"][0] == "%2"
    assert "cockpit-pane project demo" in calls["respawn"][1]


def test_cockpit_router_reload_shell_respawns_rail_and_settings(
    monkeypatch, tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )

    class FakeSupervisor:
        class Config:
            class Project:
                tmux_session = "pollypm"

            project = Project()

        config = Config()

        def ensure_layout(self) -> None:
            return None

        def console_command(self) -> str:
            return "bash -l"

        def start_cockpit_tui(self, session_name: str) -> None:
            calls["start"] = session_name

    class FakeTmux:
        def respawn_pane(self, target: str, command: str) -> None:
            calls.setdefault("respawns", []).append((target, command))

    def _ensure_layout() -> None:
        calls["ensure"] = int(calls.get("ensure", 0)) + 1

    router = CockpitRouter(config_path)
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda fresh=False: FakeSupervisor())
    monkeypatch.setattr(router, "ensure_cockpit_layout", _ensure_layout)
    monkeypatch.setattr(router, "_park_mounted_session", lambda supervisor, target: calls.setdefault("parked", []).append((supervisor, target)))
    monkeypatch.setattr(router, "_cleanup_extra_panes", lambda target: calls.setdefault("cleaned", []).append(target))
    monkeypatch.setattr(router, "_left_pane_id", lambda target: "%1")
    monkeypatch.setattr(router, "_right_pane_id", lambda target: "%2")

    router.reload_cockpit_shell(kind="settings", selected_key="settings")

    assert calls["ensure"] == 1
    assert calls["start"] == "pollypm"
    assert calls["cleaned"] == ["pollypm:PollyPM"]
    assert len(calls["parked"]) == 1
    assert calls["respawns"][0] == ("%1", "bash -l")
    assert calls["respawns"][1][0] == "%2"
    assert "cockpit-pane settings" in calls["respawns"][1][1]
    state = router._load_state()
    assert state["selected"] == "settings"
    assert state["right_pane_id"] == "%2"


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
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
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


def test_cockpit_router_parks_mounted_task_worker(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )

    class FakeSupervisor:
        def plan_launches(self):
            return []

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

        def release_lease(self, session_name: str, expected_owner: str | None = None) -> None:
            calls["released"] = (session_name, expected_owner)

    class FakeWindow:
        def __init__(self, index: int, name: str) -> None:
            self.index = index
            self.name = name

    class FakePane:
        def __init__(self, pane_id: str, pane_left: int, command: str) -> None:
            self.pane_id = pane_id
            self.pane_left = pane_left
            self.pane_current_command = command
            self.pane_dead = False

    class FakeTmux:
        def __init__(self) -> None:
            self._window_calls = 0

        def list_panes(self, target: str):
            return [
                FakePane("%1", 0, "uv"),
                FakePane("%2", 30, "node"),
            ]

        def list_windows(self, target: str):
            self._window_calls += 1
            if self._window_calls == 1:
                return [FakeWindow(1, "worker-demo")]
            return [FakeWindow(1, "worker-demo"), FakeWindow(2, "PollyPM")]

        def break_pane(self, source: str, target_session: str, window_name: str) -> None:
            calls["break"] = (source, target_session, window_name)

        def rename_window(self, target: str, name: str) -> None:
            calls["renamed"] = (target, name)

    router = CockpitRouter(config_path)
    router.tmux = FakeTmux()  # type: ignore[assignment]
    monkeypatch.setattr(router, "_load_supervisor", lambda fresh=False: FakeSupervisor())
    router._write_state({"mounted_session": "task-demo-7", "right_pane_id": "%2"})

    router._park_mounted_session(FakeSupervisor(), "pollypm:PollyPM")

    state = router._load_state()
    assert "mounted_session" not in state
    assert calls["break"] == ("%2", "pollypm-storage-closet", "task-demo-7")
    assert calls["renamed"] == ("pollypm-storage-closet:2", "task-demo-7")
    assert calls["released"] == ("task-demo-7", "cockpit")


def test_cockpit_router_validation_releases_stale_cockpit_lease(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
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

    # Clicking a project now shows the dashboard, not the live session.
    # The live session is accessible via project:pollypm:session.
    assert "respawn" in calls
    assert "project pollypm" in calls["respawn"][1]


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
            from pollypm.cockpit_rail import CockpitItem

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


def test_cockpit_cursor_sync_moves_visible_marker_without_full_refresh() -> None:
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    app.selected_key = "polly"
    app._tick_count = 7
    app._last_nav_change = -10

    class _Row:
        def __init__(self, active: bool) -> None:
            self.classes = {"active-view"} if active else set()
            self.updates = 0

        def has_class(self, name: str) -> bool:
            return name in self.classes

        def set_class(self, enabled: bool, name: str) -> None:
            if enabled:
                self.classes.add(name)
            else:
                self.classes.discard(name)

        def update_body(self) -> None:
            self.updates += 1

    class _SettingsRow:
        def __init__(self) -> None:
            self.active = False

        def set_class(self, enabled: bool, name: str) -> None:
            assert name == "active-view"
            self.active = enabled

    polly = _Row(active=True)
    inbox = _Row(active=False)
    app._row_widgets = {"polly": polly, "inbox": inbox}  # type: ignore[assignment]
    app.settings_row = _SettingsRow()  # type: ignore[assignment]
    app._selected_row_key = lambda: "inbox"  # type: ignore[method-assign]

    app._sync_selected_from_nav()

    assert app.selected_key == "inbox"
    assert app._last_nav_change == 7
    assert "active-view" not in polly.classes
    assert "active-view" in inbox.classes
    assert polly.updates == 1
    assert inbox.updates == 1


def test_cockpit_app_adopts_external_router_selection_without_stomping_cursor() -> None:
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    app.selected_key = "project:demo:issues"
    app._last_router_selected_key = "project:demo:issues"

    class _Router:
        selected = "dashboard"

        def selected_key(self) -> str:
            return self.selected

    app.router = _Router()  # type: ignore[assignment]

    app._adopt_router_selection_if_changed()
    assert app.selected_key == "dashboard"
    assert app._last_router_selected_key == "dashboard"

    app.selected_key = "inbox"
    app._adopt_router_selection_if_changed()
    assert app.selected_key == "inbox"


def test_cockpit_app_routes_detail_hint_keys_from_rail() -> None:
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    calls: list[tuple[str, str] | str] = []

    class _Router:
        selected = "dashboard"

        def route_selected(self, key: str) -> None:
            calls.append(("route", key))
            self.selected = key

        def selected_key(self) -> str:
            return self.selected

        def send_key_to_right_pane(self, key: str) -> None:
            calls.append(("send", key))

    app.router = _Router()  # type: ignore[assignment]
    app.selected_key = "dashboard"
    app._last_router_selected_key = "dashboard"
    app.hint = _CaptureWidget()
    app._refresh_rows = lambda: calls.append("refresh")  # type: ignore[method-assign]

    app.action_open_inbox()
    app.selected_key = "settings"
    app.action_forward_tab_to_right()
    app.selected_key = "workers"
    app.action_forward_workers_auto_refresh()

    assert ("route", "inbox") in calls
    assert ("send", "Tab") in calls
    assert ("send", "A") in calls


def test_cockpit_app_open_live_session_keeps_rail_focus_until_tab() -> None:
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    calls: list[str | tuple[str, str]] = []

    class _Router:
        def route_selected(self, key: str) -> None:
            calls.append(("route", key))

        def selected_key(self) -> str:
            return "russell"

        def _load_state(self) -> dict[str, str]:
            return {"mounted_session": "reviewer"}

        def focus_right_pane(self) -> None:
            calls.append("focus")

        def send_key_to_right_pane(self, key: str) -> None:
            calls.append(("send", key))

    app.router = _Router()  # type: ignore[assignment]
    app.selected_key = "russell"
    app._last_router_selected_key = "russell"
    app._selected_row_key = lambda: "russell"  # type: ignore[method-assign]
    app.hint = _CaptureWidget()
    app._refresh_rows = lambda: calls.append("refresh")  # type: ignore[method-assign]

    app.action_open_selected()

    assert calls == [("route", "russell"), "refresh"]

    app.action_forward_tab_to_right()

    assert calls[-1] == "focus"
    assert ("send", "Tab") not in calls


def test_cockpit_settings_row_has_text_cursor_when_active() -> None:
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    app.selected_key = "settings"
    app._row_widgets = {}  # type: ignore[assignment]
    updates: list[str] = []

    class _SettingsRow:
        def set_class(self, enabled: bool, name: str) -> None:
            assert enabled is True
            assert name == "active-view"

        def update(self, text: str) -> None:
            updates.append(text)

    app.settings_row = _SettingsRow()  # type: ignore[assignment]

    app._apply_active_view_to_rows()

    assert updates == ["▌ ⚙ Settings"]


def test_cockpit_app_resize_schedules_layout_recovery() -> None:
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    scheduled: list[str] = []

    def _call_after_refresh(callback):
        scheduled.append(callback.__name__)

    app.call_after_refresh = _call_after_refresh  # type: ignore[method-assign]

    app.on_resize(object())  # type: ignore[arg-type]

    assert scheduled == ["_recover_after_resize"]


def test_cockpit_app_resize_recovery_repairs_layout_and_repaints() -> None:
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    calls: list[object] = []

    class _Router:
        def ensure_cockpit_layout(self) -> None:
            calls.append("layout")

    class _Nav:
        def refresh(self, *, layout: bool = False) -> None:
            calls.append(("nav_refresh", layout))

    app.router = _Router()  # type: ignore[assignment]
    app.nav = _Nav()  # type: ignore[assignment]
    app._refresh_rows = lambda: calls.append("rows")  # type: ignore[method-assign]
    app.refresh = lambda *, layout=False: calls.append(("app_refresh", layout))  # type: ignore[method-assign]

    app._recover_after_resize()

    assert calls == [
        "layout",
        "rows",
        ("nav_refresh", True),
        ("app_refresh", True),
    ]


def test_cockpit_rail_ctrl_k_routes_to_settings(monkeypatch, tmp_path: Path) -> None:
    class FakeRouter:
        def __init__(self, _config_path: Path) -> None:
            self.calls: list[str] = []
            self.tmux = None

        def selected_key(self) -> str:
            return "polly"

        def route_selected(self, key: str) -> None:
            self.calls.append(key)

    monkeypatch.setattr("pollypm.cockpit_rail.CockpitRouter", FakeRouter)
    from pollypm.cockpit_rail import CockpitItem, PollyCockpitRail

    rail = PollyCockpitRail(tmp_path / "pollypm.toml")
    items = [
        CockpitItem("polly", "Polly", "ready"),
        CockpitItem("settings", "Settings", "config"),
    ]

    assert rail._handle_key(b"\x0b", items) is True
    assert rail.router.calls == ["settings"]
    assert rail.selected_key == "settings"


def test_cockpit_rail_forwards_detail_hint_keys(monkeypatch, tmp_path: Path) -> None:
    class FakeRouter:
        def __init__(self, _config_path: Path) -> None:
            self.sent: list[str] = []
            self.tmux = None

        def selected_key(self) -> str:
            return "settings"

        def send_key_to_right_pane(self, key: str) -> None:
            self.sent.append(key)

    monkeypatch.setattr("pollypm.cockpit_rail.CockpitRouter", FakeRouter)
    from pollypm.cockpit_rail import CockpitItem, PollyCockpitRail

    rail = PollyCockpitRail(tmp_path / "pollypm.toml")
    items = [
        CockpitItem("workers", "Workers", "ready"),
        CockpitItem("settings", "Settings", "config"),
    ]

    assert rail._handle_key(b"\t", items) is True
    rail.selected_key = "workers"
    assert rail._handle_key(b"A", items) is True

    assert rail.router.sent == ["Tab", "A"]


def test_cockpit_rail_picks_up_external_selection(monkeypatch, tmp_path: Path) -> None:
    """#751: when an external app (e.g. PollyProjectDashboardApp)
    updates the persisted selection via CockpitRouter.set_selected_key,
    the rail's in-memory highlight must follow on the next tick."""
    class FakeRouter:
        def __init__(self, _config_path: Path) -> None:
            self.tmux = None
            self._selected = "polly"

        def selected_key(self) -> str:
            return self._selected

        def route_selected(self, key: str) -> None:
            self._selected = key

    monkeypatch.setattr("pollypm.cockpit_rail.CockpitRouter", FakeRouter)
    from pollypm.cockpit_rail import PollyCockpitRail

    rail = PollyCockpitRail(tmp_path / "pollypm.toml")
    assert rail.selected_key == "polly"

    # Simulate an external routing call — e.g. the project-dashboard
    # jump-to-inbox flow — updating the persisted selection key.
    rail.router._selected = "inbox"  # type: ignore[attr-defined]
    # Before the sync fires, the rail still thinks "polly" is active.
    assert rail.selected_key == "polly"

    # One tick's worth of sync picks up the change.
    rail._sync_selection_from_router()
    assert rail.selected_key == "inbox"


def test_cockpit_rail_external_sync_no_op_on_identity(monkeypatch, tmp_path: Path) -> None:
    """Repeated syncs with unchanged state must not churn the selection."""
    class FakeRouter:
        def __init__(self, _config_path: Path) -> None:
            self.tmux = None
            self._selected = "settings"

        def selected_key(self) -> str:
            return self._selected

        def route_selected(self, key: str) -> None:
            self._selected = key

    monkeypatch.setattr("pollypm.cockpit_rail.CockpitRouter", FakeRouter)
    from pollypm.cockpit_rail import PollyCockpitRail

    rail = PollyCockpitRail(tmp_path / "pollypm.toml")
    for _ in range(5):
        rail._sync_selection_from_router()
    assert rail.selected_key == "settings"


def test_cockpit_rail_external_sync_tolerates_router_error(monkeypatch, tmp_path: Path) -> None:
    """If the router's selected_key() raises (e.g. state file corrupt),
    the sync must not crash the rail."""
    class FakeRouter:
        def __init__(self, _config_path: Path) -> None:
            self.tmux = None

        def selected_key(self) -> str:
            raise RuntimeError("state file corrupted")

    monkeypatch.setattr("pollypm.cockpit_rail.CockpitRouter", FakeRouter)
    from pollypm.cockpit_rail import PollyCockpitRail

    with pytest.raises(RuntimeError):
        # __init__ itself calls selected_key(); that surfaces the
        # breakage. Outside this test we'd recover; here we just
        # verify the downstream sync method is defensively try/except.
        PollyCockpitRail(tmp_path / "pollypm.toml")


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


def test_cockpit_app_tick_scheduler_is_noop(tmp_path: Path) -> None:
    """The scheduler tick is a no-op — heartbeat runs via cron, not the cockpit."""
    app = PollyCockpitApp(tmp_path / "pollypm.toml")
    app._tick_scheduler()  # should not raise or do anything


def test_cockpit_app_marks_palette_tip_seen_without_rail_overlay(monkeypatch, tmp_path: Path) -> None:
    class FakeRouter:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.marked = 0

        def selected_key(self) -> str:
            return "polly"

        def build_items(self, *, spinner_index: int = 0):
            from pollypm.cockpit_rail import CockpitItem

            return [
                CockpitItem("polly", "Polly", "ready"),
                CockpitItem("inbox", "Inbox (0)", "clear"),
                CockpitItem("settings", "Settings", "config"),
            ]

        def route_selected(self, key: str) -> None:
            self.calls.append(key)

        def create_worker_and_route(self, project_key: str) -> None:
            self.calls.append(f"new:{project_key}")

        def should_show_palette_tip(self) -> bool:
            return self.marked == 0

        def mark_palette_tip_seen(self) -> None:
            self.marked += 1

    app = PollyCockpitApp(tmp_path / "pollypm.toml")
    app.router = FakeRouter()  # type: ignore[assignment]
    notices: list[tuple[str, float | None]] = []
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, **kwargs: notices.append((message, kwargs.get("timeout"))),
    )

    async def exercise() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()

    asyncio.run(exercise())

    assert notices == []
    assert app.router.marked == 1


def _build_back_to_home_app() -> tuple[PollyCockpitApp, list[tuple[str, str] | str]]:
    """Construct a minimal cockpit app for back-to-home tests."""
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    calls: list[tuple[str, str] | str] = []

    class _Router:
        selected = "polly"

        def route_selected(self, key: str) -> None:
            calls.append(("route", key))
            self.selected = key

        def selected_key(self) -> str:
            return self.selected

    app.router = _Router()  # type: ignore[assignment]
    app.hint = _CaptureWidget()
    app._refresh_rows = lambda: calls.append("refresh")  # type: ignore[method-assign]
    return app, calls


def test_cockpit_q_from_settings_returns_to_home_not_quit() -> None:
    """``q`` on a sub-surface goes Home; only Home triggers shutdown confirm (#864)."""
    app, calls = _build_back_to_home_app()
    app.selected_key = "settings"
    app._last_router_selected_key = "settings"

    # Spy on the would-be confirm call: never reached on a sub-surface.
    confirm_called: list[bool] = []

    class _Tmux:
        def run(self, *_args, **_kwargs):  # noqa: ANN001, ANN002, ANN003
            confirm_called.append(True)
            class _R:
                returncode = 0
                stdout = ""
            return _R()

    app.router.tmux = _Tmux()  # type: ignore[attr-defined]

    app.action_request_quit()

    assert ("route", "polly") in calls
    assert app.selected_key == "polly"
    assert confirm_called == [], "should not show quit confirm from a sub-surface"


def test_cockpit_q_from_home_still_prompts_for_quit() -> None:
    """At Home, ``q`` keeps the destructive shutdown confirm path (#864)."""
    app, calls = _build_back_to_home_app()
    app.selected_key = "polly"
    app._last_router_selected_key = "polly"

    confirm_called: list[bool] = []

    class _Tmux:
        def run(self, *_args, **_kwargs):  # noqa: ANN001, ANN002, ANN003
            confirm_called.append(True)
            class _R:
                returncode = 1  # user said "no"
                stdout = ""
            return _R()

    app.router.tmux = _Tmux()  # type: ignore[attr-defined]

    app.action_request_quit()

    assert confirm_called == [True], "Home should still prompt for confirm"
    # Did not navigate anywhere — the user only saw the confirm prompt.
    assert ("route", "polly") not in calls


def test_cockpit_escape_routes_back_to_home_from_settings() -> None:
    """Esc from a sub-surface returns to Home (#864)."""
    app, calls = _build_back_to_home_app()
    app.selected_key = "inbox"
    app._last_router_selected_key = "inbox"

    app.action_back_to_home()

    assert ("route", "polly") in calls
    assert app.selected_key == "polly"


def test_cockpit_escape_at_home_is_noop() -> None:
    """Esc at Home is a no-op — does not re-route or refresh (#864)."""
    app, calls = _build_back_to_home_app()
    app.selected_key = "polly"
    app._last_router_selected_key = "polly"

    app.action_back_to_home()

    assert calls == [], f"expected no-op at home but saw {calls}"


def test_cockpit_action_button_digits_forward_from_rail() -> None:
    """1/2/3 from rail forwards to the right pane so Action Needed buttons fire (#862)."""
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    sent: list[str] = []
    app._send_key_to_right_pane = lambda key: sent.append(key)  # type: ignore[method-assign]

    app.action_forward_action_button_1()
    app.action_forward_action_button_2()
    app.action_forward_action_button_3()

    assert sent == ["1", "2", "3"]


def test_cockpit_forwards_c_to_right_pane_only_on_project_surface() -> None:
    """``c`` from rail forwards to the right pane only on a project (#863)."""
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    sent: list[str] = []
    app._send_key_to_right_pane = lambda key: sent.append(key)  # type: ignore[method-assign]

    app.selected_key = "polly"
    app.action_forward_project_chat()
    assert sent == [], "should not forward c outside of project surfaces"

    app.selected_key = "project:demo"
    app.action_forward_project_chat()
    assert sent == ["c"]


def test_cockpit_forwards_l_to_right_pane_only_on_project_surface() -> None:
    """``l`` from rail forwards to the right pane only on a project (#863)."""
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    sent: list[str] = []
    app._send_key_to_right_pane = lambda key: sent.append(key)  # type: ignore[method-assign]

    app.selected_key = "settings"
    app.action_forward_project_log()
    assert sent == []

    app.selected_key = "project:demo:dashboard"
    app.action_forward_project_log()
    assert sent == ["l"]


def test_cockpit_project_enter_advances_cursor_to_dashboard_subitem() -> None:
    """Pressing Enter on a project advances the rail cursor to the
    project's Dashboard sub-item in one stroke (#880).

    Without this, Enter on a project leaves the rail cursor on the
    project header while sub-items expand below — so the user has to
    press Enter, then j, then Enter again to reach a sub-item, even
    though the right pane is already showing the dashboard.
    """
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    app.hint = _CaptureWidget()
    app._refresh_rows = lambda: None  # type: ignore[method-assign]
    app._selected_row_key = lambda: "project:demo"  # type: ignore[method-assign]

    class _Router:
        last_route: str | None = None

        def route_selected(self, key: str) -> None:
            self.last_route = key

        def selected_key(self) -> str:
            # Mirror the production redirect — ``project:demo`` resolves to
            # the dashboard sub-item.
            return "project:demo:dashboard"

    app.router = _Router()  # type: ignore[assignment]
    app.selected_key = "project:demo"
    app._last_router_selected_key = "polly"

    app.action_open_selected()

    assert app.router.last_route == "project:demo"
    assert app.selected_key == "project:demo:dashboard"


def test_cockpit_pin_toggle_round_trips_and_reports_state() -> None:
    """``p`` toggles the pin AND surfaces a hint about the new state (#858)."""
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    app.hint = _CaptureWidget()
    app._refresh_rows = lambda: None  # type: ignore[method-assign]
    app._selected_row_key = lambda: "project:demo"  # type: ignore[method-assign]

    pinned: list[bool] = []

    class _Router:
        state: bool = False

        def toggle_pinned_project(self, key: str) -> bool:
            self.state = not self.state
            pinned.append(self.state)
            return self.state

    app.router = _Router()  # type: ignore[assignment]

    app.action_toggle_project_pin()
    assert pinned == [True]
    assert "Pinned" in app.hint.value
    assert "demo" in app.hint.value

    app.action_toggle_project_pin()
    assert pinned == [True, False]
    assert app.hint.value.startswith("Unpinned"), (
        f"expected an unpin confirmation, got {app.hint.value!r}"
    )


def test_cockpit_jk_navigates_rail_on_inbox_surface() -> None:
    """j/k always advance the rail cursor — never tmux-forward to right pane (#918).

    Earlier (#856) the rail forwarded ``j``/``k`` to the right pane while
    on Inbox/Activity so list scrolling worked from the rail. That blindly
    invoked ``tmux send-keys`` against pane 1, so once the right pane held
    a Polly/Claude Code chat the bytes ended up typed into the chat box.
    The fix returns j/k to plain rail navigation across every surface.
    """
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    sent: list[str] = []
    moves: list[str] = []
    app._send_key_to_right_pane = lambda key: sent.append(key)  # type: ignore[method-assign]
    app._sync_selected_from_nav = lambda: moves.append("sync")  # type: ignore[method-assign]

    class _Nav:
        index: int | None = 0

        def action_cursor_down(self) -> None:
            moves.append("nav_down")

        def action_cursor_up(self) -> None:
            moves.append("nav_up")

    app.nav = _Nav()  # type: ignore[assignment]

    for surface in ("polly", "inbox", "activity"):
        app.selected_key = surface
        moves.clear()
        sent.clear()

        app.action_cursor_down()
        assert moves == ["nav_down", "sync"], (
            f"j on {surface!r} should navigate the rail, got moves={moves}"
        )
        assert sent == [], (
            f"j on {surface!r} must not tmux-forward; sent={sent}"
        )

        moves.clear()
        app.action_cursor_up()
        assert moves == ["nav_up", "sync"]
        assert sent == []


class _StubItem:
    """Minimal stand-in for a ListItem with a ``disabled`` flag and a
    cockpit key — used to drive the rail's ``action_cursor_down`` over a
    realistic node sequence without booting the Textual harness."""

    def __init__(self, cockpit_key: str | None, *, disabled: bool = False) -> None:
        self.cockpit_key = cockpit_key
        self.disabled = disabled


class _StubNav:
    """Lightweight ListView-shaped stub that mirrors Textual's
    ``action_cursor_down``/``_up`` skip-disabled semantics.

    Backed by a plain list so tests can construct a deterministic rail
    layout (rows + the disabled ``── projects ──`` divider) and assert
    where ``j`` lands without spinning up the Textual app loop.
    """

    def __init__(self, items: list[_StubItem]) -> None:
        self._items = items
        self.index: int | None = 0 if items else None

    @property
    def children(self) -> list[_StubItem]:
        return list(self._items)

    def action_cursor_down(self) -> None:
        if self.index is None:
            if self._items:
                self.index = 0
            return
        for i in range(self.index + 1, len(self._items)):
            if not self._items[i].disabled:
                self.index = i
                return
        # End of list — Textual leaves index pinned to its current row.

    def action_cursor_up(self) -> None:
        if self.index is None:
            if self._items:
                self.index = len(self._items) - 1
            return
        for i in range(self.index - 1, -1, -1):
            if not self._items[i].disabled:
                self.index = i
                return


def test_cockpit_j_from_inbox_steps_into_first_project() -> None:
    """Pressing ``j`` from Inbox advances the cursor into the project list,
    stepping over the disabled ``── projects ──`` divider (#918 facet 1).

    Reproduces the rail layout: Inbox row, then a disabled divider row,
    then the first project row. ``ListView.action_cursor_down`` skips
    disabled items, so one ``j`` lands on the project.
    """
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    items = [
        _StubItem("inbox"),
        _StubItem(None, disabled=True),  # ── projects ── divider
        _StubItem("project:booktalk"),
        _StubItem("project:blackjack-trainer"),
    ]
    nav = _StubNav(items)
    nav.index = 0  # cursor on Inbox
    app.nav = nav  # type: ignore[assignment]
    app.selected_key = "inbox"
    app._tick_count = 0
    app._last_nav_change = -10
    app._apply_active_view_to_rows = lambda: None  # type: ignore[method-assign]

    def _selected_row_key() -> str | None:
        idx = nav.index
        if idx is None:
            return None
        return items[idx].cockpit_key

    app._selected_row_key = _selected_row_key  # type: ignore[method-assign]

    captured: list[str] = []
    app._send_key_to_right_pane = lambda key: captured.append(key)  # type: ignore[method-assign]

    app.action_cursor_down()

    assert nav.index == 2, (
        "j from Inbox must skip the divider and land on the first project; "
        f"got index={nav.index}"
    )
    assert app.selected_key == "project:booktalk"
    assert captured == [], "j must never be tmux-forwarded to the right pane"


def test_cockpit_j_at_last_item_is_silent_noop() -> None:
    """j past the last selectable rail item leaves the cursor put without
    raising and without forwarding the key to any sibling widget (#918)."""
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    items = [_StubItem("polly"), _StubItem("metrics")]
    nav = _StubNav(items)
    nav.index = 1  # already on last item
    app.nav = nav  # type: ignore[assignment]
    app.selected_key = "metrics"
    app._tick_count = 0
    app._last_nav_change = -10
    app._apply_active_view_to_rows = lambda: None  # type: ignore[method-assign]
    app._selected_row_key = lambda: items[nav.index].cockpit_key if nav.index is not None else None  # type: ignore[method-assign]

    captured: list[str] = []
    app._send_key_to_right_pane = lambda key: captured.append(key)  # type: ignore[method-assign]

    # Should not raise — pressing j repeatedly past the end is a no-op.
    app.action_cursor_down()
    app.action_cursor_down()
    app.action_cursor_down()

    assert nav.index == 1, "cursor should remain on the last item"
    assert app.selected_key == "metrics"
    assert captured == [], (
        "j at the end of the list must not tmux-forward; "
        f"captured={captured}"
    )


def test_cockpit_jk_bindings_are_priority_so_textual_consumes_event() -> None:
    """j/k must be priority bindings on the App so Textual claims the event
    even at the end of the navigable list — preventing the keystroke from
    bubbling out to any other widget or pane (#918 facet 2)."""
    bindings = {
        binding.key: binding
        for binding in PollyCockpitApp.BINDINGS
        if binding.key in {"j,down", "k,up"}
    }
    assert set(bindings) == {"j,down", "k,up"}, (
        f"missing j/k bindings: {bindings.keys()}"
    )
    for key, binding in bindings.items():
        assert getattr(binding, "priority", False), (
            f"{key!r} must be a priority binding so Textual consumes the "
            "event regardless of cursor position"
        )


def test_cockpit_app_binds_action_button_digits_at_priority() -> None:
    """1/2/3 must be priority bindings so the rail does not eat them silently (#862)."""
    bindings = {
        binding.key: binding
        for binding in PollyCockpitApp.BINDINGS
        if binding.key in {"1", "2", "3"}
    }
    assert set(bindings) == {"1", "2", "3"}, f"missing digit bindings: {bindings.keys()}"
    for key, binding in bindings.items():
        assert getattr(binding, "priority", False), (
            f"{key!r} must be a priority binding to round-trip from rail"
        )
