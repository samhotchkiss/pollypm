from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from pollypm.cockpit import _build_cockpit_detail_dispatch
from pollypm.cockpit_sections.dashboard import (
    DashboardBriefingBanner,
    _briefing_banner,
    _build_dashboard,
    _build_token_gauge,
    _rank_dashboard_suggestions,
    _render_token_gauge,
    _shipper_streaks,
)
from pollypm.plugins_builtin.morning_briefing.handlers.synthesize import (
    BriefingDraft,
)
from pollypm.plugins_builtin.morning_briefing.inbox import emit_briefing
from pollypm.storage.state import AccountUsageRecord, TokenUsageHourlyRecord


NOW = datetime(2026, 4, 21, 15, 0, tzinfo=UTC)


@dataclass
class _FakeTransition:
    to_state: str
    timestamp: datetime
    actor: str = "worker"
    from_state: str = ""


@dataclass
class _FakeExecution:
    decision: str | None = None
    completed_at: datetime | None = None


class _FakeTask:
    def __init__(
        self,
        *,
        task_number: int,
        title: str,
        work_status: str,
        updated_at: datetime,
        assignee: str | None = None,
        current_node_id: str | None = None,
        priority: str = "normal",
        transitions: list[_FakeTransition] | None = None,
        executions: list[_FakeExecution] | None = None,
    ) -> None:
        self.task_number = task_number
        self.title = title
        self.work_status = work_status
        self.updated_at = updated_at
        self.assignee = assignee
        self.current_node_id = current_node_id
        self.priority = priority
        self.transitions = transitions or []
        self.executions = executions or []
        self.roles = {"worker": assignee} if assignee else {}


class _FakeProject:
    def __init__(self, path: Path, label: str) -> None:
        self.path = path
        self._label = label

    def display_label(self) -> str:
        return self._label


class _FakeStore:
    def __init__(
        self,
        *,
        open_alerts: list | None = None,
        recent_events: list | None = None,
        token_rows: list[TokenUsageHourlyRecord] | None = None,
        account_usage: dict[str, AccountUsageRecord] | None = None,
    ) -> None:
        self._open_alerts = open_alerts or []
        self._recent_events = recent_events or []
        self._token_rows = token_rows or []
        self._account_usage = account_usage or {}

    def open_alerts(self):
        return list(self._open_alerts)

    def recent_events(self, limit: int = 300):
        return list(self._recent_events[:limit])

    def recent_token_usage(self, limit: int = 5000):
        return list(self._token_rows[:limit])

    def get_account_usage(self, account_name: str):
        return self._account_usage.get(account_name)


def _make_config(tmp_path: Path):
    base_dir = tmp_path / ".pollypm"
    return SimpleNamespace(
        project=SimpleNamespace(base_dir=base_dir, root_dir=tmp_path),
        pollypm=SimpleNamespace(timezone="UTC"),
        projects={
            "demo": _FakeProject(tmp_path / "demo", "Demo"),
            "docs": _FakeProject(tmp_path / "docs", "Docs"),
        },
        accounts={
            "claude_main": SimpleNamespace(provider="claude"),
            "codex_main": SimpleNamespace(provider="codex"),
        },
    )


def test_shipper_streaks_only_count_clean_consecutive_done() -> None:
    bob_clean_1 = _FakeTask(
        task_number=1,
        title="Bob clean 1",
        work_status="done",
        updated_at=NOW - timedelta(hours=1),
        assignee="bob",
        transitions=[_FakeTransition("done", NOW - timedelta(hours=1), actor="russell")],
    )
    bob_clean_2 = _FakeTask(
        task_number=2,
        title="Bob clean 2",
        work_status="done",
        updated_at=NOW - timedelta(hours=3),
        assignee="bob",
        transitions=[_FakeTransition("done", NOW - timedelta(hours=3), actor="russell")],
    )
    alice_rejected = _FakeTask(
        task_number=3,
        title="Alice rejected",
        work_status="in_progress",
        updated_at=NOW - timedelta(minutes=30),
        assignee="alice",
        transitions=[
            _FakeTransition(
                "in_progress",
                NOW - timedelta(minutes=30),
                actor="russell",
                from_state="review",
            )
        ],
        executions=[_FakeExecution(decision="rejected", completed_at=NOW - timedelta(minutes=30))],
    )
    carol_dirty_done = _FakeTask(
        task_number=4,
        title="Carol dirty done",
        work_status="done",
        updated_at=NOW - timedelta(hours=2),
        assignee="carol",
        transitions=[
            _FakeTransition(
                "in_progress",
                NOW - timedelta(hours=4),
                actor="russell",
                from_state="review",
            ),
            _FakeTransition("done", NOW - timedelta(hours=2), actor="russell"),
        ],
        executions=[_FakeExecution(decision="rejected", completed_at=NOW - timedelta(hours=4))],
    )

    streaks = _shipper_streaks(
        [
            ("demo", bob_clean_1),
            ("demo", bob_clean_2),
            ("docs", alice_rejected),
            ("docs", carol_dirty_done),
        ],
        now=NOW,
    )

    assert streaks == {"bob": 2}


