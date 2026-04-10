"""Unit tests for the history import pipeline."""

import json
import os
from pathlib import Path

import pytest

from pollypm.history_import import (
    DiscoveredSources,
    ExtractedUnderstanding,
    ImportResult,
    TimelineEntry,
    build_interview_questions,
    build_timeline,
    copy_provider_transcripts,
    discover_sources,
    extract_understanding,
    generate_docs,
    load_import_state,
    lock_import,
    save_import_state,
    _dedupe,
    _heuristic_understanding,
    _jsonl_file_to_timeline,
    _doc_file_to_timeline,
    _config_file_to_timeline,
)


# ---------------------------------------------------------------------------
# TimelineEntry
# ---------------------------------------------------------------------------


class TestTimelineEntry:
    def test_sort_key(self) -> None:
        entry = TimelineEntry(
            timestamp="2026-01-01T00:00:00Z",
            source_type="git_commit",
            summary="Initial commit",
        )
        assert entry.sort_key() == ("2026-01-01T00:00:00Z", "git_commit")

    def test_details_default_empty(self) -> None:
        entry = TimelineEntry(
            timestamp="2026-01-01T00:00:00Z",
            source_type="jsonl_event",
            summary="test",
        )
        assert entry.details == {}


# ---------------------------------------------------------------------------
# DiscoveredSources
# ---------------------------------------------------------------------------


class TestDiscoveredSources:
    def test_total_sources_empty(self) -> None:
        sources = DiscoveredSources()
        assert sources.total_sources() == 0

    def test_total_sources_with_git(self) -> None:
        sources = DiscoveredSources(git_available=True)
        assert sources.total_sources() == 1

    def test_total_sources_all_types(self) -> None:
        sources = DiscoveredSources(
            jsonl_files=[Path("a.jsonl"), Path("b.jsonl")],
            git_available=True,
            doc_files=[Path("README.md")],
            config_files=[Path("pyproject.toml")],
        )
        assert sources.total_sources() == 5


# ---------------------------------------------------------------------------
# Stage 1: Discover Sources
# ---------------------------------------------------------------------------


