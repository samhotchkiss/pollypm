"""Tests for the ``pm activity`` CLI (lf05).

Covers:
    * Duration parsing (``30s``, ``5m``, ``1h``, ``2d``, ``1w``).
    * One-shot invocation prints the last N entries in reverse order.
    * Filters (project, kind, actor, since) compose.
    * ``--json`` emits NDJSON (one entry per line, valid JSON objects).
    * ``--follow`` tails new entries using a short polling loop
      exercised via a monkeypatched ``time.sleep``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from pollypm.plugins_builtin.activity_feed.cli import (
    activity_app,
    parse_duration,
)
from pollypm.plugins_builtin.activity_feed.summaries import activity_summary
from pollypm.storage.state import StateStore


# ---------------------------------------------------------------------------
# parse_duration.
# ---------------------------------------------------------------------------


def test_parse_duration_none() -> None:
    assert parse_duration(None) is None
    assert parse_duration("") is None


@pytest.mark.parametrize(
    "raw,expected_seconds",
    [
        ("30s", 30),
        ("30", 30),  # default unit is seconds
        ("5m", 300),
        ("1h", 3600),
        ("2d", 2 * 86400),
        ("1w", 7 * 86400),
        ("1.5h", int(1.5 * 3600)),
    ],
)
def test_parse_duration_values(raw: str, expected_seconds: int) -> None:
    result = parse_duration(raw)
    assert result is not None
    assert int(result.total_seconds()) == expected_seconds


def test_parse_duration_invalid_raises() -> None:
    with pytest.raises(typer.BadParameter):
        parse_duration("bogus")


# ---------------------------------------------------------------------------
# CLI harness: build a tiny pollypm.toml pointing at a temp state DB so
# the real ``load_config`` path works end-to-end.
# ---------------------------------------------------------------------------


def _write_minimal_config(tmp_path: Path) -> Path:
    """Write the smallest TOML ``load_config`` accepts.

    We only need the [project] block to point at a writable state DB
    — the CLI path exercised here (build_projector + project) doesn't
    touch accounts / sessions.
    """
    config_path = tmp_path / "pollypm.toml"
    base_dir = tmp_path / ".pollypm"
    state_db = base_dir / "state.db"
    config_path.write_text(
        "[project]\n"
        f'name = "PollyPM"\n'
        f'root_dir = "{tmp_path}"\n'
        f'tmux_session = "pollypm-test"\n'
        f'base_dir = "{base_dir}"\n'
        f'state_db = "{state_db}"\n'
        f'logs_dir = "{base_dir / "logs"}"\n'
        f'workspace_root = "{tmp_path}"\n'
    )
    # Pre-create the state DB so the projector has something to read.
    state_db.parent.mkdir(parents=True, exist_ok=True)
    StateStore(state_db).close()
    return config_path


def _seed(state_db: Path, records: list[tuple[str, str, str]]) -> None:
    store = StateStore(state_db)
    for session, event_type, message in records:
        store.record_event(session, event_type, message)
    store.close()


def test_activity_cli_prints_entries(tmp_path: Path) -> None:
    config_path = _write_minimal_config(tmp_path)
    state_db = tmp_path / ".pollypm" / "state.db"
    _seed(state_db, [
        ("operator", "alert", activity_summary(summary="Pane died", severity="critical", verb="alerted")),
        ("worker", "commit", activity_summary(summary="Shipped lf01", severity="routine", verb="committed")),
    ])

    runner = CliRunner()
    result = runner.invoke(activity_app, ["--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "Pane died" in result.output
    assert "Shipped lf01" in result.output
    # Critical entry has the '!' prefix from format_entry_row.
    assert result.output.splitlines()[1].lstrip().startswith("!") or \
           any(line.lstrip().startswith("!") for line in result.output.splitlines())


def test_activity_cli_json_is_ndjson(tmp_path: Path) -> None:
    config_path = _write_minimal_config(tmp_path)
    state_db = tmp_path / ".pollypm" / "state.db"
    _seed(state_db, [
        ("a", "k", activity_summary(summary="one")),
        ("a", "k", activity_summary(summary="two")),
    ])

    runner = CliRunner()
    result = runner.invoke(
        activity_app, ["--config", str(config_path), "--json"],
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)
        assert "summary" in obj
        assert "severity" in obj
        assert obj["severity"] == "routine"


def test_activity_cli_filter_by_kind(tmp_path: Path) -> None:
    config_path = _write_minimal_config(tmp_path)
    state_db = tmp_path / ".pollypm" / "state.db"
    _seed(state_db, [
        ("a", "alert", activity_summary(summary="alert A")),
        ("a", "nudge", activity_summary(summary="nudge B")),
    ])

    runner = CliRunner()
    result = runner.invoke(
        activity_app, ["--config", str(config_path), "--kind", "alert"],
    )
    assert result.exit_code == 0, result.output
    assert "alert A" in result.output
    assert "nudge B" not in result.output


def test_activity_cli_limit_cap(tmp_path: Path) -> None:
    config_path = _write_minimal_config(tmp_path)
    state_db = tmp_path / ".pollypm" / "state.db"
    _seed(state_db, [
        ("a", "k", activity_summary(summary=f"row {i}")) for i in range(10)
    ])

    runner = CliRunner()
    result = runner.invoke(
        activity_app, ["--config", str(config_path), "--limit", "3"],
    )
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert len(lines) == 3


def test_activity_cli_follow_picks_up_new_events(monkeypatch, tmp_path: Path) -> None:
    """The follow loop should emit new rows after each tick.

    We short-circuit ``time.sleep`` so the loop runs deterministically,
    seed a fresh row in the DB on the first sleep, and raise
    KeyboardInterrupt on the second to exit.
    """
    config_path = _write_minimal_config(tmp_path)
    state_db = tmp_path / ".pollypm" / "state.db"
    _seed(state_db, [("a", "k", activity_summary(summary="first"))])

    call_count = {"n": 0}

    def _fake_sleep(_seconds: float) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Seed a new event so the next _fetch sees it.
            _seed(state_db, [("a", "k", activity_summary(summary="fresh"))])
        elif call_count["n"] >= 2:
            raise KeyboardInterrupt

    import pollypm.plugins_builtin.activity_feed.cli as cli_mod

    monkeypatch.setattr(cli_mod.time, "sleep", _fake_sleep)
    # The signal.signal call inside the follow path can fail in pytest's
    # captured environment — make it a no-op.
    monkeypatch.setattr(cli_mod, "_install_sigint_handler", lambda: None)

    runner = CliRunner()
    result = runner.invoke(
        activity_app, ["--config", str(config_path), "--follow"],
    )
    assert result.exit_code == 0, result.output
    assert "first" in result.output
    assert "fresh" in result.output
