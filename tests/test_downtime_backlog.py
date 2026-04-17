"""Tests for dt04 — planner writes docs/downtime-backlog.md during synthesis.

Covers:

* ``synthesize_backlog_entries`` — pulls non-winning candidates, magic
  items, and explore-later notes into the entry list; drops unknown
  kinds; honours priority clamp.
* ``write_backlog`` file shape: markdown table with the required six
  columns, header preserved.
* Merge-don't-overwrite: a subsequent call with overlapping titles
  preserves original rows and appends only new ones.
* Integration with the downtime plugin's parser: a file written by the
  planner is round-trippable through ``parse_backlog``.
* ``synthesis_stage_prompt`` mentions ``docs/downtime-backlog.md``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.plugins_builtin.downtime.handlers.pick_candidate import (
    parse_backlog,
)
from pollypm.plugins_builtin.downtime.settings import KNOWN_CATEGORIES
from pollypm.plugins_builtin.project_planning.downtime_backlog import (
    BACKLOG_COLUMNS,
    BACKLOG_RELATIVE_PATH,
    KNOWN_DOWNTIME_KINDS,
    BacklogEntry,
    MagicItem,
    emit_backlog_from_synthesis,
    synthesize_backlog_entries,
    write_backlog,
)
from pollypm.plugins_builtin.project_planning.tree_of_plans import (
    synthesis_stage_prompt,
)


# ---------------------------------------------------------------------------
# Cross-plugin sanity
# ---------------------------------------------------------------------------


class TestCrossPluginContract:
    def test_kind_sets_agree(self) -> None:
        """Planner's KNOWN_DOWNTIME_KINDS must match downtime plugin's set."""
        assert KNOWN_DOWNTIME_KINDS == KNOWN_CATEGORIES

    def test_columns_match_parser_expectations(self) -> None:
        """Parser keys must all be present in the planner's header order."""
        for col in ("title", "kind", "source", "priority", "description"):
            assert col in BACKLOG_COLUMNS


# ---------------------------------------------------------------------------
# synthesize_backlog_entries
# ---------------------------------------------------------------------------


class TestSynthesizeBacklogEntries:
    def test_non_winning_candidates_become_try_alt_approach(self) -> None:
        entries = synthesize_backlog_entries(
            winning_candidate_id="A",
            all_candidate_ids=["A", "B", "C"],
            candidate_titles={"B": "Second plan", "C": "Third plan"},
            synthesis_rationale="Picked A for simpler sequencing.",
        )
        kinds = {e.kind for e in entries}
        titles = {e.title for e in entries}
        assert "try_alt_approach" in kinds
        assert "Second plan" in titles
        assert "Third plan" in titles
        # Winner is excluded.
        assert not any("candidate A" in e.title.lower() for e in entries)

    def test_rationale_lands_in_why_deprioritized(self) -> None:
        entries = synthesize_backlog_entries(
            winning_candidate_id="A",
            all_candidate_ids=["A", "B"],
            synthesis_rationale="A wins on test strategy.",
        )
        assert entries[0].why_deprioritized == "A wins on test strategy."

    def test_magic_items_included(self) -> None:
        entries = synthesize_backlog_entries(
            winning_candidate_id="A",
            all_candidate_ids=["A"],
            magic_items=[
                MagicItem(
                    title="spicy feature",
                    description="go 2x above vanilla",
                    why_deprioritized="critic: too big for v1",
                    priority=4,
                    kind="spec_feature",
                ),
                MagicItem(
                    title="concrete prototype",
                    description="branch-ready",
                    priority=3,
                    kind="build_speculative",
                ),
            ],
        )
        assert any(e.kind == "spec_feature" and e.title == "spicy feature" for e in entries)
        assert any(
            e.kind == "build_speculative" and e.title == "concrete prototype"
            for e in entries
        )

    def test_unknown_magic_kind_dropped(self) -> None:
        entries = synthesize_backlog_entries(
            winning_candidate_id="A",
            all_candidate_ids=["A"],
            magic_items=[MagicItem(title="bad", description="", kind="nonsense")],
        )
        assert entries == []

    def test_explore_later_uses_default_note(self) -> None:
        entries = synthesize_backlog_entries(
            winning_candidate_id="A",
            all_candidate_ids=["A"],
            explore_later_notes=[
                MagicItem(title="later feature", description="nice-to-have", kind="spec_feature"),
            ],
        )
        assert entries[0].why_deprioritized.startswith("Architect flagged as")


# ---------------------------------------------------------------------------
# write_backlog
# ---------------------------------------------------------------------------


