"""Tests for ``pm briefing`` CLI (mb05).

Exercises each subcommand via ``typer.testing.CliRunner`` with the
``fire_briefing`` path monkeypatched so we don't touch git / DB.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from pollypm.plugins_builtin.morning_briefing import cli as briefing_cli
from pollypm.plugins_builtin.morning_briefing.cli import briefing_app
from pollypm.plugins_builtin.morning_briefing.handlers import briefing_tick
from pollypm.plugins_builtin.morning_briefing.handlers.synthesize import (
    BriefingDraft,
    PriorityLine,
)
from pollypm.plugins_builtin.morning_briefing.inbox import (
    briefings_dir,
    emit_briefing,
)
from pollypm.plugins_builtin.morning_briefing.state import (
    BriefingState,
    save_state,
)


runner = CliRunner()
NY = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Fixture — a minimal pollypm.toml on disk
# ---------------------------------------------------------------------------


def _write_config(
    tmp_path: Path,
    *,
    briefing_section: str = "",
    pollypm_tz: str = "America/New_York",
) -> Path:
    base_dir = tmp_path / "state"
    base_dir.mkdir(parents=True, exist_ok=True)
    tz_line = f'\ntimezone = "{pollypm_tz}"' if pollypm_tz else ""
    path = tmp_path / "pollypm.toml"
    path.write_text(
        "[project]\n"
        'name = "Fixture"\n'
        f'base_dir = "{base_dir}"\n'
        f'logs_dir = "{base_dir / "logs"}"\n'
        f'snapshots_dir = "{base_dir / "snapshots"}"\n'
        f'state_db = "{base_dir / "state.db"}"\n'
        "\n"
        "[pollypm]\n"
        'controller_account = "acct"'
        + tz_line
        + "\n\n"
        "[accounts.acct]\n"
        'provider = "claude"\n'
        + briefing_section
    )
    return path


def _stub_fire(monkeypatch, *, markdown: str = "body", mode: str = "synthesized"):
    """Replace ``fire_briefing`` with a deterministic stub.

    The stub records the ``emit_to_inbox`` flag + the now_local it saw so
    tests can assert behaviour without touching gather/synthesize.
    """
    captured: dict = {}

    def fake_fire(**kwargs):
        captured.update(kwargs)
        draft = BriefingDraft(
            date_local=kwargs["now_local"].date().isoformat(),
            mode=mode,
            yesterday="quiet day",
            priorities=[PriorityLine(title="Ship x", project="demo", why="unblocks y")],
            watch=[],
            markdown=markdown,
        )
        result = {
            "fired": True,
            "stub": False,
            "emitted": kwargs.get("emit_to_inbox", True),
            "inbox_date": draft.date_local if kwargs.get("emit_to_inbox", True) else None,
            "quiet_mode": False,
            "draft": {
                "date_local": draft.date_local,
                "mode": draft.mode,
                "yesterday": draft.yesterday,
                "priorities": [
                    {"title": p.title, "project": p.project, "why": p.why}
                    for p in draft.priorities
                ],
                "watch": list(draft.watch),
                "markdown": draft.markdown,
                "meta": dict(draft.meta),
            },
        }
        return result

    monkeypatch.setattr(briefing_tick, "fire_briefing", fake_fire)
    return captured


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_text(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        result = runner.invoke(
            briefing_app, ["status", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "enabled:" in result.stdout
        assert "hour (local):" in result.stdout
        assert "06:00" in result.stdout
        assert "next scheduled:" in result.stdout
        assert "last briefing date:" in result.stdout
        assert "(never)" in result.stdout

    def test_status_json(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        result = runner.invoke(
            briefing_app,
            ["status", "--config", str(config_path), "--json"],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["enabled"] is True
        assert payload["hour"] == 6
        assert payload["priorities_count"] == 5
        assert payload["last_briefing_date"] is None
        assert payload["mode"] == "daily"
        assert "next_scheduled_local" in payload

    def test_status_reflects_disabled_section(self, tmp_path: Path) -> None:
        config_path = _write_config(
            tmp_path,
            briefing_section="\n[briefing]\nenabled = false\nhour = 7\n",
        )
        result = runner.invoke(
            briefing_app,
            ["status", "--config", str(config_path), "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["enabled"] is False
        assert payload["hour"] == 7

    def test_status_reports_last_briefing(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        base_dir = tmp_path / "state"
        save_state(base_dir, BriefingState(last_briefing_date="2026-04-15"))
        result = runner.invoke(
            briefing_app,
            ["status", "--config", str(config_path), "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["last_briefing_date"] == "2026-04-15"


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


class TestEnableDisable:
    def test_disable_appends_section(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        result = runner.invoke(
            briefing_app, ["disable", "--config", str(config_path)],
        )
        assert result.exit_code == 0
        text = config_path.read_text()
        assert "[briefing]" in text
        assert "enabled = false" in text

    def test_disable_idempotent(self, tmp_path: Path) -> None:
        config_path = _write_config(
            tmp_path, briefing_section="\n[briefing]\nenabled = false\n",
        )
        result = runner.invoke(
            briefing_app,
            ["disable", "--config", str(config_path), "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["enabled"] is False
        assert payload["changed"] is False

    def test_enable_flips_existing_false(self, tmp_path: Path) -> None:
        config_path = _write_config(
            tmp_path, briefing_section="\n[briefing]\nenabled = false\nhour = 7\n",
        )
        result = runner.invoke(
            briefing_app,
            ["enable", "--config", str(config_path), "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["enabled"] is True
        assert payload["changed"] is True
        # Re-read + verify the 'hour' was preserved.
        from pollypm.plugins_builtin.morning_briefing.settings import (
            load_briefing_settings,
        )
        settings = load_briefing_settings(config_path)
        assert settings.enabled is True
        assert settings.hour == 7

    def test_disable_then_enable_roundtrip(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        disable = runner.invoke(
            briefing_app, ["disable", "--config", str(config_path)],
        )
        assert disable.exit_code == 0
        enable = runner.invoke(
            briefing_app, ["enable", "--config", str(config_path)],
        )
        assert enable.exit_code == 0

        from pollypm.plugins_builtin.morning_briefing.settings import (
            load_briefing_settings,
        )
        settings = load_briefing_settings(config_path)
        assert settings.enabled is True

    def test_disable_creates_section_when_missing_and_tick_respects_it(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: disabling stops the tick from firing."""
        config_path = _write_config(tmp_path)
        result = runner.invoke(
            briefing_app, ["disable", "--config", str(config_path)],
        )
        assert result.exit_code == 0

        from pollypm.plugins_builtin.morning_briefing.handlers.briefing_tick import (
            briefing_tick_handler,
        )
        now_local = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        tick_result = briefing_tick_handler({
            "config_path": str(config_path),
            "now_local": now_local.isoformat(),
        })
        assert tick_result["fired"] is False
        assert tick_result["reason"] == "disabled"


