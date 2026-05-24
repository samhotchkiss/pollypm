from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from scripts.evals.run import (
    EvalCase,
    apply_assertions,
    capture_model_versions,
    load_cases,
    render_html_report,
    render_markdown_report,
    resolve_session_name,
    run_cases,
)


def test_load_cases_from_manifest_and_resolve_session(tmp_path: Path) -> None:
    case_path = tmp_path / "architect.yaml"
    case_path.write_text(
        """
id: architect-planning
role: architect
session_template: architect_<project>
prompt: Give me 3 candidates.
assertions:
  must_contain_at_least:
    - candidates_count: 3
""".strip(),
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """
defaults:
  timeout_seconds: 12
cases:
  - architect.yaml
""".strip(),
        encoding="utf-8",
    )

    cases = load_cases(manifest=manifest)

    assert len(cases) == 1
    assert cases[0].timeout_seconds == 12
    assert resolve_session_name(cases[0].session_template, project="pollypm") == (
        "architect_pollypm"
    )


def test_assertion_engine_covers_structural_regex_keyword_and_category() -> None:
    case = EvalCase(
        id="architect-planning",
        role="architect",
        session_template="architect_<project>",
        prompt="",
        assertions={
            "must_contain_at_least": [{"candidates_count": 3}],
            "must_match_regex": [r"(?i)next sprint"],
            "must_not_match_regex": [r"(?i)i am happy to help"],
            "must_contain_keywords": ["public chat API"],
            "response_category": "planning",
        },
    )
    response = (
        "Next sprint priority candidates:\n"
        "1. Public chat API reliability because evals need stable messages.\n"
        "2. Task lifecycle visibility because operators need clear states.\n"
        "3. Smoke automation because releases need repeatable checks."
    )

    assert apply_assertions(case, response) == []


def test_assertion_engine_reports_failures() -> None:
    case = EvalCase(
        id="bad",
        role="operator",
        session_template="operator",
        prompt="",
        assertions={
            "must_contain_at_least": [{"bullets_count": 2}],
            "must_not_match_regex": [r"boilerplate"],
            "response_category": "refusal",
        },
    )

    failures = apply_assertions(case, "boilerplate answer")

    assert [failure.assertion for failure in failures] == [
        "bullets_count",
        "must_not_match_regex",
        "response_category",
    ]


def test_dry_run_uses_canned_response_and_report_includes_model_versions(
    tmp_path: Path,
) -> None:
    case = EvalCase(
        id="operator-routing",
        role="operator",
        session_template="operator",
        prompt="",
        assertions={
            "must_match_regex": [r"(?i)pause"],
            "response_category": "action",
        },
    )
    config = tmp_path / "pollypm.toml"
    config.write_text(
        """
[sessions.operator]
role = "operator-pm"
provider = "claude"
model = "claude-opus-4-1"
""".strip(),
        encoding="utf-8",
    )

    results = run_cases([case], project="pollypm", dry_run=True)
    report = render_markdown_report(
        results,
        model_versions=capture_model_versions(config),
        generated_at=datetime(2026, 5, 23, tzinfo=UTC),
    )

    assert results[0].passed
    assert "operator-pm: claude/claude-opus-4-1" in report
    assert "| operator-routing | operator | `operator` | PASS |" in report


def test_html_report_escapes_failure_text() -> None:
    case = EvalCase(
        id="html",
        role="advisor",
        session_template="advisor_<project>",
        prompt="",
        assertions={"must_match_regex": ["<risk>"]},
    )
    result = run_cases(
        [case],
        project="pollypm",
        dry_run=True,
        canned={"html": "no matching text"},
    )[0]

    html = render_html_report(
        [result],
        model_versions={"advisor_pollypm": "advisor: claude/default"},
        generated_at=datetime(2026, 5, 23, tzinfo=UTC),
    )

    assert "&lt;risk&gt;" in html
    assert "<td>FAIL</td>" in html
