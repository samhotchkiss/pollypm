from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from pollypm.cockpit import _build_cockpit_detail_dispatch
from pollypm.cockpit_sections.dashboard import (
    DashboardAccountUsage,
    DashboardBriefingBanner,
    _briefing_banner,
    _build_account_usage_rows,
    _build_dashboard,
    _build_token_gauge,
    _rank_dashboard_suggestions,
    _render_account_usage_lines,
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
            "claude_main": SimpleNamespace(
                provider="claude", email="claude@swh.me",
            ),
            "codex_main": SimpleNamespace(
                provider="codex", email="",
            ),
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


def test_build_account_usage_rows_one_row_per_account_with_severity(
    tmp_path: Path,
) -> None:
    """#1093: per-account quota rows with threshold severity."""
    config = _make_config(tmp_path)
    store = _FakeStore(
        account_usage={
            "claude_main": AccountUsageRecord(
                account_name="claude_main",
                provider="claude",
                plan="max",
                health="near-limit",
                usage_summary="21% left this week",
                raw_text="",
                updated_at="2026-04-21T14:10:00+00:00",
                used_pct=79,
                remaining_pct=21,
                reset_at="Apr 24 01:00",
                period_label="current week",
            ),
            "codex_main": AccountUsageRecord(
                account_name="codex_main",
                provider="codex",
                plan="default",
                health="healthy",
                usage_summary="",
                raw_text="",
                updated_at="2026-04-21T14:10:00+00:00",
                used_pct=None,
                remaining_pct=None,
                reset_at=None,
                period_label=None,
            ),
        },
    )
    supervisor = SimpleNamespace(store=store)

    rows = _build_account_usage_rows(supervisor, config, now=NOW)

    assert [row.account_name for row in rows] == ["claude_main", "codex_main"]
    claude_row = rows[0]
    assert claude_row.provider == "Anthropic"
    assert claude_row.email == "claude@swh.me"
    assert claude_row.used_pct == 79
    assert claude_row.severity == "ok"  # 79 < 80
    codex_row = rows[1]
    assert codex_row.provider == "OpenAI"
    assert codex_row.used_pct is None
    assert codex_row.severity == "unknown"


def test_build_account_usage_rows_dedupes_by_email(tmp_path: Path) -> None:
    """Two PollyPM accounts pointing at the same Anthropic identity
    collapse to a single row — quota is shared at the account level
    (Sam's #1093 scope refinement).
    """
    config = _make_config(tmp_path)
    config.accounts = {
        "polly_claude": SimpleNamespace(
            provider="claude", email="claude@swh.me",
        ),
        "reviewer_claude": SimpleNamespace(
            provider="claude", email="claude@swh.me",
        ),
    }
    store = _FakeStore(
        account_usage={
            "polly_claude": AccountUsageRecord(
                account_name="polly_claude",
                provider="claude",
                plan="max",
                health="near-limit",
                usage_summary="",
                raw_text="",
                updated_at="2026-04-21T14:10:00+00:00",
                used_pct=82,
                remaining_pct=18,
                reset_at="Apr 24 01:00",
                period_label="current week",
            ),
        },
    )
    supervisor = SimpleNamespace(store=store)

    rows = _build_account_usage_rows(supervisor, config, now=NOW)

    assert len(rows) == 1
    assert rows[0].account_name == "polly_claude"
    assert rows[0].used_pct == 82
    assert rows[0].severity == "warning"  # 80 <= 82 < 95


def test_render_account_usage_lines_uses_threshold_markers() -> None:
    """◆ at 80%+, ▲ at 95%+, · otherwise (#1093)."""
    rows = [
        DashboardAccountUsage(
            account_name="claude_main",
            provider="Anthropic",
            email="claude@swh.me",
            used_pct=97,
            summary="97% of weekly limit",
            severity="critical",
            reset_at="Apr 24 01:00",
        ),
        DashboardAccountUsage(
            account_name="claude_alt",
            provider="Anthropic",
            email="alt@swh.me",
            used_pct=82,
            summary="82% of weekly limit",
            severity="warning",
            reset_at="Apr 24 01:00",
        ),
        DashboardAccountUsage(
            account_name="claude_low",
            provider="Anthropic",
            email="low@swh.me",
            used_pct=12,
            summary="12% of weekly limit",
            severity="ok",
        ),
        DashboardAccountUsage(
            account_name="codex_main",
            provider="OpenAI",
            email="",
            used_pct=None,
            summary="no signal yet",
            severity="unknown",
        ),
    ]

    rendered = "\n".join(_render_account_usage_lines(rows))

    assert "Accounts" in rendered
    assert "▲ Anthropic (claude@swh.me)" in rendered
    assert "97% of weekly limit" in rendered
    assert "over weekly limit" in rendered
    assert "◆ Anthropic (alt@swh.me)" in rendered
    assert "approaching ceiling" in rendered
    assert "· Anthropic (low@swh.me)" in rendered
    assert "? OpenAI" in rendered  # unknown signal marker
    assert "no signal yet" in rendered


def test_render_account_usage_lines_empty_when_no_accounts() -> None:
    assert _render_account_usage_lines([]) == []


def test_briefing_banner_does_not_fire_briefing_during_render(
    tmp_path: Path,
) -> None:
    """#801: dashboard render is read-only with respect to morning_briefing.

    The previous implementation imported plugin handlers and called
    ``fire_briefing()`` from inside ``_briefing_banner`` if no recent
    briefing existed — that turned the dashboard into a hidden
    scheduling path and swallowed plugin failures. Briefings now fire
    only through the recurring-job/roster path; the banner just
    surfaces what the plugin has already published.
    """
    config = _make_config(tmp_path)
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[briefing]\nenabled = true\n", encoding="utf-8")

    # No briefing on disk → banner returns None instead of triggering
    # the plugin from the render path.
    banner = _briefing_banner(config, config_path=config_path, now=NOW)
    assert banner is None
    assert not (config.project.base_dir / "briefings").exists()


def test_briefing_banner_surfaces_existing_briefing(tmp_path: Path) -> None:
    """When the morning_briefing plugin has already published a briefing
    for today, the dashboard banner finds and surfaces it.
    """
    config = _make_config(tmp_path)
    base_dir = config.project.base_dir
    emit_briefing(
        base_dir,
        BriefingDraft(
            date_local="2026-04-21",
            mode="fallback",
            markdown="Daily summary",
        ),
        now_utc=NOW,
    )
    banner = _briefing_banner(config, config_path=tmp_path / "pollypm.toml", now=NOW)
    assert banner is not None
    assert "Morning briefing available" in banner.text


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

    # #1093: per-account quota surface — Accounts section is rendered
    # near the top and shows one row per LLM account regardless of
    # how many sessions share the account.
    assert "Accounts" in output
    assert "Anthropic (claude@swh.me)" in output
    # claude_main usage is 84% → ◆ warning marker + approaching-ceiling tag.
    assert "◆" in output
    assert "approaching ceiling" in output

    # Cycle 132: ``review`` count appears once on the attention bar
    # ("◉ N awaiting review") and must NOT also appear on the
    # flow-state count_parts line two lines below — same number,
    # two glyphs, was just visual duplication.
    assert "1 awaiting review" in output  # attention bar still shows it
    # The workspace-wide flow-state line previously rendered
    # "⟳ 1 review · ○ 1 queued · ✓ 2 done"; after the dedup it
    # renders queued + done only. (The per-project scorecards still
    # show "N review" — that's a different surface.)
    assert "⟳ 1 review" not in output
    # Find the flow-state line specifically (preceded by the briefing
    # and attention rows, followed by the Projects divider) and
    # verify it has no review glyph.
    flow_line = next(
        line for line in output.splitlines()
        if "queued" in line and "done" in line and "Projects" not in line
    )
    assert "review" not in flow_line, (
        f"flow-state line still mentions review: {flow_line!r}"
    )


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


def test_dashboard_activity_line_pluralises_singular_counts(
    monkeypatch, tmp_path: Path,
) -> None:
    """The Activity line must read ``1 commit · 1 message · 1 recovery``.

    The ``Today: …`` line under the Activity divider used bare-plural
    ``commits`` / ``messages`` / ``recoveries`` for every count. At
    a low-traffic state with one of each it printed
    ``Today: 1 commits · 1 messages · 1 recoveries`` — three copy
    bugs on one line. Mirrors cycles 57–63 across other surfaces.
    """
    from pollypm.storage.state import EventRecord

    monkeypatch.setattr(
        "pollypm.cockpit_sections.dashboard._dashboard_project_tasks",
        lambda project_key, project_path: ({}, {}),
    )
    monkeypatch.setattr("pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 0)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return NOW
            return NOW.astimezone(tz)
    monkeypatch.setattr("pollypm.cockpit_sections.dashboard.datetime", _FrozenDateTime)

    base_dir = tmp_path / ".pollypm"
    one_each = [
        EventRecord(
            session_name="worker_demo",
            event_type="commit",
            message="committed abc123",
            created_at=(NOW - timedelta(minutes=5)).isoformat(),
        ),
        EventRecord(
            session_name="worker_demo",
            event_type="send_input",
            message="poke",
            created_at=(NOW - timedelta(minutes=3)).isoformat(),
        ),
        EventRecord(
            session_name="worker_demo",
            event_type="recover",
            message="recovered after pane drift",
            created_at=(NOW - timedelta(minutes=2)).isoformat(),
        ),
    ]
    config = SimpleNamespace(
        project=SimpleNamespace(base_dir=base_dir, root_dir=tmp_path),
        pollypm=SimpleNamespace(timezone="UTC"),
        projects={"only": _FakeProject(tmp_path / "only", "Only")},
        accounts={},
    )
    supervisor = SimpleNamespace(store=_FakeStore(recent_events=one_each))
    out = _build_dashboard(supervisor, config)
    assert "Today: 1 commit · 1 message · 1 recovery" in out
    assert "1 commits" not in out
    assert "1 messages" not in out
    assert "1 recoveries" not in out


def test_dashboard_footer_pluralises_project_count(monkeypatch, tmp_path: Path) -> None:
    """The dashboard footer must read ``1 project`` / ``5 projects``.

    The bottom-of-dashboard line ``  N projects · j/k navigate · S
    settings`` printed bare-plural ``projects`` at every count. New
    installs with one tracked project saw ``1 projects`` — a copy
    bug at the earliest user touchpoint. Mirrors cycles 57/58 on
    other dashboard surfaces.
    """
    monkeypatch.setattr(
        "pollypm.cockpit_sections.dashboard._dashboard_project_tasks",
        lambda project_key, project_path: ({}, {}),
    )
    monkeypatch.setattr("pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 0)

    base_dir = tmp_path / ".pollypm"
    supervisor = SimpleNamespace(store=_FakeStore())

    one_proj = SimpleNamespace(
        project=SimpleNamespace(base_dir=base_dir, root_dir=tmp_path),
        pollypm=SimpleNamespace(timezone="UTC"),
        projects={"only": _FakeProject(tmp_path / "only", "Only")},
        accounts={},
    )
    out = _build_dashboard(supervisor, one_proj)
    assert "1 project  ·  j/k navigate" in out
    assert "1 projects" not in out

    many_proj = SimpleNamespace(
        project=SimpleNamespace(base_dir=base_dir, root_dir=tmp_path),
        pollypm=SimpleNamespace(timezone="UTC"),
        projects={
            "a": _FakeProject(tmp_path / "a", "A"),
            "b": _FakeProject(tmp_path / "b", "B"),
            "c": _FakeProject(tmp_path / "c", "C"),
        },
        accounts={},
    )
    out = _build_dashboard(supervisor, many_proj)
    assert "3 projects  ·  j/k navigate" in out
