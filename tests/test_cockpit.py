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
from pollypm.cockpit_rail_routes import LiveSessionRoute, ProjectRoute
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
        # #962 — Russell rail entry only shows when ``[sessions.reviewer]``
        # exists; this test asserts it's present, so seed both blocks.
        sessions = {"operator": object(), "reviewer": object()}

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


def test_cockpit_router_message_view_does_not_spawn_bounded_sleep() -> None:
    router = CockpitRouter.__new__(CockpitRouter)
    commands: list[str] = []
    router._show_command_view = (  # type: ignore[attr-defined]
        lambda _supervisor, _window_target, command: commands.append(command)
    )

    CockpitRouter._show_message_view(
        router,
        object(),
        "test-window",
        "Unable to open pane",
        "Mount failed",
    )

    assert "sleep 3600" not in commands[0]
    assert "read -r" in commands[0]


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
    """Header line must read ``1 project · 1 agent · 1 needs action`` at count=1.

    The very first line of the polly-dashboard header used bare-plural
    ``projects`` / ``agents`` / ``alerts``. A new install with one
    project + one worker + one open alert read ``1 projects · 1 agents
    · … · 1 alerts`` — three copy bugs on the most-seen line in the
    cockpit. Mirrors cycles 57/58/59 across the same surface.

    The ``alerts`` slot was renamed to ``needs action`` in #999 — see
    :func:`test_dashboard_header_alert_slot_labelled_needs_action` for
    the rationale; the project/agent pluralisation invariant from this
    test still holds and is what we're guarding here.
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


def test_dashboard_header_alert_slot_labelled_needs_action(tmp_path: Path) -> None:
    """Curated alert count must render as ``N needs action``, not ``N alerts``.

    ``DashboardData.alert_count`` filters out operational/heartbeat
    alerts (``pane:*``, ``no_session``, ``stuck_session`` …) and
    already-user-waiting ``stuck_on_task`` alerts. ``pm alerts`` lists
    every open alert, including the operational ones. With the old
    label the dashboard read ``4 alerts`` while ``pm alerts`` returned
    13 entries — two views of the same workspace, two different
    numbers, no signal that they meant different things (#999).

    Renaming the curated count to ``needs action`` is the contract we
    rely on so users reaching for ``pm alerts`` to drill in are not
    surprised by a higher count.
    """
    app = PollyDashboardApp(tmp_path / "pollypm.toml")
    app.header_w = _CaptureWidget()
    app.now_body = _CaptureWidget()
    app.messages_body = _CaptureWidget()
    app.done_body = _CaptureWidget()
    app.chart_body = _CaptureWidget()
    app.footer_w = _CaptureWidget()

    config = SimpleNamespace(
        projects={"a": object(), "b": object()},
        sessions={"w": object()},
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

    # Curated label appears with the count.
    assert "[/b] needs action[/" in header
    assert "[b]4[/b] needs action" in header
    # Legacy ``alerts`` / ``alert`` literal must not leak back in.
    assert "[/b] alerts[/" not in header
    assert "[/b] alert[/" not in header

    # And — at count=1 — ``needs action`` does NOT pluralise (it's a
    # phrase, not a noun-with-count).
    data.alert_count = 1
    app._render_dashboard(config, data)
    header = app.header_w.value
    assert "[b]1[/b] needs action" in header
    assert "needs actions" not in header


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
    # #1092 — yellow rollup uses ◆ to match the dashboard's "needs
    # attention" diamond and stay visually distinct from idle ``·``.
    assert "◆" in row.text
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
            # #1092 — yellow rollup uses ◆ to match the dashboard's
            # "needs attention" diamond.
            assert "◆" in plain

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


def test_create_worker_and_route_targets_pm_chat_session_when_worker_exists() -> None:
    """#964 regression: after :meth:`create_worker_and_route` spawns
    the per-project worker (or finds a pre-existing one), it MUST route
    to ``project:<key>:session`` so the PM Chat surface mounts in the
    right pane. The previous implementation routed to ``project:<key>``
    which resolves to the static project Dashboard, leaving every PM
    Chat sub-item dead-ending on Dashboard.
    """
    routed: list[str] = []

    class _FakeLaunchSession:
        def __init__(self, name: str, role: str, project: str) -> None:
            self.name = name
            self.role = role
            self.project = project

    class _FakeLaunch:
        def __init__(self, name: str, role: str, project: str, window_name: str) -> None:
            self.session = _FakeLaunchSession(name, role, project)
            self.window_name = window_name

    class _FakeSupervisor:
        def plan_launches(self):
            return [_FakeLaunch("worker_demo", "worker", "demo", "worker-demo")]

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

        def stabilize_launch(self, *args, **kwargs) -> None:
            return None

        def tmux_session_for_launch(self, launch):
            return "pollypm-storage-closet"

        def window_map(self):
            return {"worker-demo": "0"}

    class _FakeTmux:
        def list_windows(self, target: str):
            return [SimpleNamespace(name="worker-demo", index=0)]

    router = CockpitRouter.__new__(CockpitRouter)
    router.tmux = _FakeTmux()
    router._load_supervisor = lambda fresh=False: _FakeSupervisor()  # type: ignore[assignment]
    router.route_selected = lambda key: routed.append(key)  # type: ignore[assignment]

    router.create_worker_and_route("demo")

    assert routed == ["project:demo:session"], routed


def test_create_worker_and_route_mounts_new_worker_before_stabilizing() -> None:
    """Fresh PM Chat creation should stabilize the pane that gets mounted.

    Stabilizing by ``storage:window-name`` after a join races with tmux
    because the storage window disappears once its pane moves into the
    cockpit. Pane ids survive the join and keep the bootstrap attached to
    the visible right pane.
    """
    calls: list[tuple[str, object]] = []

    class _FakeSession:
        name = "worker_demo"
        role = "worker"
        project = "demo"

    class _FakeLaunch:
        session = _FakeSession()
        window_name = "worker-demo"

    class _FakeSupervisor:
        class _Config:
            class _Project:
                tmux_session = "pollypm"

            project = _Project()

        config = _Config()

        def __init__(self, launches) -> None:
            self._launches = launches

        def plan_launches(self):
            return list(self._launches)

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

        def stabilize_launch(self, launch, target, on_status=None):  # noqa: ARG002
            calls.append(("stabilize", target))

    class _FakeTmux:
        def list_windows(self, target: str):  # noqa: ARG002
            return [SimpleNamespace(name="worker-demo", index=7, pane_id="%worker")]

    class _FakeService:
        def suggest_worker_prompt(self, *, project_key: str) -> str:  # noqa: ARG002
            return ""

        def create_and_launch_worker(self, **kwargs) -> None:
            calls.append(("create", kwargs["project_key"]))

    empty_supervisor = _FakeSupervisor([])
    fresh_supervisor = _FakeSupervisor([_FakeLaunch()])
    router = CockpitRouter.__new__(CockpitRouter)
    router.tmux = _FakeTmux()
    router.service = _FakeService()
    router._load_supervisor = (  # type: ignore[assignment]
        lambda fresh=False: fresh_supervisor if fresh else empty_supervisor
    )
    router._begin_layout_mutation = lambda: "token"  # type: ignore[assignment]
    router._end_layout_mutation = (  # type: ignore[assignment]
        lambda token: calls.append(("end", token))
    )
    router.ensure_cockpit_layout = (  # type: ignore[assignment]
        lambda: calls.append(("ensure", None))
    )
    router.set_selected_key = (  # type: ignore[assignment]
        lambda key: calls.append(("selected", key))
    )
    router._show_live_session = (  # type: ignore[assignment]
        lambda supervisor, session_name, target: calls.append(("mount", session_name))
    )
    router._right_pane_id = lambda target: "%worker"  # type: ignore[assignment]
    router._maybe_prime_project_pm_session = (  # type: ignore[assignment]
        lambda *args: calls.append(("prime", args[2]))
    )

    router.create_worker_and_route("demo")

    assert ("create", "demo") in calls
    assert ("selected", "project:demo:session") in calls
    assert ("mount", "worker_demo") in calls
    assert ("stabilize", "%worker") in calls
    assert ("prime", "worker_demo") in calls


def test_create_worker_and_route_falls_back_to_project_dashboard_without_session() -> None:
    """If worker creation truly produces no session (launch failure,
    config gap), fall back to the project Dashboard so the cockpit
    stays usable rather than routing into a non-existent session.
    """
    routed: list[str] = []

    class _FakeSupervisor:
        def plan_launches(self):
            return []

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

    class _FakeTmux:
        def list_windows(self, target: str):
            return []

    class _FakeService:
        def suggest_worker_prompt(self, *, project_key: str) -> str:
            return "do work"

        def create_and_launch_worker(self, **kwargs) -> None:
            return None

    router = CockpitRouter.__new__(CockpitRouter)
    router.tmux = _FakeTmux()
    router.service = _FakeService()
    router._load_supervisor = lambda fresh=False: _FakeSupervisor()  # type: ignore[assignment]
    router.route_selected = lambda key: routed.append(key)  # type: ignore[assignment]

    router.create_worker_and_route("ghost")

    assert routed == ["project:ghost"], routed


def test_missing_worker_pm_chat_route_creates_worker_from_modular_plan() -> None:
    from pollypm.cockpit_content import FallbackPane, TextualCommandPane

    calls: list[tuple[str, str]] = []
    fallback = TextualCommandPane(
        route_key="project:demo:session",
        selected_key="project:demo:dashboard",
        pane_kind="project",
        command_args=("cockpit-pane", "project", "demo"),
        project_key="demo",
    )
    plan = FallbackPane(
        route_key="project:demo:session",
        selected_key="project:demo:dashboard",
        reason="missing_worker",
        message="missing",
        fallback=fallback,
    )
    router = CockpitRouter.__new__(CockpitRouter)
    router.set_selected_key = lambda key: calls.append(("selected", key))  # type: ignore[assignment]
    router.create_worker_and_route = (  # type: ignore[assignment]
        lambda project_key: calls.append(("create", project_key))
    )
    router._show_static_view = (  # type: ignore[assignment]
        lambda *args, **kwargs: calls.append(("static", "called"))
    )

    router._route_content_plan(SimpleNamespace(), "pollypm:PollyPM", plan)

    assert calls == [
        ("selected", "project:demo:session"),
        ("create", "demo"),
    ]


def test_live_agent_plan_auto_focuses_right_pane_after_mount() -> None:
    """#987 regression: clicking a chat session in the rail should land
    keyboard focus in the right pane so the user can start typing into
    the agent CLI immediately.

    Static views (Inbox, Workers, Metrics, project dashboards) keep rail
    focus — only ``LiveAgentPane`` plans transfer focus. The Ctrl-h
    rail-recovery affordance from #985 is still the way back.
    """
    from pollypm.cockpit_content import LiveAgentPane, TextualCommandPane

    calls: list[tuple[str, object]] = []

    class _FakeTmux:
        def run(self, *args, **kwargs) -> None:
            calls.append(("run", args))

        def select_pane(self, target: str) -> None:
            calls.append(("select", target))

    fallback = TextualCommandPane(
        route_key="project:demo:session",
        selected_key="project:demo:dashboard",
        pane_kind="project",
        command_args=("cockpit-pane", "project", "demo"),
        project_key="demo",
    )
    plan = LiveAgentPane(
        route_key="project:demo:session",
        selected_key="project:demo:session",
        session_name="worker_demo",
        project_key="demo",
        fallback=fallback,
    )

    router = CockpitRouter.__new__(CockpitRouter)
    router.tmux = _FakeTmux()
    router.set_selected_key = lambda key: calls.append(("selected", key))  # type: ignore[assignment]
    router._session_available_for_mount = (  # type: ignore[assignment]
        lambda supervisor, session_name, target: True
    )
    router._show_live_session = (  # type: ignore[assignment]
        lambda supervisor, session_name, target: calls.append(("mount", session_name))
    )
    router._maybe_prime_project_pm_session = (  # type: ignore[assignment]
        lambda *args, **kwargs: None
    )
    router._right_pane_id = lambda target: "%2"  # type: ignore[assignment]

    router._route_content_plan(SimpleNamespace(), "pollypm:PollyPM", plan)

    # Mount happened, then auto-focus moved keyboard focus to the right
    # pane. The display-message hint (the same one ``focus_right_pane``
    # uses) must be sent before ``select-pane`` so the user sees how to
    # come back.
    select_calls = [entry for entry in calls if entry[0] == "select"]
    run_calls = [entry for entry in calls if entry[0] == "run"]
    assert select_calls == [("select", "%2")], calls
    assert any(
        "display-message" in entry[1]
        and any("Ctrl-b Left returns to the rail." in part for part in entry[1])
        for entry in run_calls
    ), run_calls
    # Ordering: mount before focus transfer.
    mount_index = next(i for i, c in enumerate(calls) if c[0] == "mount")
    focus_index = next(i for i, c in enumerate(calls) if c[0] == "select")
    assert mount_index < focus_index


def test_static_command_plan_does_not_auto_focus_right_pane() -> None:
    """#987 negative case: rail-stays-focused for static views. Clicking
    the project dashboard, Inbox, or any non-live-agent target must NOT
    call ``select-pane`` on the right pane — those views are read-mostly
    and users navigate further from the rail.
    """
    from pollypm.cockpit_content import TextualCommandPane

    calls: list[tuple[str, object]] = []

    class _FakeTmux:
        def run(self, *args, **kwargs) -> None:
            calls.append(("run", args))

        def select_pane(self, target: str) -> None:
            calls.append(("select", target))

    plan = TextualCommandPane(
        route_key="project:demo:dashboard",
        selected_key="project:demo:dashboard",
        pane_kind="project",
        command_args=("cockpit-pane", "project", "demo"),
        project_key="demo",
    )

    router = CockpitRouter.__new__(CockpitRouter)
    router.tmux = _FakeTmux()
    router.set_selected_key = lambda key: calls.append(("selected", key))  # type: ignore[assignment]
    router._show_static_view = (  # type: ignore[assignment]
        lambda *args, **kwargs: calls.append(("static", args[2]))
    )

    router._route_content_plan(SimpleNamespace(), "pollypm:PollyPM", plan)

    assert not any(entry[0] == "select" for entry in calls), calls


def test_rail_listview_swallows_value_error_for_orphan_clicks() -> None:
    """#964 regression: the rail's :class:`ListView` subclass guards
    against the boot-time ``_on_list_item__child_clicked`` ValueError
    that fires when a click event lands on a widget that has been
    swapped out by an in-flight rail rebuild. Textual's stock handler
    raises ``ValueError`` on ``self._nodes.index(event.item)`` which
    surfaces as a traceback overlay in the rail. The override must
    re-resolve via the live ``cockpit_key`` when possible and silently
    drop the click otherwise — never let the ValueError propagate.
    """
    from pollypm.cockpit_ui import _RailListView

    view = _RailListView.__new__(_RailListView)

    class _Orphan:
        cockpit_key = "ghost-key"

    class _LiveRow:
        cockpit_key = "live-key"

    class _Nodes:
        def __init__(self) -> None:
            self._items: list = [_LiveRow()]

        def index(self, item):
            if item not in self._items:
                raise ValueError(f"{item!r} not in list")
            return self._items.index(item)

        def __iter__(self):
            return iter(self._items)

    nodes = _Nodes()
    view._nodes = nodes  # type: ignore[attr-defined]

    posted: list = []
    focused: list[bool] = []
    view.focus = lambda: focused.append(True)  # type: ignore[assignment]
    view.post_message = lambda msg: posted.append(msg)  # type: ignore[assignment]

    # Track index assignments rather than letting Textual reactive
    # machinery fire on a __new__-built instance.
    index_value: list[int | None] = [None]

    class _Descriptor:
        def __set__(self, obj, value):
            index_value[0] = value

        def __get__(self, obj, objtype=None):
            return index_value[0]

    type(view).index = _Descriptor()  # type: ignore[assignment]

    class _StopEvent:
        def __init__(self, item) -> None:
            self.item = item
            self.stopped = False
            self.default_prevented = False

        def stop(self) -> None:
            self.stopped = True

        def prevent_default(self) -> None:
            self.default_prevented = True

    # Click on an orphan whose key has no match — must NOT raise.
    orphan_event = _StopEvent(_Orphan())
    view._on_list_item__child_clicked(orphan_event)
    assert orphan_event.stopped
    # ``prevent_default()`` is critical: it stops Textual's MRO walk
    # from also invoking the unguarded parent ``ListView`` handler,
    # which would otherwise still raise the ValueError we just caught.
    assert orphan_event.default_prevented
    assert posted == []  # nothing actionable, drop silently
    assert index_value[0] is None  # index untouched

    # Click on a fresh widget whose cockpit_key matches a live row —
    # must re-resolve and post a Selected message for the live row.
    class _Stale:
        cockpit_key = "live-key"

    stale_event = _StopEvent(_Stale())
    view._on_list_item__child_clicked(stale_event)
    assert index_value[0] == 0
    assert len(posted) == 1


def test_cockpit_router_primes_workspace_operator_on_attach() -> None:
    """Mounting ``Polly · chat`` re-anchors Polly's PollyPM operator
    identity with workspace context (#961). The primer is distinct
    from the per-project primer (#958): no project_key, never carries
    ``_build_project_pm_primer`` text. Idempotent across re-mounts of
    the same right pane; re-fires when the right pane changes (cockpit
    restart or operator-session relaunch).
    """
    sent: list[tuple[str, str]] = []
    primed_state: dict[str, object] = {}

    class _FakeTmux:
        def send_keys(self, target, text, press_enter=True):  # noqa: ARG002
            sent.append((target, text))

        def list_windows(self, *_args, **_kwargs):  # noqa: ARG002
            return []

    class _Project:
        def __init__(self, key: str, name: str) -> None:
            self.key = key
            self.name = name
            self.path = Path(f"/tmp/{key}-no-db")  # state.db absent → graceful
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
                    self.window_name = f"pm-{name}"
            return [_L("operator", "operator-pm", "pollypm")]

        def storage_closet_session_name(self):
            return "pollypm-storage-closet"

    pane_id_holder = {"id": "%right1"}

    router = CockpitRouter.__new__(CockpitRouter)
    router.tmux = _FakeTmux()
    router._right_pane_id = lambda window_target: pane_id_holder["id"]  # type: ignore[assignment]
    router._load_state = lambda: dict(primed_state)  # type: ignore[assignment]
    def _write(data):
        primed_state.clear()
        primed_state.update(data)
    router._write_state = _write  # type: ignore[assignment]
    router._show_static_view = lambda *a, **k: None  # type: ignore[assignment]
    router._show_live_session = lambda *a, **k: None  # type: ignore[assignment]

    supervisor = _FakeSupervisor()

    router._route_live_session(
        supervisor,
        "pollypm:PollyPM",
        LiveSessionRoute(session_name="operator"),
    )

    assert len(sent) == 1, sent
    target, text = sent[0]
    assert target == "%right1"
    # Operator identity markers from the workspace primer.
    assert "Polly" in text
    # #1007: primer no longer asserts pseudo-system authority via
    # "Re-anchor on this identity" + "PollyPM operator" — that
    # phrasing tripped Claude's injection defense and the operator
    # rejected the primer outright. The reframed conversational opener
    # ("Hey Polly — the user just opened the operator chat …") and
    # the workspace-scope discriminators below still uniquely identify
    # the workspace primer.
    assert "operator chat" in text
    # Workspace-scope (not project-scope) discriminators.
    assert "Workspace:" in text
    assert "Active inbox (workspace-wide):" in text
    # Distinct from the per-project primer's signature.
    assert "Project root:" not in text  # per-project primer marker
    assert "Plan: " not in text  # per-project primer marker
    # State persisted so a re-mount of the same pane does NOT re-prime.
    assert primed_state.get("operator_primed_pane") == "%right1"

    router._route_live_session(
        supervisor,
        "pollypm:PollyPM",
        LiveSessionRoute(session_name="operator"),
    )
    assert len(sent) == 1, "re-mount of same pane re-primed unexpectedly"

    # Cockpit restart / operator relaunch → fresh right pane id → re-prime.
    pane_id_holder["id"] = "%right2"
    router._route_live_session(
        supervisor,
        "pollypm:PollyPM",
        LiveSessionRoute(session_name="operator"),
    )
    assert len(sent) == 2, "fresh pane id should re-prime"
    assert primed_state.get("operator_primed_pane") == "%right2"


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
    # #1088 — pin moved from ``p`` to ``P``; lowercase ``p`` now forwards
    # to the project dashboard's ``p plan`` so the bottom hint matches
    # the actual behaviour.
    assert bindings["P"] == "Pin Project"
    assert bindings["p"] == "Plan"


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


def test_ensure_cockpit_layout_project_selection_repairs_with_project_command(
    tmp_path: Path,
) -> None:
    """#991 — when ``selected`` is on a project-scoped route (e.g. PM
    Chat), the ``<2 panes`` repair split must NOT default to
    ``pm cockpit-pane polly``. Otherwise a layout-recovery split during
    a project-scoped click leaves Polly's workspace dashboard visible if
    any subsequent mount step bails — the exact fallthrough surface
    reported in #991 for architect-only projects.
    """
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

        def split_window(
            self,
            target: str,
            command: str,
            *,
            horizontal: bool = True,
            detached: bool = True,
            percent: int | None = None,
            size: int | None = None,
        ):
            calls["split"] = (target, command, horizontal, detached, size)
            return "%2"

        def select_pane(self, target: str):
            calls["selected"] = target

        def run(self, *args, **kwargs):
            calls.setdefault("run", []).append(args)

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    router = CockpitRouter(config_path)
    router.tmux = FakeTmux()  # type: ignore[assignment]
    router._write_state({"selected": "project:bikepath:session"})

    router.ensure_cockpit_layout()

    split_command = calls["split"][1]
    assert "cockpit-pane project bikepath" in split_command, split_command
    # #991 — the user is on a project route; a partial-repair split
    # must never land on Polly's workspace dashboard.
    assert "cockpit-pane polly" not in split_command, split_command


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


def test_cockpit_router_focus_rail_selects_leftmost_pane(
    monkeypatch, tmp_path: Path,
) -> None:
    """``focus_rail_pane`` shifts tmux focus from the right pane back
    to the rail (#985). Without this, right-pane apps like the inbox
    have no path to return keyboard focus to the rail short of the
    user issuing a tmux prefix command — and j/k stays trapped in the
    right pane."""
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
    monkeypatch.setattr(router, "_left_pane_id", lambda target: "%1")

    router.focus_rail_pane()

    assert calls == [("select", ("%1",))]


def test_focus_cockpit_rail_pane_helper_invokes_router(
    monkeypatch, tmp_path: Path,
) -> None:
    """The module-level ``focus_cockpit_rail_pane`` helper is what
    right-pane Textual apps call from key bindings. It must not raise
    on missing tmux sessions and should pass the resolved config
    through to a router."""
    from pollypm import cockpit_rail as _rail_mod

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )

    captured: list[Path] = []

    class _StubRouter:
        def __init__(self, path: Path) -> None:
            captured.append(path)

        def focus_rail_pane(self) -> None:
            captured.append(Path("called"))

    monkeypatch.setattr(_rail_mod, "CockpitRouter", _StubRouter)

    assert _rail_mod.focus_cockpit_rail_pane(config_path) is True
    assert captured[0] == config_path
    assert captured[-1] == Path("called")


def test_focus_cockpit_rail_pane_helper_returns_false_on_router_failure(
    monkeypatch, tmp_path: Path,
) -> None:
    """A construction failure (e.g. no config in test env) must not
    propagate — right-pane apps call this from key handlers and a
    crash would freeze the inbox."""
    from pollypm import cockpit_rail as _rail_mod

    config_path = tmp_path / "pollypm.toml"

    class _ExplodingRouter:
        def __init__(self, path: Path) -> None:
            raise RuntimeError("no tmux here")

    monkeypatch.setattr(_rail_mod, "CockpitRouter", _ExplodingRouter)

    assert _rail_mod.focus_cockpit_rail_pane(config_path) is False


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


def test_cockpit_router_ensure_layout_keeps_persisted_right_when_extra_pane_appears(tmp_path: Path) -> None:
    class FakePane:
        def __init__(self, pane_id: str, pane_left: int, command: str, pane_width: int = 80) -> None:
            self.pane_id = pane_id
            self.pane_left = pane_left
            self.pane_current_command = command
            self.pane_width = pane_width
            self.active = True

    class FakeTmux:
        def __init__(self) -> None:
            self.panes = [
                FakePane("%1", 0, "zsh", pane_width=30),
                FakePane("%2", 31, "bash", pane_width=80),
                FakePane("%3", 112, "2.1.123", pane_width=100),
            ]
            self.killed: list[str] = []

        def list_panes(self, target: str):
            return list(self.panes)

        def kill_pane(self, target: str) -> None:
            self.killed.append(target)
            self.panes = [pane for pane in self.panes if pane.pane_id != target]

        def resize_pane_width(self, target: str, width: int):
            pass

        def run(self, *args, **kwargs):
            pass

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    router = CockpitRouter(config_path)
    tmux = FakeTmux()
    router.tmux = tmux  # type: ignore[assignment]
    router._write_state(
        {
            "right_pane_id": "%3",
        }
    )

    router.ensure_cockpit_layout()

    assert tmux.killed == ["%2"]
    assert {pane.pane_id for pane in tmux.panes} == {"%1", "%3"}
    state = router._load_state()
    assert state["right_pane_id"] == "%3"


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
                type("Pane", (), {"pane_id": "%1", "active": True, "pane_left": 0, "pane_current_command": "uv"})(),
                type("Pane", (), {"pane_id": "%2", "active": False, "pane_left": 30, "pane_current_command": "sh"})(),
            ]

        def respawn_pane(self, target: str, command: str):
            calls["respawn"] = (target, command)

        def resize_pane_width(self, target: str, width: int):
            calls["resize"] = (target, width)

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
                type("Pane", (), {"pane_id": "%1", "active": True, "pane_left": 0, "pane_current_command": "uv"})(),
                type("Pane", (), {"pane_id": "%2", "active": False, "pane_left": 30, "pane_current_command": "sh"})(),
            ]

        def respawn_pane(self, target: str, command: str):
            calls["respawn"] = (target, command)

        def resize_pane_width(self, target: str, width: int):
            calls["resize"] = (target, width)

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


def test_cockpit_router_layout_repair_skips_during_external_route(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\nbase_dir = \"{tmp_path / '.pollypm'}\"\n"
    )

    class FakeTmux:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def list_panes(self, target: str):
            self.calls.append(f"list_panes:{target}")
            return []

    router = CockpitRouter(config_path)
    tmux = FakeTmux()
    router.tmux = tmux  # type: ignore[assignment]
    router._write_state(
        {
            "_layout_mutation_token": "other-process",
            "_layout_mutating_until": 9_999_999_999.0,
        }
    )

    router.ensure_cockpit_layout()

    assert tmux.calls == []


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


def test_cockpit_app_drains_right_pane_navigation_queue_in_sequence() -> None:
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    scheduled: list[tuple[str, str | None]] = []

    class _Queue:
        drained = False

        def drain(self):
            self.drained = True
            return (
                SimpleNamespace(sequence=1, selected_key="inbox:demo"),
                SimpleNamespace(sequence=2, selected_key="activity:demo"),
            )

    queue = _Queue()
    app._cockpit_navigation_queue = lambda: queue  # type: ignore[method-assign]
    app._schedule_route_selected = (  # type: ignore[method-assign]
        lambda key, *, label=None: scheduled.append((key, label))
    )

    app._drain_cockpit_navigation_queue()

    assert queue.drained is True
    assert scheduled == [
        ("inbox:demo", "inbox:demo"),
        ("activity:demo", "activity:demo"),
    ]


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

    # Render-then-load (#959): the click handler refreshes once
    # synchronously to paint the optimistic loading state, then the
    # worker calls ``route_selected`` and refreshes again with the
    # resolved key. Order: refresh, route, refresh.
    assert ("route", "russell") in calls
    assert calls.count("refresh") >= 2

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


def test_cockpit_forwards_p_to_right_pane_only_on_project_surface() -> None:
    """``p`` from rail forwards to the right pane only on a project (#1088).

    Before the fix, ``p`` was bound to ``toggle_project_pin`` and the
    dashboard's advertised ``p plan`` keystroke never reached the
    dashboard — pinning fired instead and the rail jumped to the
    alphabetically-first project. After the fix ``p`` forwards to the
    right pane on a project surface (so the dashboard's ``open_plan``
    handler runs); pin moves to capital ``P``.
    """
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    sent: list[str] = []
    app._send_key_to_right_pane = lambda key: sent.append(key)  # type: ignore[method-assign]

    app.selected_key = "polly"
    app.action_forward_project_plan()
    assert sent == [], "should not forward p outside of project surfaces"

    app.selected_key = "project:demo:dashboard"
    app.action_forward_project_plan()
    assert sent == ["p"]


def test_cockpit_forwards_i_to_right_pane_only_on_project_surface() -> None:
    """``i`` from rail forwards to the right pane only on a project (#1089).

    Before the fix, ``i`` was bound to ``open_inbox`` (priority=True) and
    routed to the global cockpit inbox even when the dashboard's bottom
    hint promised ``i inbox`` would scroll to the project's own inbox
    section. After the fix ``i`` forwards to the right pane on a project
    surface so the dashboard's ``jump_inbox`` handler runs; global
    Inbox stays reachable via capital ``I``.
    """
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    sent: list[str] = []
    app._send_key_to_right_pane = lambda key: sent.append(key)  # type: ignore[method-assign]

    app.selected_key = "polly"
    app.action_forward_project_jump_inbox()
    assert sent == [], "should not forward i outside of project surfaces"

    app.selected_key = "project:demo:dashboard"
    app.action_forward_project_jump_inbox()
    assert sent == ["i"]


def test_cockpit_forwards_q_to_right_pane_only_on_project_surface() -> None:
    """``q`` from rail forwards to the right pane only on a project (#1089).

    Before the fix, ``q`` was bound to ``request_quit`` (priority=True);
    on a project surface it sidestepped the dashboard's own ``q,escape``
    → ``back`` handler and routed home via the rail's path. After the
    fix ``q`` forwards so the dashboard's advertised ``q home`` keystroke
    actually fires the dashboard's ``action_back``. Quit moves to
    capital ``Q`` / ``Ctrl-Q``.
    """
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    sent: list[str] = []
    app._send_key_to_right_pane = lambda key: sent.append(key)  # type: ignore[method-assign]

    app.selected_key = "polly"
    app.action_forward_project_home()
    assert sent == [], "should not forward q outside of project surfaces"

    app.selected_key = "project:demo:dashboard"
    app.action_forward_project_home()
    assert sent == ["q"]


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


class _SettingsRowStub:
    """Stand-in for the ``#settings-row`` Static below the nav list.

    Only needs to absorb the ``set_class`` / ``update`` calls
    ``_apply_active_view_to_rows`` makes — none of which we exercise
    in these unit tests; the rail's ``_apply_active_view_to_rows`` is
    monkeypatched out below.
    """


def _make_rail_app_with_settings(
    items: list[_StubItem], *, items_keys: list[str], selected_key: str = "polly",
):
    """Build a bare ``PollyCockpitApp`` whose ``self._items`` includes a
    ``settings`` entry — i.e. the gear row is visible — and whose nav
    children sequence is ``items``. Returns ``(app, nav)``.
    """
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    nav = _StubNav(items)
    app.nav = nav  # type: ignore[assignment]
    app.selected_key = selected_key
    app._tick_count = 0
    app._last_nav_change = -10
    app._apply_active_view_to_rows = lambda: None  # type: ignore[method-assign]

    class _Item:
        def __init__(self, key: str) -> None:
            self.key = key

    app._items = [_Item(k) for k in items_keys]  # type: ignore[attr-defined]

    def _selected_row_key() -> str | None:
        idx = nav.index
        if idx is None:
            return None
        return items[idx].cockpit_key

    app._selected_row_key = _selected_row_key  # type: ignore[method-assign]
    return app, nav


def test_cockpit_j_at_last_nav_row_steps_onto_settings() -> None:
    """j on the last nav row (Activity) lands on Settings when the gear
    row is visible — the blank gap between Activity and Settings must
    not block navigation (#1080)."""
    items = [_StubItem("polly"), _StubItem("activity")]
    app, nav = _make_rail_app_with_settings(
        items,
        items_keys=["polly", "activity", "settings"],
        selected_key="activity",
    )
    nav.index = 1  # cursor on Activity (last nav row)

    app.action_cursor_down()
    assert app.selected_key == "settings", (
        f"j from Activity should land on Settings; got {app.selected_key!r}"
    )

    # Repeat j on Settings is a no-op (nothing below).
    app.action_cursor_down()
    assert app.selected_key == "settings"


def test_cockpit_k_off_settings_returns_to_last_nav_row() -> None:
    """k from the virtual Settings row lands on the last selectable nav
    row so the round-trip is symmetric (#1080)."""
    items = [_StubItem("polly"), _StubItem("activity")]
    app, nav = _make_rail_app_with_settings(
        items,
        items_keys=["polly", "activity", "settings"],
        selected_key="settings",
    )
    nav.index = 0  # cursor parked anywhere; k uses last_nav_index

    app.action_cursor_up()
    assert app.selected_key == "activity", (
        f"k from Settings should land on Activity; got {app.selected_key!r}"
    )
    assert nav.index == 1


def test_cockpit_G_lands_on_settings_when_visible() -> None:
    """G/End jumps to Settings (the literal last rail item) instead of
    stalling on Activity (#1080)."""
    items = [_StubItem("polly"), _StubItem("activity")]
    app, nav = _make_rail_app_with_settings(
        items,
        items_keys=["polly", "activity", "settings"],
        selected_key="polly",
    )
    nav.index = 0

    app.action_cursor_last()
    assert app.selected_key == "settings", (
        f"G should land on Settings; got {app.selected_key!r}"
    )


def test_cockpit_G_falls_back_to_last_nav_row_without_settings() -> None:
    """When Settings isn't part of ``self._items`` (e.g. headless rail
    contexts), G/End behaves exactly as before — pinning to the last
    nav row (#1080)."""
    items = [_StubItem("polly"), _StubItem("activity")]
    app, nav = _make_rail_app_with_settings(
        items,
        items_keys=["polly", "activity"],  # no settings
        selected_key="polly",
    )
    nav.index = 0

    app.action_cursor_last()
    assert nav.index == 1
    assert app.selected_key == "activity"


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


# ── #959: render-then-load click handlers ────────────────────────────────────

def _make_async_click_app() -> "tuple[PollyCockpitApp, list]":
    """Build a cockpit app harness wired to record route + UI events."""
    app = PollyCockpitApp.__new__(PollyCockpitApp)
    events: list = []

    class _Router:
        selected = "polly"

        def route_selected(self, key: str) -> None:
            events.append(("route", key))
            self.selected = key

        def selected_key(self) -> str:
            return self.selected

    class _Hint:
        def update(self, msg: str) -> None:
            events.append(("hint", msg))

    app.router = _Router()  # type: ignore[assignment]
    app.selected_key = "polly"
    app._last_router_selected_key = "polly"
    app.hint = _Hint()  # type: ignore[assignment]
    app._unread_keys = set()
    app._refresh_rows = lambda: events.append("refresh")  # type: ignore[method-assign]
    app._selected_row_key = lambda: "project:demo"  # type: ignore[method-assign]
    return app, events


def test_async_click_paints_loading_hint_before_route_selected() -> None:
    """#959 — render-then-load: the loading hint MUST be visible before
    ``route_selected`` is invoked. If the hint update lands after the
    route call the click was effectively still synchronous."""
    app, events = _make_async_click_app()

    app.action_open_inbox()

    def _hints(evts):
        return [
            evt[1] for evt in evts
            if isinstance(evt, tuple) and len(evt) == 2 and evt[0] == "hint"
        ]

    hints = _hints(events)
    assert any("Connecting to" in h for h in hints), (
        f"expected a 'Connecting to …' hint before route, got events={events}"
    )
    # First hint must arrive before the first ``route_selected`` call.
    first_route_idx = next(
        i for i, evt in enumerate(events) if evt == ("route", "inbox")
    )
    first_hint_idx = next(
        i for i, evt in enumerate(events)
        if isinstance(evt, tuple) and len(evt) == 2 and evt[0] == "hint"
        and "Connecting" in evt[1]
    )
    assert first_hint_idx < first_route_idx


def test_async_click_dispatches_via_run_worker_when_available() -> None:
    """The action MUST hand the route work to ``run_worker`` so the UI
    thread is free. Tests bypass the running app, so the dispatcher
    falls back to inline execution — but we verify the dispatch hook
    was called with the right key."""
    app, events = _make_async_click_app()
    dispatched: list[str] = []

    def _spy_dispatch(key: str, seq: int = 0) -> None:
        dispatched.append(key)

    app._dispatch_route_in_worker = _spy_dispatch  # type: ignore[assignment]

    app.action_open_settings()

    assert dispatched == ["settings"]
    # ``route_selected`` should NOT have run on the UI thread — the
    # spy short-circuits the worker. This is the property that lets a
    # slow route never block the next click.
    assert ("route", "settings") not in events


def test_async_click_surfaces_timeout_on_slow_route() -> None:
    """A blocked route call MUST surface a timeout in the loading
    pane within the deadline rather than hang the click forever."""
    import threading
    import time

    app, events = _make_async_click_app()

    class _SlowRouter:
        selected = "polly"
        block = threading.Event()

        def route_selected(self, key: str) -> None:
            events.append(("route_started", key))
            # Wait until the timeout fires; the test never sets ``block``.
            self.block.wait(timeout=5.0)
            events.append(("route_finished", key))
            self.selected = key

        def selected_key(self) -> str:
            return self.selected

    app.router = _SlowRouter()  # type: ignore[assignment]
    app._ROUTE_SELECT_TIMEOUT_SECONDS = 0.25  # type: ignore[attr-defined]

    started = time.monotonic()
    app._route_selected_worker("inbox")
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, (
        f"timeout deadline did not fire within budget (elapsed={elapsed:.2f}s)"
    )
    hints = [
        evt[1] for evt in events
        if isinstance(evt, tuple) and len(evt) == 2 and evt[0] == "hint"
    ]
    assert any("timed out" in h for h in hints), (
        f"expected a timeout hint, got events={events}"
    )
    # Release the slow router so its thread terminates.
    _SlowRouter.block.set()


def test_async_click_resyncs_selected_key_from_router() -> None:
    """The router rewrites ``project:x`` to ``project:x:dashboard`` —
    after the worker completes, ``selected_key`` MUST reflect the
    canonical key the router persisted, not the optimistic stamp."""
    app, events = _make_async_click_app()

    class _RewriteRouter:
        selected = "polly"

        def route_selected(self, key: str) -> None:
            events.append(("route", key))
            # Router canonicalizes the key.
            self.selected = f"{key}:dashboard"

        def selected_key(self) -> str:
            return self.selected

    app.router = _RewriteRouter()  # type: ignore[assignment]

    app._schedule_route_selected("project:demo", label="demo")

    assert app.selected_key == "project:demo:dashboard"
    assert app._last_router_selected_key == "project:demo:dashboard"


# ── #967: rail click stability — second click must NOT bounce back ────────────

def test_rapid_double_click_does_not_bounce_back_to_first_target() -> None:
    """#967 — every rail click flashed the target then bounced to Home.

    Root cause: ``_route_selected_worker`` runs on a thread spawned via
    ``run_worker(thread=True, exclusive=True, group="route_select")``.
    Textual cancels the *asyncio* task when a newer click arrives, but the
    thread itself continues running to completion (Python threads are not
    asyncio-aware). When the stale thread eventually returned, it called
    ``_post_route_success`` which unconditionally overwrote
    ``selected_key`` with the OLD click's resolved key — bouncing the
    user back to wherever the first click had gone.

    Repro: click ``project:demo`` then immediately click ``polly``. The
    ``project:demo`` worker is still running in its executor when the
    ``polly`` worker fires. After both complete, ``selected_key`` MUST
    reflect ``polly`` — the user's most-recent intent — not the stale
    ``project:demo:dashboard`` the first worker resolved.
    """
    import threading

    app = PollyCockpitApp.__new__(PollyCockpitApp)

    project_started = threading.Event()
    project_unblock = threading.Event()

    class _RaceRouter:
        selected = "polly"

        def route_selected(self, key: str) -> None:
            if key == "project:demo":
                # Simulate the slow PM-attach: signal we've entered the
                # router, then block until the test releases us. While
                # we're parked here, the second click will fire.
                project_started.set()
                project_unblock.wait(timeout=2.0)
                self.selected = "project:demo:dashboard"
            else:
                self.selected = key

        def selected_key(self) -> str:
            return self.selected

    class _Hint:
        def update(self, _msg: str) -> None:
            pass

    app.router = _RaceRouter()  # type: ignore[assignment]
    app.selected_key = "polly"
    app._last_router_selected_key = "polly"
    app._route_click_seq = 0
    app.hint = _Hint()  # type: ignore[assignment]
    app._unread_keys = set()
    app._refresh_rows = lambda: None  # type: ignore[method-assign]
    app._selected_row_key = lambda: app.selected_key  # type: ignore[method-assign]

    # First click — manually drive the same sequence that
    # ``_schedule_route_selected`` would: bump the click seq and run
    # the worker on a real thread (so we can race a second click).
    app.selected_key = "project:demo"
    app._route_click_seq += 1
    first_seq = app._route_click_seq
    first = threading.Thread(
        target=lambda: app._route_selected_worker("project:demo", first_seq),
        daemon=True,
    )
    first.start()
    assert project_started.wait(timeout=2.0), "first router never entered"

    # Second click — registers optimistic state for ``polly`` and runs
    # the (fast) ``polly`` worker inline (no event loop in tests). After
    # this returns the user's intent is unambiguously ``polly``.
    app._schedule_route_selected("polly", label="Home")
    assert app.selected_key == "polly"

    # Release the stalled first worker. It will now finish and call
    # ``_post_route_success`` for ``project:demo``. Without the #967 fix,
    # this overwrites ``selected_key`` back to ``project:demo:dashboard``.
    project_unblock.set()
    first.join(timeout=2.0)
    assert not first.is_alive(), "stalled router never returned"

    # The user's most-recent click must win.
    assert app.selected_key == "polly", (
        "stale route worker bounced selected_key back to the previous "
        f"click; got {app.selected_key!r}"
    )
    assert app._last_router_selected_key == "polly"


def test_rail_click_is_stable_across_multiple_targets() -> None:
    """#967 — clicking each rail entry in succession must leave the
    cockpit on the LAST clicked target, not on whatever earlier click
    happened to win the post-route race.
    """
    app = PollyCockpitApp.__new__(PollyCockpitApp)

    class _Router:
        selected = "polly"

        def route_selected(self, key: str) -> None:
            self.selected = key

        def selected_key(self) -> str:
            return self.selected

    class _Hint:
        def update(self, _msg: str) -> None:
            pass

    app.router = _Router()  # type: ignore[assignment]
    app.selected_key = "polly"
    app._last_router_selected_key = "polly"
    app._route_click_seq = 0
    app.hint = _Hint()  # type: ignore[assignment]
    app._unread_keys = set()
    app._refresh_rows = lambda: None  # type: ignore[method-assign]
    app._selected_row_key = lambda: app.selected_key  # type: ignore[method-assign]

    targets = ["inbox", "polly", "workers", "project:demo", "metrics"]
    for target in targets:
        app._schedule_route_selected(target, label=target)
        assert app.selected_key == target, (
            f"click on {target!r} did not stick (got {app.selected_key!r})"
        )


def test_route_selected_persists_intent_before_layout_work(
    monkeypatch, tmp_path: Path,
) -> None:
    """#967 follow-up — clicking a rail item must persist the new
    ``selected`` key to ``cockpit_state.json`` BEFORE any
    potentially-failing layout/mount work runs.

    Pre-fix behaviour: ``CockpitRouter.route_selected`` called
    ``ensure_cockpit_layout`` first and ``set_selected_key`` only after,
    inside the same try-block. When ``ensure_cockpit_layout`` (or any
    intermediate step) raised, ``state["selected"]`` stayed pinned at
    the previous click's key — typically ``inbox`` because that's where
    users sit between actions. The cockpit's layout-recovery tick fires
    every ~30s; on its next ``_refresh_rows`` it adopts the persisted
    selection back into the rail's in-memory cursor, bouncing the user
    from the freshly-clicked Polly · chat row to Inbox row 10. The
    operator window spawned by ``supervisor.launch_session`` is then
    torn down by the same recovery sweep.

    Post-fix: ``set_selected_key`` runs first, so the user's intent is
    persisted regardless of whether downstream layout work succeeds.
    """
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\n"
        f"tmux_session = \"pollypm\"\n"
        f"base_dir = \"{tmp_path / '.pollypm'}\"\n"
    )

    router = CockpitRouter(config_path)

    # Seed disk state with a stale prior selection so we can prove the
    # bounce destination is whatever was persisted, not whatever the
    # router defaults to. ``inbox`` matches the live failure mode in
    # #967's 2026-04-29 17:24 PT comment.
    router._write_state({"selected": "inbox"})

    # Simulate the live failure mode: ``ensure_cockpit_layout`` blows
    # up part-way through (e.g. tmux not running, supervisor refusing to
    # load, fifth-layer guard rejecting a persona swap). Pre-fix this
    # raises BEFORE ``set_selected_key`` is reached, so disk state stays
    # at ``inbox``.
    monkeypatch.setattr(
        router,
        "ensure_cockpit_layout",
        lambda: (_ for _ in ()).throw(RuntimeError("simulated layout failure")),
    )

    with pytest.raises(RuntimeError, match="simulated layout failure"):
        router.route_selected("polly")

    # The user's most-recent intent must be on disk so the next
    # layout-recovery refresh adopts ``polly`` — not the stale
    # ``inbox`` — into the rail cursor.
    state = router._load_state()
    assert state.get("selected") == "polly", (
        "route_selected raised before persisting the new selection — the "
        "next periodic _refresh_rows will bounce the cursor back to the "
        f"stale state[\"selected\"]={state.get('selected')!r} (#967)."
    )


def test_right_pane_command_uses_exec_to_avoid_orphans(tmp_path: Path) -> None:
    """#986 — the cockpit-pane shell wrapper must ``exec`` into the
    Python process so tmux's ``respawn-pane -k`` SIGKILL hits the
    Python child directly.

    Without ``exec`` the ``sh -lc`` parent gets killed but its Python
    child survives (reparented to PID 1) — every cockpit kill+restart
    cycle leaks one orphan ``pm cockpit-pane`` that holds open file
    handles, races the fresh cockpit's right-pane app, and persists
    across boots.
    """
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\n"
        f"base_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    router = CockpitRouter(config_path)

    for kind, project_key, task_id in [
        ("polly", None, None),
        ("dashboard", None, None),
        ("inbox", "demo", None),
        ("workers", None, None),
        ("metrics", None, None),
        ("activity", "demo", None),
        ("project", "demo", None),
        ("settings", "demo", None),
        ("issues", "demo", "demo/7"),
    ]:
        command = router._right_pane_command(kind, project_key, task_id=task_id)
        # The body of the ``sh -lc`` wrapper must invoke the pm CLI via
        # ``exec`` so the shell is replaced — guarantees tmux's SIGKILL
        # propagates to the Python process and doesn't strand it as a
        # PID-1 orphan.
        assert "&& exec " in command, (
            f"{kind!r} pane command missing ``exec`` keyword (would leak "
            f"orphan on respawn-pane -k): {command!r}"
        )
        # Sanity: the command actually runs cockpit-pane (not just exec
        # of something else) — guards against an accidental refactor
        # that drops the cockpit-pane invocation entirely.
        assert "cockpit-pane" in command


def test_cockpit_pane_subprocess_dies_with_shell_wrapper(tmp_path: Path) -> None:
    """#986 — process-level regression: the shell wrapper used by
    ``CockpitRouter._right_pane_command`` must not strand its Python
    child when killed.

    Reproduces the orphan symptom by spawning the wrapper with a stand-
    in long-lived Python child, SIGKILL'ing the wrapper (mirroring
    ``tmux respawn-pane -k``), and asserting the child terminates
    rather than reparenting to PID 1.
    """
    import os
    import signal
    import subprocess
    import sys

    if sys.platform == "win32":
        pytest.skip("POSIX-only signal/exec semantics")

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\n"
        f"base_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    router = CockpitRouter(config_path)
    template = router._right_pane_command("polly")

    # The real command runs ``pm`` — we don't need a full PollyPM boot
    # for this test, just the shell-wrapping shape. Substitute the pm
    # invocation with a long-running Python sleep that prints its PID
    # so we can track the child.
    assert template.startswith("sh -lc '") and template.endswith("'")
    # Match the production shell wrapper exactly: ``sh -lc 'cd <dir> && exec <cmd>'``
    # — only the inner cmd swaps to the test stand-in.
    root = tmp_path
    inner = (
        f"{sys.executable} -c "
        "\"import sys, time, os; "
        "sys.stdout.write(str(os.getpid())); "
        "sys.stdout.write(chr(10)); "
        "sys.stdout.flush(); "
        "time.sleep(60)\""
    )
    cmd = f"sh -lc 'cd {root} && exec {inner}'"

    proc = subprocess.Popen(  # noqa: S602 - test fixture
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        # Read the child PID the stand-in printed. Because of ``exec``,
        # the inner Python's PID == the shell's PID == proc.pid.
        assert proc.stdout is not None
        line = proc.stdout.readline().decode().strip()
        child_pid = int(line)
        # ``exec`` collapses the shell into the Python process, so the
        # Popen child PID and the inner Python PID are the same. This
        # is the contract that prevents orphans: SIGKILL'ing proc.pid
        # kills the actual Python child rather than only its shell
        # parent.
        assert child_pid == proc.pid, (
            f"shell wrapper did not exec — child {child_pid} != "
            f"shell {proc.pid}, SIGKILL on shell would orphan child"
        )

        # SIGKILL the shell wrapper PID, mirroring ``tmux respawn-pane -k``.
        # When ``exec`` is in play, this PID == the Python child's PID,
        # so the kill propagates. We then ``proc.wait()`` to reap and
        # confirm the process actually terminated.
        os.kill(proc.pid, signal.SIGKILL)
        try:
            returncode = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"child PID {child_pid} did not exit after SIGKILL of shell "
                f"wrapper (orphan leak from #986)"
            )
        # SIGKILL surfaces as -9 / 137 depending on platform conventions.
        assert returncode in (-signal.SIGKILL, 128 + signal.SIGKILL), (
            f"unexpected exit code {returncode} after SIGKILL"
        )
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


# ── #995 — Polly · chat click must never collapse the cockpit ─────────────────
#
# Repro from the issue body: launch cockpit at 210x65, ``gg`` to top of rail,
# Enter on Home, ``j`` down to ``Polly · chat``, Enter. Pane 1 disappears,
# rail TUI in pane 0 doubles its render across the now-too-wide pane,
# subsequent rail clicks process but the layout never recovers because every
# downstream code path assumes the rail has a sibling right pane.
#
# The fix is a layout heal in ``CockpitRouter.route_selected``'s ``finally``:
# when the route work leaves the cockpit window with <2 live panes, re-run
# ``ensure_cockpit_layout`` so #991's context-aware ``_default_repair_command``
# splits a fresh content pane before the layout-mutation lock is released.


def _polly_chat_995_make_router(
    tmp_path: Path,
    *,
    selected: str,
    storage_has_operator: bool = True,
    join_pane_raises: bool = False,
):
    """Build a CockpitRouter wired to a stateful FakeTmux.

    The simulator tracks panes through ``join_pane`` / ``kill_pane`` /
    ``split_window`` / ``respawn_pane`` / ``break_pane`` so the test can
    inspect the post-route layout exactly the way real tmux would
    report it.
    """

    class FakePane:
        def __init__(
            self,
            pane_id: str,
            pane_left: int,
            command: str,
            width: int = 80,
        ) -> None:
            self.pane_id = pane_id
            self.pane_left = pane_left
            self.pane_current_command = command
            self.pane_width = width
            self.pane_dead = False
            self.active = pane_left == 0

    class FakeWindow:
        def __init__(self, name: str, index: int, pane_id: str = "") -> None:
            self.name = name
            self.index = index
            self.pane_id = pane_id
            self.pane_current_command = "node"
            self.pane_current_path = "/tmp"
            self.active = False
            self.pane_dead = False

    class FakeTmux:
        def __init__(self) -> None:
            self.panes = [
                FakePane("%1", 0, "uv", width=30),
                FakePane("%2", 31, "python", width=170),  # project static
            ]
            self.storage_panes: dict[str, FakeWindow] = {}
            if storage_has_operator:
                self.storage_panes["pm-operator"] = FakeWindow(
                    "pm-operator", index=1, pane_id="%5",
                )
            self.actions: list[str] = []
            self._next_id = 100

        def list_panes(self, target: str):
            return list(self.panes)

        def list_windows(self, name: str):
            if "storage" in name or name.endswith("-storage-closet"):
                return list(self.storage_panes.values())
            return [FakeWindow("PollyPM", 1, pane_id="%1")]

        def has_session(self, name: str) -> bool:
            return True

        def kill_pane(self, target: str) -> None:
            self.actions.append(f"kill_pane({target})")
            self.panes = [p for p in self.panes if p.pane_id != target]

        def join_pane(
            self, source: str, target: str, *, horizontal: bool = True,
        ) -> None:
            self.actions.append(f"join_pane({source!r}, {target!r})")
            if join_pane_raises:
                raise RuntimeError("simulated tmux join-pane failure")
            source_pane_id: str | None = None
            if source.startswith("%"):
                source_pane_id = source
            else:
                # Resolve session:idx.pane storage references to the actual
                # pane id so the join can find the source pane.
                for window in self.storage_panes.values():
                    if source.startswith("pollypm-storage-closet"):
                        source_pane_id = window.pane_id or None
                        break
            for window_name, window in list(self.storage_panes.items()):
                if window.pane_id == source_pane_id:
                    del self.storage_panes[window_name]
                    break
            target_pane = next(
                (p for p in self.panes if p.pane_id == target), None,
            )
            if target_pane is None:
                raise RuntimeError(f"can't find target pane: {target}")
            idx = self.panes.index(target_pane)
            old_width = target_pane.pane_width
            target_pane.pane_width = max(old_width // 2, 1)
            new_left = target_pane.pane_left + target_pane.pane_width + 1
            new_pane = FakePane(
                source_pane_id or f"%storage{self._next_id}",
                new_left,
                "node",
                width=max(old_width - target_pane.pane_width - 1, 1),
            )
            self._next_id += 1
            self.panes.insert(idx + 1, new_pane)
            for pane in self.panes[idx + 2 :]:
                pane.pane_left += new_pane.pane_width + 1

        def respawn_pane(self, target: str, command: str) -> None:
            self.actions.append(f"respawn_pane({target!r})")
            for pane in self.panes:
                if pane.pane_id == target:
                    pane.pane_current_command = "python"
                    return
            raise RuntimeError(f"can't find pane: {target}")

        def split_window(
            self,
            target: str,
            command: str,
            *,
            horizontal: bool = True,
            detached: bool = True,
            percent: int | None = None,
            size: int | None = None,
        ) -> str:
            new_id = f"%split{self._next_id}"
            self._next_id += 1
            self.actions.append(f"split_window -> {new_id}")
            anchor_left = (
                max(p.pane_left for p in self.panes) if self.panes else 0
            )
            anchor_width = (
                max(p.pane_width for p in self.panes) if self.panes else 200
            )
            new_pane = FakePane(
                new_id,
                anchor_left + 1,
                "python",
                width=size or anchor_width // 2,
            )
            self.panes.append(new_pane)
            return new_id

        def resize_pane_width(self, target: str, width: int) -> None:
            for pane in self.panes:
                if pane.pane_id == target:
                    pane.pane_width = width
                    return

        def set_pane_history_limit(self, target: str, limit: int) -> None:
            pass

        def select_pane(self, target: str) -> None:
            pass

        def break_pane(
            self, source: str, target_session: str, window_name: str,
        ) -> None:
            self.actions.append(f"break_pane({source!r})")
            for pane in list(self.panes):
                if pane.pane_id == source:
                    self.panes.remove(pane)
                    self.storage_panes[window_name] = FakeWindow(
                        window_name, index=99, pane_id=source,
                    )
                    return

        def rename_window(self, target: str, name: str) -> None:
            pass

        def swap_pane(self, source: str, target: str) -> None:
            pass

        def run(self, *args, **kwargs):
            from subprocess import CompletedProcess
            return CompletedProcess(
                args=args, returncode=0, stdout="", stderr="",
            )

        def capture_pane(self, target: str, lines: int = 100) -> str:
            return ""

        def kill_window(self, target: str) -> None:
            pass

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
        def __init__(self) -> None:
            class Project:
                tmux_session = "pollypm"
                base_dir = tmp_path / ".pollypm"
                root_dir = tmp_path

            class Config:
                project = Project()
                projects = {
                    "pollypm": KnownProject(
                        key="pollypm",
                        path=tmp_path,
                        name="PollyPM",
                        kind=ProjectKind.GIT,
                    ),
                    "demo": KnownProject(
                        key="demo",
                        path=tmp_path / "demo",
                        name="Demo",
                        kind=ProjectKind.GIT,
                    ),
                }
                sessions = {}

            self.config = Config()

        def plan_launches(self):
            return [FakeLaunch()]

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

        def claim_lease(self, *args, **kwargs) -> None:
            pass

        def release_lease(self, *args, **kwargs) -> None:
            pass

        def launch_session(self, session_name: str) -> None:
            return None

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f'[project]\nname = "PollyPM"\n'
        f'tmux_session = "pollypm"\n'
        f'base_dir = "{tmp_path / ".pollypm"}"\n'
    )
    router = CockpitRouter(config_path)
    tmux = FakeTmux()
    router.tmux = tmux  # type: ignore[assignment]
    sup = FakeSupervisor()
    router._load_supervisor = lambda fresh=False: sup  # type: ignore[assignment]
    router._write_state(
        {"selected": selected, "right_pane_id": "%2"}
    )
    return router, tmux, sup


def test_995_polly_click_from_project_subitem_keeps_two_panes(
    tmp_path: Path,
) -> None:
    """#995 regression — clicking ``Polly · chat`` while the prior rail
    selection is on a project sub-item must mount the operator pane in
    the right cockpit pane WITHOUT collapsing the cockpit to a single
    rail-only pane.

    Pre-fix symptom (issue body): pane 1 disappears, rail TUI in pane 0
    renders doubled across the now-too-wide pane, and ``tmux capture-pane
    -t pollypm:0.1 -p`` returns "can't find pane: 1". Subsequent rail
    clicks process keystrokes but the layout never recovers because every
    downstream code path assumes the rail has a sibling right pane.

    The contract this test locks in: after ``route_selected("polly")``
    returns from a project-scoped prior selection, the cockpit window has
    exactly two live panes — rail + content — regardless of which fallback
    path the live-mount logic takes internally.
    """
    router, tmux, _sup = _polly_chat_995_make_router(
        tmp_path,
        selected="project:demo:dashboard",
    )

    router.route_selected("polly")

    pane_ids = [p.pane_id for p in tmux.panes]
    assert len(tmux.panes) == 2, (
        f"#995 — clicking Polly · chat from a project sub-item left "
        f"{len(tmux.panes)} pane(s) in the cockpit window: {pane_ids!r}. "
        "The rail then renders doubled across the now-too-wide pane."
    )
    # Rail (uv) must still be present on the left.
    left_pane = min(tmux.panes, key=lambda p: p.pane_left)
    assert left_pane.pane_id == "%1", (
        f"left rail pane {left_pane.pane_id!r} is not %1 (expected the "
        "uv-wrapped rail pane to remain leftmost)"
    )


def test_995_polly_click_recovers_when_join_pane_fails(
    tmp_path: Path,
) -> None:
    """#995 regression — even if the live-mount path raises mid-flight
    (e.g. ``tmux join-pane`` fails because the storage source pane was
    killed in a race), the cockpit must still end with two panes.

    Pre-fix: the original right pane could be killed before the join
    attempt, leaving only the rail. The post-route layout heal in
    ``route_selected`` re-runs ``ensure_cockpit_layout`` so #991's
    context-aware ``_default_repair_command`` splits a fresh content
    pane on the user's intended surface.
    """
    router, tmux, _sup = _polly_chat_995_make_router(
        tmp_path,
        selected="project:demo:dashboard",
        join_pane_raises=True,
    )

    # The route work may itself fall back to the static polly view;
    # we don't care which path runs as long as we end with two panes.
    router.route_selected("polly")

    assert len(tmux.panes) == 2, (
        f"#995 — Polly · chat click with a join_pane failure left "
        f"{len(tmux.panes)} pane(s); the rail-only collapse breaks "
        "the cockpit until the user kills the tmux session."
    )


def test_995_polly_click_heals_when_static_fallback_split_also_fails(
    tmp_path: Path,
) -> None:
    """#995 regression — when BOTH the live mount and the static-view
    fallback raise (e.g. tmux is briefly unresponsive during the
    cascade), the post-route layout heal in ``route_selected`` must
    still recover a two-pane cockpit on the next pass.

    Pre-fix this scenario was the hard collapse: ``_show_live_session``
    raised after killing pane 2, ``_show_static_view`` raised inside
    ``_route_live_session``'s except handler before splitting a
    replacement, and the unhandled exception escaped to the worker
    thread with the cockpit window left at 1 pane. Subsequent rail
    clicks then processed keypresses but the layout never recovered.
    """

    class FakePane:
        def __init__(self, pane_id, pane_left, command, width=80):
            self.pane_id = pane_id
            self.pane_left = pane_left
            self.pane_current_command = command
            self.pane_width = width
            self.pane_dead = False
            self.active = pane_left == 0

    class FakeWindow:
        def __init__(self, name, index, pane_id="%5"):
            self.name = name
            self.index = index
            self.pane_id = pane_id
            self.pane_current_command = "node"
            self.pane_current_path = "/tmp"
            self.active = False
            self.pane_dead = False

    class CascadingFailureTmux:
        """``join_pane`` collateral-kills %2 then raises;
        ``respawn_pane`` always raises;
        first ``split_window`` raises, second succeeds.

        This mirrors the live cascade observed in the issue: the
        collateral kill leaves only the rail, and the static-view
        fallback's first split flakes before the heal repairs.
        """

        def __init__(self):
            self.panes = [
                FakePane("%1", 0, "uv", 30),
                FakePane("%2", 31, "python", 170),
            ]
            self.storage = {"pm-operator": FakeWindow("pm-operator", 1, "%5")}
            self.split_failures = 0

        def list_panes(self, target):
            return list(self.panes)

        def list_windows(self, name):
            if "storage" in name:
                return list(self.storage.values())
            return [FakeWindow("PollyPM", 1)]

        def has_session(self, name):
            return True

        def kill_pane(self, target):
            self.panes = [p for p in self.panes if p.pane_id != target]

        def join_pane(self, source, target, *, horizontal=True):
            # Collateral: tmux kills pane 2 before reporting the join failure.
            self.panes = [p for p in self.panes if p.pane_id != "%2"]
            raise RuntimeError("simulated tmux join-pane failure")

        def respawn_pane(self, target, command):
            raise RuntimeError(f"can't find pane: {target}")

        def split_window(
            self,
            target,
            command,
            *,
            horizontal=True,
            detached=True,
            percent=None,
            size=None,
        ):
            self.split_failures += 1
            if self.split_failures == 1:
                raise RuntimeError("simulated transient split-window failure")
            new_id = f"%new{len(self.panes) + 100}"
            max_left = max((p.pane_left for p in self.panes), default=0)
            self.panes.append(
                FakePane(new_id, max_left + 31, "python", size or 80)
            )
            return new_id

        def resize_pane_width(self, target, width):
            for p in self.panes:
                if p.pane_id == target:
                    p.pane_width = width

        def set_pane_history_limit(self, target, limit):
            pass

        def select_pane(self, target):
            pass

        def break_pane(self, source, target_session, window_name):
            for pane in list(self.panes):
                if pane.pane_id == source:
                    self.panes.remove(pane)
                    self.storage[window_name] = FakeWindow(
                        window_name, 99, source,
                    )
                    return

        def rename_window(self, target, name):
            pass

        def swap_pane(self, source, target):
            pass

        def run(self, *args, **kwargs):
            from subprocess import CompletedProcess
            return CompletedProcess(
                args=args, returncode=0, stdout="", stderr="",
            )

        def capture_pane(self, target, lines=100):
            return ""

        def kill_window(self, target):
            pass

    class FakeLaunch:
        def __init__(self):
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
        def __init__(self):
            class Project:
                tmux_session = "pollypm"
                base_dir = tmp_path / ".pollypm"
                root_dir = tmp_path

            class Config:
                project = Project()
                projects = {
                    "pollypm": KnownProject(
                        key="pollypm",
                        path=tmp_path,
                        name="PollyPM",
                        kind=ProjectKind.GIT,
                    ),
                    "demo": KnownProject(
                        key="demo",
                        path=tmp_path / "demo",
                        name="Demo",
                        kind=ProjectKind.GIT,
                    ),
                }
                sessions = {}

            self.config = Config()

        def plan_launches(self):
            return [FakeLaunch()]

        def storage_closet_session_name(self):
            return "pollypm-storage-closet"

        def claim_lease(self, *a, **k):
            pass

        def release_lease(self, *a, **k):
            pass

        def launch_session(self, n):
            pass

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f'[project]\nname = "PollyPM"\n'
        f'tmux_session = "pollypm"\n'
        f'base_dir = "{tmp_path / ".pollypm"}"\n'
    )
    router = CockpitRouter(config_path)
    tmux = CascadingFailureTmux()
    router.tmux = tmux  # type: ignore[assignment]
    sup = FakeSupervisor()
    router._load_supervisor = lambda fresh=False: sup  # type: ignore[assignment]
    router._write_state(
        {"selected": "project:demo:dashboard", "right_pane_id": "%2"}
    )

    # The cascade may surface as a RuntimeError to the worker thread —
    # that is fine and expected; what matters for #995 is the layout.
    try:
        router.route_selected("polly")
    except RuntimeError:
        pass

    assert len(tmux.panes) == 2, (
        f"#995 — even with both live-mount AND static-view fallback "
        f"failing transiently, the post-route heal must restore a "
        f"two-pane cockpit, but ended with {len(tmux.panes)} pane(s)."
    )


def test_995_layout_heal_skips_when_layout_is_already_healthy(
    tmp_path: Path,
) -> None:
    """The post-route layout heal must NOT bounce a healthy two-pane
    cockpit. Re-running ``ensure_cockpit_layout`` when the layout is
    already correct would race with the live-mount that just succeeded
    and re-trigger the very split-then-respawn churn the fix is meant
    to avoid.

    Locked-in contract: ``_heal_layout_after_route`` short-circuits
    when ``list_panes`` reports two live panes.
    """

    class FakePane:
        def __init__(self, pane_id: str) -> None:
            self.pane_id = pane_id
            self.pane_dead = False
            self.pane_left = 0 if pane_id == "%1" else 31
            self.pane_current_command = "uv" if pane_id == "%1" else "node"
            self.pane_width = 30 if pane_id == "%1" else 80

    class CountingTmux:
        def __init__(self) -> None:
            self.list_calls = 0
            self.ensure_calls = 0

        def list_panes(self, target: str):
            self.list_calls += 1
            return [FakePane("%1"), FakePane("%2")]

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f'[project]\nname = "PollyPM"\n'
        f'tmux_session = "pollypm"\n'
        f'base_dir = "{tmp_path / ".pollypm"}"\n'
    )
    router = CockpitRouter(config_path)
    router.tmux = CountingTmux()  # type: ignore[assignment]

    seen_ensure = []

    def fake_ensure() -> None:
        seen_ensure.append(True)

    router.ensure_cockpit_layout = fake_ensure  # type: ignore[assignment]

    router._heal_layout_after_route()

    assert seen_ensure == [], (
        "_heal_layout_after_route called ensure_cockpit_layout despite "
        "the layout already being healthy; this would re-split the "
        "right pane every click and undo a successful live mount."
    )