class TestDiscoverSources:
    def test_discovers_jsonl_in_transcripts_dir(self, tmp_path: Path) -> None:
        transcripts = tmp_path / ".pollypm" / "transcripts" / "session-a"
        transcripts.mkdir(parents=True)
        (transcripts / "events.jsonl").write_text('{"test": true}\n')

        sources = discover_sources(tmp_path)
        assert len(sources.jsonl_files) == 1
        assert sources.jsonl_files[0].name == "events.jsonl"

    def test_discovers_provider_transcript_dirs(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "transcript.jsonl").write_text('{"test": true}\n')

        sources = discover_sources(tmp_path)
        assert len(sources.provider_transcript_dirs) == 1
        assert sources.provider_transcript_dirs[0].name == ".claude"
        assert any(f.name == "transcript.jsonl" for f in sources.jsonl_files)

    def test_discovers_doc_files(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# Hello")

        sources = discover_sources(tmp_path)
        assert len(sources.doc_files) == 1
        assert sources.doc_files[0].name == "README.md"

    def test_discovers_config_files(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")

        sources = discover_sources(tmp_path)
        assert len(sources.config_files) == 1
        assert sources.config_files[0].name == "pyproject.toml"

    def test_no_git_when_missing(self, tmp_path: Path) -> None:
        sources = discover_sources(tmp_path)
        assert not sources.git_available
        assert sources.git_commit_count == 0

    def test_empty_project(self, tmp_path: Path) -> None:
        sources = discover_sources(tmp_path)
        assert sources.total_sources() == 0

    def test_discovers_multiple_config_files(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "Makefile").write_text("all:\n\techo hi")

        sources = discover_sources(tmp_path)
        assert len(sources.config_files) == 2


# ---------------------------------------------------------------------------
# Stage 2: Build Timeline (JSONL parsing)
# ---------------------------------------------------------------------------


class TestJsonlToTimeline:
    def test_parses_events(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "events.jsonl"
        jsonl.write_text(
            json.dumps({
                "timestamp": "2026-01-01T00:00:00Z",
                "event_type": "user_turn",
                "payload": {"text": "Hello world"},
            })
            + "\n"
        )

        entries = _jsonl_file_to_timeline(jsonl)
        assert len(entries) == 1
        assert entries[0].source_type == "jsonl_event"
        assert "Hello world" in entries[0].summary

    def test_skips_invalid_json(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "events.jsonl"
        jsonl.write_text("not json\n" + json.dumps({"timestamp": "2026-01-01T00:00:00Z", "event_type": "test"}) + "\n")

        entries = _jsonl_file_to_timeline(jsonl)
        assert len(entries) == 1

    def test_skips_entries_without_timestamp(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "events.jsonl"
        jsonl.write_text(json.dumps({"event_type": "user_turn"}) + "\n")

        entries = _jsonl_file_to_timeline(jsonl)
        assert len(entries) == 0

    def test_handles_missing_file(self, tmp_path: Path) -> None:
        entries = _jsonl_file_to_timeline(tmp_path / "missing.jsonl")
        assert entries == []


class TestDocFileToTimeline:
    def test_creates_entry(self, tmp_path: Path) -> None:
        doc = tmp_path / "README.md"
        doc.write_text("# My Project\nSome content.")

        entries = _doc_file_to_timeline(doc, tmp_path)
        assert len(entries) == 1
        assert entries[0].source_type == "doc_file"
        assert "README.md" in entries[0].summary


class TestConfigFileToTimeline:
    def test_creates_entry(self, tmp_path: Path) -> None:
        cfg = tmp_path / "pyproject.toml"
        cfg.write_text("[project]\nname = 'test'\n")

        entries = _config_file_to_timeline(cfg, tmp_path)
        assert len(entries) == 1
        assert entries[0].source_type == "config_file"
        assert "pyproject.toml" in entries[0].summary


class TestBuildTimeline:
    def test_sorts_chronologically(self, tmp_path: Path) -> None:
        transcripts = tmp_path / ".pollypm" / "transcripts" / "s1"
        transcripts.mkdir(parents=True)
        (transcripts / "events.jsonl").write_text(
            json.dumps({"timestamp": "2026-03-01T00:00:00Z", "event_type": "user_turn", "payload": {"text": "later"}})
            + "\n"
            + json.dumps({"timestamp": "2026-01-01T00:00:00Z", "event_type": "user_turn", "payload": {"text": "earlier"}})
            + "\n"
        )

        sources = DiscoveredSources(
            jsonl_files=[transcripts / "events.jsonl"],
        )
        timeline = build_timeline(tmp_path, sources)
        assert len(timeline) == 2
        assert "earlier" in timeline[0].summary
        assert "later" in timeline[1].summary


# ---------------------------------------------------------------------------
# Stage 3: Extract Understanding (heuristic)
# ---------------------------------------------------------------------------


class TestHeuristicUnderstanding:
    def test_extracts_decisions_from_jsonl(self) -> None:
        timeline = [
            TimelineEntry(
                timestamp="2026-01-01T00:00:00Z",
                source_type="jsonl_event",
                summary="[assistant_turn] Decision: we will use SQLite for state storage",
                details={"event_type": "assistant_turn"},
            ),
        ]
        understanding = _heuristic_understanding(timeline, "TestProject")
        assert len(understanding.decisions) == 1
        assert "SQLite" in understanding.decisions[0]

    def test_extracts_architecture_from_jsonl(self) -> None:
        timeline = [
            TimelineEntry(
                timestamp="2026-01-01T00:00:00Z",
                source_type="jsonl_event",
                summary="[assistant_turn] Architecture: split into pipeline stages",
                details={"event_type": "assistant_turn"},
            ),
        ]
        understanding = _heuristic_understanding(timeline, "TestProject")
        assert len(understanding.architecture) >= 1

    def test_extracts_history_from_git(self) -> None:
        timeline = [
            TimelineEntry(
                timestamp="2026-01-01T00:00:00Z",
                source_type="git_commit",
                summary="Initial commit",
                details={"hash": "abc123"},
            ),
            TimelineEntry(
                timestamp="2026-01-02T00:00:00Z",
                source_type="git_commit",
                summary="Add tests",
                details={"hash": "def456"},
            ),
        ]
        understanding = _heuristic_understanding(timeline, "TestProject")
        assert len(understanding.history) == 2
        assert "abc123" in understanding.history[0]

    def test_overview_includes_counts(self) -> None:
        timeline = [
            TimelineEntry(
                timestamp="2026-01-01T00:00:00Z",
                source_type="git_commit",
                summary="Initial commit",
                details={"hash": "abc123"},
            ),
        ]
        understanding = _heuristic_understanding(timeline, "TestProject")
        assert "1 commit" in understanding.overview

    def test_empty_timeline(self) -> None:
        understanding = _heuristic_understanding([], "Empty")
        assert understanding.project_name == "Empty"
        assert understanding.overview == "Empty project."

    def test_extracts_conventions(self) -> None:
        timeline = [
            TimelineEntry(
                timestamp="2026-01-01T00:00:00Z",
                source_type="jsonl_event",
                summary="[assistant_turn] Convention: always use snake_case for variables",
                details={"event_type": "assistant_turn"},
            ),
        ]
        understanding = _heuristic_understanding(timeline, "TestProject")
        assert len(understanding.conventions) == 1

    def test_extracts_goals(self) -> None:
        timeline = [
            TimelineEntry(
                timestamp="2026-01-01T00:00:00Z",
                source_type="jsonl_event",
                summary="[user_turn] Goal: ship v1 by end of quarter",
                details={"event_type": "user_turn"},
            ),
        ]
        understanding = _heuristic_understanding(timeline, "TestProject")
        assert len(understanding.goals) == 1

    def test_architecture_from_config(self) -> None:
        timeline = [
            TimelineEntry(
                timestamp="2026-01-01T00:00:00Z",
                source_type="config_file",
                summary="Config: pyproject.toml",
                details={"path": "pyproject.toml"},
            ),
        ]
        understanding = _heuristic_understanding(timeline, "TestProject")
        assert any("pyproject.toml" in item for item in understanding.architecture)

    def test_deduplicates(self) -> None:
        timeline = [
            TimelineEntry(
                timestamp="2026-01-01T00:00:00Z",
                source_type="jsonl_event",
                summary="[assistant_turn] Decision: use SQLite",
                details={},
            ),
            TimelineEntry(
                timestamp="2026-01-02T00:00:00Z",
                source_type="jsonl_event",
                summary="[assistant_turn] Decision: use SQLite",
                details={},
            ),
        ]
        understanding = _heuristic_understanding(timeline, "TestProject")
        assert len(understanding.decisions) == 1


# ---------------------------------------------------------------------------
# Stage 4: Generate Documentation
# ---------------------------------------------------------------------------


class TestGenerateDocs:
    def test_generates_all_five_docs(self, tmp_path: Path) -> None:
        understanding = ExtractedUnderstanding(
            project_name="TestProject",
            overview="A test project for validating the import pipeline.",
            decisions=["Use SQLite for state storage"],
            architecture=["Pipeline-based architecture"],
            history=["Initial commit", "Added tests"],
            conventions=["Use snake_case everywhere"],
            goals=["Ship v1"],
        )

        count = generate_docs(tmp_path, understanding, timestamp="2026-04-10T00:00:00Z")
        assert count == 5

        docs_dir = tmp_path / "docs"
        assert (docs_dir / "project-overview.md").exists()
        assert (docs_dir / "decisions.md").exists()
        assert (docs_dir / "architecture.md").exists()
        assert (docs_dir / "history.md").exists()
        assert (docs_dir / "conventions.md").exists()

    def test_overview_has_summary_first(self, tmp_path: Path) -> None:
        understanding = ExtractedUnderstanding(
            project_name="TestProject",
            overview="A test project.",
        )
        generate_docs(tmp_path, understanding, timestamp="2026-04-10T00:00:00Z")

        content = (tmp_path / "docs" / "project-overview.md").read_text()
        assert content.index("## Summary") < content.index("A test project.")

    def test_docs_have_timestamps(self, tmp_path: Path) -> None:
        understanding = ExtractedUnderstanding(project_name="TestProject")
        generate_docs(tmp_path, understanding, timestamp="2026-04-10T00:00:00Z")

        for doc_name in ("project-overview.md", "decisions.md", "architecture.md", "history.md", "conventions.md"):
            content = (tmp_path / "docs" / doc_name).read_text()
            assert "2026-04-10T00:00:00Z" in content

    def test_overview_cross_references_other_docs(self, tmp_path: Path) -> None:
        understanding = ExtractedUnderstanding(
            project_name="TestProject",
            decisions=["Use SQLite"],
            architecture=["Pipeline design"],
            conventions=["snake_case"],
        )
        generate_docs(tmp_path, understanding, timestamp="2026-04-10T00:00:00Z")

        content = (tmp_path / "docs" / "project-overview.md").read_text()
        assert "decisions.md" in content
        assert "architecture.md" in content
        assert "conventions.md" in content

    def test_empty_sections_show_placeholder(self, tmp_path: Path) -> None:
        understanding = ExtractedUnderstanding(project_name="TestProject")
        generate_docs(tmp_path, understanding, timestamp="2026-04-10T00:00:00Z")

        content = (tmp_path / "docs" / "decisions.md").read_text()
        assert "None recorded yet." in content

    def test_no_secrets_in_output(self, tmp_path: Path) -> None:
        understanding = ExtractedUnderstanding(
            project_name="TestProject",
            overview="Token: ghp_aabbccddeeff00112233",
            decisions=["Use key aabbccddeeff00112233445566778899aabbccdd"],
        )
        generate_docs(tmp_path, understanding, timestamp="2026-04-10T00:00:00Z")

        for doc_name in ("project-overview.md", "decisions.md"):
            content = (tmp_path / "docs" / doc_name).read_text()
            assert "ghp_" not in content
            assert "aabbccddeeff00112233445566778899aabbccdd" not in content


# ---------------------------------------------------------------------------
# Stage 5: User Interview
# ---------------------------------------------------------------------------


class TestBuildInterviewQuestions:
    def test_always_asks_about_overview(self) -> None:
        understanding = ExtractedUnderstanding(
            project_name="TestProject",
            overview="A test project.",
        )
        questions = build_interview_questions(understanding)
        assert len(questions) >= 1
        assert "overview" in questions[0].lower() or "accurate" in questions[0].lower()

    def test_asks_about_missing_decisions(self) -> None:
        understanding = ExtractedUnderstanding(
            project_name="TestProject",
            overview="A test project.",
            decisions=[],
        )
        questions = build_interview_questions(understanding)
        assert any("decision" in q.lower() for q in questions)

    def test_asks_about_missing_conventions(self) -> None:
        understanding = ExtractedUnderstanding(
            project_name="TestProject",
            overview="A test project.",
            conventions=[],
        )
        questions = build_interview_questions(understanding)
        assert any("convention" in q.lower() for q in questions)

    def test_asks_about_missing_goals(self) -> None:
        understanding = ExtractedUnderstanding(
            project_name="TestProject",
            overview="A test project.",
            goals=[],
        )
        questions = build_interview_questions(understanding)
        assert any("goal" in q.lower() for q in questions)

    def test_includes_open_questions(self) -> None:
        understanding = ExtractedUnderstanding(
            project_name="TestProject",
            overview="A test project.",
            open_questions=["Why was Redis removed?"],
        )
        questions = build_interview_questions(understanding)
        assert "Why was Redis removed?" in questions

    def test_no_missing_sections_no_extra_questions(self) -> None:
        understanding = ExtractedUnderstanding(
            project_name="TestProject",
            overview="A test project.",
            decisions=["Use SQLite"],
            conventions=["snake_case"],
            goals=["Ship v1"],
        )
        questions = build_interview_questions(understanding)
        # Only the overview question
        assert len(questions) == 1


# ---------------------------------------------------------------------------
# Copy provider transcripts
# ---------------------------------------------------------------------------


class TestCopyProviderTranscripts:
    def test_copies_provider_transcripts(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "session.jsonl").write_text('{"test": true}\n')

        sources = DiscoveredSources(
            provider_transcript_dirs=[claude_dir],
        )

        copied = copy_provider_transcripts(tmp_path, sources)
        assert copied == 1

        dest = tmp_path / ".pollypm" / "transcripts" / "imported-claude" / "session.jsonl"
        assert dest.exists()

    def test_does_not_overwrite_existing(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "session.jsonl").write_text('{"test": true}\n')

        # Pre-create destination
        dest = tmp_path / ".pollypm" / "transcripts" / "imported-claude"
        dest.mkdir(parents=True)
        (dest / "session.jsonl").write_text('{"existing": true}\n')

        sources = DiscoveredSources(
            provider_transcript_dirs=[claude_dir],
        )

        copied = copy_provider_transcripts(tmp_path, sources)
        assert copied == 0
        assert '{"existing": true}' in (dest / "session.jsonl").read_text()

    def test_no_provider_dirs(self, tmp_path: Path) -> None:
        sources = DiscoveredSources()
        copied = copy_provider_transcripts(tmp_path, sources)
        assert copied == 0


# ---------------------------------------------------------------------------
# Import state (checkpoint)
# ---------------------------------------------------------------------------


class TestImportState:
    def test_save_and_load(self, tmp_path: Path) -> None:
        state = {"status": "locked", "docs_generated": 5}
        save_import_state(tmp_path, state)

        loaded = load_import_state(tmp_path)
        assert loaded["status"] == "locked"
        assert loaded["docs_generated"] == 5

    def test_load_missing(self, tmp_path: Path) -> None:
        loaded = load_import_state(tmp_path)
        assert loaded == {}

    def test_lock_import(self, tmp_path: Path) -> None:
        save_import_state(tmp_path, {"status": "pending_review"})
        lock_import(tmp_path)
        loaded = load_import_state(tmp_path)
        assert loaded["status"] == "locked"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestDedupe:
    def test_deduplicates(self) -> None:
        assert _dedupe(["a", "b", "a", "c"]) == ["a", "b", "c"]

    def test_strips_whitespace(self) -> None:
        assert _dedupe(["  a  ", "a"]) == ["a"]

    def test_removes_empty(self) -> None:
        assert _dedupe(["", "a", "", "b"]) == ["a", "b"]

    def test_preserves_order(self) -> None:
        assert _dedupe(["c", "a", "b"]) == ["c", "a", "b"]