# ---------------------------------------------------------------------------
# now — force-fire
# ---------------------------------------------------------------------------


class TestNow:
    def test_now_fires_at_off_hour(self, tmp_path: Path, monkeypatch) -> None:
        """``pm briefing now`` must bypass the 6-a.m. gate."""
        config_path = _write_config(tmp_path)

        captured = _stub_fire(monkeypatch, markdown="## Today\n- Ship")
        # Run at 3 p.m. — off-hour by default.
        fixed_now = datetime(2026, 4, 16, 15, 0, tzinfo=NY)
        monkeypatch.setattr(
            briefing_cli._tick, "_local_now",
            lambda *a, **kw: fixed_now,
        )

        result = runner.invoke(
            briefing_app, ["now", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "briefing: fired" in result.stdout
        assert "inbox: yes" in result.stdout
        assert "## Today" in result.stdout
        # The stub recorded the arguments — confirm emit_to_inbox=True.
        assert captured["emit_to_inbox"] is True

    def test_now_bypasses_disabled_flag(self, tmp_path: Path, monkeypatch) -> None:
        """``pm briefing now`` ignores the ``enabled`` flag."""
        config_path = _write_config(
            tmp_path, briefing_section="\n[briefing]\nenabled = false\n",
        )
        _stub_fire(monkeypatch)
        fixed_now = datetime(2026, 4, 16, 15, 0, tzinfo=NY)
        monkeypatch.setattr(
            briefing_cli._tick, "_local_now",
            lambda *a, **kw: fixed_now,
        )
        result = runner.invoke(
            briefing_app, ["now", "--config", str(config_path)],
        )
        assert result.exit_code == 0
        assert "briefing: fired" in result.stdout

    def test_now_json(self, tmp_path: Path, monkeypatch) -> None:
        config_path = _write_config(tmp_path)
        _stub_fire(monkeypatch)
        fixed_now = datetime(2026, 4, 16, 15, 0, tzinfo=NY)
        monkeypatch.setattr(
            briefing_cli._tick, "_local_now",
            lambda *a, **kw: fixed_now,
        )

        result = runner.invoke(
            briefing_app,
            ["now", "--config", str(config_path), "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["fired"] is True
        assert payload["emitted"] is True
        assert payload["draft"]["date_local"] == "2026-04-16"


# ---------------------------------------------------------------------------
# preview — dry-run (no inbox)
# ---------------------------------------------------------------------------


class TestPreview:
    def test_preview_does_not_write_inbox(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        config_path = _write_config(tmp_path)
        captured = _stub_fire(monkeypatch, markdown="## Preview\n- x")

        fixed_now = datetime(2026, 4, 16, 15, 0, tzinfo=NY)
        monkeypatch.setattr(
            briefing_cli._tick, "_local_now",
            lambda *a, **kw: fixed_now,
        )

        result = runner.invoke(
            briefing_app, ["preview", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Morning Briefing Preview" in result.stdout
        assert "## Preview" in result.stdout
        # The stub recorded arguments — confirm emit_to_inbox=False.
        assert captured["emit_to_inbox"] is False

        # The briefings directory should either be missing or have no
        # entry for today — since we didn't emit to inbox.
        base_dir = tmp_path / "state"
        briefings = briefings_dir(base_dir)
        assert not briefings.exists() or not any(briefings.glob("2026-04-16.*"))

    def test_preview_json(self, tmp_path: Path, monkeypatch) -> None:
        config_path = _write_config(tmp_path)
        _stub_fire(monkeypatch)
        fixed_now = datetime(2026, 4, 16, 15, 0, tzinfo=NY)
        monkeypatch.setattr(
            briefing_cli._tick, "_local_now",
            lambda *a, **kw: fixed_now,
        )

        result = runner.invoke(
            briefing_app,
            ["preview", "--config", str(config_path), "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["fired"] is True
        # emitted reflects the stub — since emit_to_inbox=False was passed,
        # the stub reports emitted=False.
        assert payload["emitted"] is False


# ---------------------------------------------------------------------------
# pin
# ---------------------------------------------------------------------------


class TestPin:
    def test_pin_by_date(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        base_dir = tmp_path / "state"
        # Create a briefing directly via emit_briefing so we can pin it.
        draft = BriefingDraft(
            date_local="2026-04-15", mode="fallback",
            yesterday="quiet", priorities=[], watch=[],
            markdown="- body",
        )
        emit_briefing(base_dir, draft)

        result = runner.invoke(
            briefing_app,
            ["pin", "2026-04-15", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "pinned 2026-04-15" in result.stdout

    def test_pin_latest(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        base_dir = tmp_path / "state"
        for day in ("2026-04-13", "2026-04-14", "2026-04-15"):
            draft = BriefingDraft(
                date_local=day, mode="fallback",
                yesterday="quiet", priorities=[], watch=[], markdown="body",
            )
            emit_briefing(base_dir, draft)

        result = runner.invoke(
            briefing_app,
            ["pin", "latest", "--config", str(config_path), "--json"],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["pinned"] is True
        assert payload["date_local"] == "2026-04-15"

    def test_pin_invalid_id(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        result = runner.invoke(
            briefing_app,
            ["pin", "not-a-date", "--config", str(config_path)],
        )
        assert result.exit_code == 1

    def test_pin_unknown_date(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        result = runner.invoke(
            briefing_app,
            ["pin", "2026-04-15", "--config", str(config_path)],
        )
        assert result.exit_code == 1
