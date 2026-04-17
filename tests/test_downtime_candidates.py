"""Tests for dt03 — candidate sourcing + scored selection.

Covers:

* ``parse_backlog`` — ignores prose outside tables, accepts valid rows,
  skips malformed rows, clamps priority, folds why_deprioritized into
  the description.
* User-queue round-trip: append → read → remove.
* ``pick_candidate`` scoring: priority ordering, variety penalty for
  last_kind / last_source, disabled-categories filter,
  recent-titles dedupe, user-queue consumption.
* Empty/absent sources produce ``None``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pollypm.plugins_builtin.downtime.handlers.pick_candidate import (
    BACKLOG_RELATIVE_PATH,
    USER_QUEUE_RELATIVE_PATH,
    Candidate,
    append_to_user_queue,
    gather_candidates,
    parse_backlog,
    pick_candidate,
    read_user_queue,
    remove_from_user_queue,
    score_candidate,
)
from pollypm.plugins_builtin.downtime.settings import DowntimeSettings
from pollypm.plugins_builtin.downtime.state import DowntimeState


# ---------------------------------------------------------------------------
# parse_backlog
# ---------------------------------------------------------------------------


class TestParseBacklog:
    def test_empty(self) -> None:
        assert parse_backlog("") == []

    def test_ignores_prose(self) -> None:
        text = "Some prose\n\nMore prose without a table.\n"
        assert parse_backlog(text) == []

    def test_basic_row(self) -> None:
        text = (
            "| title | kind | source | priority | description | why_deprioritized |\n"
            "|-------|------|--------|----------|-------------|-------------------|\n"
            "| Try foo | spec_feature | planner | 4 | Spec out foo. | critic deprioritized |\n"
        )
        result = parse_backlog(text)
        assert len(result) == 1
        c = result[0]
        assert c.title == "Try foo"
        assert c.kind == "spec_feature"
        assert c.priority == 4
        assert c.source == "planner"
        assert "Spec out foo." in c.description
        assert "critic deprioritized" in c.description

    def test_skips_unknown_kind(self) -> None:
        text = (
            "| title | kind | source | priority | description |\n"
            "|-------|------|--------|----------|-------------|\n"
            "| Bad   | nonsense | planner | 3 | blah |\n"
            "| Good  | audit_docs | planner | 3 | good |\n"
        )
        result = parse_backlog(text)
        assert len(result) == 1
        assert result[0].title == "Good"

    def test_clamps_priority(self) -> None:
        text = (
            "| title | kind | priority | description |\n"
            "|-------|------|----------|-------------|\n"
            "| Hi  | spec_feature | 99 | high |\n"
            "| Lo  | spec_feature | -5 | low |\n"
            "| Bad | spec_feature | abc | bad |\n"
        )
        result = parse_backlog(text)
        titles = {c.title: c.priority for c in result}
        assert titles["Hi"] == 5
        assert titles["Lo"] == 1
        assert titles["Bad"] == 3

    def test_handles_missing_source(self) -> None:
        text = (
            "| title | kind | description |\n"
            "|-------|------|-------------|\n"
            "| foo | spec_feature | bar |\n"
        )
        result = parse_backlog(text)
        assert len(result) == 1
        assert result[0].source == "planner"


# ---------------------------------------------------------------------------
# User-queue round-trip
# ---------------------------------------------------------------------------


class TestUserQueue:
    def test_append_and_read(self, tmp_path: Path) -> None:
        path = tmp_path / "queue.jsonl"
        cand = Candidate(
            title="Try X",
            kind="try_alt_approach",
            description="Try alternative X",
            priority=5,
            source="user",
        )
        append_to_user_queue(path, cand)
        loaded = read_user_queue(path)
        assert len(loaded) == 1
        assert loaded[0].title == "Try X"
        assert loaded[0].kind == "try_alt_approach"
        assert loaded[0].priority == 5

    def test_read_skips_malformed_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "queue.jsonl"
        path.write_text(
            '{"title": "ok", "kind": "spec_feature", "description": "d"}\n'
            "not-json\n"
            '{"title": "no-kind"}\n'
        )
        loaded = read_user_queue(path)
        assert len(loaded) == 1
        assert loaded[0].title == "ok"

    def test_remove_first_match(self, tmp_path: Path) -> None:
        path = tmp_path / "queue.jsonl"
        for i, title in enumerate(["one", "two", "three"]):
            append_to_user_queue(
                path,
                Candidate(
                    title=title, kind="spec_feature", description=f"d{i}", source="user"
                ),
            )
        target = Candidate(title="two", kind="spec_feature", description="", source="user")
        removed = remove_from_user_queue(path, target)
        assert removed is True
        remaining = [c.title for c in read_user_queue(path)]
        assert remaining == ["one", "three"]

    def test_remove_missing_returns_false(self, tmp_path: Path) -> None:
        path = tmp_path / "queue.jsonl"
        path.write_text("")
        target = Candidate(title="nope", kind="spec_feature", description="")
        assert remove_from_user_queue(path, target) is False


# ---------------------------------------------------------------------------
# Scoring + selection
# ---------------------------------------------------------------------------


class TestScoring:
    def test_priority_baseline(self) -> None:
        state = DowntimeState()
        cand = Candidate(title="t", kind="spec_feature", description="", priority=4)
        assert score_candidate(cand, state=state) == 4

    def test_same_kind_penalty(self) -> None:
        state = DowntimeState(last_kind="spec_feature")
        cand = Candidate(title="t", kind="spec_feature", description="", priority=4)
        assert score_candidate(cand, state=state) == 2  # 4 - 2

    def test_same_source_penalty(self) -> None:
        state = DowntimeState(last_source="planner")
        cand = Candidate(
            title="t", kind="spec_feature", description="", priority=4, source="planner"
        )
        assert score_candidate(cand, state=state) == 3  # 4 - 1

    def test_stacked_penalties(self) -> None:
        state = DowntimeState(last_kind="spec_feature", last_source="planner")
        cand = Candidate(
            title="t", kind="spec_feature", description="", priority=4, source="planner"
        )
        assert score_candidate(cand, state=state) == 1  # 4 - 2 - 1


class TestPickCandidate:
    def _write_backlog(self, root: Path, rows: str) -> None:
        path = root / BACKLOG_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "| title | kind | source | priority | description | why_deprioritized |\n"
            "|-------|------|--------|----------|-------------|-------------------|\n"
        )
        path.write_text(header + rows)

    def test_returns_none_when_no_sources(self, tmp_path: Path) -> None:
        state = DowntimeState()
        settings = DowntimeSettings()
        got = pick_candidate(
            settings=settings,
            state=state,
            project_root=tmp_path,
        )
        assert got is None

    def test_picks_highest_priority_from_backlog(self, tmp_path: Path) -> None:
        self._write_backlog(
            tmp_path,
            "| Low  | spec_feature | planner | 2 | low  | - |\n"
            "| High | audit_docs   | planner | 5 | high | - |\n",
        )
        got = pick_candidate(
            settings=DowntimeSettings(),
            state=DowntimeState(),
            project_root=tmp_path,
        )
        assert got is not None
        assert got.title == "High"
        assert got.kind == "audit_docs"

    def test_variety_penalty_avoids_repeat(self, tmp_path: Path) -> None:
        """When last_kind=X, an equal-priority X loses to a non-X."""
        self._write_backlog(
            tmp_path,
            "| AuditA | audit_docs  | planner | 4 | a | - |\n"
            "| SpecB  | spec_feature | planner | 4 | b | - |\n",
        )
        state = DowntimeState(last_kind="audit_docs")
        got = pick_candidate(
            settings=DowntimeSettings(),
            state=state,
            project_root=tmp_path,
        )
        assert got is not None
        assert got.kind == "spec_feature"

    def test_disabled_categories_filter(self, tmp_path: Path) -> None:
        self._write_backlog(
            tmp_path,
            "| Build | build_speculative | planner | 5 | b | - |\n"
            "| Spec  | spec_feature      | planner | 3 | s | - |\n",
        )
        got = pick_candidate(
            settings=DowntimeSettings(disabled_categories=("build_speculative",)),
            state=DowntimeState(),
            project_root=tmp_path,
        )
        assert got is not None
        assert got.kind == "spec_feature"

    def test_recent_titles_dedupe(self, tmp_path: Path) -> None:
        self._write_backlog(
            tmp_path,
            "| Repeat | spec_feature | planner | 5 | r | - |\n"
            "| Fresh  | spec_feature | planner | 3 | f | - |\n",
        )
        state = DowntimeState(recent_titles=["Repeat"])
        got = pick_candidate(
            settings=DowntimeSettings(),
            state=state,
            project_root=tmp_path,
        )
        assert got is not None
        assert got.title == "Fresh"

    def test_user_queue_consumed_on_pick(self, tmp_path: Path) -> None:
        queue = tmp_path / USER_QUEUE_RELATIVE_PATH
        cand = Candidate(
            title="User idea",
            kind="spec_feature",
            description="from user",
            priority=5,
            source="user",
        )
        append_to_user_queue(queue, cand)
        got = pick_candidate(
            settings=DowntimeSettings(),
            state=DowntimeState(),
            project_root=tmp_path,
        )
        assert got is not None
        assert got.title == "User idea"
        # And the queue is now empty.
        assert read_user_queue(queue) == []

    def test_user_queue_beats_lower_priority_backlog(self, tmp_path: Path) -> None:
        self._write_backlog(
            tmp_path,
            "| Planner A | audit_docs | planner | 2 | pa | - |\n",
        )
        queue = tmp_path / USER_QUEUE_RELATIVE_PATH
        append_to_user_queue(
            queue,
            Candidate(
                title="User pick",
                kind="spec_feature",
                description="user-driven",
                priority=5,
                source="user",
            ),
        )
        got = pick_candidate(
            settings=DowntimeSettings(),
            state=DowntimeState(),
            project_root=tmp_path,
        )
        assert got is not None
        assert got.source == "user"

    def test_auto_discoverer_can_feed(self, tmp_path: Path) -> None:
        """A custom auto-discoverer can inject candidates."""
        def scanner(_root: Path) -> list[Candidate]:
            return [
                Candidate(
                    title="doc drift",
                    kind="audit_docs",
                    description="docs behind code",
                    priority=5,
                    source="auto",
                )
            ]
        got = pick_candidate(
            settings=DowntimeSettings(),
            state=DowntimeState(),
            project_root=tmp_path,
            auto_discoverers=(scanner,),
        )
        assert got is not None
        assert got.source == "auto"

    def test_scanner_error_is_swallowed(self, tmp_path: Path) -> None:
        self._write_backlog(
            tmp_path,
            "| Foo | spec_feature | planner | 3 | f | - |\n",
        )

        def boom(_root: Path) -> list[Candidate]:
            raise RuntimeError("scanner broken")

        got = pick_candidate(
            settings=DowntimeSettings(),
            state=DowntimeState(),
            project_root=tmp_path,
            auto_discoverers=(boom,),
        )
        assert got is not None
        assert got.title == "Foo"
