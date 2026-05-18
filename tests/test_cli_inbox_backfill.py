"""Integration tests for ``pm inbox backfill-kinds`` (#1570).

Covers the four invariants the issue calls out:

* Dry-run (default) classifies but never writes.
* ``--commit`` writes, emits one audit event per row, and reports
  the unmatched count.
* Idempotence — a second commit run reclassifies zero rows because
  the first run already moved them off ``legacy``.
* Mutually exclusive flags are refused with a non-zero exit code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.audit.log import EVENT_INBOX_KIND_BACKFILLED, central_log_path
from pollypm.inbox.kind import InboxItemKind, coerce_kind
from pollypm.store import SQLAlchemyStore
from pollypm.work.inbox_cli import inbox_app


runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_audit_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Redirect the central-tail root so tests never touch ~/.pollypm/."""
    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))
    return audit_home


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_legacy_message(
    db_path: Path,
    *,
    subject: str,
    sender: str = "polly",
    scope: str = "demo",
    msg_type: str = "notify",
) -> int:
    """Insert a single legacy-kind message and return its row id.

    ``enqueue_message`` defaults ``kind`` to ``'legacy'`` precisely to
    model the pre-#1565 state we're backfilling here.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        return store.enqueue_message(
            type=msg_type,
            tier="immediate",
            recipient="user",
            sender=sender,
            subject=subject,
            body="body content",
            scope=scope,
        )
    finally:
        store.close()


def _read_kind(db_path: Path, msg_id: int) -> InboxItemKind:
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        rows = store.query_messages(recipient="user")
    finally:
        store.close()
    match = next((row for row in rows if row.get("id") == msg_id), None)
    assert match is not None, f"msg:{msg_id} missing after backfill"
    return coerce_kind(match.get("kind"))


def _seed_full_sample(db_path: Path) -> dict[str, int]:
    """Seed one row per heuristic + two unmatched rows.

    Returns a mapping of label → message id so assertions can pin
    each row by intent without re-querying the DB.
    """
    return {
        "completion": _seed_legacy_message(
            db_path,
            subject="Web API phase 1 complete",
            sender="polly",
            scope="demo",
        ),
        "self_bug": _seed_legacy_message(
            db_path,
            subject="Misrouted Polly notification (3rd today)",
            sender="polly",
            scope="demo",
        ),
        "plan_review": _seed_legacy_message(
            db_path,
            subject="Plan ready for review — demo/12",
            sender="architect",
            scope="demo",
        ),
        "manual_decision": _seed_legacy_message(
            db_path,
            subject="Digest: 7 open findings need your call",
            sender="polly",
            scope="inbox",
        ),
        "watchdog": _seed_legacy_message(
            db_path,
            subject="[Action] queue_without_motion needs review",
            sender="audit_watchdog",
            scope="demo",
        ),
        "unmatched_a": _seed_legacy_message(
            db_path,
            subject="Random one-off note from a worker",
            sender="worker-7",
            scope="demo",
        ),
        "unmatched_b": _seed_legacy_message(
            db_path,
            subject="Heartbeat tick at 12:30",
            sender="heartbeat",
            scope="demo",
        ),
    }


def _audit_lines_for_project(audit_home: Path, project: str) -> list[dict]:
    """Return decoded audit-event records for ``project`` from the central tail."""
    path = central_log_path(project)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_dry_run_classifies_but_writes_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    ids = _seed_full_sample(db_path)

    # No --commit and no --dry-run — the default branch is dry-run.
    result = runner.invoke(
        inbox_app, ["backfill-kinds", "--db", str(db_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Would reclassify 5 row(s); 2 unmatched." in result.output
    assert "Re-run with --commit" in result.output
    # Each classified row is printed with its heuristic + new kind.
    assert "completion_fyi" in result.output
    assert "self_bug_report" in result.output
    assert "plan_review_pending" in result.output
    assert "manual_decision" in result.output
    assert "watchdog_operator_dispatch" in result.output
    # Unmatched rows are flagged separately.
    assert "(unmatched, stays legacy)" in result.output

    # DB state must be untouched.
    for label, msg_id in ids.items():
        assert _read_kind(db_path, msg_id) is InboxItemKind.LEGACY, label


def test_explicit_dry_run_flag_behaves_the_same(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _seed_full_sample(db_path)

    result = runner.invoke(
        inbox_app, ["backfill-kinds", "--dry-run", "--db", str(db_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Would reclassify" in result.output


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


def test_commit_writes_audit_events_and_updates_kinds(
    tmp_path: Path, _isolate_audit_home: Path,
) -> None:
    db_path = tmp_path / "state.db"
    ids = _seed_full_sample(db_path)

    result = runner.invoke(
        inbox_app, ["backfill-kinds", "--commit", "--db", str(db_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Reclassified 5 row(s); 2 unmatched." in result.output

    # Each matched row landed on its expected kind.
    assert _read_kind(db_path, ids["completion"]) is InboxItemKind.COMPLETION_FYI
    assert _read_kind(db_path, ids["self_bug"]) is InboxItemKind.SELF_BUG_REPORT
    assert _read_kind(db_path, ids["plan_review"]) is InboxItemKind.PLAN_REVIEW_PENDING
    assert _read_kind(db_path, ids["manual_decision"]) is InboxItemKind.MANUAL_DECISION
    assert _read_kind(db_path, ids["watchdog"]) is InboxItemKind.WATCHDOG_OPERATOR_DISPATCH
    # Unmatched rows stay legacy.
    assert _read_kind(db_path, ids["unmatched_a"]) is InboxItemKind.LEGACY
    assert _read_kind(db_path, ids["unmatched_b"]) is InboxItemKind.LEGACY

    # Audit log carries one inbox.kind_backfilled per matched row, split
    # across the per-project central tails (demo + inbox).
    demo_events = [
        e for e in _audit_lines_for_project(_isolate_audit_home, "demo")
        if e.get("event") == EVENT_INBOX_KIND_BACKFILLED
    ]
    inbox_events = [
        e for e in _audit_lines_for_project(_isolate_audit_home, "inbox")
        if e.get("event") == EVENT_INBOX_KIND_BACKFILLED
    ]
    assert len(demo_events) == 4
    assert len(inbox_events) == 1

    # Each event carries the contracted metadata shape.
    for event in demo_events + inbox_events:
        meta = event["metadata"]
        assert meta["old_kind"] == InboxItemKind.LEGACY.value
        assert meta["new_kind"] in {k.value for k in InboxItemKind}
        assert meta["heuristic"]
        assert event["subject"].startswith("msg:")
        assert event["actor"] == "user"
        assert event["status"] == "ok"


def test_commit_is_idempotent(
    tmp_path: Path, _isolate_audit_home: Path,
) -> None:
    db_path = tmp_path / "state.db"
    _seed_full_sample(db_path)

    first = runner.invoke(
        inbox_app, ["backfill-kinds", "--commit", "--db", str(db_path)],
    )
    assert first.exit_code == 0, first.output
    assert "Reclassified 5 row(s); 2 unmatched." in first.output

    second = runner.invoke(
        inbox_app, ["backfill-kinds", "--commit", "--db", str(db_path)],
    )
    assert second.exit_code == 0, second.output
    # The two remaining legacy rows are still scanned but only re-printed
    # as unmatched; nothing reclassifies on the second pass.
    assert "Reclassified 0 row(s); 2 unmatched." in second.output


def test_dry_run_after_commit_reports_remaining_legacy_rows(
    tmp_path: Path, _isolate_audit_home: Path,
) -> None:
    db_path = tmp_path / "state.db"
    _seed_full_sample(db_path)
    runner.invoke(
        inbox_app, ["backfill-kinds", "--commit", "--db", str(db_path)],
    )

    result = runner.invoke(
        inbox_app, ["backfill-kinds", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.output
    # Two legacy rows remain (the unmatched ones); both still print.
    assert "Would reclassify 0 row(s); 2 unmatched." in result.output


# ---------------------------------------------------------------------------
# Project filter
# ---------------------------------------------------------------------------


def test_project_filter_scopes_to_one_project(
    tmp_path: Path, _isolate_audit_home: Path,
) -> None:
    db_path = tmp_path / "state.db"
    demo_id = _seed_legacy_message(
        db_path,
        subject="Web API phase 1 complete",
        sender="polly",
        scope="demo",
    )
    other_id = _seed_legacy_message(
        db_path,
        subject="Web API phase 2 complete",
        sender="polly",
        scope="other",
    )

    result = runner.invoke(
        inbox_app,
        [
            "backfill-kinds", "--commit",
            "--project", "demo",
            "--db", str(db_path),
        ],
    )
    assert result.exit_code == 0, result.output

    assert _read_kind(db_path, demo_id) is InboxItemKind.COMPLETION_FYI
    # The other-project row is untouched because the --project filter
    # excluded it from the scan.
    assert _read_kind(db_path, other_id) is InboxItemKind.LEGACY


# ---------------------------------------------------------------------------
# Flag validation
# ---------------------------------------------------------------------------


def test_commit_and_dry_run_are_mutually_exclusive(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _seed_legacy_message(db_path, subject="Random row", sender="worker", scope="demo")

    result = runner.invoke(
        inbox_app,
        ["backfill-kinds", "--commit", "--dry-run", "--db", str(db_path)],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_empty_db_reports_no_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    # Touch the DB by inserting a non-legacy row, then closing it so the
    # backfill scan finds zero legacy rows.
    msg_id = _seed_legacy_message(
        db_path, subject="Plan ready for review — alpha/1",
        sender="architect", scope="alpha",
    )
    # Manually flip its kind off legacy so the scan finds nothing.
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        store.update_message(msg_id, kind=InboxItemKind.PLAN_REVIEW_PENDING.value)
    finally:
        store.close()

    result = runner.invoke(
        inbox_app, ["backfill-kinds", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.output
    assert "No legacy inbox rows to reclassify." in result.output
