"""Tests for advisor session context packing + output parsing (ad03).

Covers:

* Context pack structure: plan + delta + trajectory sections all
  present; missing plan degrades gracefully.
* Truncation: context pack stays within MAX_CONTEXT_CHARS even for
  oversized inputs.
* Trajectory surface includes last 3 advisor decisions + recent
  dismissals.
* Output parser: valid emit JSON, valid silent JSON, JSON wrapped in
  markdown fences, malformed JSON, missing-field emit → silent with
  reason, unknown topic coerced to "other".
* run_assess writes .pollypm-advisor-context.md into the project
  worktree.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.plugins_builtin.advisor.handlers.assess import (
    CONTEXT_FILENAME,
    MAX_CONTEXT_CHARS,
    AdvisorDecision,
    ContextPack,
    _extract_first_json_object,
    build_context_pack,
    parse_decision,
    run_assess,
    write_context_file,
)
from pollypm.plugins_builtin.advisor.handlers.detect_changes import (
    ChangeReport,
    TaskTransitionRecord,
)
from pollypm.plugins_builtin.advisor.handlers.history_log import (
    HistoryEntry,
    append_decision,
    append_log_entry,
)
from pollypm.plugins_builtin.advisor.state import (
    AdvisorState,
    Dismissal,
    save_state,
)


# ---------------------------------------------------------------------------
# Context pack
# ---------------------------------------------------------------------------


class TestBuildContextPack:
    def test_missing_plan_degrades_gracefully(self, tmp_path: Path) -> None:
        project_path = tmp_path / "proj"
        project_path.mkdir()
        base_dir = tmp_path / "state"
        base_dir.mkdir()
        report = ChangeReport(project_path=project_path, since=None)
        pack = build_context_pack(
            project_key="proj",
            project_path=project_path,
            report=report,
            base_dir=base_dir,
            include_diffs=False,
        )
        assert isinstance(pack, ContextPack)
        assert "no docs/project-plan.md" in pack.plan_text
        assert "## Delta since last advisor run" in pack.markdown
        assert "trajectory" in pack.markdown.lower()

    def test_plan_present_is_included(self, tmp_path: Path) -> None:
        project_path = tmp_path / "proj"
        (project_path / "docs").mkdir(parents=True)
        (project_path / "docs" / "project-plan.md").write_text(
            "# Plan\n\nPluginify everything."
        )
        base_dir = tmp_path / "state"
        base_dir.mkdir()
        report = ChangeReport(project_path=project_path, since=None)
        pack = build_context_pack(
            project_key="proj",
            project_path=project_path,
            report=report,
            base_dir=base_dir,
            include_diffs=False,
        )
        assert "Pluginify everything." in pack.plan_text
        assert "Pluginify everything." in pack.markdown

    def test_plan_truncation(self, tmp_path: Path) -> None:
        project_path = tmp_path / "proj"
        (project_path / "docs").mkdir(parents=True)
        big = "x" * 20_000
        (project_path / "docs" / "project-plan.md").write_text(big)
        base_dir = tmp_path / "state"
        base_dir.mkdir()
        pack = build_context_pack(
            project_key="proj",
            project_path=project_path,
            report=ChangeReport(project_path=project_path, since=None),
            base_dir=base_dir,
            include_diffs=False,
        )
        assert "[plan truncated" in pack.plan_text

    def test_delta_renders_commits_files_transitions(self, tmp_path: Path) -> None:
        project_path = tmp_path / "proj"
        project_path.mkdir()
        base_dir = tmp_path / "state"
        base_dir.mkdir()
        report = ChangeReport(
            project_path=project_path,
            since=datetime(2026, 4, 16, 12, 0, tzinfo=UTC),
            commit_shas=["abc1234", "def5678"],
            changed_files=[Path("src/x.py"), Path("tests/test_x.py")],
            task_transitions=[
                TaskTransitionRecord(
                    project="proj",
                    task_number=42,
                    task_title="do thing",
                    from_state="queued",
                    to_state="in_progress",
                    actor="worker",
                    timestamp="2026-04-16T12:01:00+00:00",
                )
            ],
            files_diff_summary="2 commits, 2 files changed, 1 task transitions",
        )
        pack = build_context_pack(
            project_key="proj",
            project_path=project_path,
            report=report,
            base_dir=base_dir,
            include_diffs=False,
        )
        assert "abc1234" in pack.delta_section
        assert "src/x.py" in pack.delta_section
        assert "proj/42" in pack.delta_section
        assert "queued -> in_progress" in pack.delta_section

    def test_trajectory_surfaces_prior_entries(self, tmp_path: Path) -> None:
        project_path = tmp_path / "proj"
        project_path.mkdir()
        base_dir = tmp_path / "state"
        base_dir.mkdir()

        # Prior history: one emit, one silent.
        append_log_entry(
            base_dir,
            HistoryEntry(
                timestamp="2026-04-15T09:00:00+00:00",
                project="proj",
                decision="emit",
                topic="architecture_drift",
                severity="recommendation",
                summary="Cockpit going monolithic",
            ),
        )
        append_log_entry(
            base_dir,
            HistoryEntry(
                timestamp="2026-04-16T09:00:00+00:00",
                project="proj",
                decision="silent",
                rationale_if_silent="Test-coverage pass, on-plan.",
            ),
        )

        pack = build_context_pack(
            project_key="proj",
            project_path=project_path,
            report=ChangeReport(project_path=project_path, since=None),
            base_dir=base_dir,
            include_diffs=False,
        )
        assert "Cockpit going monolithic" in pack.trajectory_section
        assert "Test-coverage pass" in pack.trajectory_section

    def test_trajectory_surfaces_recent_dismissals(self, tmp_path: Path) -> None:
        project_path = tmp_path / "proj"
        project_path.mkdir()
        base_dir = tmp_path / "state"
        base_dir.mkdir()
        state = AdvisorState()
        proj = state.get("proj")
        proj.recent_dismissals.append(
            Dismissal(topic="missing_tests", at="2026-04-15T10:00:00+00:00")
        )
        save_state(base_dir, state)

        pack = build_context_pack(
            project_key="proj",
            project_path=project_path,
            report=ChangeReport(project_path=project_path, since=None),
            base_dir=base_dir,
            include_diffs=False,
        )
        assert "missing_tests" in pack.trajectory_section
        assert "topic_cooldown" in pack.trajectory_section or "dismissals" in pack.trajectory_section.lower()

    def test_context_within_20k_chars_even_with_huge_inputs(
        self, tmp_path: Path
    ) -> None:
        project_path = tmp_path / "proj"
        (project_path / "docs").mkdir(parents=True)
        (project_path / "docs" / "project-plan.md").write_text("x" * 50_000)
        base_dir = tmp_path / "state"
        base_dir.mkdir()
        big_report = ChangeReport(
            project_path=project_path,
            since=None,
            commit_shas=[f"sha{i:04d}" for i in range(100)],
            changed_files=[Path(f"file_{i}.py") for i in range(200)],
            task_transitions=[
                TaskTransitionRecord(
                    project="proj",
                    task_number=i,
                    task_title="x" * 50,
                    from_state="queued",
                    to_state="in_progress",
                    actor="worker",
                    timestamp="2026-04-16T12:00:00+00:00",
                )
                for i in range(50)
            ],
        )
        pack = build_context_pack(
            project_key="proj",
            project_path=project_path,
            report=big_report,
            base_dir=base_dir,
            include_diffs=False,
        )
        assert len(pack.markdown) <= MAX_CONTEXT_CHARS


# ---------------------------------------------------------------------------
# write + run_assess
# ---------------------------------------------------------------------------


class TestWriteContext:
    def test_write_creates_file(self, tmp_path: Path) -> None:
        project_path = tmp_path / "proj"
        project_path.mkdir()
        base_dir = tmp_path / "state"
        base_dir.mkdir()
        pack = build_context_pack(
            project_key="proj",
            project_path=project_path,
            report=ChangeReport(project_path=project_path, since=None),
            base_dir=base_dir,
            include_diffs=False,
        )
        path = write_context_file(project_path, pack)
        assert path.exists()
        assert path.name == CONTEXT_FILENAME
        assert "Advisor context pack" in path.read_text()

    def test_run_assess_returns_summary(self, tmp_path: Path) -> None:
        project_path = tmp_path / "proj"
        project_path.mkdir()
        base_dir = tmp_path / "state"
        base_dir.mkdir()
        report = ChangeReport(
            project_path=project_path,
            since=None,
            commit_shas=["abc1234"],
            changed_files=[Path("a.py")],
        )
        result = run_assess(
            project_key="proj",
            project_path=project_path,
            report=report,
            base_dir=base_dir,
        )
        assert "context_path" in result
        assert Path(result["context_path"]).exists()
        assert result["commit_count"] == 1
        assert result["file_count"] == 1


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


class TestExtractJSON:
    def test_bare_object(self) -> None:
        assert _extract_first_json_object('{"a": 1}') == '{"a": 1}'

    def test_with_prose_around(self) -> None:
        text = "Here is my decision:\n\n{\"emit\": false, \"rationale_if_silent\": \"x\"}\n\nThanks."
        assert _extract_first_json_object(text) is not None

    def test_with_code_fence(self) -> None:
        text = '```json\n{"emit": false, "rationale_if_silent": "x"}\n```'
        got = _extract_first_json_object(text)
        assert got is not None
        assert '"emit": false' in got

    def test_handles_nested_braces(self) -> None:
        text = '{"a": {"b": 1}, "c": 2}'
        got = _extract_first_json_object(text)
        assert got == text

    def test_handles_strings_with_braces(self) -> None:
        text = '{"msg": "see {this}", "n": 1}'
        got = _extract_first_json_object(text)
        assert got == text

    def test_no_object_returns_none(self) -> None:
        assert _extract_first_json_object("just prose") is None


class TestParseDecision:
    def test_valid_emit(self) -> None:
        payload = (
            '{"emit": true, "topic": "architecture_drift", '
            '"severity": "recommendation", '
            '"summary": "Cockpit is going monolithic.", '
            '"details": "Several commits landed panels directly in cockpit.py.", '
            '"suggestion": "Extract the largest panels to their own plugins."}'
        )
        d = parse_decision(payload)
        assert d.emit is True
        assert d.topic == "architecture_drift"
        assert d.severity == "recommendation"
        assert "Cockpit" in d.summary
        assert d.invalid_output_reason is None

    def test_valid_silent(self) -> None:
        payload = '{"emit": false, "rationale_if_silent": "Test-coverage pass, on-plan."}'
        d = parse_decision(payload)
        assert d.emit is False
        assert "on-plan" in d.rationale_if_silent
        assert d.invalid_output_reason is None

    def test_emit_with_prose_around(self) -> None:
        text = (
            "Here is my decision:\n\n"
            '{"emit": false, "rationale_if_silent": "progressing reasonably"}'
            "\n\nDone."
        )
        d = parse_decision(text)
        assert d.emit is False
        assert "progressing" in d.rationale_if_silent

    def test_emit_in_code_fence(self) -> None:
        text = (
            "```json\n"
            '{"emit": true, "topic": "missing_tests", "severity": "recommendation", '
            '"summary": "Module x has no tests.", "details": "see commit abc", "suggestion": "add tests"}'
            "\n```"
        )
        d = parse_decision(text)
        assert d.emit is True
        assert d.topic == "missing_tests"

    def test_empty_output_is_silent(self) -> None:
        d = parse_decision("")
        assert d.emit is False
        assert d.invalid_output_reason == "empty"
        assert "empty" in d.rationale_if_silent.lower()

    def test_none_output_is_silent(self) -> None:
        d = parse_decision(None)
        assert d.emit is False
        assert d.invalid_output_reason == "empty"

    def test_non_json_output_is_silent(self) -> None:
        d = parse_decision("just some prose — no JSON here at all")
        assert d.emit is False
        assert d.invalid_output_reason == "no-json"

    def test_malformed_json_is_silent(self) -> None:
        d = parse_decision('{"emit": tru}')
        assert d.emit is False
        assert d.invalid_output_reason.startswith("json-decode")

    def test_non_object_json_is_silent(self) -> None:
        d = parse_decision('[1, 2, 3]')
        assert d.emit is False

    def test_emit_non_bool_is_silent(self) -> None:
        d = parse_decision('{"emit": "yes"}')
        assert d.emit is False
        assert d.invalid_output_reason == "emit-not-bool"

    def test_emit_missing_fields_degrades_to_silent(self) -> None:
        payload = '{"emit": true, "topic": "architecture_drift"}'
        d = parse_decision(payload)
        assert d.emit is False
        assert d.invalid_output_reason.startswith("emit-missing")
        assert "severity" in d.invalid_output_reason
        assert "summary" in d.invalid_output_reason

    def test_emit_invalid_severity_degrades_to_silent(self) -> None:
        payload = (
            '{"emit": true, "topic": "architecture_drift", '
            '"severity": "mild", "summary": "s", "details": "d", "suggestion": "x"}'
        )
        d = parse_decision(payload)
        assert d.emit is False
        assert "severity" in d.invalid_output_reason

    def test_unknown_topic_coerced_to_other(self) -> None:
        payload = (
            '{"emit": true, "topic": "weather_anomaly", '
            '"severity": "suggestion", "summary": "s", "details": "d", "suggestion": "x"}'
        )
        d = parse_decision(payload)
        assert d.emit is True
        assert d.topic == "other"

    def test_silent_without_rationale_is_invalid(self) -> None:
        d = parse_decision('{"emit": false}')
        assert d.emit is False
        assert d.invalid_output_reason == "silent-no-rationale"

    def test_decision_roundtrip_to_dict(self) -> None:
        d = parse_decision(
            '{"emit": true, "topic": "architecture_drift", "severity": "critical", '
            '"summary": "s", "details": "d", "suggestion": "x"}'
        )
        out = d.to_dict()
        assert out["emit"] is True
        assert out["severity"] == "critical"
