"""Unit + integration tests for the per-project dashboard rewrite (#245).

The dashboard is assembled from per-section helpers so these tests
exercise each helper with hand-rolled fake data plus one end-to-end
integration test that seeds a real SQLite-backed work service.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass
from pathlib import Path

import pytest

from pollypm.cockpit import (
    _aggregate_project_tokens,
    _dashboard_divider,
    _find_commit_sha,
    _format_clock,
    _format_tokens,
    _iso_to_dt,
    _render_project_dashboard,
    _section_activity,
    _section_downtime,
    _section_header,
    _section_in_flight,
    _section_insights,
    _section_quick_actions,
    _section_recent,
    _section_summary,
    _section_velocity,
    _section_you_need_to,
    _task_cycle_minutes,
    _worker_presence,
)
from pollypm.cockpit_sections.action_bar import render_project_action_bar
from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    OutputType,
    Priority,
    WorkOutput,
    WorkStatus,
)
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Helpers — fakes shared across tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeTransition:
    to_state: str
    actor: str
    timestamp: datetime
    from_state: str = ""


@dataclass
class _FakeArtifact:
    kind: ArtifactKind
    ref: str | None


@dataclass
class _FakeOutput:
    artifacts: list


@dataclass
class _FakeExecution:
    work_output: _FakeOutput | None


class _FakeTask:
    """Lightweight stand-in for a ``work.models.Task``."""

    def __init__(
        self,
        *,
        task_number: int,
        title: str,
        status: str,
        priority: str = "normal",
        updated_at: datetime | None = None,
        transitions: list | None = None,
        executions: list | None = None,
        assignee: str | None = None,
        current_node_id: str | None = None,
        blocked_by: list[tuple[str, int]] | None = None,
    ) -> None:
        self.task_number = task_number
        self.title = title
        self.work_status = WorkStatus(status)
        self.priority = Priority(priority)
        self.updated_at = updated_at
        self.transitions = transitions or []
        self.executions = executions or []
        self.assignee = assignee
        self.current_node_id = current_node_id
        self.blocked_by = blocked_by or []


class _FakeEvent:
    def __init__(self, created_at: str, event_type: str, message: str, session_name: str):
        self.created_at = created_at
        self.event_type = event_type
        self.message = message
        self.session_name = session_name


class _FakeAlert:
    def __init__(self, session_name: str, alert_type: str, message: str):
        self.session_name = session_name
        self.alert_type = alert_type
        self.message = message
        self.severity = "warning"


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


class TestFormatters:
    def test_format_tokens_under_1k(self):
        assert _format_tokens(42) == "42"

    def test_format_tokens_k_range(self):
        assert _format_tokens(45200) == "45.2k"

    def test_format_tokens_m_range(self):
        assert _format_tokens(2_100_000) == "2.1M"

    def test_iso_to_dt_handles_naive_string(self):
        dt = _iso_to_dt("2026-04-16T10:30:00")
        assert dt is not None and dt.tzinfo is not None

    def test_iso_to_dt_handles_datetime(self):
        now = datetime.now(UTC)
        assert _iso_to_dt(now) == now

    def test_iso_to_dt_returns_none_on_garbage(self):
        assert _iso_to_dt("not a date") is None

    def test_format_clock_renders_hhmm(self):
        dt = datetime(2026, 4, 16, 21, 5, tzinfo=UTC)
        assert _format_clock(dt) == "21:05"

    def test_dashboard_divider_embeds_title(self):
        line = _dashboard_divider("Recent")
        assert "Recent" in line
        assert line.startswith("  ─── Recent ")


# ---------------------------------------------------------------------------
# Section helpers
# ---------------------------------------------------------------------------


class TestSectionHeader:
    def test_header_includes_name_and_presence(self):
        out = _section_header("shortlink-gen", "● worker alive")
        assert "shortlink-gen" in out
        assert "● worker alive" in out


class TestSectionSummary:
    def test_counts_render_left_to_right(self):
        out = _section_summary(
            {"in_progress": 1, "queued": 2, "review": 0, "done": 3},
        )
        assert "1 in progress" in out
        assert "2 queued" in out
        assert "3 done" in out
        # Zero buckets are suppressed.
        assert "0 review" not in out

    def test_empty_counts_show_placeholder(self):
        assert "No tasks yet." in _section_summary({})


class TestSectionVelocity:
    def test_velocity_sparkline_renders_when_tasks_completed(self):
        now = datetime.now(UTC)
        tasks = [
            _FakeTask(
                task_number=i,
                title=f"t{i}",
                status="done",
                updated_at=now - timedelta(days=i),
                transitions=[
                    _FakeTransition("in_progress", "w", now - timedelta(days=i, minutes=30)),
                    _FakeTransition("done", "r", now - timedelta(days=i)),
                ],
            )
            for i in range(1, 6)
        ]
        lines = _section_velocity(tasks, None)
        assert any("Velocity" in l for l in lines)
        assert any("Cycle time" in l for l in lines)
        # 30m per task on average.
        assert any("30m avg" in l for l in lines)

    def test_tokens_line_present_when_tokens_supplied(self):
        lines = _section_velocity([], (45200, 12100))
        assert any("45.2k in" in l and "12.1k out" in l for l in lines)

    def test_tokens_line_skipped_when_zero(self):
        lines = _section_velocity([], (0, 0))
        assert not any("Tokens" in l for l in lines)

    def test_tokens_line_skipped_when_none(self):
        """None aggregation (DB missing work_sessions table) drops the line."""
        lines = _section_velocity([], None)
        assert not any("Tokens" in l for l in lines)


class TestSectionYouNeedTo:
    def test_empty_shows_nothing_pending(self):
        lines = _section_you_need_to([], [], 0)
        assert any("Nothing pending" in l for l in lines)

    def test_review_tasks_surface_as_approval_prompts(self):
        t = _FakeTask(task_number=7, title="Ship it", status="review")
        lines = _section_you_need_to([t], [], 0)
        assert any("approve #7" in l for l in lines)

    def test_alerts_surface_with_type_and_message(self):
        alert = _FakeAlert("worker-x", "pane_dead", "pane exited unexpectedly")
        lines = _section_you_need_to([], [alert], 0)
        assert any("pane_dead" in l for l in lines)


class TestSectionInFlight:
    def test_empty_in_flight_is_labeled(self):
        lines = _section_in_flight([])
        assert any("(none)" in l for l in lines)

    def test_in_flight_lists_task(self):
        t = _FakeTask(
            task_number=2,
            title="Add favicon",
            status="in_progress",
            assignee="worker",
            current_node_id="implement",
            updated_at=datetime.now(UTC),
        )
        lines = _section_in_flight([t])
        joined = "\n".join(lines)
        assert "#2 Add favicon" in joined
        assert "[worker]" in joined
        assert "@ implement" in joined

    def test_in_flight_surfaces_priority_glyphs_and_sorts_critical_first(self):
        low = _FakeTask(
            task_number=2,
            title="Add favicon",
            status="in_progress",
            priority="low",
            updated_at=datetime(2026, 4, 20, 16, 0, tzinfo=UTC),
        )
        critical = _FakeTask(
            task_number=7,
            title="Patch auth outage",
            status="in_progress",
            priority="critical",
            updated_at=datetime(2026, 4, 20, 15, 0, tzinfo=UTC),
        )
        lines = _section_in_flight([low, critical])
        task_lines = [line for line in lines if "#7" in line or "#2" in line]
        assert task_lines[0].startswith("  ⟳ 🔴 #7 Patch auth outage")
        assert task_lines[1].startswith("  ⟳ 🟢 #2 Add favicon")

    def test_blocked_tasks_render_nested_under_their_blocker(self):
        blocker = _FakeTask(
            task_number=2,
            title="Ship router",
            status="in_progress",
            assignee="worker",
            current_node_id="implement",
            updated_at=datetime.now(UTC),
        )
        blocked = _FakeTask(
            task_number=3,
            title="Wire alerts",
            status="blocked",
            assignee="worker",
            current_node_id="implement",
            updated_at=datetime.now(UTC),
            blocked_by=[("proj", 2)],
        )

        lines = _section_in_flight([blocker], [blocked])
        joined = "\n".join(lines)

        assert "#2 Ship router" in joined
        assert "└─ ⊘" in joined
        assert "#3 Wire alerts" in joined
        assert "waiting on #2" in joined

    def test_orphan_blocked_tasks_render_with_wait_reason(self):
        blocked = _FakeTask(
            task_number=8,
            title="Retry sync",
            status="blocked",
            updated_at=datetime.now(UTC),
            blocked_by=[("proj", 1), ("proj", 4)],
        )

        lines = _section_in_flight([], [blocked])
        joined = "\n".join(lines)

        assert "#8 Retry sync" in joined
        assert "waiting on #1, #4" in joined


class TestSectionRecent:
    def test_recent_without_completions(self):
        lines = _section_recent([])
        assert any("(none)" in l for l in lines)

    def test_recent_surfaces_commit_sha_and_cycle(self):
        now = datetime.now(UTC)
        transitions = [
            _FakeTransition("in_progress", "worker", now - timedelta(minutes=25)),
            _FakeTransition("done", "russell", now),
        ]
        executions = [
            _FakeExecution(
                _FakeOutput(
                    artifacts=[_FakeArtifact(ArtifactKind.COMMIT, "237dfb01234567")]
                ),
            ),
        ]
        t = _FakeTask(
            task_number=1,
            title="Build shortlink-gen CLI",
            status="done",
            updated_at=now,
            transitions=transitions,
            executions=executions,
        )
        lines = _section_recent([t])
        joined = "\n".join(lines)
        assert "#1 Build shortlink-gen CLI" in joined
        assert "approved by russell" in joined
        assert "commit 237dfb0" in joined
        assert "25m cycle" in joined


class TestSectionActivity:
    def test_empty_activity(self):
        lines = _section_activity([], [])
        assert any("(none)" in l for l in lines)

    def test_transitions_within_24h_render(self):
        now = datetime.now(UTC)
        t = _FakeTask(
            task_number=1,
            title="t",
            status="done",
            transitions=[
                _FakeTransition("queued", "polly", now - timedelta(hours=1)),
                _FakeTransition("in_progress", "worker", now - timedelta(minutes=30)),
                _FakeTransition("done", "russell", now - timedelta(minutes=5)),
            ],
        )
        lines = _section_activity([t], [])
        joined = "\n".join(lines)
        assert "task/1 → done" in joined
        assert "task/1 → in_progress" in joined
        assert "(russell)" in joined

    def test_transitions_older_than_24h_filtered_out(self):
        now = datetime.now(UTC)
        t = _FakeTask(
            task_number=1,
            title="t",
            status="done",
            transitions=[
                _FakeTransition("queued", "polly", now - timedelta(days=3)),
            ],
        )
        lines = _section_activity([t], [])
        assert any("(none)" in l for l in lines)


class TestSectionInsights:
    def test_no_log_shows_empty_line(self, tmp_path: Path):
        lines = _section_insights(tmp_path, "proj")
        assert any("no advisor insights" in l for l in lines)

    def test_recent_emit_entry_rendered(self, tmp_path: Path):
        state_dir = tmp_path / ".pollypm"
        state_dir.mkdir()
        log = state_dir / "advisor-log.jsonl"
        log.write_text(
            json.dumps({
                "timestamp": datetime.now(UTC).isoformat(),
                "project": "proj",
                "decision": "emit",
                "topic": "architecture_drift",
                "severity": "recommendation",
                "summary": "Consider extracting URL validation",
            }) + "\n"
        )
        lines = _section_insights(tmp_path, "proj")
        joined = "\n".join(lines)
        assert "Consider extracting URL validation" in joined
        assert "[recommendation]" in joined

    def test_silent_entries_are_skipped(self, tmp_path: Path):
        state_dir = tmp_path / ".pollypm"
        state_dir.mkdir()
        log = state_dir / "advisor-log.jsonl"
        log.write_text(
            json.dumps({
                "timestamp": datetime.now(UTC).isoformat(),
                "project": "proj",
                "decision": "silent",
                "summary": "boring",
            }) + "\n"
        )
        lines = _section_insights(tmp_path, "proj")
        assert any("no advisor insights" in l for l in lines)

    def test_wrong_project_entries_filtered_out(self, tmp_path: Path):
        state_dir = tmp_path / ".pollypm"
        state_dir.mkdir()
        log = state_dir / "advisor-log.jsonl"
        log.write_text(
            json.dumps({
                "timestamp": datetime.now(UTC).isoformat(),
                "project": "other",
                "decision": "emit",
                "summary": "not us",
            }) + "\n"
        )
        lines = _section_insights(tmp_path, "proj")
        assert any("no advisor insights" in l for l in lines)


class TestSectionDowntime:
    def test_no_file_renders_none_queued(self, tmp_path: Path):
        lines = _section_downtime(tmp_path)
        assert any("(none queued)" in l for l in lines)

    def test_first_five_markdown_items(self, tmp_path: Path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "downtime-backlog.md").write_text(
            "# Backlog\n\n"
            "- first item\n"
            "- second item\n"
            "* third (with asterisk)\n"
            "+ fourth (with plus)\n"
            "- fifth item\n"
            "- sixth should be skipped\n"
        )
        lines = _section_downtime(tmp_path)
        joined = "\n".join(lines)
        assert "first item" in joined
        assert "fifth item" in joined
        assert "sixth" not in joined


class TestSectionQuickActions:
    def test_hotkey_hints_present(self):
        lines = _section_quick_actions()
        joined = "\n".join(lines)
        assert "n  new task" in joined
        assert "w  start worker" in joined
        assert "r  replan" in joined


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


class TestWorkerPresence:
    def test_no_launches_returns_no_worker(self):
        class _Sup:
            class store:
                @staticmethod
                def latest_heartbeat(name): return None
            def plan_launches(self): return []
        assert _worker_presence(_Sup(), "proj") == "– no worker"

    def test_alive_when_recent_heartbeat(self):
        from datetime import UTC, datetime, timedelta

        class _HB:
            created_at = datetime.now(UTC).isoformat()
            pane_dead = False

        class _Store:
            def latest_heartbeat(self, name):
                return _HB

        class _Session:
            def __init__(self, name, project, role):
                self.name = name
                self.project = project
                self.role = role

        class _Launch:
            def __init__(self, session):
                self.session = session

        class _Sup:
            store = _Store()
            def plan_launches(self):
                return [_Launch(_Session("worker-x", "proj", "worker"))]

        assert _worker_presence(_Sup(), "proj") == "● worker alive"

    def test_idle_when_stale_heartbeat(self):
        from datetime import UTC, datetime, timedelta

        class _HB:
            created_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
            pane_dead = False

        class _Store:
            def latest_heartbeat(self, name):
                return _HB

        class _Session:
            def __init__(self):
                self.name = "worker-x"
                self.project = "proj"
                self.role = "worker"

        class _Launch:
            session = _Session()

        class _Sup:
            store = _Store()
            def plan_launches(self):
                return [_Launch()]

        assert _worker_presence(_Sup(), "proj") == "○ worker idle"


class TestCommitShaExtraction:
    def test_returns_first_commit_artifact(self):
        execs = [
            _FakeExecution(
                _FakeOutput(
                    artifacts=[_FakeArtifact(ArtifactKind.COMMIT, "abcdef0123456")]
                )
            )
        ]
        t = _FakeTask(task_number=1, title="x", status="done", executions=execs)
        assert _find_commit_sha(t) == "abcdef0"

    def test_returns_none_when_no_commit_artifacts(self):
        t = _FakeTask(task_number=1, title="x", status="done", executions=[])
        assert _find_commit_sha(t) is None


class TestCycleMinutes:
    def test_none_when_transitions_missing(self):
        t = _FakeTask(task_number=1, title="x", status="done")
        assert _task_cycle_minutes(t) is None

    def test_returns_minutes_between_start_and_end(self):
        now = datetime.now(UTC)
        transitions = [
            _FakeTransition("in_progress", "w", now - timedelta(minutes=25)),
            _FakeTransition("done", "r", now),
        ]
        t = _FakeTask(
            task_number=1, title="x", status="done", transitions=transitions,
        )
        assert _task_cycle_minutes(t) == 25


class TestTokenAggregation:
    def test_returns_none_for_missing_db(self, tmp_path: Path):
        assert _aggregate_project_tokens(tmp_path / "nope.db", "proj") is None

    def test_sums_across_sessions(self, tmp_path: Path):
        """SUM tokens only for the matching project (FK-safe via real tasks)."""
        db_path = tmp_path / "state.db"
        svc = SQLiteWorkService(db_path=db_path, project_path=tmp_path)
        svc.ensure_worker_session_schema()

        def _seed(project: str, agent="w"):
            task = svc.create(
                title=f"t-{project}-{agent}",
                description="",
                type="task",
                project=project,
                flow_template="standard",
                roles={"worker": "pete", "reviewer": "polly"},
                priority="normal",
                created_by="tester",
            )
            svc.upsert_worker_session(
                task_project=project, task_number=task.task_number,
                agent_name=agent, pane_id=f"%{task.task_number}",
                worktree_path=f"/tmp/{project}-{task.task_number}",
                branch_name=f"task/{task.task_number}",
                started_at="2026-04-16T00:00:00+00:00",
            )
            return task

        t_proj_1 = _seed("proj", agent="w1")
        svc.end_worker_session(
            task_project="proj", task_number=t_proj_1.task_number,
            ended_at="2026-04-16T00:30:00+00:00",
            total_input_tokens=1000, total_output_tokens=500, archive_path=None,
        )
        t_proj_2 = _seed("proj", agent="w2")
        svc.end_worker_session(
            task_project="proj", task_number=t_proj_2.task_number,
            ended_at="2026-04-16T00:30:00+00:00",
            total_input_tokens=200, total_output_tokens=100, archive_path=None,
        )
        t_other = _seed("other", agent="w3")
        svc.end_worker_session(
            task_project="other", task_number=t_other.task_number,
            ended_at="2026-04-16T00:30:00+00:00",
            total_input_tokens=9999, total_output_tokens=9999, archive_path=None,
        )
        svc.close()

        assert _aggregate_project_tokens(db_path, "proj") == (1200, 600)


# ---------------------------------------------------------------------------
# Integration — seed a real project, render the dashboard
# ---------------------------------------------------------------------------


def _seed_project(tmp_path: Path) -> tuple[Path, str]:
    """Seed a SQLite-backed project with one completed + one active task."""
    proj_path = tmp_path / "shortlink-gen"
    proj_path.mkdir()
    (proj_path / ".pollypm").mkdir()
    db_path = proj_path / ".pollypm" / "state.db"
    svc = SQLiteWorkService(db_path=db_path, project_path=proj_path)
    t1 = svc.create(
        title="Build shortlink-gen CLI",
        description="Initial CLI",
        type="task",
        project="shortlink-gen",
        flow_template="standard",
        roles={"worker": "pete", "reviewer": "russell"},
        priority="normal",
        created_by="polly",
    )
    svc.queue(t1.task_id, "polly")
    svc.claim(t1.task_id, "worker")
    svc.node_done(
        t1.task_id, "worker",
        WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="Built it",
            artifacts=[
                Artifact(kind=ArtifactKind.COMMIT, description="build", ref="237dfb01234"),
            ],
        ),
    )
    svc.approve(t1.task_id, "russell")

    t2 = svc.create(
        title="Add favicon fetching",
        description="fetch icons",
        type="task",
        project="shortlink-gen",
        flow_template="standard",
        roles={"worker": "pete", "reviewer": "russell"},
        priority="normal",
        created_by="polly",
    )
    svc.queue(t2.task_id, "polly")
    svc.claim(t2.task_id, "worker")

    svc.ensure_worker_session_schema()
    svc.upsert_worker_session(
        task_project="shortlink-gen", task_number=t1.task_number,
        agent_name="worker", pane_id="%1", worktree_path="/tmp/wt",
        branch_name="task/1", started_at=datetime.now(UTC).isoformat(),
    )
    svc.end_worker_session(
        task_project="shortlink-gen", task_number=t1.task_number,
        ended_at=datetime.now(UTC).isoformat(),
        total_input_tokens=45200, total_output_tokens=12100, archive_path=None,
    )
    svc.close()
    return proj_path, "shortlink-gen"


class _DashFakeProject:
    def __init__(self, key: str, path: Path, name: str) -> None:
        self.key = key
        self.path = path
        self.name = name


class _DashFakeStore:
    def open_alerts(self): return []
    def recent_events(self, limit=200): return []
    def latest_heartbeat(self, name): return None


class _DashFakeSupervisor:
    store = _DashFakeStore()
    def plan_launches(self): return []


def test_render_project_dashboard_integration(tmp_path: Path):
    """End-to-end: seed a project and assert every section appears."""
    proj_path, project_key = _seed_project(tmp_path)

    # Advisor log with an emit entry.
    state_dir = proj_path / ".pollypm"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "advisor-log.jsonl").write_text(
        json.dumps({
            "timestamp": datetime.now(UTC).isoformat(),
            "project": project_key,
            "decision": "emit",
            "topic": "architecture_drift",
            "severity": "recommendation",
            "summary": "Consider extracting URL validation",
        }) + "\n"
    )

    # Downtime backlog file.
    docs = proj_path / "docs"
    docs.mkdir()
    (docs / "downtime-backlog.md").write_text(
        "- refactor cli.py\n- add logging\n"
    )

    project = _DashFakeProject(project_key, proj_path, "shortlink-gen")
    out = _render_project_dashboard(
        project, project_key, tmp_path / "pollypm.toml", _DashFakeSupervisor(),
    )
    assert out is not None

    # Every labeled section heading must be present.
    for heading in (
        "You need to",
        "In flight",
        "Recent",
        "Activity (last 24h)",
        "Insights",
        "Downtime backlog",
        "Quick actions",
    ):
        assert heading in out, f"missing section: {heading}"

    # Content spot-checks.
    assert "▸ Clear · no approvals, alerts, or inbox items" in out
    assert "shortlink-gen" in out
    assert "1 in progress" in out
    assert "1 done" in out
    assert "45.2k in" in out and "12.1k out" in out
    assert "Add favicon" in out          # in-flight
    assert "Build shortlink-gen CLI" in out  # recent
    assert "commit 237dfb0" in out
    assert "approved by russell" in out
    assert "Consider extracting URL validation" in out  # insight
    assert "refactor cli.py" in out       # downtime
    assert "n  new task" in out           # quick actions


def test_render_project_dashboard_missing_db_returns_none(tmp_path: Path):
    """A project with no state.db returns ``None`` so the caller can fall back."""
    proj_path = tmp_path / "empty"
    proj_path.mkdir()
    project = _DashFakeProject("empty", proj_path, "Empty")
    out = _render_project_dashboard(
        project, "empty", tmp_path / "pollypm.toml", _DashFakeSupervisor(),
    )
    assert out is None


def test_render_project_dashboard_empty_project_has_all_sections(tmp_path: Path):
    """Fresh project with zero tasks still renders every section placeholder."""
    proj_path = tmp_path / "fresh"
    proj_path.mkdir()
    (proj_path / ".pollypm").mkdir()
    db_path = proj_path / ".pollypm" / "state.db"
    svc = SQLiteWorkService(db_path=db_path, project_path=proj_path)
    svc.close()

    project = _DashFakeProject("fresh", proj_path, "Fresh")
    out = _render_project_dashboard(
        project, "fresh", tmp_path / "pollypm.toml", _DashFakeSupervisor(),
    )
    assert out is not None
    # Empty-state markers for sections without data.
    assert "No tasks yet." in out
    assert "Nothing pending." in out
    assert "(none)" in out
    assert "no advisor insights" in out
    assert "(none queued)" in out
    # Quick actions always render.
    assert "n  new task" in out


# ---------------------------------------------------------------------------
# Visual-parity snapshot — guard against future regressions
# ---------------------------------------------------------------------------


def test_dashboard_visual_structure_is_stable(tmp_path: Path):
    """Snapshot-style test — exact section-heading sequence must hold.

    If a future change reorders sections or renames a heading, this test
    flags the regression. Content inside sections is allowed to drift;
    only the skeleton is checked.
    """
    proj_path, project_key = _seed_project(tmp_path)
    project = _DashFakeProject(project_key, proj_path, "shortlink-gen")
    out = _render_project_dashboard(
        project, project_key, tmp_path / "pollypm.toml", _DashFakeSupervisor(),
    )
    assert out is not None

    expected_sections = [
        "You need to",
        "In flight",
        "Recent",
        "Activity (last 24h)",
        "Insights",
        "Downtime backlog",
        "Quick actions",
    ]
    # Each heading must appear in order.
    cursor = 0
    for heading in expected_sections:
        idx = out.find(heading, cursor)
        assert idx >= 0, f"section {heading!r} missing or out of order"
        cursor = idx


def test_render_project_action_bar_summarizes_pending_counts() -> None:
    assert (
        render_project_action_bar(review_count=3, alert_count=1, inbox_count=5)
        == "▸ 3 approvals · 1 alert · 5 new in inbox"
    )
    assert (
        render_project_action_bar(review_count=0, alert_count=0, inbox_count=0)
        == "▸ Clear · no approvals, alerts, or inbox items"
    )