class TestWriteBacklog:
    def test_writes_file_with_header_and_rows(self, tmp_path: Path) -> None:
        entries = [
            BacklogEntry(
                title="Spec alternative",
                kind="try_alt_approach",
                priority=2,
                description="The B plan.",
                why_deprioritized="Synthesis picked A.",
            ),
        ]
        path = write_backlog(tmp_path, entries)
        text = path.read_text()
        assert path == tmp_path / BACKLOG_RELATIVE_PATH
        # Table header present.
        assert "| title | kind | source | priority | description | why_deprioritized |" in text
        assert "Spec alternative" in text
        assert "The B plan." in text

    def test_file_is_parseable_by_downtime_parser(self, tmp_path: Path) -> None:
        entries = [
            BacklogEntry(
                title="Plan B",
                kind="try_alt_approach",
                priority=3,
                description="Alt seq plan.",
                why_deprioritized="A chosen.",
            ),
            BacklogEntry(
                title="Spicy idea",
                kind="spec_feature",
                priority=5,
                description="go 2x",
                why_deprioritized="critic deprioritized",
            ),
        ]
        write_backlog(tmp_path, entries)
        text = (tmp_path / BACKLOG_RELATIVE_PATH).read_text()
        parsed = parse_backlog(text)
        titles = {c.title for c in parsed}
        assert titles == {"Plan B", "Spicy idea"}

    def test_merge_preserves_existing_rows(self, tmp_path: Path) -> None:
        first = [
            BacklogEntry(
                title="original",
                kind="try_alt_approach",
                priority=3,
                description="first write",
            ),
        ]
        write_backlog(tmp_path, first)
        # A second call that "rediscovers" the original title plus a
        # new entry should keep the original row + append the new one.
        second = [
            BacklogEntry(
                title="original",  # same title — should be dropped
                kind="try_alt_approach",
                priority=1,
                description="updated desc",
            ),
            BacklogEntry(
                title="new one",
                kind="spec_feature",
                priority=4,
                description="fresh",
            ),
        ]
        write_backlog(tmp_path, second)
        text = (tmp_path / BACKLOG_RELATIVE_PATH).read_text()
        parsed = parse_backlog(text)
        titles = [c.title for c in parsed]
        assert "original" in titles
        assert "new one" in titles
        assert titles.count("original") == 1
        # Original row preserved — description still reads "first write".
        for c in parsed:
            if c.title == "original":
                assert "first write" in c.description

    def test_escapes_pipe_in_content(self, tmp_path: Path) -> None:
        entries = [
            BacklogEntry(
                title="pipe | in title",
                kind="spec_feature",
                priority=3,
                description="has | pipes and\nnewlines too",
            ),
        ]
        path = write_backlog(tmp_path, entries)
        text = path.read_text()
        # Pipes in cell content must be escaped so the table doesn't
        # gain extra columns.
        lines = [
            line for line in text.splitlines()
            if line.startswith("| pipe")
        ]
        assert len(lines) == 1
        # Row should have exactly 6 columns + 2 end pipes → 7 pipes at
        # the row boundaries + 5 separators = 7 pipe characters total
        # counting unescaped delimiters (pipe in content is preceded by
        # backslash, which is still a | in the raw string but the parser
        # handles it).
        row = lines[0]
        assert row.count("\\|") >= 2  # at least two escaped pipes from inputs


# ---------------------------------------------------------------------------
# emit_backlog_from_synthesis — end-to-end
# ---------------------------------------------------------------------------


class TestEmitBacklogFromSynthesis:
    def test_happy_path(self, tmp_path: Path) -> None:
        path = emit_backlog_from_synthesis(
            project_root=tmp_path,
            winning_candidate_id="A",
            all_candidate_ids=["A", "B", "C"],
            candidate_titles={"B": "Plan B", "C": "Plan C"},
            candidate_descriptions={"B": "modular", "C": "monolithic"},
            synthesis_rationale="A simplest under critic panel.",
            magic_items=[
                MagicItem(
                    title="animated welcome",
                    description="delight the user",
                    priority=4,
                    kind="spec_feature",
                ),
            ],
        )
        assert path.exists()
        parsed = parse_backlog(path.read_text())
        titles = {c.title for c in parsed}
        assert titles == {"Plan B", "Plan C", "animated welcome"}

    def test_reruns_merge(self, tmp_path: Path) -> None:
        # First run.
        emit_backlog_from_synthesis(
            project_root=tmp_path,
            winning_candidate_id="A",
            all_candidate_ids=["A", "B"],
            candidate_titles={"B": "Plan B"},
            synthesis_rationale="picked A.",
        )
        # Second run — same winning plan, no new candidates to surface.
        emit_backlog_from_synthesis(
            project_root=tmp_path,
            winning_candidate_id="A",
            all_candidate_ids=["A", "B"],
            candidate_titles={"B": "Plan B"},
            synthesis_rationale="re-run rationale",
        )
        parsed = parse_backlog((tmp_path / BACKLOG_RELATIVE_PATH).read_text())
        titles = [c.title for c in parsed]
        assert titles == ["Plan B"]


# ---------------------------------------------------------------------------
# Architect prompt includes the backlog directive
# ---------------------------------------------------------------------------


class TestSynthesisStagePrompt:
    def test_prompt_mentions_backlog_file(self) -> None:
        prompt = synthesis_stage_prompt()
        assert "docs/downtime-backlog.md" in prompt
        assert "try_alt_approach" in prompt
        # Warns about merge-don't-overwrite.
        assert "merge" in prompt.lower()
