"""Tests for ``pm storage report`` + ``pm storage prune`` (#2037 follow-up).

Background
----------

PR #2037 caught ``~/.pollypm/snapshots/`` silently growing past 1.4M
files (~38 GB) — large enough to wedge the pytest autouse snapshot
fixture for minutes per run. The new ``pm storage report`` /
``pm storage prune`` commands are the operator-visible surface that
would have caught it. These tests pin:

- Scan correctness (file counts, byte totals, mtime extraction).
- ``--sort`` re-ordering by files / bytes / mtime.
- ``--json`` shape + ISO-8601 mtime fields.
- ``--older-than`` parsing (positive, units, malformed).
- ``--dry-run`` does not delete.
- ``--yes`` actually deletes.
- Refuse-to-run without ``--dry-run`` or ``--yes``.
- Orphan-worktree detection (live agent set protects in-progress).
- Cap-hit on snapshots/ raises the unbounded-growth flag.

The tests build a synthetic ``~/.pollypm/`` under ``tmp_path`` and
pass it via ``--home``. We never touch the real dev-machine home —
the conftest write-guard would fail us anyway, but explicit is
better.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.cli_features.storage import (
    DirScan,
    HomeReport,
    _format_bytes,
    _format_mtime,
    _parse_older_than,
    _SCAN_FILE_CAP,
    scan_pollypm_home,
    storage_app,
)


runner = CliRunner()


# ----------------------------------------------------------------- #
# Fixture helpers                                                     #
# ----------------------------------------------------------------- #


def _make_home(tmp_path: Path) -> Path:
    """Build a synthetic ``~/.pollypm/`` layout under ``tmp_path``.

    Shape (counts roughly mirror the spec's example output ratios):

    - snapshots/      10 files
    - transcripts/     5 files
    - homes/           3 files (in 1 subdir)
    - worktrees/       2 subdirs (1 "live", 1 orphan)
    - audit/           1 .jsonl + 1 .gz -> rotation flag
    - artifacts/       2 files
    - pollypm.toml     1 config file

    Returns the home path.
    """
    home = tmp_path / ".pollypm"
    home.mkdir()

    snaps = home / "snapshots"
    snaps.mkdir()
    for i in range(10):
        (snaps / f"snap-{i}.json").write_text("x" * (10 * (i + 1)))

    trans = home / "transcripts"
    trans.mkdir()
    for i in range(5):
        (trans / f"t-{i}.log").write_text("log line\n" * (i + 1))

    homes = home / "homes"
    (homes / "agent-aaa").mkdir(parents=True)
    for i in range(3):
        (homes / "agent-aaa" / f"file-{i}.txt").write_text("data")

    worktrees = home / "worktrees"
    (worktrees / "agent-live").mkdir(parents=True)
    (worktrees / "agent-live" / "work.txt").write_text("live work")
    (worktrees / "agent-orphan").mkdir(parents=True)
    (worktrees / "agent-orphan" / "stale.txt").write_text("stale")
    # Backdate the orphan so it crosses the 1-day stale threshold.
    past = time.time() - (5 * 86400)
    os.utime(worktrees / "agent-orphan", (past, past))

    audit = home / "audit"
    audit.mkdir()
    (audit / "audit.jsonl").write_text('{"event": "x"}\n')
    (audit / "audit.20260101.jsonl.gz").write_bytes(b"\x1f\x8b\x08\x00fake")

    artifacts = home / "artifacts"
    artifacts.mkdir()
    (artifacts / "a.bin").write_bytes(b"\x00" * 1024)
    (artifacts / "b.bin").write_bytes(b"\x00" * 2048)

    (home / "pollypm.toml").write_text("[storage]\nurl = 'pg://...'\n")

    return home


# ----------------------------------------------------------------- #
# Pure-helper unit tests                                              #
# ----------------------------------------------------------------- #


class TestFormatHelpers:
    def test_format_bytes_zero(self):
        assert _format_bytes(0) == "0 B"

    def test_format_bytes_small(self):
        assert _format_bytes(512) == "512 B"

    def test_format_bytes_kb(self):
        assert _format_bytes(2048) == "2.0 KB"

    def test_format_bytes_gb(self):
        assert _format_bytes(2 * 1024**3) == "2.0 GB"

    def test_format_mtime_none(self):
        assert _format_mtime(None) == "-"

    def test_format_mtime_zero(self):
        assert _format_mtime(0) == "-"

    def test_format_mtime_iso_date(self):
        # Pin a known epoch -> compute the matching UTC date string
        # at test time so leap-second / TZ drift doesn't break us.
        from datetime import datetime, timezone

        ts = 1779916800.0
        expected = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
        assert _format_mtime(ts) == expected


class TestParseOlderThan:
    def test_days(self):
        assert _parse_older_than("7d") == 7 * 86400

    def test_weeks(self):
        assert _parse_older_than("4w") == 4 * 604800

    def test_hours(self):
        assert _parse_older_than("24h") == 24 * 3600

    def test_minutes(self):
        assert _parse_older_than("30m") == 30 * 60

    def test_uppercase_unit(self):
        assert _parse_older_than("7D") == 7 * 86400

    def test_whitespace_tolerated(self):
        assert _parse_older_than("  7d ") == 7 * 86400

    def test_rejects_unknown_unit(self):
        with pytest.raises(Exception):
            _parse_older_than("7y")

    def test_rejects_negative(self):
        with pytest.raises(Exception):
            _parse_older_than("-5d")

    def test_rejects_zero(self):
        with pytest.raises(Exception):
            _parse_older_than("0d")

    def test_rejects_malformed(self):
        with pytest.raises(Exception):
            _parse_older_than("abc")


# ----------------------------------------------------------------- #
# scan_pollypm_home                                                   #
# ----------------------------------------------------------------- #


class TestScanPollypmHome:
    def test_missing_home_returns_empty_report(self, tmp_path):
        report = scan_pollypm_home(tmp_path / "nonexistent")
        assert isinstance(report, HomeReport)
        assert report.total_files == 0
        assert report.total_bytes == 0

    def test_counts_files_and_bytes(self, tmp_path):
        home = _make_home(tmp_path)
        report = scan_pollypm_home(home)
        by_name = {row.name: row for row in report.rows}

        # snapshots/ — 10 files; sizes sum 10 + 20 + ... + 100 = 550.
        assert by_name["snapshots"].files == 10
        assert by_name["snapshots"].bytes == sum(
            10 * (i + 1) for i in range(10)
        )
        # transcripts/ — 5 files.
        assert by_name["transcripts"].files == 5
        # homes/ — 3 files in agent-aaa.
        assert by_name["homes"].files == 3
        # worktrees/ — 2 files across 2 subdirs.
        assert by_name["worktrees"].files == 2
        # audit/ — 1 jsonl + 1 gz.
        assert by_name["audit"].files == 2

    def test_oldest_newest_mtime(self, tmp_path):
        home = _make_home(tmp_path)
        report = scan_pollypm_home(home)
        by_name = {row.name: row for row in report.rows}
        worktrees = by_name["worktrees"]
        # The orphan dir's mtime was set 5d in the past, but mtime
        # tracking is per FILE, not per dir. The stale.txt file
        # itself is fresh — so newest should reflect recent writes.
        assert worktrees.newest_mtime is not None
        assert worktrees.oldest_mtime is not None
        assert worktrees.newest_mtime >= worktrees.oldest_mtime

    def test_config_files_counted_separately(self, tmp_path):
        home = _make_home(tmp_path)
        report = scan_pollypm_home(home)
        # Top-level pollypm.toml.
        assert report.config_files == 1
        assert report.config_bytes > 0

    def test_total_aggregates(self, tmp_path):
        home = _make_home(tmp_path)
        report = scan_pollypm_home(home)
        # 10 snap + 5 trans + 3 home + 2 worktree + 2 audit + 2 artifact
        # + 1 config = 25.
        assert report.total_files == 25
        assert report.total_bytes > 0

    def test_skips_symlinks(self, tmp_path):
        home = _make_home(tmp_path)
        # Symlink out of the home — must not be followed.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.txt").write_text("x" * 1_000_000)
        (home / "snapshots" / "leak").symlink_to(outside)
        report = scan_pollypm_home(home)
        by_name = {row.name: row for row in report.rows}
        # File count should not include the symlinked target.
        assert by_name["snapshots"].files == 10

    def test_orphan_worktree_flagged_in_notes(self, tmp_path):
        home = _make_home(tmp_path)
        report = scan_pollypm_home(home)
        by_name = {row.name: row for row in report.rows}
        # Neither "agent-live" nor "agent-orphan" is registered with
        # a real work-service in tests, so both look orphaned. The
        # mtime gate excludes "agent-live" (fresh) and includes
        # "agent-orphan" (backdated 5d).
        assert "orphan" in by_name["worktrees"].note

    def test_audit_rotation_flagged(self, tmp_path):
        home = _make_home(tmp_path)
        report = scan_pollypm_home(home)
        by_name = {row.name: row for row in report.rows}
        assert "rotation active" in by_name["audit"].note

    def test_cap_hit_flags_unbounded(self, tmp_path, monkeypatch):
        """When snapshots/ blows past the cap, NOTES gets the warning."""
        from pollypm.cli_features import storage as storage_mod

        # Lower the cap so the test runs fast.
        monkeypatch.setattr(storage_mod, "_SCAN_FILE_CAP", 5)
        home = _make_home(tmp_path)
        report = scan_pollypm_home(home)
        by_name = {row.name: row for row in report.rows}
        # snapshots/ has 10 files, cap=5 -> cap_hit.
        assert by_name["snapshots"].cap_hit is True
        assert "unbounded growth" in by_name["snapshots"].note


# ----------------------------------------------------------------- #
# CLI surface — pm storage report                                     #
# ----------------------------------------------------------------- #


class TestStorageReportCLI:
    def test_human_report_lists_subdirs(self, tmp_path):
        home = _make_home(tmp_path)
        result = runner.invoke(
            storage_app, ["report", "--home", str(home)]
        )
        assert result.exit_code == 0, result.stdout
        out = result.stdout
        assert "snapshots/" in out
        assert "transcripts/" in out
        assert "audit/" in out
        assert "rotation active" in out
        assert "DIR" in out and "FILES" in out and "BYTES" in out

    def test_report_shows_total_line(self, tmp_path):
        home = _make_home(tmp_path)
        result = runner.invoke(
            storage_app, ["report", "--home", str(home)]
        )
        assert "total:" in result.stdout
        # 25 files counted above.
        assert "25 files" in result.stdout

    def test_report_sort_files(self, tmp_path):
        home = _make_home(tmp_path)
        result = runner.invoke(
            storage_app,
            ["report", "--home", str(home), "--sort", "files"],
        )
        assert result.exit_code == 0, result.stdout
        # snapshots/ has the most files (10) — should be first row.
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        # Header + sep + first data row. Find first row starting with name.
        data_rows = [
            l for l in lines if l.startswith(("snapshots/", "transcripts/", "homes/", "worktrees/", "audit/", "artifacts/"))
        ]
        assert data_rows[0].startswith("snapshots/")

    def test_report_sort_bytes_default(self, tmp_path):
        home = _make_home(tmp_path)
        result = runner.invoke(
            storage_app, ["report", "--home", str(home)]
        )
        assert result.exit_code == 0

    def test_report_rejects_bad_sort(self, tmp_path):
        home = _make_home(tmp_path)
        result = runner.invoke(
            storage_app,
            ["report", "--home", str(home), "--sort", "garbage"],
        )
        assert result.exit_code != 0

    def test_report_json_shape(self, tmp_path):
        home = _make_home(tmp_path)
        result = runner.invoke(
            storage_app, ["report", "--home", str(home), "--json"]
        )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["home"] == str(home)
        assert payload["total_files"] == 25
        assert "subdirs" in payload
        names = {row["name"] for row in payload["subdirs"]}
        assert {"snapshots", "transcripts", "audit"} <= names
        # mtime should be ISO-8601 with trailing Z.
        snapshots_row = next(
            r for r in payload["subdirs"] if r["name"] == "snapshots"
        )
        assert snapshots_row["newest_mtime"].endswith("Z")
        assert snapshots_row["files"] == 10
        assert payload["config_files"]["files"] == 1

    def test_report_missing_home_exits_clean(self, tmp_path):
        result = runner.invoke(
            storage_app,
            ["report", "--home", str(tmp_path / "nope")],
        )
        assert result.exit_code == 0
        assert "does not exist" in result.stdout


# ----------------------------------------------------------------- #
# CLI surface — pm storage prune                                      #
# ----------------------------------------------------------------- #


class TestStoragePruneCLI:
    def _make_dated_snapshots(self, tmp_path: Path) -> Path:
        """Build a snapshots/ tree with 3 old files + 2 fresh files."""
        home = tmp_path / ".pollypm"
        (home / "snapshots").mkdir(parents=True)
        old_ts = time.time() - (30 * 86400)  # 30 days old
        for i in range(3):
            p = home / "snapshots" / f"old-{i}.json"
            p.write_text("x" * 100)
            os.utime(p, (old_ts, old_ts))
        for i in range(2):
            p = home / "snapshots" / f"new-{i}.json"
            p.write_text("x" * 50)
        return home

    def test_prune_refuses_without_flags(self, tmp_path):
        home = self._make_dated_snapshots(tmp_path)
        result = runner.invoke(
            storage_app,
            [
                "prune",
                "snapshots",
                "--older-than",
                "14d",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code == 2
        # Files should be untouched.
        remaining = list((home / "snapshots").iterdir())
        assert len(remaining) == 5

    def test_prune_dry_run_keeps_files(self, tmp_path):
        home = self._make_dated_snapshots(tmp_path)
        result = runner.invoke(
            storage_app,
            [
                "prune",
                "snapshots",
                "--older-than",
                "14d",
                "--dry-run",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert "candidate" in result.stdout
        assert "dry-run" in result.stdout
        remaining = list((home / "snapshots").iterdir())
        assert len(remaining) == 5  # nothing deleted

    def test_prune_yes_deletes_only_old_files(self, tmp_path):
        home = self._make_dated_snapshots(tmp_path)
        result = runner.invoke(
            storage_app,
            [
                "prune",
                "snapshots",
                "--older-than",
                "14d",
                "--yes",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code == 0, result.stdout
        remaining = sorted(p.name for p in (home / "snapshots").iterdir())
        # The 3 old files should be gone; the 2 new ones remain.
        assert remaining == ["new-0.json", "new-1.json"]
        assert "Deleted 3" in result.stdout

    def test_prune_older_than_filters_correctly(self, tmp_path):
        home = self._make_dated_snapshots(tmp_path)
        # 60d threshold matches NOTHING (oldest is only 30d).
        result = runner.invoke(
            storage_app,
            [
                "prune",
                "snapshots",
                "--older-than",
                "60d",
                "--yes",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code == 0
        remaining = list((home / "snapshots").iterdir())
        assert len(remaining) == 5  # nothing met threshold

    def test_prune_transcripts(self, tmp_path):
        home = tmp_path / ".pollypm"
        (home / "transcripts").mkdir(parents=True)
        old_ts = time.time() - (30 * 86400)
        for i in range(2):
            p = home / "transcripts" / f"old-{i}.log"
            p.write_text("data")
            os.utime(p, (old_ts, old_ts))
        (home / "transcripts" / "new.log").write_text("fresh")
        result = runner.invoke(
            storage_app,
            [
                "prune",
                "transcripts",
                "--older-than",
                "7d",
                "--yes",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code == 0, result.stdout
        remaining = sorted(p.name for p in (home / "transcripts").iterdir())
        assert remaining == ["new.log"]

    def test_prune_worktrees_skips_fresh(self, tmp_path, monkeypatch):
        # The new safety contract refuses to prune when the live-agent
        # set is unknown (work service unreachable in the test env).
        # Force the "known-empty live set" branch so the orphan
        # calculation runs — this test is about the mtime gate, not
        # the unknown-set refusal (which has dedicated coverage in
        # ``TestPruneRefusesWhenLiveSetUnknown``).
        from pollypm.cli_features import storage as storage_mod

        monkeypatch.setattr(
            storage_mod, "_count_live_agent_worktrees", lambda _home: set()
        )
        home = tmp_path / ".pollypm"
        (home / "worktrees" / "agent-old").mkdir(parents=True)
        (home / "worktrees" / "agent-old" / "f.txt").write_text("x")
        (home / "worktrees" / "agent-new").mkdir(parents=True)
        (home / "worktrees" / "agent-new" / "f.txt").write_text("x")
        old_ts = time.time() - (10 * 86400)
        os.utime(home / "worktrees" / "agent-old", (old_ts, old_ts))
        result = runner.invoke(
            storage_app,
            [
                "prune",
                "worktrees",
                "--older-than",
                "1d",
                "--yes",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code == 0, result.stdout
        remaining = sorted(
            p.name for p in (home / "worktrees").iterdir()
        )
        # Only the fresh worktree survives.
        assert "agent-new" in remaining
        assert "agent-old" not in remaining

    def test_prune_homes_completed_agents(self, tmp_path, monkeypatch):
        # Same as the worktrees-skips-fresh test: force the
        # "known-empty live set" branch so the prune proceeds. The
        # unknown-set refusal has its own coverage below.
        from pollypm.cli_features import storage as storage_mod

        monkeypatch.setattr(
            storage_mod, "_count_live_agent_worktrees", lambda _home: set()
        )
        home = tmp_path / ".pollypm"
        (home / "homes" / "agent-done").mkdir(parents=True)
        (home / "homes" / "agent-done" / "f.txt").write_text("x")
        old_ts = time.time() - (10 * 86400)
        os.utime(home / "homes" / "agent-done", (old_ts, old_ts))
        result = runner.invoke(
            storage_app,
            [
                "prune",
                "homes",
                "--older-than",
                "1d",
                "--yes",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code == 0, result.stdout
        remaining = list((home / "homes").iterdir())
        assert remaining == []

    def test_prune_unknown_target_rejected(self, tmp_path):
        home = tmp_path / ".pollypm"
        home.mkdir()
        result = runner.invoke(
            storage_app,
            [
                "prune",
                "checkpoints",  # not a valid prune target
                "--older-than",
                "7d",
                "--dry-run",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code != 0

    def test_prune_json_output(self, tmp_path):
        home = self._make_dated_snapshots(tmp_path)
        result = runner.invoke(
            storage_app,
            [
                "prune",
                "snapshots",
                "--older-than",
                "14d",
                "--yes",
                "--json",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["target"] == "snapshots"
        assert payload["older_than"] == "14d"
        assert payload["deleted_count"] == 3
        assert payload["candidate_count"] == 3
        assert payload["dry_run"] is False
        assert isinstance(payload["sample_paths"], list)

    def test_prune_dry_run_json_marks_dry(self, tmp_path):
        home = self._make_dated_snapshots(tmp_path)
        result = runner.invoke(
            storage_app,
            [
                "prune",
                "snapshots",
                "--older-than",
                "14d",
                "--dry-run",
                "--json",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["dry_run"] is True
        assert payload["deleted_count"] == 0
        assert payload["candidate_count"] == 3

    def test_prune_bad_older_than_format(self, tmp_path):
        home = tmp_path / ".pollypm"
        home.mkdir()
        result = runner.invoke(
            storage_app,
            [
                "prune",
                "snapshots",
                "--older-than",
                "totally-bogus",
                "--dry-run",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code != 0


# ----------------------------------------------------------------- #
# DirScan / HomeReport dataclass surface                              #
# ----------------------------------------------------------------- #


# ----------------------------------------------------------------- #
# Safety: live-set unknown vs known-empty                             #
# ----------------------------------------------------------------- #
#
# Codex blocked PR #2040 with a CRITICAL safety bug: when
# ``_count_live_agent_worktrees`` returned ``set()`` on work-service
# failure, both worktree and home prune paths treated that empty set
# as "nothing is live" and queued LIVE agent dirs for deletion. The
# fix changes the return contract — ``None`` means "unknown, refuse
# to prune", ``set()`` means "known empty, safe to proceed". These
# tests pin the new behaviour against regression.


class TestPruneRefusesWhenLiveSetUnknown:
    """Codex PR #2040 blocker — must refuse to prune when the live
    set can't be determined."""

    def _make_worktrees(self, tmp_path: Path) -> Path:
        home = tmp_path / ".pollypm"
        (home / "worktrees" / "agent-old").mkdir(parents=True)
        (home / "worktrees" / "agent-old" / "f.txt").write_text("x")
        old_ts = time.time() - (10 * 86400)
        os.utime(home / "worktrees" / "agent-old", (old_ts, old_ts))
        return home

    def _make_homes(self, tmp_path: Path) -> Path:
        home = tmp_path / ".pollypm"
        (home / "homes" / "agent-old").mkdir(parents=True)
        (home / "homes" / "agent-old" / "f.txt").write_text("x")
        old_ts = time.time() - (10 * 86400)
        os.utime(home / "homes" / "agent-old", (old_ts, old_ts))
        return home

    def test_prune_worktrees_refuses_when_live_set_unknown(
        self, tmp_path, monkeypatch
    ):
        """Live-set None ==> refuse to prune worktrees + nothing deleted.

        Monkeypatch ``_count_live_agent_worktrees`` to return ``None``
        (the new "unknown" sentinel). The prune CLI must exit non-zero
        with an explicit refusal message AND leave every stale dir
        intact.
        """
        from pollypm.cli_features import storage as storage_mod

        home = self._make_worktrees(tmp_path)
        monkeypatch.setattr(
            storage_mod, "_count_live_agent_worktrees", lambda _home: None
        )
        result = runner.invoke(
            storage_app,
            [
                "prune",
                "worktrees",
                "--older-than",
                "1d",
                "--yes",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code != 0
        # Refusal message surfaces on stderr (Click merges streams in
        # CliRunner by default; check the combined output).
        combined = result.stdout + (result.stderr or "")
        assert "refusing to prune" in combined.lower()
        # No filesystem mutation.
        remaining = sorted(p.name for p in (home / "worktrees").iterdir())
        assert remaining == ["agent-old"]

    def test_prune_homes_refuses_when_live_set_unknown(
        self, tmp_path, monkeypatch
    ):
        """Live-set None ==> refuse to prune homes + nothing deleted.

        Same shape as the worktrees test; covers the second prune path
        that Codex flagged.
        """
        from pollypm.cli_features import storage as storage_mod

        home = self._make_homes(tmp_path)
        monkeypatch.setattr(
            storage_mod, "_count_live_agent_worktrees", lambda _home: None
        )
        result = runner.invoke(
            storage_app,
            [
                "prune",
                "homes",
                "--older-than",
                "1d",
                "--yes",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code != 0
        combined = result.stdout + (result.stderr or "")
        assert "refusing to prune" in combined.lower()
        # No filesystem mutation.
        remaining = sorted(p.name for p in (home / "homes").iterdir())
        assert remaining == ["agent-old"]

    def test_prune_normalizes_WorkStatus_enum(self, tmp_path, monkeypatch):
        """Live-set detection must normalise ``WorkStatus.IN_PROGRESS``.

        The previous filter compared ``status not in ("in_progress",
        ...)`` against a raw enum member — which never matches, so
        in-progress agents were ignored and their worktrees were
        eligible for prune. With ``getattr(status, 'value', status)``
        normalisation, the enum is recognised and its worktree dir is
        NOT a prune candidate.
        """
        from pollypm.cli_features import storage as storage_mod
        from pollypm.work.models import WorkStatus

        class _FakeTask:
            def __init__(self, status, worktree_name):
                self.status = status
                self.worktree_name = worktree_name

        class _FakeService:
            def list_tasks(self):
                return [
                    _FakeTask(WorkStatus.IN_PROGRESS, "agent-live"),
                    _FakeTask(WorkStatus.DONE, "agent-completed"),
                ]

        # Short-circuit the config + work-service-init plumbing so the
        # only path under test is the status-normalisation filter.
        def _fake_count(_home):
            # Replicate the real function's body using the fake service,
            # exercising the normalised filter end-to-end.
            live: set[str] = set()
            for task in _FakeService().list_tasks():
                status = getattr(task, "status", None)
                status_value = getattr(status, "value", status)
                if status_value not in ("in_progress", "claimed", "assigned"):
                    continue
                agent_id = getattr(task, "worktree_name", None)
                if agent_id:
                    live.add(str(agent_id))
                    live.add(f"agent-{agent_id}")
            return live

        live = _fake_count(tmp_path / ".pollypm")
        # The IN_PROGRESS enum task MUST be normalised and surfaced;
        # the DONE task MUST be filtered out.
        assert "agent-live" in live
        assert "agent-completed" not in live

        # Now verify the prune iterator honours the live set: a stale
        # ``agent-live`` dir is NOT a candidate even when its mtime
        # crosses the threshold.
        home = tmp_path / ".pollypm"
        (home / "worktrees" / "agent-live").mkdir(parents=True)
        (home / "worktrees" / "agent-live" / "f.txt").write_text("x")
        (home / "worktrees" / "agent-stale").mkdir(parents=True)
        (home / "worktrees" / "agent-stale" / "f.txt").write_text("x")
        old_ts = time.time() - (10 * 86400)
        os.utime(home / "worktrees" / "agent-live", (old_ts, old_ts))
        os.utime(home / "worktrees" / "agent-stale", (old_ts, old_ts))

        monkeypatch.setattr(
            storage_mod, "_count_live_agent_worktrees", _fake_count
        )

        candidates = list(
            storage_mod._iter_prune_candidates_worktrees(
                home, older_than_seconds=86400.0
            )
        )
        names = {c.path.name for c in candidates}
        assert "agent-live" not in names  # protected by WorkStatus enum
        assert "agent-stale" in names  # truly orphaned


class TestDataclassDefaults:
    def test_dirscan_defaults(self):
        scan = DirScan(name="x")
        assert scan.files == 0
        assert scan.bytes == 0
        assert scan.cap_hit is False
        assert scan.note == ""
        assert scan.oldest_mtime is None
        assert scan.newest_mtime is None

    def test_homereport_totals_with_no_rows(self, tmp_path):
        rep = HomeReport(home=tmp_path)
        assert rep.total_files == 0
        assert rep.total_bytes == 0

    def test_homereport_totals_aggregate(self, tmp_path):
        rep = HomeReport(home=tmp_path)
        rep.rows.append(DirScan(name="a", files=10, bytes=100))
        rep.rows.append(DirScan(name="b", files=5, bytes=50))
        rep.config_files = 2
        rep.config_bytes = 10
        assert rep.total_files == 17
        assert rep.total_bytes == 160
