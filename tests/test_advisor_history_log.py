"""Tests for the advisor history log (ad03/ad04).

Covers:

* append_decision persists required fields for both emit and silent
  decisions.
* JSONL file stays parseable across multiple appends.
* read_log tolerates corrupt lines without raising.
* recent_entries_for_project honors the limit.
* stats computes emit-rate and topic distribution correctly.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.plugins_builtin.advisor.handlers.history_log import (
    HistoryEntry,
    LOG_FILENAME,
    append_decision,
    append_log_entry,
    entries_in_window,
    log_path,
    read_log,
    recent_entries_for_project,
    stats,
)


class TestAppendContract:
    def test_emit_entry_fields(self, tmp_path: Path) -> None:
        decision = {
            "emit": True,
            "topic": "architecture_drift",
            "severity": "recommendation",
            "summary": "Cockpit monolith",
            "details": "blah",
            "suggestion": "split it",
        }
        entry = append_decision(
            tmp_path, project="pollypm",
            decision_json=decision,
            task_id="pollypm/412",
            commits_reviewed=["abc", "def"],
        )
        assert entry.decision == "emit"
        assert entry.topic == "architecture_drift"
        assert entry.severity == "recommendation"
        assert entry.summary == "Cockpit monolith"
        assert entry.task_id == "pollypm/412"
        assert entry.commits_reviewed == ["abc", "def"]

        # File on disk is a single JSONL line with those fields.
        raw = log_path(tmp_path).read_text().strip()
        obj = json.loads(raw)
        assert obj["decision"] == "emit"
        assert obj["topic"] == "architecture_drift"
        assert obj["commits_reviewed"] == ["abc", "def"]

    def test_silent_entry_fields(self, tmp_path: Path) -> None:
        decision = {"emit": False, "rationale_if_silent": "on-plan"}
        entry = append_decision(
            tmp_path, project="pollypm", decision_json=decision,
        )
        assert entry.decision == "silent"
        assert entry.rationale_if_silent == "on-plan"

    def test_malformed_payload_becomes_silent(self, tmp_path: Path) -> None:
        entry = append_decision(
            tmp_path, project="pollypm",
            decision_json="not a dict",  # type: ignore[arg-type]
        )
        assert entry.decision == "silent"
        assert "invalid-output" in entry.rationale_if_silent


class TestReadLog:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert read_log(tmp_path) == []

    def test_round_trip_multiple_entries(self, tmp_path: Path) -> None:
        append_decision(
            tmp_path, project="a",
            decision_json={"emit": False, "rationale_if_silent": "x"},
        )
        append_decision(
            tmp_path, project="b",
            decision_json={
                "emit": True, "topic": "missing_tests", "severity": "critical",
                "summary": "s", "details": "d", "suggestion": "y",
            },
        )
        entries = read_log(tmp_path)
        assert len(entries) == 2
        assert entries[0].project == "a"
        assert entries[1].project == "b"
        assert entries[1].decision == "emit"

    def test_corrupt_line_is_skipped(self, tmp_path: Path) -> None:
        path = log_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"timestamp": "2026-04-16T10:00:00+00:00", "project": "a", "decision": "silent", "rationale_if_silent": "x"}\n'
            "not json at all\n"
            '{"timestamp": "2026-04-16T11:00:00+00:00", "project": "a", "decision": "silent", "rationale_if_silent": "y"}\n'
        )
        entries = read_log(tmp_path)
        assert len(entries) == 2
        assert entries[0].rationale_if_silent == "x"
        assert entries[1].rationale_if_silent == "y"


class TestRecentEntries:
    def test_limit_returns_most_recent(self, tmp_path: Path) -> None:
        for i in range(5):
            append_log_entry(
                tmp_path,
                HistoryEntry(
                    timestamp=f"2026-04-16T{10 + i:02d}:00:00+00:00",
                    project="proj",
                    decision="silent",
                    rationale_if_silent=f"r{i}",
                ),
            )
        recent = recent_entries_for_project(tmp_path, "proj", limit=3)
        assert [e.rationale_if_silent for e in recent] == ["r2", "r3", "r4"]

    def test_filters_by_project(self, tmp_path: Path) -> None:
        append_log_entry(
            tmp_path,
            HistoryEntry(
                timestamp="2026-04-16T10:00:00+00:00", project="a",
                decision="silent", rationale_if_silent="x",
            ),
        )
        append_log_entry(
            tmp_path,
            HistoryEntry(
                timestamp="2026-04-16T11:00:00+00:00", project="b",
                decision="silent", rationale_if_silent="y",
            ),
        )
        got = recent_entries_for_project(tmp_path, "a", limit=10)
        assert len(got) == 1
        assert got[0].rationale_if_silent == "x"


class TestStats:
    def test_empty_log(self, tmp_path: Path) -> None:
        s = stats(tmp_path)
        assert s["total"] == 0
        assert s["emit_count"] == 0
        assert s["emit_rate"] == 0.0

    def test_emit_rate_and_topic_distribution(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        entries = [
            HistoryEntry(
                timestamp=now.isoformat(), project="p1",
                decision="emit", topic="architecture_drift",
                severity="recommendation", summary="x",
            ),
            HistoryEntry(
                timestamp=now.isoformat(), project="p1",
                decision="silent", rationale_if_silent="on-plan",
            ),
            HistoryEntry(
                timestamp=now.isoformat(), project="p1",
                decision="emit", topic="architecture_drift",
                severity="suggestion", summary="y",
            ),
            HistoryEntry(
                timestamp=now.isoformat(), project="p2",
                decision="emit", topic="missing_tests",
                severity="critical", summary="z",
            ),
        ]
        for e in entries:
            append_log_entry(tmp_path, e)
        s = stats(tmp_path)
        assert s["total"] == 4
        assert s["emit_count"] == 3
        assert s["silent_count"] == 1
        assert s["topic_distribution"]["architecture_drift"] == 2
        assert s["topic_distribution"]["missing_tests"] == 1
        assert s["per_project"]["p1"]["emit"] == 2
        assert s["per_project"]["p1"]["silent"] == 1
        assert s["per_project"]["p1"]["emit_rate"] == round(2 / 3, 4)
        assert s["per_project"]["p2"]["emit_rate"] == 1.0

    def test_window_filtering(self, tmp_path: Path) -> None:
        old = datetime.now(UTC) - timedelta(days=30)
        recent = datetime.now(UTC) - timedelta(hours=1)
        append_log_entry(
            tmp_path,
            HistoryEntry(
                timestamp=old.isoformat(), project="p1",
                decision="emit", topic="other",
                severity="suggestion", summary="old",
            ),
        )
        append_log_entry(
            tmp_path,
            HistoryEntry(
                timestamp=recent.isoformat(), project="p1",
                decision="emit", topic="other",
                severity="suggestion", summary="recent",
            ),
        )
        # Default 7-day window excludes the 30-day-old entry.
        s = stats(tmp_path)
        assert s["total"] == 1


class TestEntriesInWindow:
    def test_project_filter(self, tmp_path: Path) -> None:
        append_log_entry(tmp_path, HistoryEntry(
            timestamp=datetime.now(UTC).isoformat(),
            project="a", decision="silent", rationale_if_silent="x",
        ))
        append_log_entry(tmp_path, HistoryEntry(
            timestamp=datetime.now(UTC).isoformat(),
            project="b", decision="silent", rationale_if_silent="y",
        ))
        a_only = entries_in_window(tmp_path, project="a")
        assert [e.project for e in a_only] == ["a"]

    def test_decision_filter(self, tmp_path: Path) -> None:
        append_log_entry(tmp_path, HistoryEntry(
            timestamp=datetime.now(UTC).isoformat(),
            project="a", decision="silent", rationale_if_silent="x",
        ))
        append_log_entry(tmp_path, HistoryEntry(
            timestamp=datetime.now(UTC).isoformat(),
            project="a", decision="emit", topic="other",
            severity="suggestion", summary="s",
        ))
        emits = entries_in_window(tmp_path, decision="emit")
        assert [e.decision for e in emits] == ["emit"]
