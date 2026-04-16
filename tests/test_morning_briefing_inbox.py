"""Tests for the morning_briefing inbox surface (mb04)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.heartbeat.roster import EverySchedule, Roster
from pollypm.jobs import JobHandlerRegistry
from pollypm.plugin_api.v1 import JobHandlerAPI, RosterAPI
from pollypm.plugins_builtin.morning_briefing import plugin as plugin_module
from pollypm.plugins_builtin.morning_briefing.handlers.synthesize import (
    BriefingDraft,
    PriorityLine,
)
from pollypm.plugins_builtin.morning_briefing.inbox import (
    BRIEFING_KIND,
    BriefingEntry,
    DEFAULT_AUTO_CLOSE_HOURS,
    auto_close_expired,
    briefing_sweep_handler,
    briefings_dir,
    emit_briefing,
    list_briefings,
    pin_briefing,
    read_briefing,
    unpin_briefing,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _draft(
    *, date_local: str = "2026-04-15", mode: str = "synthesized",
) -> BriefingDraft:
    return BriefingDraft(
        date_local=date_local,
        mode=mode,
        yesterday="Did some things.",
        priorities=[
            PriorityLine(title="ship widget", project="alpha", why="unblocks demo"),
            PriorityLine(title="review PR", project="beta"),
        ],
        watch=["aging approval on alpha/99"],
        markdown=(
            "## Yesterday\nDid some things.\n\n"
            "## Today's priorities\n- **alpha**: ship widget — unblocks demo\n"
            "- **beta**: review PR\n\n## Watch\n- aging approval on alpha/99"
        ),
    )


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------


class TestEmitBriefing:
    def test_creates_files(self, tmp_path: Path) -> None:
        entry = emit_briefing(tmp_path, _draft())
        assert entry.kind == BRIEFING_KIND
        assert entry.date_local == "2026-04-15"
        assert entry.status == "open"
        assert entry.pinned is False
        md_path = briefings_dir(tmp_path) / "2026-04-15.md"
        json_path = briefings_dir(tmp_path) / "2026-04-15.json"
        assert md_path.exists()
        assert json_path.exists()
        body = md_path.read_text()
        assert "Morning Briefing — 2026-04-15" in body
        assert "ship widget" in body

    def test_idempotent_preserves_pin_and_created_at(self, tmp_path: Path) -> None:
        first = emit_briefing(tmp_path, _draft())
        pin_briefing(tmp_path, "2026-04-15")
        # Re-emit (e.g. a force-fire from CLI).
        second = emit_briefing(tmp_path, _draft())
        assert second.pinned is True, "re-emit must preserve user pin"
        assert second.created_at == first.created_at, "re-emit must preserve creation time"

    def test_fallback_mode_labels_the_body(self, tmp_path: Path) -> None:
        entry = emit_briefing(tmp_path, _draft(mode="fallback"))
        md = (briefings_dir(tmp_path) / "2026-04-15.md").read_text()
        assert "Mode: fallback" in md


# ---------------------------------------------------------------------------
# List / read
# ---------------------------------------------------------------------------


class TestListAndRead:
    def test_lists_newest_first(self, tmp_path: Path) -> None:
        emit_briefing(tmp_path, _draft(date_local="2026-04-13"))
        emit_briefing(tmp_path, _draft(date_local="2026-04-15"))
        emit_briefing(tmp_path, _draft(date_local="2026-04-14"))
        entries = list_briefings(tmp_path)
        assert [e.date_local for e in entries] == [
            "2026-04-15", "2026-04-14", "2026-04-13",
        ]

    def test_status_filter(self, tmp_path: Path) -> None:
        emit_briefing(tmp_path, _draft(date_local="2026-04-15"))
        # Manually close one.
        json_path = briefings_dir(tmp_path) / "2026-04-15.json"
        data = json.loads(json_path.read_text())
        data["status"] = "closed"
        json_path.write_text(json.dumps(data))

        assert list_briefings(tmp_path, status="open") == []
        assert len(list_briefings(tmp_path, status="closed")) == 1
        assert len(list_briefings(tmp_path, status="all")) == 1

    def test_read_returns_entry_and_body(self, tmp_path: Path) -> None:
        emit_briefing(tmp_path, _draft())
        result = read_briefing(tmp_path, "2026-04-15")
        assert result is not None
        entry, body = result
        assert entry.date_local == "2026-04-15"
        assert "ship widget" in body

    def test_read_missing(self, tmp_path: Path) -> None:
        assert read_briefing(tmp_path, "2099-01-01") is None


# ---------------------------------------------------------------------------
# Pin / unpin
# ---------------------------------------------------------------------------


class TestPinning:
    def test_pin_and_unpin(self, tmp_path: Path) -> None:
        emit_briefing(tmp_path, _draft())
        pinned = pin_briefing(tmp_path, "2026-04-15")
        assert pinned.pinned is True
        unpinned = unpin_briefing(tmp_path, "2026-04-15")
        assert unpinned.pinned is False

    def test_pin_reopens_closed_briefing(self, tmp_path: Path) -> None:
        emit_briefing(tmp_path, _draft())
        # Manually close it.
        json_path = briefings_dir(tmp_path) / "2026-04-15.json"
        data = json.loads(json_path.read_text())
        data["status"] = "closed"
        data["closed_at"] = datetime.now(UTC).isoformat()
        json_path.write_text(json.dumps(data))
        entry = pin_briefing(tmp_path, "2026-04-15")
        assert entry.status == "open"
        assert entry.pinned is True

    def test_pin_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            pin_briefing(tmp_path, "2099-01-01")


# ---------------------------------------------------------------------------
# Auto-close sweep
# ---------------------------------------------------------------------------


class TestAutoClose:
    def test_closes_briefings_older_than_24h(self, tmp_path: Path) -> None:
        now = datetime(2026, 4, 17, 6, 0, tzinfo=UTC)
        emit_briefing(
            tmp_path, _draft(date_local="2026-04-15"),
            now_utc=now - timedelta(hours=48),
        )
        emit_briefing(
            tmp_path, _draft(date_local="2026-04-16"),
            now_utc=now - timedelta(hours=12),
        )
        closed = auto_close_expired(tmp_path, now_utc=now)
        assert [e.date_local for e in closed] == ["2026-04-15"]

        remaining_open = {e.date_local for e in list_briefings(tmp_path, status="open")}
        assert "2026-04-15" not in remaining_open
        assert "2026-04-16" in remaining_open

    def test_pinned_is_not_closed(self, tmp_path: Path) -> None:
        now = datetime(2026, 4, 17, 6, 0, tzinfo=UTC)
        emit_briefing(
            tmp_path, _draft(date_local="2026-04-15"),
            now_utc=now - timedelta(hours=48),
        )
        pin_briefing(tmp_path, "2026-04-15")
        closed = auto_close_expired(tmp_path, now_utc=now)
        assert closed == []

    def test_sweep_handler_via_config(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config_path = tmp_path / "pollypm.toml"
        config_path.write_text(
            "[project]\n"
            f'base_dir = "{state_dir}"\n'
            f'logs_dir = "{state_dir / "logs"}"\n'
            f'snapshots_dir = "{state_dir / "snap"}"\n'
            f'state_db = "{state_dir / "state.db"}"\n'
            "\n[pollypm]\n"
            'controller_account = "acct"\n'
            "\n[accounts.acct]\n"
            'provider = "claude"\n'
        )
        now = datetime(2026, 4, 17, 6, 0, tzinfo=UTC)
        emit_briefing(
            state_dir, _draft(date_local="2026-04-15"),
            now_utc=now - timedelta(hours=48),
        )
        result = briefing_sweep_handler({
            "config_path": str(config_path),
            "now_utc": now.isoformat(),
        })
        assert result["closed"] == 1
        assert result["dates_closed"] == ["2026-04-15"]


# ---------------------------------------------------------------------------
# Plugin registration (sweep handler + roster entry)
# ---------------------------------------------------------------------------


class TestSweepRegistration:
    def test_sweep_handler_registered(self) -> None:
        registry = JobHandlerRegistry()
        api = JobHandlerAPI(registry, plugin_name="morning_briefing")
        plugin_module.plugin.register_handlers(api)
        assert "briefing.sweep" in registry.names()

    def test_sweep_roster_every_6h(self) -> None:
        roster = Roster()
        api = RosterAPI(roster, plugin_name="morning_briefing")
        plugin_module.plugin.register_roster(api)
        entries = {e.handler_name: e for e in roster.entries}
        assert "briefing.sweep" in entries
        schedule = entries["briefing.sweep"].schedule
        assert isinstance(schedule, EverySchedule)
        assert int(schedule.interval.total_seconds()) == 6 * 3600

    def test_declared_capabilities_include_sweep(self) -> None:
        caps = {(c.kind, c.name) for c in plugin_module.plugin.capabilities}
        assert ("job_handler", "briefing.sweep") in caps
        assert ("roster_entry", "briefing.sweep") in caps


# ---------------------------------------------------------------------------
# fire_briefing emits to inbox
# ---------------------------------------------------------------------------


class TestFireBriefingEmitsInbox:
    def test_successful_fire_writes_inbox_entry(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from pollypm.plugins_builtin.morning_briefing.handlers import (
            briefing_tick,
            gather_yesterday as gy,
            identify_priorities as ip,
            synthesize as synth,
        )
        from pollypm.plugins_builtin.morning_briefing.handlers.gather_yesterday import (
            YesterdaySnapshot,
            CommitInfo,
            TransitionRecord,
        )
        from pollypm.plugins_builtin.morning_briefing.handlers.identify_priorities import (
            PriorityList,
        )

        def busy(date_local: str) -> YesterdaySnapshot:
            s = YesterdaySnapshot(
                date_local=date_local,
                window_start_utc="x",
                window_end_utc="y",
            )
            s.commits_by_project["alpha"] = [
                CommitInfo(sha="abc", timestamp="t", author="A", subject="s"),
            ]
            return s

        monkeypatch.setattr(
            gy, "gather_yesterday",
            lambda config, *, now_local, project_root=None: busy("2026-04-15"),
        )
        monkeypatch.setattr(
            ip, "identify_priorities",
            lambda config, *, now_local, priorities_count=5, project_root=None: PriorityList(),
        )
        monkeypatch.setattr(
            synth, "herald_invocation",
            lambda context_md, *, budget_seconds: json.dumps({
                "yesterday": "shipped widget",
                "priorities": [{"title": "t", "project": "alpha", "why": "w"}],
                "watch": [],
            }),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config_path = tmp_path / "pollypm.toml"
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
        from zoneinfo import ZoneInfo
        NY = ZoneInfo("America/New_York")
        now_local = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        result = briefing_tick.briefing_tick_handler({
            "config_path": str(config_path),
            "now_local": now_local.isoformat(),
        })
        assert result["fired"] is True
        assert result["result"]["emitted"] is True
        assert result["result"]["inbox_date"] == "2026-04-15"

        # The briefing is now in the inbox.
        entries = list_briefings(state_dir)
        assert len(entries) == 1
        assert entries[0].kind == BRIEFING_KIND
        assert entries[0].mode == "synthesized"
