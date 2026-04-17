"""Tests for supersession semantics (M08 / #237)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from pollypm.memory_backends import (
    FileMemoryBackend,
    MemoryType,
    ProjectMemory,
)
from pollypm.memory_extractors import (
    MemoryCandidate,
    flag_supersession_candidates,
    run_extractors,
)


# ---------------------------------------------------------------------------
# write_entry(..., supersedes=...) — low-level supersession contract.
# ---------------------------------------------------------------------------


def test_write_entry_with_supersedes_marks_old_row(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    old = backend.write_entry(
        ProjectMemory(
            fact="Test runner was nose",
            why="Historical baseline",
            how_to_apply="Run nosetests",
            scope="pollypm",
        )
    )
    new = backend.write_entry(
        ProjectMemory(
            fact="Test runner is pytest",
            why="Team convention",
            how_to_apply="Run pytest -q",
            scope="pollypm",
        ),
        supersedes=old.entry_id,
    )

    refreshed_old = backend.read_entry(old.entry_id)
    assert refreshed_old is not None
    assert refreshed_old.superseded_by == new.entry_id
    # New entry itself isn't flagged superseded.
    assert new.superseded_by is None


def test_recall_hides_superseded_by_default(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    old = backend.write_entry(
        ProjectMemory(
            fact="Test runner was nose",
            why="Historical baseline",
            how_to_apply="Run nosetests",
            scope="pollypm",
        )
    )
    new = backend.write_entry(
        ProjectMemory(
            fact="Test runner is pytest",
            why="Team convention",
            how_to_apply="Run pytest -q",
            scope="pollypm",
        ),
        supersedes=old.entry_id,
    )

    ids = [r.entry.entry_id for r in backend.recall("pytest")]
    assert new.entry_id in ids
    assert old.entry_id not in ids


def test_recall_include_superseded_returns_both(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    old = backend.write_entry(
        ProjectMemory(
            fact="Test runner was nose",
            why="Historical baseline",
            how_to_apply="Run nosetests",
            scope="pollypm",
        )
    )
    new = backend.write_entry(
        ProjectMemory(
            fact="Test runner is pytest",
            why="Team convention",
            how_to_apply="Run pytest -q",
            scope="pollypm",
        ),
        supersedes=old.entry_id,
    )

    ids = {
        r.entry.entry_id
        for r in backend.recall("runner", include_superseded=True)
    }
    assert {old.entry_id, new.entry_id} <= ids


def test_write_entry_supersedes_unknown_id_is_noop(tmp_path: Path) -> None:
    """Passing a non-existent id doesn't crash — the UPDATE is a no-op."""
    backend = FileMemoryBackend(tmp_path)
    new = backend.write_entry(
        ProjectMemory(
            fact="New fact",
            why="Reason",
            how_to_apply="How",
            scope="pollypm",
        ),
        supersedes=99999,
    )
    assert new.entry_id > 0


# ---------------------------------------------------------------------------
# pm memory show renders the supersession chain (already tested via M07
# test_show_json_includes_supersession_chain). Add a chain-walk test here.
# ---------------------------------------------------------------------------


def test_show_chain_walks_multi_level_supersession(tmp_path: Path) -> None:
    from pollypm.memory_cli import _supersession_chain

    backend = FileMemoryBackend(tmp_path)
    a = backend.write_entry(
        ProjectMemory(fact="v1 fact", why="w", how_to_apply="h", scope="pollypm")
    )
    b = backend.write_entry(
        ProjectMemory(fact="v2 fact", why="w", how_to_apply="h", scope="pollypm"),
        supersedes=a.entry_id,
    )
    c = backend.write_entry(
        ProjectMemory(fact="v3 fact", why="w", how_to_apply="h", scope="pollypm"),
        supersedes=b.entry_id,
    )

    a_refresh = backend.read_entry(a.entry_id)
    assert a_refresh is not None
    chain = _supersession_chain(backend, a_refresh)
    assert [e.entry_id for e in chain] == [a.entry_id, b.entry_id, c.entry_id]


# ---------------------------------------------------------------------------
# Extractor flagging — candidates that overlap existing rows get flagged.
# ---------------------------------------------------------------------------


