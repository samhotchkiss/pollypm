"""Static coverage for the front-door audit-log docs."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUDIT_DOC = PROJECT_ROOT / "docs" / "audit-log.md"
DOCS_README = PROJECT_ROOT / "docs" / "README.md"
WORK_SERVICE_SPEC = PROJECT_ROOT / "docs" / "work-service-spec.md"
MORNING_BRIEFING_SPEC = PROJECT_ROOT / "docs" / "morning-briefing-plugin-spec.md"


def test_audit_log_doc_covers_operator_and_contributor_surface() -> None:
    text = AUDIT_DOC.read_text(encoding="utf-8")

    required = [
        "POLLYPM_AUDIT_HOME",
        "<project>/.pollypm/audit.jsonl",
        "~/.pollypm/audit/<project>.jsonl",
        "task.created",
        "task.status_changed",
        "marker.created",
        "marker.released",
        "work_db.opened",
        "audit.finding",
        "orphan_marker",
        "stuck_draft",
        "cancellation_no_promotion",
        "POLLYPM_BRIEFING_INCLUDE_ALL",
        "`pm audit clear`",
        "src/pollypm/audit/log.py",
        "tests/test_audit_watchdog.py",
    ]
    missing = [needle for needle in required if needle not in text]
    assert not missing


def test_related_docs_link_to_audit_log() -> None:
    assert "[Audit Log](audit-log.md)" in DOCS_README.read_text(encoding="utf-8")
    assert "[Audit Log](audit-log.md)" in WORK_SERVICE_SPEC.read_text(
        encoding="utf-8",
    )
    assert "[Audit Log](audit-log.md)" in MORNING_BRIEFING_SPEC.read_text(
        encoding="utf-8",
    )
