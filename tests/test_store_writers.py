"""Unit tests for the :class:`Store` writer surface added in issue #340.

Covers:

* :func:`apply_title_contract` — every tier/type branch + existing-tag
  passthrough.
* :meth:`SQLAlchemyStore.upsert_message` — insert-then-update dedupe,
  custom ``dedupe_key`` contract, unknown-key rejection.
* :meth:`SQLAlchemyStore.upsert_alert` / :meth:`clear_alert` — the alert
  wrappers over ``upsert_message`` / ``close_message``.
* :meth:`SQLAlchemyStore.execute` — raw-statement escape hatch.

Run with ``HOME=/tmp/pytest-storage-d uv run pytest -x
tests/test_store_writers.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import insert, select

from pollypm.store import SQLAlchemyStore, apply_title_contract
from pollypm.store.schema import messages
from pollypm.store.title_contract import has_bracket_tag


# ---------------------------------------------------------------------------
# Title contract
# ---------------------------------------------------------------------------


class TestApplyTitleContract:
    def test_immediate_notify_gets_action_tag(self):
        out = apply_title_contract("Deploy blocked", tier="immediate", type="notify")
        assert out == "[Action] Deploy blocked"

    def test_digest_notify_gets_fyi_tag(self):
        out = apply_title_contract("task shipped", tier="digest", type="notify")
        assert out == "[FYI] task shipped"

    def test_silent_tier_gets_audit_tag(self):
        # silent wins regardless of type.
        out = apply_title_contract("recorded trace", tier="silent", type="notify")
        assert out == "[Audit] recorded trace"
        # Also for alert with silent tier — still [Audit].
        out = apply_title_contract("x", tier="silent", type="alert")
        assert out == "[Audit] x"

    def test_alert_type_gets_alert_tag(self):
        out = apply_title_contract("pane dead", tier="immediate", type="alert")
        assert out == "[Alert] pane dead"

    def test_inbox_task_type_gets_task_tag(self):
        out = apply_title_contract("Assigned task 12", type="inbox_task")
        assert out == "[Task] Assigned task 12"

    def test_existing_bracket_tag_left_alone(self):
        # Caller knew what they were doing — no double prefix.
        out = apply_title_contract(
            "[Done] milestone 02", tier="digest", type="notify"
        )
        assert out == "[Done] milestone 02"

    def test_has_bracket_tag_rejects_plain(self):
        assert not has_bracket_tag("plain subject")
        assert has_bracket_tag("[Ready] x")
        # Leading whitespace before the bracket still counts.
        assert has_bracket_tag("  [FYI] x")

    def test_empty_subject_returns_tag_alone(self):
        # Defensive — empty input should still get stamped (caller's
        # validation ought to have caught this, but not our problem).
        out = apply_title_contract("", tier="immediate", type="notify")
        assert out == "[Action]"

    def test_unknown_tier_type_falls_back_to_note(self):
        out = apply_title_contract("mystery", tier="weird", type="weird")
        assert out == "[Note] mystery"


# ---------------------------------------------------------------------------
# upsert_message dedupe contract
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> SQLAlchemyStore:
    store = SQLAlchemyStore(f"sqlite:///{tmp_path}/state.db")
    yield store
    store.close()


class TestUpsertMessage:
    def test_first_upsert_inserts_new_row(self, store: SQLAlchemyStore):
        row_id = store.upsert_message(
            type="alert",
            tier="immediate",
            recipient="user",
            sender="pane_dead",
            subject="Pane is dead",
            body="",
            scope="worker-foo",
        )
        assert row_id > 0

        rows = store.query_messages(type="alert", scope="worker-foo")
        assert len(rows) == 1
        assert rows[0]["subject"].startswith("[Alert]")
        assert rows[0]["state"] == "open"

    def test_second_upsert_refreshes_existing_open_row(
        self, store: SQLAlchemyStore
    ):
        first = store.upsert_message(
            type="alert",
            tier="immediate",
            recipient="user",
            sender="pane_dead",
            subject="Pane is dead",
            body="",
            scope="worker-foo",
        )
        second = store.upsert_message(
            type="alert",
            tier="immediate",
            recipient="user",
            sender="pane_dead",
            subject="Pane is dead (retrying)",
            body="Retry #2",
            scope="worker-foo",
        )
        # Same row id — no duplicate inserted.
        assert first == second

        rows = store.query_messages(type="alert", scope="worker-foo")
        assert len(rows) == 1
        assert "retrying" in rows[0]["subject"].lower()
        assert rows[0]["body"] == "Retry #2"

    def test_closed_row_does_not_dedupe(self, store: SQLAlchemyStore):
        # After close, a new upsert should insert a fresh row.
        first = store.upsert_message(
            type="alert",
            tier="immediate",
            recipient="user",
            sender="pane_dead",
            subject="Pane is dead",
            body="",
            scope="worker-foo",
        )
        store.close_message(first)
        second = store.upsert_message(
            type="alert",
            tier="immediate",
            recipient="user",
            sender="pane_dead",
            subject="Pane is dead again",
            body="",
            scope="worker-foo",
        )
        assert first != second

    def test_different_sender_inserts_new_row(self, store: SQLAlchemyStore):
        store.upsert_message(
            type="alert",
            tier="immediate",
            recipient="user",
            sender="pane_dead",
            subject="Pane is dead",
            body="",
            scope="worker-foo",
        )
        store.upsert_message(
            type="alert",
            tier="immediate",
            recipient="user",
            sender="shell_returned",
            subject="Shell returned",
            body="",
            scope="worker-foo",
        )
        rows = store.query_messages(type="alert", scope="worker-foo")
        assert len(rows) == 2

    def test_unknown_dedupe_key_field_rejected(self, store: SQLAlchemyStore):
        with pytest.raises(ValueError, match="dedupe_key"):
            store.upsert_message(
                type="alert",
                tier="immediate",
                recipient="user",
                sender="x",
                subject="x",
                body="",
                scope="s",
                dedupe_key=("scope", "totally_fake_field"),
            )


# ---------------------------------------------------------------------------
# Alert wrappers
# ---------------------------------------------------------------------------


class TestAlertWrappers:
    def test_upsert_alert_then_clear(self, store: SQLAlchemyStore):
        store.upsert_alert(
            session_name="worker-foo",
            alert_type="pane_dead",
            severity="warn",
            message="Pane is dead",
        )
        rows = store.query_messages(type="alert", scope="worker-foo")
        assert len(rows) == 1
        assert rows[0]["state"] == "open"
        assert rows[0]["sender"] == "pane_dead"
        # severity rides along in payload.
        assert rows[0]["payload"]["severity"] == "warn"

        store.clear_alert("worker-foo", "pane_dead")
        rows = store.query_messages(type="alert", scope="worker-foo")
        assert len(rows) == 1
        assert rows[0]["state"] == "closed"

    def test_upsert_alert_is_idempotent(self, store: SQLAlchemyStore):
        for _ in range(3):
            store.upsert_alert(
                session_name="s",
                alert_type="t",
                severity="warn",
                message="m",
            )
        rows = store.query_messages(type="alert", scope="s")
        # Dedupe — only one open row per (session, alert_type).
        assert len(rows) == 1

    def test_clear_alert_when_none_open_is_noop(self, store: SQLAlchemyStore):
        # Must not raise.
        store.clear_alert("nothing", "never_opened")
        rows = store.query_messages(type="alert", scope="nothing")
        assert rows == []


# ---------------------------------------------------------------------------
# execute() escape hatch
# ---------------------------------------------------------------------------


class TestExecute:
    def test_execute_runs_core_statement_and_returns_cursor(
        self, store: SQLAlchemyStore
    ):
        result = store.execute(
            insert(messages).values(
                scope="x",
                type="event",
                tier="immediate",
                recipient="*",
                sender="unit-test",
                state="open",
                subject="hello",
                body="",
                payload_json="{}",
                labels="[]",
            )
        )
        assert result.inserted_primary_key is not None

        # Read it back through the Store's read engine using a Core select.
        with store.read_engine.connect() as conn:
            rows = conn.execute(
                select(messages.c.subject).where(messages.c.sender == "unit-test")
            ).all()
        assert rows[0][0] == "hello"


# ---------------------------------------------------------------------------
# Title contract integration via enqueue_message
# ---------------------------------------------------------------------------


class TestEnqueueStampsSubject:
    def test_enqueue_applies_title_contract_automatically(
        self, store: SQLAlchemyStore
    ):
        row_id = store.enqueue_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="polly",
            subject="plain subject",
            body="body",
            scope="demo",
        )
        rows = store.query_messages(type="notify", scope="demo")
        assert rows[0]["subject"] == "[Action] plain subject"
        assert rows[0]["id"] == row_id

    def test_enqueue_respects_pre_tagged_subject(self, store: SQLAlchemyStore):
        store.enqueue_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="polly",
            subject="[Hot] custom tag",
            body="",
            scope="demo",
        )
        rows = store.query_messages(type="notify", scope="demo")
        assert rows[0]["subject"] == "[Hot] custom tag"
