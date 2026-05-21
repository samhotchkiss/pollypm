"""Tests for ``pm audit grep`` — rotation-aware forensic CLI.

Covers:

* file walk includes the live ``.jsonl`` AND rotated ``.gz`` archives,
* archives are walked newest-first,
* ``--since`` accepts both ISO-8601 and shortcuts (``1h`` / ``24h`` / ``7d``),
* ``--event-type`` is an exact match (not a regex),
* ``--limit 0`` returns every match,
* pattern is a Python regex (``samblog/\\d+`` style),
* ``--project NAME`` scopes target file selection.

The tests drive ``_iter_matching_events`` / ``_resolve_target_files``
directly because the Typer command's ``raise typer.Exit`` makes the
CliRunner path more verbose without adding signal — but the CLI
surface itself is smoke-tested via ``typer.testing.CliRunner`` to
confirm the command is mounted and the formatter renders.
"""

from __future__ import annotations

import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pollypm.cli_features.audit import (
    _iter_matching_events,
    _resolve_target_files,
    _walk_log_chain,
    audit_app,
    format_event,
    parse_since,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, separators=(",", ":")))
            fh.write("\n")


def _write_jsonl_gz(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, separators=(",", ":")))
            fh.write("\n")


def _make_config(tmp_path: Path, *, projects: dict | None = None) -> Path:
    """Write a minimal valid PollyPM config and return its path.

    Mirrors the test_projects.py pattern — the full ``PollyPMConfig``
    dataclass demands a ``ProjectSettings`` block + scaffolding paths,
    so we fill in the minimum to satisfy serialization.
    """
    from pollypm.config import write_config
    from pollypm.models import (
        PollyPMConfig,
        PollyPMSettings,
        ProjectSettings,
    )

    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account=""),
        accounts={},
        sessions={},
        projects=projects or {},
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    return config_path


def _make_event(
    *,
    project: str,
    event: str,
    subject: str = "",
    ts: str | None = None,
    status: str = "ok",
    metadata: dict | None = None,
) -> dict:
    return {
        "schema": 1,
        "ts": ts or "2026-05-21T00:00:00+00:00",
        "project": project,
        "event": event,
        "subject": subject,
        "actor": "polly",
        "status": status,
        "metadata": metadata or {},
    }


# ---------------------------------------------------------------------------
# parse_since
# ---------------------------------------------------------------------------


def test_parse_since_iso_with_z_suffix() -> None:
    parsed = parse_since("2026-05-21T03:14:15Z")
    assert parsed == datetime(2026, 5, 21, 3, 14, 15, tzinfo=timezone.utc)


def test_parse_since_iso_with_offset() -> None:
    parsed = parse_since("2026-05-21T03:14:15+00:00")
    assert parsed == datetime(2026, 5, 21, 3, 14, 15, tzinfo=timezone.utc)


def test_parse_since_naive_iso_treated_as_utc() -> None:
    parsed = parse_since("2026-05-21T03:14:15")
    assert parsed.tzinfo is not None
    assert parsed == datetime(2026, 5, 21, 3, 14, 15, tzinfo=timezone.utc)


def test_parse_since_shortcut_hours() -> None:
    parsed = parse_since("1h")
    delta = datetime.now(timezone.utc) - parsed
    # Allow a couple seconds of slack for test runtime drift.
    assert timedelta(hours=1) - timedelta(seconds=5) <= delta <= timedelta(hours=1) + timedelta(seconds=5)


def test_parse_since_shortcut_days() -> None:
    parsed = parse_since("7d")
    delta = datetime.now(timezone.utc) - parsed
    assert timedelta(days=7) - timedelta(seconds=5) <= delta <= timedelta(days=7) + timedelta(seconds=5)


def test_parse_since_shortcut_with_whitespace() -> None:
    # Operators often type ``24h `` from history; trim and accept.
    parsed = parse_since(" 24h ")
    delta = datetime.now(timezone.utc) - parsed
    assert timedelta(hours=24) - timedelta(seconds=5) <= delta <= timedelta(hours=24) + timedelta(seconds=5)


def test_parse_since_invalid_raises_bad_parameter() -> None:
    import typer

    with pytest.raises(typer.BadParameter):
        parse_since("yesterday")


# ---------------------------------------------------------------------------
# _walk_log_chain — rotation awareness
# ---------------------------------------------------------------------------


def test_walk_log_chain_yields_live_first_then_gz_newest(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    _write_jsonl(live, [_make_event(project="x", event="a")])
    older = tmp_path / "audit.jsonl.1000.gz"
    _write_jsonl_gz(older, [_make_event(project="x", event="b")])
    newer = tmp_path / "audit.jsonl.2000.gz"
    _write_jsonl_gz(newer, [_make_event(project="x", event="c")])

    # Force distinct mtimes so the newest-first sort is deterministic
    # regardless of the FS's sub-second precision.
    import os
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))

    chain = list(_walk_log_chain(live))
    assert chain == [live, newer, older]