def test_rank_dashboard_suggestions_prefers_review_blocked_briefing_then_queue(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    review = _FakeTask(
        task_number=5,
        title="Approve me",
        work_status="review",
        updated_at=NOW - timedelta(minutes=12),
        assignee="sam",
    )
    blocked = _FakeTask(
        task_number=3,
        title="Blocked work",
        work_status="blocked",
        updated_at=NOW - timedelta(hours=2),
        assignee="dora",
    )
    queued = _FakeTask(
        task_number=7,
        title="Queued task",
        work_status="queued",
        updated_at=NOW - timedelta(minutes=5),
        assignee="ian",
        priority="high",
    )

    suggestions = _rank_dashboard_suggestions(
        review_tasks=[("demo", review)],
        blocked_tasks=[("docs", blocked)],
        queued_tasks=[("demo", queued)],
        briefing_banner=DashboardBriefingBanner(
            text="Morning briefing available",
            date_local="2026-04-21",
        ),
        user_inbox=0,
        config=config,
        now=NOW,
    )

    assert [item.label for item in suggestions] == [
        "Approve demo/5",
        "Check why docs/3 is blocked",
        "Read morning briefing",
        "Claim demo/7",
    ]


def test_build_token_gauge_picks_hottest_account_and_estimates_eta(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    store = _FakeStore(
        token_rows=[
            TokenUsageHourlyRecord(
                hour_bucket="2026-04-21T13:00:00+00:00",
                account_name="claude_main",
                provider="claude",
                model_name="Opus",
                project_key="demo",
                tokens_used=180_000,
                updated_at="2026-04-21T13:05:00+00:00",
            ),
            TokenUsageHourlyRecord(
                hour_bucket="2026-04-21T14:00:00+00:00",
                account_name="claude_main",
                provider="claude",
                model_name="Opus",
                project_key="demo",
                tokens_used=240_000,
                updated_at="2026-04-21T14:05:00+00:00",
            ),
            TokenUsageHourlyRecord(
                hour_bucket="2026-04-21T14:00:00+00:00",
                account_name="codex_main",
                provider="codex",
                model_name="gpt-5.4",
                project_key="docs",
                tokens_used=50_000,
                updated_at="2026-04-21T14:05:00+00:00",
            ),
        ],
        account_usage={
            "claude_main": AccountUsageRecord(
                account_name="claude_main",
                provider="claude",
                plan="max",
                health="healthy",
                usage_summary="16% left this week",
                raw_text="",
                updated_at="2026-04-21T14:10:00+00:00",
                used_pct=84,
                remaining_pct=16,
                reset_at="Apr 24 01:00",
                period_label="current week",
            ),
            "codex_main": AccountUsageRecord(
                account_name="codex_main",
                provider="codex",
                plan="default",
                health="healthy",
                usage_summary="70% left",
                raw_text="",
                updated_at="2026-04-21T14:10:00+00:00",
                used_pct=30,
                remaining_pct=70,
                reset_at="",
                period_label="current period",
            ),
        },
    )
    supervisor = SimpleNamespace(store=store)

    gauge = _build_token_gauge(supervisor, config, now=NOW)

    assert gauge is not None
    assert gauge.account_name == "claude_main"
    rendered = _render_token_gauge(gauge)
    assert "84%)" in rendered
    assert "left @" in rendered


def test_briefing_banner_autofires_when_no_recent_briefing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[briefing]\nenabled = true\n", encoding="utf-8")

    def fake_fire_briefing(*, base_dir: Path, now_local, **_kwargs):
        emit_briefing(
            base_dir,
            BriefingDraft(
                date_local="2026-04-21",
                mode="fallback",
                markdown="Daily summary",
            ),
            now_utc=now_local,
        )
        return {"fired": True}

    monkeypatch.setattr(
        "pollypm.plugins_builtin.morning_briefing.handlers.briefing_tick.fire_briefing",
        fake_fire_briefing,
    )

    banner = _briefing_banner(config, config_path=config_path, now=NOW)

    assert banner is not None
    assert "Morning briefing available" in banner.text
    assert (config.project.base_dir / "briefings" / "2026-04-21.json").exists()


def test_build_dashboard_renders_new_header_and_suggestions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return NOW
            return NOW.astimezone(tz)

    config = _make_config(tmp_path)
    for project in config.projects.values():
        project.path.mkdir(parents=True, exist_ok=True)

    review = _FakeTask(
        task_number=5,
        title="Review dashboard polish",
        work_status="review",
        updated_at=NOW - timedelta(minutes=15),
        assignee="bob",
    )
    queued = _FakeTask(
        task_number=7,
        title="Claim next task",
        work_status="queued",
        updated_at=NOW - timedelta(minutes=5),
        assignee="bob",
        priority="high",
    )
    done_1 = _FakeTask(
        task_number=1,
        title="Ship one",
        work_status="done",
        updated_at=NOW - timedelta(hours=1),
        assignee="bob",
        transitions=[_FakeTransition("done", NOW - timedelta(hours=1), actor="russell")],
    )
    done_2 = _FakeTask(
        task_number=2,
        title="Ship two",
        work_status="done",
        updated_at=NOW - timedelta(hours=3),
        assignee="bob",
        transitions=[_FakeTransition("done", NOW - timedelta(hours=3), actor="russell")],
    )
    partitions = {
        "in_progress": [],
        "review": [review],
        "queued": [queued],
        "blocked": [],
        "done": [done_1, done_2],
    }
    counts = {
        "review": 1,
        "queued": 1,
        "done": 2,
    }

    monkeypatch.setattr(
        "pollypm.cockpit_sections.dashboard._dashboard_project_tasks",
        lambda project_key, project_path: (partitions, counts) if project_key == "demo" else ({}, {}),
    )
    monkeypatch.setattr("pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 0)
    monkeypatch.setattr("pollypm.cockpit_sections.dashboard.datetime", _FrozenDateTime)

    emit_briefing(
        Path(config.project.base_dir),
        BriefingDraft(
            date_local="2026-04-21",
            mode="fallback",
            markdown="Daily summary",
        ),
        now_utc=NOW,
    )

    store = _FakeStore(
        token_rows=[
            TokenUsageHourlyRecord(
                hour_bucket="2026-04-21T14:00:00+00:00",
                account_name="claude_main",
                provider="claude",
                model_name="Opus",
                project_key="demo",
                tokens_used=200_000,
                updated_at="2026-04-21T14:05:00+00:00",
            )
        ],
        account_usage={
            "claude_main": AccountUsageRecord(
                account_name="claude_main",
                provider="claude",
                plan="max",
                health="healthy",
                usage_summary="16% left this week",
                raw_text="",
                updated_at="2026-04-21T14:10:00+00:00",
                used_pct=84,
                remaining_pct=16,
                reset_at="Apr 24 01:00",
                period_label="current week",
            )
        },
    )
    supervisor = SimpleNamespace(store=store)

    output = _build_dashboard(
        supervisor,
        config,
        config_path=tmp_path / "pollypm.toml",
    )

    assert "Token burn:" in output
    assert "Hottest workers today: bob (2)" in output
    assert "Morning briefing available" in output
    assert "What's next?" in output
    assert "Approve demo/5" in output


def test_cockpit_dispatch_passes_config_path_to_dashboard_builder(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    supervisor = SimpleNamespace(config=config)
    config_path = tmp_path / "pollypm.toml"
    seen: dict[str, Path | None] = {}

    def fake_build_dashboard(supervisor_arg, config_arg, config_path=None):
        seen["config_path"] = config_path
        assert supervisor_arg is supervisor
        assert config_arg is config
        return "ok"

    monkeypatch.setattr("pollypm.cockpit._build_dashboard", fake_build_dashboard)

    result = _build_cockpit_detail_dispatch(supervisor, config_path, "dashboard")

    assert result == "ok"
    assert seen["config_path"] == config_path
