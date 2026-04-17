"""Tests for memory prompt injection (M05 / #234).

Covers the spec acceptance criteria from issue #234:

* A session for a project with seeded user/feedback/project memories
  starts with those memories in its prompt.
* Token budget is respected — no injection exceeds 4K tokens.
* A session for a brand-new project with no memories starts cleanly
  (no empty "What you should know" section).
* Output is deterministic for a fixed (user, project, task) at a fixed
  time — repeated invocations produce byte-identical strings.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.memory_backends import (
    FeedbackMemory,
    FileMemoryBackend,
    PatternMemory,
    ProjectMemory,
    UserMemory,
)
from pollypm.memory_prompts import (
    BUDGET_CHARS,
    INJECTION_HEADING,
    build_memory_injection,
    compute_task_context_summary,
    prepend_memory_injection,
)


# ---------------------------------------------------------------------------
# Seeded fixture — a small corpus with one of each surfaced type
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_backend(tmp_path: Path) -> FileMemoryBackend:
    backend = FileMemoryBackend(tmp_path)
    backend.write_entry(
        UserMemory(
            name="Sam",
            description="Senior engineer, prefers small modules.",
            body="Cares about dependency boundaries.",
            scope="operator",
            scope_tier="user",
            importance=5,
        )
    )
    backend.write_entry(
        FeedbackMemory(
            rule="Never use --no-verify",
            why="Hooks catch test regressions before push.",
            how_to_apply="If a pre-commit hook fails, fix the issue then re-commit.",
            scope="pollypm",
            scope_tier="project",
            importance=5,
        )
    )
    backend.write_entry(
        ProjectMemory(
            fact="Test runner is pytest with --tb=short.",
            why="Agreed convention across the team.",
            how_to_apply="Run uv run python -m pytest --tb=short -q before commit.",
            scope="pollypm",
            scope_tier="project",
            importance=5,
        )
    )
    backend.write_entry(
        PatternMemory(
            when="Cockpit crashes unexpectedly.",
            then="Run pm down && pm up to reset the supervisor.",
            scope="pollypm",
            scope_tier="project",
            importance=4,
        )
    )
    return backend


# ---------------------------------------------------------------------------
# compute_task_context_summary — deterministic string builder
# ---------------------------------------------------------------------------


def test_compute_task_context_summary_prefers_task_fields() -> None:
    summary = compute_task_context_summary(
        task_title="Implement feature X",
        task_description="Ship an extractor.",
        session_role="worker",
        project="pollypm",
    )
    assert summary == "Implement feature X Ship an extractor."


def test_compute_task_context_summary_falls_back_to_role_and_project() -> None:
    summary = compute_task_context_summary(session_role="herald", project="pollypm")
    assert summary == "herald pollypm"


def test_compute_task_context_summary_empty_when_no_inputs() -> None:
    assert compute_task_context_summary() == ""


# ---------------------------------------------------------------------------
# build_memory_injection happy path + structure
# ---------------------------------------------------------------------------


def test_build_memory_injection_surfaces_all_four_sections(seeded_backend: FileMemoryBackend) -> None:
    injection = build_memory_injection(
        seeded_backend,
        user_id="operator",
        project_name="pollypm",
        task_context_summary="testing pytest",
    )
    assert injection, "expected non-empty injection for a seeded store"
    # Heading present
    assert injection.startswith(INJECTION_HEADING)
    # Each surfaced type has its section heading in output
    assert "About the user:" in injection
    assert "Feedback from past sessions:" in injection
    assert "Project facts:" in injection
    assert "Patterns to apply:" in injection


def test_build_memory_injection_contains_salient_fields(seeded_backend: FileMemoryBackend) -> None:
    injection = build_memory_injection(
        seeded_backend,
        user_id="operator",
        project_name="pollypm",
        task_context_summary="testing pytest",
    )
    # Each seeded memory should surface its salient value.
    assert "Sam" in injection
    assert "Never use --no-verify" in injection
    assert "Test runner is pytest" in injection
    assert "When: Cockpit crashes" in injection


# ---------------------------------------------------------------------------
# Empty store → empty injection (no hollow section on a new project)
# ---------------------------------------------------------------------------


def test_build_memory_injection_empty_when_store_is_empty(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    injection = build_memory_injection(
        backend,
        user_id="operator",
        project_name="new-project",
        task_context_summary="brand new",
    )
    assert injection == ""


def test_build_memory_injection_empty_when_no_matching_scope(seeded_backend: FileMemoryBackend) -> None:
    # A different project name should surface user memories (they live
    # under the user tier) but nothing else. The user entry here DOES
    # match because scope is on ("user", "operator"), which is always
    # requested. So let's use a brand-new user to get a true empty.
    injection = build_memory_injection(
        seeded_backend,
        user_id="different-user",
        project_name="different-project",
        task_context_summary="irrelevant",
    )
    assert injection == ""


# ---------------------------------------------------------------------------
# Determinism — same inputs → same output
# ---------------------------------------------------------------------------


def test_build_memory_injection_is_deterministic(seeded_backend: FileMemoryBackend) -> None:
    kwargs = dict(
        user_id="operator",
        project_name="pollypm",
        task_context_summary="testing pytest",
    )
    a = build_memory_injection(seeded_backend, **kwargs)
    b = build_memory_injection(seeded_backend, **kwargs)
    c = build_memory_injection(seeded_backend, **kwargs)
    assert a == b == c


# ---------------------------------------------------------------------------
# Budget cap — oversized corpus is truncated to the budget
# ---------------------------------------------------------------------------


def test_build_memory_injection_respects_budget_cap(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    # Seed 30 large project memories — each body is deliberately long.
    long_why = "x " * 400  # ~800 chars
    for i in range(30):
        backend.write_entry(
            ProjectMemory(
                fact=f"Large fact {i:02d}",
                why=long_why,
                how_to_apply=long_why,
                scope="pollypm",
                scope_tier="project",
                importance=4,
            )
        )
    injection = build_memory_injection(
        backend,
        user_id="operator",
        project_name="pollypm",
        task_context_summary="large corpus test",
        limit=30,
    )
    assert injection, "expected non-empty injection"
    assert len(injection) <= BUDGET_CHARS


def test_build_memory_injection_budget_override_works(seeded_backend: FileMemoryBackend) -> None:
    # Setting a tiny budget forces aggressive dropping but never crashes.
    tiny = build_memory_injection(
        seeded_backend,
        user_id="operator",
        project_name="pollypm",
        task_context_summary="testing pytest",
        budget_chars=400,
    )
    assert len(tiny) <= 400


# ---------------------------------------------------------------------------
# Importance filter — entries below the importance floor are excluded
# ---------------------------------------------------------------------------


def test_build_memory_injection_respects_importance_floor(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    backend.write_entry(
        ProjectMemory(
            fact="Low importance fact",
            why="noise",
            how_to_apply="ignore",
            scope="pollypm",
            scope_tier="project",
            importance=1,  # below default importance_min=3
        )
    )
    injection = build_memory_injection(
        backend,
        user_id="operator",
        project_name="pollypm",
        task_context_summary="anything",
    )
    assert injection == ""


# ---------------------------------------------------------------------------
# prepend_memory_injection — clean composition with existing prompt
# ---------------------------------------------------------------------------


def test_prepend_memory_injection_noop_on_empty_injection() -> None:
    result = prepend_memory_injection("persona prompt body", "")
    assert result == "persona prompt body"


def test_prepend_memory_injection_prepends_with_separator() -> None:
    injection = f"{INJECTION_HEADING}\n\nAbout the user:\n- Sam"
    result = prepend_memory_injection("## Persona\n\nYou are Polly.", injection)
    assert result.startswith(INJECTION_HEADING)
    assert result.endswith("You are Polly.")
    # Blank line between the injection and the caller prompt keeps
    # markdown rendering stable.
    assert "\n\n## Persona" in result


def test_prepend_memory_injection_empty_prompt_returns_injection() -> None:
    injection = f"{INJECTION_HEADING}\n\nAbout the user:\n- Sam"
    assert prepend_memory_injection("", injection) == injection


# ---------------------------------------------------------------------------
# Session injection round-trip — seeded backend through the builder
# ---------------------------------------------------------------------------


def test_full_injection_matches_spec_shape(seeded_backend: FileMemoryBackend) -> None:
    """The rendered section must follow the spec structure exactly:

        ## What you should know

        About the user:
        - <entry>

        Feedback from past sessions:
        - <entry>

        Project facts:
        - <entry>

        Patterns to apply:
        - <entry>

    Order is fixed; blank line between sections.
    """
    injection = build_memory_injection(
        seeded_backend,
        user_id="operator",
        project_name="pollypm",
        task_context_summary="testing",
    )
    lines = injection.splitlines()
    assert lines[0] == INJECTION_HEADING
    # Section ordering — user first, then feedback, project, pattern.
    order = []
    for line in lines:
        if line.endswith(":") and line[0].isalpha():
            order.append(line)
    assert order == [
        "About the user:",
        "Feedback from past sessions:",
        "Project facts:",
        "Patterns to apply:",
    ]
