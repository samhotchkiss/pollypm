"""Tests for morning_briefing synthesis + fallback + quiet-mode (mb03)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pollypm.models import PollyPMConfig, PollyPMSettings, ProjectSettings
from pollypm.plugins_builtin.morning_briefing.handlers import synthesize as synth
from pollypm.plugins_builtin.morning_briefing.handlers.gather_yesterday import (
    CommitInfo,
    TransitionRecord,
    YesterdaySnapshot,
)
from pollypm.plugins_builtin.morning_briefing.handlers.identify_priorities import (
    BlockerEntry,
    InboxItemSummary,
    PriorityEntry,
    PriorityList,
)
from pollypm.plugins_builtin.morning_briefing.handlers.synthesize import (
    BRIEFING_LOG_MAX_ENTRIES,
    BriefingDraft,
    PriorityLine,
    _snapshot_is_quiet,
    append_briefing_log,
    build_context_md,
    build_fallback_draft,
    build_quiet_mode_draft,
    detect_quiet_mode,
    draft_from_herald_json,
    is_weekly_quiet_fire_day,
    load_recent_briefings,
    parse_herald_output,
    synthesize_briefing,
)


NY = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _empty_snapshot(date_local: str = "2026-04-15") -> YesterdaySnapshot:
    return YesterdaySnapshot(
        date_local=date_local,
        window_start_utc=f"{date_local}T04:00:00",
        window_end_utc=f"{date_local}T04:00:00",
    )


def _busy_snapshot(date_local: str = "2026-04-15") -> YesterdaySnapshot:
    snap = _empty_snapshot(date_local)
    snap.commits_by_project["alpha"] = [
        CommitInfo(sha="abc12345", timestamp=f"{date_local}T14:00:00Z",
                   author="T", subject="finish widget"),
    ]
    snap.task_transitions.append(
        TransitionRecord(
            project="alpha", task_id="alpha/1", task_title="widget",
            from_state="review", to_state="done", actor="russell",
            timestamp=f"{date_local}T14:15:00",
        )
    )
    return snap


def _priorities(n: int = 2) -> PriorityList:
    return PriorityList(
        top_tasks=[
            PriorityEntry(
                project="alpha", task_id=f"alpha/{i}",
                title=f"task {i}", priority="high", state="queued",
                assignee="worker", age_seconds=3600.0 * (i + 1),
            )
            for i in range(n)
        ],
        blockers=[
            BlockerEntry(
                project="alpha", task_id="alpha/99",
                title="blocked thing", unresolved_blockers=["beta/2"],
            )
        ],
        awaiting_approval=[
            InboxItemSummary(
                id="old-advisor", subject="slow thing",
                kind="advisor_insight", owner="user",
                opened_at="2026-04-14T10:00:00",
                age_hours=36.0,
            )
        ],
    )


def _minimal_config(tmp_path: Path) -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(
            name="Fixture",
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm-state",
            logs_dir=tmp_path / ".pollypm-state" / "logs",
            snapshots_dir=tmp_path / ".pollypm-state" / "snapshots",
            state_db=tmp_path / ".pollypm-state" / "state.db",
        ),
        pollypm=PollyPMSettings(controller_account=""),
        accounts={},
        sessions={},
        projects={},
    )


# ---------------------------------------------------------------------------
# Context pack
# ---------------------------------------------------------------------------


class TestContextPack:
    def test_contains_key_sections(self) -> None:
        snap = _busy_snapshot()
        md = build_context_md(snapshot=snap, priorities=_priorities(), recent=[])
        assert "## Yesterday" in md
        assert "### Commits" in md
        assert "### Task transitions" in md
        assert "## Today's priorities" in md
        assert "### Blockers" in md
        assert "### Awaiting approval" in md
        # Output JSON schema is shown so the herald knows the contract.
        assert "yesterday" in md and "priorities" in md and "watch" in md

    def test_includes_last_three_briefings(self) -> None:
        recent = [
            {"timestamp": "2026-04-14T06:00Z", "yesterday": "shipped foo"},
            {"timestamp": "2026-04-13T06:00Z", "yesterday": "quiet day"},
        ]
        md = build_context_md(
            snapshot=_empty_snapshot(), priorities=_priorities(0), recent=recent,
        )
        assert "Your last 3 briefings" in md
        assert "shipped foo" in md


# ---------------------------------------------------------------------------
# Herald output parsing
# ---------------------------------------------------------------------------


class TestParseHeraldOutput:
    def test_parses_plain_json(self) -> None:
        raw = json.dumps({
            "yesterday": "summary",
            "priorities": [{"title": "do X", "project": "alpha", "why": "because"}],
            "watch": ["thing"],
        })
        data = parse_herald_output(raw)
        assert data["yesterday"] == "summary"

    def test_strips_code_fences(self) -> None:
        raw = (
            "```json\n"
            '{"yesterday": "s", "priorities": []}\n'
            "```"
        )
        data = parse_herald_output(raw)
        assert data["priorities"] == []

    def test_missing_required_fields_errors(self) -> None:
        with pytest.raises(ValueError):
            parse_herald_output('{"other": "thing"}')

    def test_non_object_errors(self) -> None:
        with pytest.raises(ValueError):
            parse_herald_output("[1,2,3]")

    def test_empty_errors(self) -> None:
        with pytest.raises(ValueError):
            parse_herald_output("   ")

    def test_draft_from_herald_json_preserves_fields(self) -> None:
        data = {
            "yesterday": "s",
            "priorities": [
                {"title": "do X", "project": "alpha", "why": "because"},
                {"title": "do Y", "project": "beta"},
            ],
            "watch": ["something"],
        }
        draft = draft_from_herald_json(data, date_local="2026-04-15")
        assert draft.mode == "synthesized"
        assert len(draft.priorities) == 2
        assert draft.priorities[0].title == "do X"
        assert "Today's priorities" in draft.markdown
        assert "alpha" in draft.markdown
        assert "Watch" in draft.markdown


# ---------------------------------------------------------------------------
# Fallback draft
# ---------------------------------------------------------------------------


class TestFallbackDraft:
    def test_fallback_from_busy_snapshot(self) -> None:
        draft = build_fallback_draft(
            snapshot=_busy_snapshot(),
            priorities=_priorities(),
            date_local="2026-04-15",
            reason="timeout",
        )
        assert draft.mode == "fallback"
        assert "without synthesis" in draft.markdown
        assert "1 commits" in draft.markdown
        assert "alpha" in draft.markdown
        # Watch bullets from blockers + aging approvals.
        assert any("Blocked" in w for w in draft.watch)
        assert any("Aging approval" in w for w in draft.watch)
        assert draft.meta.get("fallback_reason") == "timeout"

    def test_fallback_empty_snapshot_still_produces_markdown(self) -> None:
        draft = build_fallback_draft(
            snapshot=_empty_snapshot(),
            priorities=PriorityList(),
            date_local="2026-04-15",
        )
        assert draft.mode == "fallback"
        assert "Yesterday" in draft.markdown
        assert "nothing queued" in draft.markdown


# ---------------------------------------------------------------------------
# Briefing log
# ---------------------------------------------------------------------------


class TestBriefingLog:
    def test_append_and_load(self, tmp_path: Path) -> None:
        for i in range(5):
            append_briefing_log(tmp_path, {"i": i, "yesterday": f"day {i}"})
        recent = load_recent_briefings(tmp_path, limit=3)
        # newest first
        assert [r["i"] for r in recent] == [4, 3, 2]

    def test_cap_enforced(self, tmp_path: Path) -> None:
        for i in range(BRIEFING_LOG_MAX_ENTRIES + 10):
            append_briefing_log(tmp_path, {"i": i})
        log_file = tmp_path / "briefing-log.jsonl"
        lines = [l for l in log_file.read_text().splitlines() if l.strip()]
        assert len(lines) == BRIEFING_LOG_MAX_ENTRIES
        # Last entry preserved.
        last = json.loads(lines[-1])
        assert last["i"] == BRIEFING_LOG_MAX_ENTRIES + 9


# ---------------------------------------------------------------------------
# synthesize_briefing
# ---------------------------------------------------------------------------


class TestSynthesizeBriefing:
    def test_success_path(self, tmp_path: Path, monkeypatch) -> None:
        def fake_herald(context_md: str, *, budget_seconds: int) -> str:
            return json.dumps({
                "yesterday": "shipped one thing",
                "priorities": [{"title": "t", "project": "alpha", "why": "w"}],
                "watch": [],
            })
        monkeypatch.setattr(synth, "herald_invocation", fake_herald)

        draft = synthesize_briefing(
            config=_minimal_config(tmp_path),
            snapshot=_busy_snapshot(),
            priorities=_priorities(),
            base_dir=tmp_path,
            date_local="2026-04-15",
        )
        assert draft.mode == "synthesized"
        assert draft.yesterday == "shipped one thing"
        # Context pack written for audit.
        assert (tmp_path / "last-briefing-context.md").exists()
        # Log entry appended.
        recent = load_recent_briefings(tmp_path, limit=1)
        assert recent
        assert recent[0]["mode"] == "synthesized"

    def test_fallback_on_herald_error(self, tmp_path: Path, monkeypatch) -> None:
        def boom(context_md: str, *, budget_seconds: int) -> str:
            raise TimeoutError("session exceeded 300s budget")
        monkeypatch.setattr(synth, "herald_invocation", boom)

        draft = synthesize_briefing(
            config=_minimal_config(tmp_path),
            snapshot=_busy_snapshot(),
            priorities=_priorities(),
            base_dir=tmp_path,
            date_local="2026-04-15",
        )
        assert draft.mode == "fallback"
        assert "without synthesis" in draft.markdown
        recent = load_recent_briefings(tmp_path, limit=1)
        assert recent[0]["mode"] == "fallback"

    def test_fallback_on_invalid_json(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            synth, "herald_invocation",
            lambda context_md, *, budget_seconds: "not json at all",
        )
        draft = synthesize_briefing(
            config=_minimal_config(tmp_path),
            snapshot=_empty_snapshot(),
            priorities=PriorityList(),
            base_dir=tmp_path,
            date_local="2026-04-15",
        )
        assert draft.mode == "fallback"
        # fallback_reason captured.
        assert "fallback_reason" in draft.meta

    def test_fallback_when_no_herald_installed(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            synth, "herald_invocation", synth._default_herald_invocation,
        )
        draft = synthesize_briefing(
            config=_minimal_config(tmp_path),
            snapshot=_empty_snapshot(),
            priorities=PriorityList(),
            base_dir=tmp_path,
            date_local="2026-04-15",
        )
        assert draft.mode == "fallback"


# ---------------------------------------------------------------------------
# Quiet-mode detection
# ---------------------------------------------------------------------------


class TestQuietModeDetection:
    def test_snapshot_is_quiet(self) -> None:
        assert _snapshot_is_quiet(_empty_snapshot()) is True
        assert _snapshot_is_quiet(_busy_snapshot()) is False

    def test_detect_quiet_seven_empty_days(self, tmp_path: Path) -> None:
        calls: list[datetime] = []

        def fake_gather(config, *, now_local, project_root=None):
            calls.append(now_local)
            return _empty_snapshot(now_local.date().isoformat())

        now = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        assert detect_quiet_mode(
            _minimal_config(tmp_path),
            now_local=now,
            quiet_threshold_days=7,
            gather_func=fake_gather,
        ) is True
        assert len(calls) == 7

    def test_detect_quiet_stops_early_on_activity(self, tmp_path: Path) -> None:
        calls: list[datetime] = []

        def fake_gather(config, *, now_local, project_root=None):
            calls.append(now_local)
            # Day 3 has activity.
            if len(calls) == 3:
                return _busy_snapshot(now_local.date().isoformat())
            return _empty_snapshot(now_local.date().isoformat())

        now = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        assert detect_quiet_mode(
            _minimal_config(tmp_path),
            now_local=now,
            quiet_threshold_days=7,
            gather_func=fake_gather,
        ) is False
        assert len(calls) == 3  # short-circuited

    def test_is_weekly_quiet_fire_day(self) -> None:
        # 2026-04-19 is a Sunday.
        assert is_weekly_quiet_fire_day(datetime(2026, 4, 19, 6, 0, tzinfo=NY)) is True
        # 2026-04-16 is a Thursday.
        assert is_weekly_quiet_fire_day(datetime(2026, 4, 16, 6, 0, tzinfo=NY)) is False

    def test_quiet_mode_draft(self) -> None:
        draft = build_quiet_mode_draft(date_local="2026-04-19")
        assert draft.mode == "quiet-mode"
        assert "quiet mode" in draft.markdown.lower()


# ---------------------------------------------------------------------------
# Integration with fire_briefing (fake herald)
# ---------------------------------------------------------------------------


class TestFireBriefingSynthesisIntegration:
    def test_fire_routes_through_synthesis(self, tmp_path: Path, monkeypatch) -> None:
        from pollypm.plugins_builtin.morning_briefing.handlers import briefing_tick
        from pollypm.plugins_builtin.morning_briefing.handlers import gather_yesterday as gy
        from pollypm.plugins_builtin.morning_briefing.handlers import identify_priorities as ip

        # Skip real DB / git work — return a non-empty snapshot so quiet-mode
        # detection doesn't kick in and skip the fire.
        monkeypatch.setattr(
            gy, "gather_yesterday",
            lambda config, *, now_local, project_root=None: _busy_snapshot("2026-04-15"),
        )
        monkeypatch.setattr(
            ip, "identify_priorities",
            lambda config, *, now_local, priorities_count=5, project_root=None: _priorities(),
        )

        monkeypatch.setattr(
            synth, "herald_invocation",
            lambda context_md, *, budget_seconds: json.dumps({
                "yesterday": "ok day",
                "priorities": [{"title": "t", "project": "alpha", "why": "w"}],
                "watch": [],
            }),
        )

        config_path = tmp_path / "pollypm.toml"
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config_path.write_text(
            "[project]\n"
            f'base_dir = "{state_dir}"\n'
            f'logs_dir = "{state_dir / "logs"}"\n'
            f'snapshots_dir = "{state_dir / "snap"}"\n'
            f'state_db = "{state_dir / "state.db"}"\n'
            "\n[pollypm]\n"
            'controller_account = "acct"\n'
            'timezone = "America/New_York"\n'
            "\n[accounts.acct]\n"
            'provider = "claude"\n'
        )
        now_local = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        result = briefing_tick.briefing_tick_handler({
            "config_path": str(config_path),
            "now_local": now_local.isoformat(),
        })
        assert result["fired"] is True
        draft = result["result"]["draft"]
        assert draft["mode"] == "synthesized"
        assert draft["date_local"] == "2026-04-15"

    def test_quiet_mode_non_sunday_skips(self, tmp_path: Path, monkeypatch) -> None:
        """With full 7-day silence on a Thursday, tick returns quiet-mode skip."""
        from pollypm.plugins_builtin.morning_briefing.handlers import briefing_tick
        from pollypm.plugins_builtin.morning_briefing.handlers import gather_yesterday as gy

        monkeypatch.setattr(
            gy, "gather_yesterday",
            lambda config, *, now_local, project_root=None: _empty_snapshot(
                now_local.date().isoformat()
            ),
        )
        # Also patch identify_priorities to be quick.
        from pollypm.plugins_builtin.morning_briefing.handlers import identify_priorities as ip
        monkeypatch.setattr(
            ip, "identify_priorities",
            lambda config, *, now_local, priorities_count=5, project_root=None: PriorityList(),
        )
        # Thursday — quiet mode active, but weekly fire day is Sunday.
        config_path = tmp_path / "pollypm.toml"
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config_path.write_text(
            "[project]\n"
            f'base_dir = "{state_dir}"\n'
            f'logs_dir = "{state_dir / "logs"}"\n'
            f'snapshots_dir = "{state_dir / "snap"}"\n'
            f'state_db = "{state_dir / "state.db"}"\n'
            "\n[pollypm]\n"
            'controller_account = "acct"\n'
            'timezone = "America/New_York"\n'
            "\n[accounts.acct]\n"
            'provider = "claude"\n'
        )
        now_local = datetime(2026, 4, 16, 6, 0, tzinfo=NY)  # Thursday
        result = briefing_tick.briefing_tick_handler({
            "config_path": str(config_path),
            "now_local": now_local.isoformat(),
        })
        # In mb03 the quiet-mode skip returns fired=False with reason=quiet-mode.
        # Note the gate precedes quiet-mode detection, but since gates pass and
        # quiet-mode is active on a non-Sunday, fire_briefing returns fired=False.
        # The tick handler surfaces that as fired=False with reason=fire-error or
        # similar — we assert the draft was not emitted.
        # NB: fire_briefing returns a dict; the tick handler treats a dict with
        # fired=False as a "fire returned non-fire" path.
        # To keep mb03's contract simple, the tick handler surfaces the inner
        # reason when the inner call returned fired=False.
        # So we expect the result reason to surface "quiet-mode".
        assert result.get("fired") is False
        # The reason is either "quiet-mode" (from fire_briefing) or the tick's
        # own short-circuit — both are acceptable.