def test_flag_supersession_candidates_detects_overlap(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    existing = backend.write_entry(
        ProjectMemory(
            fact="Test runner is nose",
            why="Historical baseline for pollypm tests",
            how_to_apply="Run nosetests across the suite",
            scope="pollypm",
        )
    )
    # Candidate with large token overlap.
    candidate = MemoryCandidate(
        memory=ProjectMemory(
            fact="Test runner is pytest now",
            why="Team convention for pollypm tests",
            how_to_apply="Run pytest across the suite",
            scope="pollypm",
        ),
        confidence=0.9,
    )
    flag_supersession_candidates(backend, [candidate])
    assert candidate.supersedes == existing.entry_id


def test_flag_supersession_candidates_ignores_unrelated(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    backend.write_entry(
        ProjectMemory(
            fact="Test runner is nose",
            why="Historical baseline",
            how_to_apply="Run nosetests",
            scope="pollypm",
        )
    )
    candidate = MemoryCandidate(
        memory=ProjectMemory(
            fact="Deploy target is railway",
            why="Railway handles postgres for us",
            how_to_apply="Push to main triggers deploy",
            scope="pollypm",
        ),
        confidence=0.9,
    )
    flag_supersession_candidates(backend, [candidate])
    assert candidate.supersedes is None


# ---------------------------------------------------------------------------
# Reviewer pass — run_extractors routes through the reviewer when flagged.
# ---------------------------------------------------------------------------


def _stub_runner(mapping: dict[str, dict[str, Any]]) -> Callable[[str], dict[str, Any] | None]:
    def _runner(prompt: str) -> dict[str, Any] | None:
        for key, response in mapping.items():
            if key in prompt:
                return response
        return {"candidates": []}
    return _runner


def test_run_extractors_reviewer_accept_writes_with_supersedes(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    existing = backend.write_entry(
        ProjectMemory(
            fact="Test runner is nose",
            why="Historical baseline for pollypm tests",
            how_to_apply="Run nosetests across the suite",
            scope="pollypm",
        )
    )
    runner = _stub_runner({
        "PROJECT memories": {
            "candidates": [
                {
                    "fact": "Test runner is pytest now",
                    "why": "Team convention for pollypm tests",
                    "how_to_apply": "Run pytest across the suite",
                    "confidence": 0.9,
                }
            ]
        }
    })
    reviewer_calls: list[int] = []

    def _reviewer(_backend, candidate: MemoryCandidate) -> str:
        reviewer_calls.append(candidate.supersedes or 0)
        return "accept"

    events = [{"event_type": "user_turn", "payload": {"text": "we switched to pytest"}}]
    result = run_extractors(
        events,
        backend,
        project_scope="pollypm",
        llm_runner=runner,
        reviewer=_reviewer,
    )
    assert result.written == 1
    assert result.superseded == 1
    assert reviewer_calls == [existing.entry_id]
    # Old entry is now hidden from recall.
    assert all(r.entry.entry_id != existing.entry_id for r in backend.recall("runner"))


def test_run_extractors_reviewer_reject_discards_candidate(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    existing = backend.write_entry(
        ProjectMemory(
            fact="Test runner is nose",
            why="Historical baseline for pollypm tests",
            how_to_apply="Run nosetests across the suite",
            scope="pollypm",
        )
    )
    runner = _stub_runner({
        "PROJECT memories": {
            "candidates": [
                {
                    "fact": "Test runner is pytest now",
                    "why": "Team convention for pollypm tests",
                    "how_to_apply": "Run pytest across the suite",
                    "confidence": 0.9,
                }
            ]
        }
    })

    def _reviewer(_backend, _candidate) -> str:
        return "reject"

    events = [{"event_type": "user_turn", "payload": {"text": "we switched to pytest"}}]
    result = run_extractors(
        events,
        backend,
        project_scope="pollypm",
        llm_runner=runner,
        reviewer=_reviewer,
    )
    assert result.written == 0
    assert result.supersession_rejected == 1
    # Old entry is untouched.
    assert backend.read_entry(existing.entry_id) is not None
    assert backend.read_entry(existing.entry_id).superseded_by is None


def test_run_extractors_reviewer_side_by_side_writes_without_supersession(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    existing = backend.write_entry(
        ProjectMemory(
            fact="Test runner is nose",
            why="Historical baseline for pollypm tests",
            how_to_apply="Run nosetests across the suite",
            scope="pollypm",
        )
    )
    runner = _stub_runner({
        "PROJECT memories": {
            "candidates": [
                {
                    "fact": "Test runner is pytest now",
                    "why": "Team convention for pollypm tests",
                    "how_to_apply": "Run pytest across the suite",
                    "confidence": 0.9,
                }
            ]
        }
    })

    def _reviewer(_backend, _candidate) -> str:
        return "side_by_side"

    events = [{"event_type": "user_turn", "payload": {"text": "we added pytest"}}]
    result = run_extractors(
        events,
        backend,
        project_scope="pollypm",
        llm_runner=runner,
        reviewer=_reviewer,
    )
    assert result.written == 1
    assert result.superseded == 0
    # Both survive in recall with include_superseded=True.
    refreshed = backend.read_entry(existing.entry_id)
    assert refreshed is not None
    assert refreshed.superseded_by is None
