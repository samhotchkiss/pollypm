"""Regression checks for the maintained work-service spec."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = PROJECT_ROOT / "docs" / "work-service-spec.md"


def test_work_service_spec_matches_current_runtime_shape():
    text = SPEC_PATH.read_text(encoding="utf-8")

    assert "in-process library" in text
    assert "work-service.sock" not in text
    assert "`list_tasks`" in text
    assert "ValidationResult" not in text
    assert "list[GateResult]" in text
    assert "one-way push only" in text
    assert "poll_inbound" not in text
    assert "work_service.move(" not in text
    assert "sync_status(task_id)" in text
    assert "`pm task sync-status`" not in text
    assert "`total_cost_usd`" not in text
