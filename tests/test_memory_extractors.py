"""Tests for type-aware memory extractors (M04 / #233).

Cover the acceptance criteria from the issue:

* A test conversation containing one clear feedback signal produces
  exactly one feedback memory.
* Low-confidence candidates are filtered out.
* Extractor is idempotent — re-running on the same events doesn't
  duplicate memories.
* Each of the six extract_* functions has a focused test using a
  deterministic in-memory LLM stub.
* The coordinator routes candidates to the right scope / type.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from pollypm.memory_backends import (
    FeedbackMemory,
    FileMemoryBackend,
    MemoryType,
    PatternMemory,
    ProjectMemory,
    ReferenceMemory,
    UserMemory,
)
from pollypm.memory_extractors import (
    CONFIDENCE_THRESHOLD,
    ExtractionResult,
    MemoryCandidate,
    extract_episodic_memory,
    extract_feedback_memory,
    extract_pattern_memory,
    extract_project_memory,
    extract_reference_memory,
    extract_user_memory,
    run_extractors,
)


# ---------------------------------------------------------------------------
# Helpers — a stub LLM runner that dispatches by prompt content.
# ---------------------------------------------------------------------------


def _stub_runner(mapping: dict[str, dict[str, Any]]) -> Callable[[str], dict[str, Any] | None]:
    """Return a callable that inspects the prompt and returns a canned response.

    ``mapping`` is a dict keyed by substring: the first key that appears
    in the prompt wins. Unmatched prompts return an empty candidates list.
    """
    def _runner(prompt: str) -> dict[str, Any] | None:
        for key, response in mapping.items():
            if key in prompt:
                return response
        return {"candidates": []}
    return _runner


_EVENTS = [
    {
        "event_type": "user_turn",
        "payload": {"text": "Never use --no-verify; it bypasses the hooks that catch regressions."},
    },
    {
        "event_type": "assistant_turn",
        "payload": {"text": "Understood."},
    },
]


# ---------------------------------------------------------------------------
# Individual extractor happy paths
# ---------------------------------------------------------------------------


def test_extract_user_memory_builds_typed_candidate() -> None:
    runner = _stub_runner({
        "USER memories": {
            "candidates": [
                {
                    "name": "Sam",
                    "description": "Senior engineer, prefers small modules.",
                    "body": "Sam reviews PRs weekly and cares about dependency boundaries.",
                    "confidence": 0.9,
                }
            ]
        }
    })
    candidates = extract_user_memory(_EVENTS, llm_runner=runner)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate.memory, UserMemory)
    assert candidate.memory.name == "Sam"
    assert candidate.confidence == pytest.approx(0.9)


def test_extract_feedback_memory_builds_typed_candidate() -> None:
    runner = _stub_runner({
        "FEEDBACK memories": {
            "candidates": [
                {
                    "rule": "Never use --no-verify",
                    "why": "Hooks catch test regressions before push",
                    "how_to_apply": "If a pre-commit hook fails, fix the issue then re-commit.",
                    "confidence": 0.85,
                }
            ]
        }
    })
    candidates = extract_feedback_memory(_EVENTS, scope="pollypm", llm_runner=runner)
    assert len(candidates) == 1
    assert isinstance(candidates[0].memory, FeedbackMemory)
    assert candidates[0].memory.scope == "pollypm"


def test_extract_project_memory_builds_typed_candidate() -> None:
    runner = _stub_runner({
        "PROJECT memories": {
            "candidates": [
                {
                    "fact": "Test runner is pytest with --tb=short",
                    "why": "Agreed convention across the team",
                    "how_to_apply": "Run uv run python -m pytest --tb=short -q before commit",
                    "confidence": 0.9,
                }
            ]
        }
    })
    candidates = extract_project_memory(_EVENTS, scope="pollypm", llm_runner=runner)
    assert len(candidates) == 1
    assert isinstance(candidates[0].memory, ProjectMemory)


def test_extract_reference_memory_builds_typed_candidate() -> None:
    runner = _stub_runner({
        "REFERENCE memories": {
            "candidates": [
                {
                    "pointer": "https://github.com/samhotchkiss/pollypm/issues",
                    "description": "GitHub issues for PollyPM",
                    "confidence": 0.95,
                }
            ]
        }
    })
    candidates = extract_reference_memory(_EVENTS, scope="pollypm", llm_runner=runner)
    assert len(candidates) == 1
    assert isinstance(candidates[0].memory, ReferenceMemory)


def test_extract_pattern_memory_builds_typed_candidate() -> None:
    runner = _stub_runner({
        "PATTERN memories": {
            "candidates": [
                {
                    "when": "Cockpit crashes unexpectedly",
                    "then": "Run pm down && pm up to reset the supervisor",
                    "confidence": 0.8,
                }
            ]
        }
    })
    candidates = extract_pattern_memory(_EVENTS, scope="pollypm", llm_runner=runner)
    assert len(candidates) == 1
    assert isinstance(candidates[0].memory, PatternMemory)


def test_extract_episodic_memory_does_not_call_llm() -> None:
    candidate = extract_episodic_memory(
        session_id="session-1",
        started_at="2026-04-16T09:00:00Z",
        ended_at="2026-04-16T09:30:00Z",
        summary="Worker shipped M04 and pushed tests.",
        scope="pollypm",
    )
    assert candidate is not None
    assert candidate.confidence == 1.0
    assert candidate.memory.session_id == "session-1"


def test_extract_episodic_memory_returns_none_when_summary_empty() -> None:
    candidate = extract_episodic_memory(
        session_id="session-1",
        started_at="2026-04-16T09:00:00Z",
        ended_at="2026-04-16T09:30:00Z",
        summary="   ",
        scope="pollypm",
    )
    assert candidate is None


# ---------------------------------------------------------------------------
# Coordinator behavior — filtering, idempotency, and type routing
# ---------------------------------------------------------------------------


def _coordinator_runner() -> Callable[[str], dict[str, Any] | None]:
    """A runner that yields a clear feedback signal and nothing else."""
    return _stub_runner({
        "FEEDBACK memories": {
            "candidates": [
                {
                    "rule": "Never use --no-verify",
                    "why": "Hooks catch test regressions before push",
                    "how_to_apply": "If a pre-commit hook fails, fix the issue then re-commit.",
                    "confidence": 0.85,
                }
            ]
        }
    })


def test_run_extractors_single_feedback_signal_produces_one_memory(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    result = run_extractors(
        _EVENTS,
        backend,
        project_scope="pollypm",
        llm_runner=_coordinator_runner(),
    )
    assert result.written == 1
    entries = backend.list_entries(scope="pollypm", type=MemoryType.FEEDBACK.value)
    assert len(entries) == 1
    assert "never use --no-verify" in entries[0].title.lower()
    # Nothing for the other types — stub only hit the feedback prompt.
    assert not backend.list_entries(scope="pollypm", type=MemoryType.PROJECT.value)
    assert not backend.list_entries(scope="pollypm", type=MemoryType.PATTERN.value)


def test_run_extractors_filters_below_threshold(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    runner = _stub_runner({
        "FEEDBACK memories": {
            "candidates": [
                {
                    "rule": "Low-signal correction",
                    "why": "Maybe",
                    "how_to_apply": "Maybe",
                    "confidence": 0.2,  # below 0.6 threshold
                }
            ]
        }
    })
    result = run_extractors(_EVENTS, backend, project_scope="pollypm", llm_runner=runner)
    assert result.written == 0
    assert result.filtered_low_confidence == 1
    assert backend.list_entries(scope="pollypm") == []


def test_run_extractors_is_idempotent(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    runner = _coordinator_runner()

    first = run_extractors(_EVENTS, backend, project_scope="pollypm", llm_runner=runner)
    assert first.written == 1

    second = run_extractors(_EVENTS, backend, project_scope="pollypm", llm_runner=runner)
    assert second.written == 0
    assert second.duplicates_skipped == 1

    entries = backend.list_entries(scope="pollypm", type=MemoryType.FEEDBACK.value)
    assert len(entries) == 1


def test_run_extractors_routes_user_memories_to_user_scope(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    runner = _stub_runner({
        "USER memories": {
            "candidates": [
                {
                    "name": "Sam",
                    "description": "Senior engineer.",
                    "body": "Prefers small modules and real integration tests.",
                    "confidence": 0.9,
                }
            ]
        },
        "FEEDBACK memories": {"candidates": []},
        "PROJECT memories": {"candidates": []},
        "REFERENCE memories": {"candidates": []},
        "PATTERN memories": {"candidates": []},
    })
    result = run_extractors(
        _EVENTS,
        backend,
        project_scope="pollypm",
        user_scope="operator",
        llm_runner=runner,
    )
    assert result.written == 1
    user_entries = backend.list_entries(scope="operator", type=MemoryType.USER.value)
    assert len(user_entries) == 1
    # And no user entry landed in the project scope.
    assert not backend.list_entries(scope="pollypm", type=MemoryType.USER.value)


def test_run_extractors_handles_empty_events(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    result = run_extractors([], backend, project_scope="pollypm")
    assert result.attempted == 0
    assert result.written == 0


def test_run_extractors_handles_none_llm_response(tmp_path: Path) -> None:
    """When the LLM returns None, extractors produce 0 candidates without crashing."""
    backend = FileMemoryBackend(tmp_path)
    def _none_runner(prompt: str) -> None:
        return None
    result = run_extractors(_EVENTS, backend, project_scope="pollypm", llm_runner=_none_runner)
    assert result.attempted == 0
    assert result.written == 0


def test_run_extractors_skips_candidates_missing_required_fields(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    runner = _stub_runner({
        "FEEDBACK memories": {
            "candidates": [
                # Missing "why" — should be silently dropped by extract_feedback_memory
                {"rule": "Do the thing", "how_to_apply": "Just do it", "confidence": 0.9},
            ]
        }
    })
    result = run_extractors(_EVENTS, backend, project_scope="pollypm", llm_runner=runner)
    assert result.written == 0


def test_confidence_threshold_is_exactly_inclusive(tmp_path: Path) -> None:
    """A candidate at exactly 0.6 should be accepted (>=, not >)."""
    backend = FileMemoryBackend(tmp_path)
    runner = _stub_runner({
        "PROJECT memories": {
            "candidates": [
                {
                    "fact": "Test at threshold",
                    "why": "Exactly CONFIDENCE_THRESHOLD",
                    "how_to_apply": "Accept it",
                    "confidence": CONFIDENCE_THRESHOLD,
                }
            ]
        }
    })
    result = run_extractors(_EVENTS, backend, project_scope="pollypm", llm_runner=runner)
    assert result.written == 1