def test_walk_log_chain_handles_missing_live(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    archive = tmp_path / "audit.jsonl.1.gz"
    _write_jsonl_gz(archive, [_make_event(project="x", event="a")])
    chain = list(_walk_log_chain(live))
    # Live missing → only the archive surfaces.
    assert chain == [archive]


def test_walk_log_chain_ignores_unrelated_siblings(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    _write_jsonl(live, [_make_event(project="x", event="a")])
    # Looks like a rotation candidate but with a different prefix.
    (tmp_path / "other.jsonl.1.gz").write_text("")
    # Right prefix but not a .gz suffix.
    (tmp_path / "audit.jsonl.1").write_text("")
    chain = list(_walk_log_chain(live))
    assert chain == [live]


# ---------------------------------------------------------------------------
# _iter_matching_events — filter ordering + rotation walk
# ---------------------------------------------------------------------------


def test_iter_matches_across_live_and_archive(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    archive = tmp_path / "audit.jsonl.1.gz"
    _write_jsonl(live, [_make_event(project="samblog", event="task.created", subject="samblog/2")])
    _write_jsonl_gz(archive, [_make_event(project="samblog", event="task.created", subject="samblog/1")])

    results = list(
        _iter_matching_events(
            targets=[live],
            pattern=re.compile(r"samblog/\d+"),
            since=None,
            event_type=None,
        )
    )
    subjects = [r["subject"] for r in results]
    # Live first, then archive.
    assert subjects == ["samblog/2", "samblog/1"]


def test_iter_regex_pattern_filters_lines(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    _write_jsonl(
        live,
        [
            _make_event(project="samblog", event="task.created", subject="samblog/1"),
            _make_event(project="samblog", event="task.created", subject="samblog/12"),
            _make_event(project="other", event="task.created", subject="other/3"),
        ],
    )
    results = list(
        _iter_matching_events(
            targets=[live],
            pattern=re.compile(r"samblog/\d+"),
            since=None,
            event_type=None,
        )
    )
    assert [r["subject"] for r in results] == ["samblog/1", "samblog/12"]


def test_iter_event_type_is_exact_match_not_regex(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    _write_jsonl(
        live,
        [
            _make_event(project="x", event="advisor.tick.fired"),
            _make_event(project="x", event="advisor.tick.skipped"),
            _make_event(project="x", event="other.event"),
        ],
    )
    results = list(
        _iter_matching_events(
            targets=[live],
            # Pattern matches everything so we isolate event-type filter.
            pattern=re.compile(r"."),
            since=None,
            # If this were treated as regex, ``advisor.tick.skipped``
            # would also match because ``.`` is a wildcard.
            event_type="advisor.tick.fired",
        )
    )
    assert [r["event"] for r in results] == ["advisor.tick.fired"]


def test_iter_since_filter_drops_older_events(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    _write_jsonl(
        live,
        [
            _make_event(project="x", event="a", ts="2026-05-20T00:00:00+00:00"),
            _make_event(project="x", event="a", ts="2026-05-21T00:00:00+00:00"),
            _make_event(project="x", event="a", ts="2026-05-22T00:00:00+00:00"),
        ],
    )
    since = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    results = list(
        _iter_matching_events(
            targets=[live],
            pattern=re.compile(r"."),
            since=since,
            event_type=None,
        )
    )
    assert [r["ts"] for r in results] == [
        "2026-05-21T00:00:00+00:00",
        "2026-05-22T00:00:00+00:00",
    ]


def test_iter_skips_malformed_lines(tmp_path: Path) -> None:
    live = tmp_path / "audit.jsonl"
    live.parent.mkdir(parents=True, exist_ok=True)
    with open(live, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_make_event(project="x", event="a")) + "\n")
        fh.write("{not valid json\n")  # truncated tail
        fh.write(json.dumps(_make_event(project="x", event="b")) + "\n")

    results = list(
        _iter_matching_events(
            targets=[live],
            pattern=re.compile(r"."),
            since=None,
            event_type=None,
        )
    )
    assert [r["event"] for r in results] == ["a", "b"]


# ---------------------------------------------------------------------------
# format_event
# ---------------------------------------------------------------------------


def test_format_event_renders_canonical_shape() -> None:
    record = _make_event(
        project="samblog",
        event="watchdog.escalation_dispatched",
        subject="samblog/15",
        ts="2026-05-21T03:14:15+00:00",
        status="warn",
        metadata={"summary": "Draft task samblog/15 has sat unpromoted for >5 min"},
    )
    formatted = format_event(record, color=False)
    assert formatted == (
        "2026-05-21T03:14:15Z [samblog] watchdog.escalation_dispatched "
        "samblog/15 (warn) — Draft task samblog/15 has sat unpromoted for >5 min"
    )


def test_format_event_falls_back_to_metadata_json() -> None:
    record = _make_event(
        project="x",
        event="task.created",
        subject="x/1",
        metadata={"title": "first task", "extra": 7},
    )
    formatted = format_event(record, color=False)
    # ``title`` is the first summary fallback key after ``summary``/``message``/``reason``.
    assert formatted.endswith("first task")


def test_format_event_truncates_long_metadata_json() -> None:
    record = _make_event(
        project="x",
        event="task.created",
        subject="x/1",
        # No summary-ish key, so JSON dump path runs.
        metadata={"blob": "z" * 500},
    )
    formatted = format_event(record, color=False)
    assert formatted.endswith("...")


# ---------------------------------------------------------------------------
# _resolve_target_files — project-filter behaviour
# ---------------------------------------------------------------------------


def test_resolve_target_files_project_filter_only_one_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Redirect both the audit-home and config so we don't touch real disk.
    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    project_root = tmp_path / "proj"
    (project_root / ".pollypm").mkdir(parents=True)

    # Synthesize a config with one project.
    from pollypm.models import KnownProject

    config_path = _make_config(
        tmp_path,
        projects={
            "proj": KnownProject(
                key="proj", name="proj", path=project_root, tracked=True,
            ),
        },
    )

    targets = _resolve_target_files(project_filter="proj", config_path=config_path)
    # Per-project log + central tail; both surfaced regardless of existence.
    # Compare via ``resolve()`` so /tmp -> /private/tmp on macOS doesn't
    # cause a spurious miss; ``project_audit_log_path`` normalises paths
    # internally so the raw test-path comparison would never match.
    resolved = {t.resolve() for t in targets}
    assert (project_root / ".pollypm" / "audit.jsonl").resolve() in resolved
    assert (audit_home / "proj.jsonl").resolve() in resolved


def test_resolve_target_files_no_filter_walks_all_central(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))
    audit_home.mkdir(parents=True)

    # Two central-tail files for projects not in config.
    (audit_home / "alpha.jsonl").write_text("")
    (audit_home / "beta.jsonl").write_text("")
    # Junk siblings the walker should ignore.
    (audit_home / "README.md").write_text("")

    config_path = _make_config(tmp_path)

    targets = _resolve_target_files(project_filter=None, config_path=config_path)
    resolved = {t.resolve() for t in targets}
    assert (audit_home / "alpha.jsonl").resolve() in resolved
    assert (audit_home / "beta.jsonl").resolve() in resolved
    assert (audit_home / "README.md").resolve() not in resolved


# ---------------------------------------------------------------------------
# End-to-end: Typer CLI surface
# ---------------------------------------------------------------------------


def test_audit_grep_cli_end_to_end_limit_zero_returns_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    # Populate a central-tail file with 5 matching events.
    central = audit_home / "samblog.jsonl"
    _write_jsonl(
        central,
        [
            _make_event(
                project="samblog",
                event="task.created",
                subject=f"samblog/{i}",
                ts=f"2026-05-21T00:00:0{i}+00:00",
            )
            for i in range(5)
        ],
    )

    config_path = _make_config(tmp_path)

    from pollypm.cli import app as root_app
    runner = CliRunner()
    result = runner.invoke(
        root_app,
        [
            "audit",
            "grep",
            r"samblog/\d+",
            "--limit",
            "0",
            "--no-color",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.output
    # 5 lines emitted (one per match).
    output_lines = [l for l in result.output.splitlines() if l.strip()]
    assert len(output_lines) == 5
    for i in range(5):
        assert f"samblog/{i}" in result.output


def test_audit_grep_cli_limit_caps_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    central = audit_home / "samblog.jsonl"
    _write_jsonl(
        central,
        [
            _make_event(project="samblog", event="task.created", subject=f"samblog/{i}")
            for i in range(10)
        ],
    )

    config_path = _make_config(tmp_path)

    from pollypm.cli import app as root_app
    runner = CliRunner()
    result = runner.invoke(
        root_app,
        [
            "audit",
            "grep",
            r"samblog/\d+",
            "--limit",
            "3",
            "--no-color",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.output
    output_lines = [l for l in result.output.splitlines() if l.strip()]
    assert len(output_lines) == 3


def test_audit_grep_cli_no_matches_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))
    audit_home.mkdir(parents=True)

    central = audit_home / "samblog.jsonl"
    _write_jsonl(central, [_make_event(project="samblog", event="task.created")])

    config_path = _make_config(tmp_path)

    from pollypm.cli import app as root_app
    runner = CliRunner()
    result = runner.invoke(
        root_app,
        [
            "audit",
            "grep",
            "definitely-no-match",
            "--no-color",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 1


def test_audit_grep_cli_invalid_regex_surfaces_bad_parameter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))
    audit_home.mkdir(parents=True)
    (audit_home / "x.jsonl").write_text("")

    config_path = _make_config(tmp_path)

    from pollypm.cli import app as root_app
    runner = CliRunner()
    result = runner.invoke(
        root_app,
        ["audit", "grep", "(", "--no-color", "--config", str(config_path)],
    )
    # BadParameter -> non-zero exit (typer surfaces as 2).
    assert result.exit_code != 0
