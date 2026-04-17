"""Tests for `pm advisor history` (ad04)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.plugins_builtin.advisor.cli.advisor_cli import (
    _parse_since,
    advisor_app,
)
from pollypm.plugins_builtin.advisor.handlers.history_log import (
    HistoryEntry,
    append_log_entry,
)


runner = CliRunner()


# ---------------------------------------------------------------------------
# _parse_since
# ---------------------------------------------------------------------------


class TestParseSince:
    def test_none_returns_none(self) -> None:
        assert _parse_since(None) is None
        assert _parse_since("") is None

    def test_duration_hours(self) -> None:
        now = datetime.now(UTC)
        got = _parse_since("24h")
        assert got is not None
        assert (now - got).total_seconds() == pytest.approx(24 * 3600, rel=0.05)

    def test_duration_days(self) -> None:
        now = datetime.now(UTC)
        got = _parse_since("7d")
        assert got is not None
        assert (now - got).total_seconds() == pytest.approx(7 * 86400, rel=0.05)

    def test_iso(self) -> None:
        got = _parse_since("2026-04-16T10:00:00+00:00")
        assert got == datetime(2026, 4, 16, 10, 0, tzinfo=UTC)

    def test_malformed_raises(self) -> None:
        import typer
        with pytest.raises(typer.BadParameter):
            _parse_since("not a duration")


# ---------------------------------------------------------------------------
# pm advisor history — integration via CliRunner with patched config.
# ---------------------------------------------------------------------------


from dataclasses import dataclass, field


@dataclass
class FakeProjectSection:
    base_dir: Path


@dataclass
class FakeConfig:
    project: FakeProjectSection
    projects: dict = field(default_factory=dict)


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base_dir = tmp_path / ".pollypm-state"
    base_dir.mkdir()
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("")
    cfg = FakeConfig(project=FakeProjectSection(base_dir=base_dir))
    monkeypatch.setattr(
        "pollypm.plugins_builtin.advisor.cli.advisor_cli.load_config",
        lambda _p: cfg,
    )
    monkeypatch.setattr(
        "pollypm.plugins_builtin.advisor.cli.advisor_cli.resolve_config_path",
        lambda _p: config_path,
    )
    return {"base_dir": base_dir, "config_path": config_path}


class TestHistoryCLI:
    def test_history_text_all(self, cli_env) -> None:
        base = cli_env["base_dir"]
        now = datetime.now(UTC)
        append_log_entry(
            base,
            HistoryEntry(
                timestamp=now.isoformat(), project="p1",
                decision="emit", topic="architecture_drift",
                severity="recommendation", summary="Cockpit going big.",
            ),
        )
        append_log_entry(
            base,
            HistoryEntry(
                timestamp=now.isoformat(), project="p1",
                decision="silent",
                rationale_if_silent="on-plan",
            ),
        )
        result = runner.invoke(advisor_app, ["history", "--config", str(cli_env["config_path"])])
        assert result.exit_code == 0, result.output
        assert "Cockpit going big." in result.output
        assert "on-plan" in result.output

    def test_history_json(self, cli_env) -> None:
        base = cli_env["base_dir"]
        append_log_entry(
            base,
            HistoryEntry(
                timestamp=datetime.now(UTC).isoformat(), project="p1",
                decision="silent", rationale_if_silent="x",
            ),
        )
        result = runner.invoke(
            advisor_app, ["history", "--config", str(cli_env["config_path"]), "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["decision"] == "silent"

    def test_history_project_filter(self, cli_env) -> None:
        base = cli_env["base_dir"]
        append_log_entry(base, HistoryEntry(
            timestamp=datetime.now(UTC).isoformat(),
            project="a", decision="silent", rationale_if_silent="x",
        ))
        append_log_entry(base, HistoryEntry(
            timestamp=datetime.now(UTC).isoformat(),
            project="b", decision="silent", rationale_if_silent="y",
        ))
        result = runner.invoke(
            advisor_app,
            ["history", "--config", str(cli_env["config_path"]),
             "--project", "a", "--json"],
        )
        data = json.loads(result.output)
        assert [e["project"] for e in data] == ["a"]

    def test_history_decision_filter(self, cli_env) -> None:
        base = cli_env["base_dir"]
        append_log_entry(base, HistoryEntry(
            timestamp=datetime.now(UTC).isoformat(),
            project="a", decision="silent", rationale_if_silent="x",
        ))
        append_log_entry(base, HistoryEntry(
            timestamp=datetime.now(UTC).isoformat(),
            project="a", decision="emit", topic="other",
            severity="suggestion", summary="s",
        ))
        result = runner.invoke(
            advisor_app,
            ["history", "--config", str(cli_env["config_path"]),
             "--decision", "emit", "--json"],
        )
        data = json.loads(result.output)
        assert [e["decision"] for e in data] == ["emit"]

    def test_history_since_filter(self, cli_env) -> None:
        base = cli_env["base_dir"]
        old = datetime.now(UTC) - timedelta(days=10)
        recent = datetime.now(UTC) - timedelta(hours=2)
        append_log_entry(base, HistoryEntry(
            timestamp=old.isoformat(), project="a",
            decision="silent", rationale_if_silent="old",
        ))
        append_log_entry(base, HistoryEntry(
            timestamp=recent.isoformat(), project="a",
            decision="silent", rationale_if_silent="recent",
        ))
        result = runner.invoke(
            advisor_app,
            ["history", "--config", str(cli_env["config_path"]),
             "--since", "24h", "--json"],
        )
        data = json.loads(result.output)
        rationales = [e["rationale_if_silent"] for e in data]
        assert "recent" in rationales
        assert "old" not in rationales

    def test_history_stats(self, cli_env) -> None:
        base = cli_env["base_dir"]
        now = datetime.now(UTC)
        for _ in range(2):
            append_log_entry(base, HistoryEntry(
                timestamp=now.isoformat(), project="p1",
                decision="emit", topic="architecture_drift",
                severity="recommendation", summary="s",
            ))
        append_log_entry(base, HistoryEntry(
            timestamp=now.isoformat(), project="p1",
            decision="silent", rationale_if_silent="x",
        ))
        result = runner.invoke(
            advisor_app,
            ["history", "--config", str(cli_env["config_path"]),
             "--stats", "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["emit_count"] == 2
        assert data["silent_count"] == 1
        assert data["topic_distribution"]["architecture_drift"] == 2
        assert data["per_project"]["p1"]["emit_rate"] == round(2 / 3, 4)

    def test_history_three_runs_chronological(self, cli_env) -> None:
        """Acceptance: 3 runs (2 emit, 1 silent) all surface in order."""
        base = cli_env["base_dir"]
        entries = [
            HistoryEntry(
                timestamp=f"2026-04-16T1{i}:00:00+00:00", project="p",
                decision="emit" if i in (1, 3) else "silent",
                topic="architecture_drift" if i in (1, 3) else None,
                severity="recommendation" if i in (1, 3) else None,
                summary=f"emit {i}" if i in (1, 3) else "",
                rationale_if_silent=f"silent {i}" if i == 2 else "",
            )
            for i in (1, 2, 3)
        ]
        for e in entries:
            append_log_entry(base, e)
        result = runner.invoke(
            advisor_app,
            ["history", "--config", str(cli_env["config_path"]), "--json"],
        )
        data = json.loads(result.output)
        assert [e["timestamp"] for e in data] == [
            "2026-04-16T11:00:00+00:00",
            "2026-04-16T12:00:00+00:00",
            "2026-04-16T13:00:00+00:00",
        ]

    def test_history_invalid_decision_rejected(self, cli_env) -> None:
        result = runner.invoke(
            advisor_app,
            ["history", "--config", str(cli_env["config_path"]),
             "--decision", "nonsense"],
        )
        assert result.exit_code != 0


class TestCLIMount:
    def test_advisor_app_mounts_on_cli(self) -> None:
        """pm advisor subcommand must be wired in pollypm.cli."""
        from pollypm import cli as pollypm_cli
        # Typer apps expose .registered_groups; assert advisor is there.
        names = [g.name for g in pollypm_cli.app.registered_groups]
        assert "advisor" in names
