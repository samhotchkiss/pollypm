from __future__ import annotations

import json
from pathlib import Path

from pollypm.doc_backends import get_doc_backend
from pollypm.knowledge_extract import (
    KNOWLEDGE_LEDGER_DIR,
    SECTION_ITEM_LIMITS,
    KnowledgeDelta,
    _append_knowledge_ledger,
    _update_doc,
)


def _section_body(content: str, heading: str) -> str:
    marker = f"\n{heading}\n"
    if marker not in content:
        return ""
    tail = content.split(marker, 1)[1]
    next_heading = tail.find("\n## ")
    if next_heading == -1:
        return tail.strip()
    return tail[:next_heading].strip()


def test_update_doc_scrubs_garbage_and_caps_managed_section(tmp_path: Path) -> None:
    backend = get_doc_backend(tmp_path)
    noisy_bullets = "\n".join(
        [
            '- [{"ts": "2026-04-10T06:00:00Z", "type": "jsonl_event"}]',
            '- {"timestamp": "2026-04-10T06:01:00Z", "payload": "noise"}',
        ]
        + [f"- Legacy decision {idx}" for idx in range(40)]
    )
    backend.write_document(
        name="decisions",
        title="Decisions",
        content=(
            "# Decisions\n\n"
            "## Summary\n- stale summary\n\n"
            "## Decisions\n"
            f"{noisy_bullets}\n\n"
            "## Notes\nKeep this manual note.\n"
        ),
    )

    changed = _update_doc(
        backend,
        "decisions",
        "Decisions",
        {"## Decisions": [f"Fresh decision {idx}" for idx in range(6)]},
    )

    assert changed is True
    content = backend.read_document("decisions").content
    assert '"timestamp"' not in content
    assert "jsonl_event" not in content
    assert "Keep this manual note." in content

    decision_lines = [
        line[2:].strip()
        for line in _section_body(content, "## Decisions").splitlines()
        if line.startswith("- ")
    ]
    assert len(decision_lines) == SECTION_ITEM_LIMITS["## Decisions"]
    assert decision_lines[-1] == "Fresh decision 5"
    assert "Fresh decision 5" in _section_body(content, "## Summary")


def test_append_knowledge_ledger_dedupes_items(tmp_path: Path) -> None:
    delta = KnowledgeDelta(
        decisions=["Use SQLite for local state."],
        ideas=["Add a morning briefing banner."],
    )

    _append_knowledge_ledger(tmp_path, delta)
    _append_knowledge_ledger(tmp_path, delta)

    decisions_path = tmp_path / KNOWLEDGE_LEDGER_DIR / "decisions.jsonl"
    ideas_path = tmp_path / KNOWLEDGE_LEDGER_DIR / "ideas.jsonl"

    decision_entries = [json.loads(line) for line in decisions_path.read_text().splitlines()]
    idea_entries = [json.loads(line) for line in ideas_path.read_text().splitlines()]

    assert len(decision_entries) == 1
    assert decision_entries[0]["kind"] == "decisions"
    assert decision_entries[0]["item"] == "Use SQLite for local state."
    assert len(idea_entries) == 1
    assert idea_entries[0]["item"] == "Add a morning briefing banner."
