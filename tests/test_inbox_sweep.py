"""Tests for the inbox auto-archive sweep (#1013, sub-bug B).

Pins:

* Age-based sweep — :func:`sweep_stale_notifies` closes ``state='open'``
  notifies whose ``created_at`` is older than the configured retention
  window. Pinned notifies (label ``pinned``) are exempt.
* Related-task sweep — :func:`sweep_notifies_for_done_task` closes any
  open notify whose ``payload.task_id`` references a task transition
  to terminal state.
* CLI mark-all-read — ``pm inbox archive --read`` bulk archives every
  open user-recipient notify in scope, with ``--dry-run`` preview and
  a pinned-label opt-out.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.cli import app as root_app
from pollypm.inbox_sweep import (
    DEFAULT_NOTIFY_RETENTION_DAYS,
    sweep_notifies_for_done_task,
    sweep_stale_notifies,
)
from pollypm.store import SQLAlchemyStore
from pollypm.work.inbox_cli import inbox_app


runner = CliRunner()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


@pytest.fixture
def store(db_path):
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    yield store
    store.close()


def _backdate(store: SQLAlchemyStore, msg_id: int, when: datetime) -> None:
    """Force a message's ``created_at`` to a specific timestamp.

    The sweep keys on ``created_at``, so the cleanest way to test
    "older than 14 days" is to write a row and then rewrite its
    timestamp via raw SQL — no production code path supports
    backdating, but the test seam keeps the sweep deterministic.
    """
    from sqlalchemy import update
    from pollypm.store.schema import messages

    with store.transaction() as conn:
        conn.execute(
            update(messages)
            .where(messages.c.id == msg_id)
            .values(created_at=when)
        )


class TestSweepStaleNotifies:
    def test_archives_old_open_notify(self, store):
        old_id = store.enqueue_message(
            type="notify", tier="immediate", recipient="user",
            sender="polly", subject="X E2E complete",
            body="shipped", scope="proj",
        )
        # Backdate to 30 days ago — well past the 14-day default.
        _backdate(
            store, old_id,
            datetime.now(timezone.utc) - timedelta(days=30),
        )

        archived = sweep_stale_notifies(store)
        assert archived == 1

        rows = store.query_messages(
            type="notify", recipient="user",
        )
        assert len(rows) == 1
        assert rows[0]["state"] == "closed"

    def test_leaves_recent_notify_untouched(self, store):
        recent_id = store.enqueue_message(
            type="notify", tier="immediate", recipient="user",
            sender="polly", subject="Just shipped",
            body="shipped", scope="proj",
        )
        # Created just now — well within retention.
        archived = sweep_stale_notifies(store)
        assert archived == 0
        row = next(
            r for r in store.query_messages(type="notify", recipient="user")
            if r["id"] == recent_id
        )
        assert row["state"] == "open"

    def test_pinned_notify_is_exempt(self, store):
        pinned_id = store.enqueue_message(
            type="notify", tier="immediate", recipient="user",
            sender="polly", subject="Important reminder",
            body="don't auto-archive me", scope="proj",
            labels=["pinned"],
        )
        _backdate(
            store, pinned_id,
            datetime.now(timezone.utc) - timedelta(days=90),
        )

        archived = sweep_stale_notifies(store)
        assert archived == 0
        row = next(
            r for r in store.query_messages(type="notify", recipient="user")
            if r["id"] == pinned_id
        )
        assert row["state"] == "open"

    def test_default_retention_is_14_days(self):
        # Documented default — pin so a reflexive bump to 30 doesn't
        # silently break the issue's verification expectation.
        assert DEFAULT_NOTIFY_RETENTION_DAYS == 14

    def test_explicit_older_than_overrides_default(self, store):
        mid = store.enqueue_message(
            type="notify", tier="immediate", recipient="user",
            sender="polly", subject="recent-but-old-by-tighter-cutoff",
            body=".", scope="proj",
        )
        _backdate(
            store, mid,
            datetime.now(timezone.utc) - timedelta(days=3),
        )
        # Default cutoff (14 days) — row is recent, sweep skips.
        assert sweep_stale_notifies(store) == 0
        # Tighter cutoff (1 day) — row is now "old".
        archived = sweep_stale_notifies(
            store,
            older_than=datetime.now(timezone.utc) - timedelta(days=1),
        )
        assert archived == 1

    def test_skips_already_closed(self, store):
        mid = store.enqueue_message(
            type="notify", tier="immediate", recipient="user",
            sender="polly", subject="already-closed",
            body=".", scope="proj",
        )
        store.close_message(mid)
        _backdate(
            store, mid,
            datetime.now(timezone.utc) - timedelta(days=99),
        )
        # query_messages with state='open' filters this out — no double-close.
        archived = sweep_stale_notifies(store)
        assert archived == 0


class TestSweepNotifiesForDoneTask:
    def test_archives_notify_referencing_task(self, store):
        mid = store.enqueue_message(
            type="notify", tier="immediate", recipient="user",
            sender="polly", subject="Plan ready for review: bikepath",
            body="approve me", scope="bikepath",
            payload={"task_id": "bikepath/7"},
        )
        archived = sweep_notifies_for_done_task(store, "bikepath/7")
        assert archived == 1

        rows = store.query_messages(type="notify", recipient="user")
        match = next(r for r in rows if r["id"] == mid)
        assert match["state"] == "closed"

    def test_skips_unrelated_notifies(self, store):
        mid = store.enqueue_message(
            type="notify", tier="immediate", recipient="user",
            sender="polly", subject="other thing",
            body=".", scope="proj",
            payload={"task_id": "proj/99"},
        )
        archived = sweep_notifies_for_done_task(store, "bikepath/7")
        assert archived == 0
        match = next(
            r for r in store.query_messages(type="notify", recipient="user")
            if r["id"] == mid
        )
        assert match["state"] == "open"

    def test_handles_missing_payload(self, store):
        # Notify with no payload.task_id — sweep must not crash.
        store.enqueue_message(
            type="notify", tier="immediate", recipient="user",
            sender="polly", subject="no payload",
            body=".", scope="proj",
        )
        archived = sweep_notifies_for_done_task(store, "bikepath/7")
        assert archived == 0


class TestArchiveTaskHook:
    """``svc.archive_task`` triggers the related-notify sweep.

    External automation that writes a notify with
    ``payload.task_id=<work-task>`` (priority other than ``immediate``,
    so the message stays ``state='open'``) gets cleaned up when the
    referenced task is archived. The architect's ``--priority
    immediate`` plan_review path closes the message at write time and
    surfaces only the task — the related-task sweep is a defensive
    catch-up for the cases where the message is still open.
    """

    def test_archive_closes_referencing_open_notify(self, db_path):
        # Seed a real chat-flow task + a still-open notify pointing at
        # it. We craft the notify directly so we can keep state='open'
        # — the immediate-priority writer in cli_features closes the
        # message at write time, which would mask the sweep.
        from pollypm.work.sqlite_service import SQLiteWorkService

        svc = SQLiteWorkService(db_path=Path(db_path))
        try:
            task = svc.create(
                title="user task",
                description="x",
                type="task",
                project="demo",
                flow_template="chat",
                roles={"requester": "user", "operator": "polly"},
                priority="normal",
                created_by="polly",
                labels=["notify", "plan_review"],
            )
            task_id = task.task_id
        finally:
            svc.close()

        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            store.enqueue_message(
                type="notify", tier="digest", recipient="user",
                sender="polly", subject="Plan ready for review: demo",
                body="approve me", scope="demo",
                payload={"task_id": task_id},
                state="open",
            )
        finally:
            store.close()

        # Archive the task via the work-service.
        svc = SQLiteWorkService(db_path=Path(db_path))
        try:
            svc.archive_task(task_id, actor="user")
        finally:
            svc.close()

        # Notify is now closed by the related-task sweep hook.
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            rows = store.query_messages(type="notify", recipient="user")
            assert rows[0]["state"] == "closed"
        finally:
            store.close()


def _seed_open_notify(db_path: str, subject: str, **kwargs) -> int:
    """Helper — write an ``open`` notify directly via the store.

    ``pm notify --priority immediate`` writes ``state='closed'`` at the
    message layer (the work-service task is the surface), so we can't
    use the CLI to seed open notifies for these tests.
    """
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        return store.enqueue_message(
            type="notify", tier="digest", recipient="user",
            sender="polly", subject=subject,
            body=".", scope="proj", state="open",
            **kwargs,
        )
    finally:
        store.close()


class TestMarkAllReadCli:
    def test_archive_read_closes_all_open_notifies(self, db_path):
        for subject in ("a complete", "b complete", "c complete"):
            _seed_open_notify(db_path, subject)

        result = runner.invoke(
            inbox_app, ["archive", "--read", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert "Archived 3" in result.output

        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            rows = store.query_messages(type="notify", recipient="user")
            assert all(r["state"] == "closed" for r in rows)
        finally:
            store.close()

    def test_archive_read_dry_run_changes_nothing(self, db_path):
        _seed_open_notify(db_path, "subj")
        result = runner.invoke(
            inbox_app,
            ["archive", "--read", "--dry-run", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert "Would archive" in result.output

        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            rows = store.query_messages(type="notify", recipient="user")
            assert rows[0]["state"] == "open"
        finally:
            store.close()

    def test_archive_read_skips_pinned(self, db_path):
        # Drop a pinned row directly via the store (pm notify doesn't
        # take a --pin flag in this slice; the label literal is the
        # contract).
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            store.enqueue_message(
                type="notify", tier="immediate", recipient="user",
                sender="polly", subject="don't archive me",
                body=".", scope="proj", labels=["pinned"],
            )
            store.enqueue_message(
                type="notify", tier="immediate", recipient="user",
                sender="polly", subject="archive me",
                body=".", scope="proj",
            )
        finally:
            store.close()

        result = runner.invoke(
            inbox_app, ["archive", "--read", "--db", db_path],
        )
        assert result.exit_code == 0, result.output

        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            rows = store.query_messages(type="notify", recipient="user")
            by_subject = {r["subject"]: r["state"] for r in rows}
            # apply_title_contract may prefix subjects, so match on
            # substring rather than equality.
            pinned_open = next(
                state for subject, state in by_subject.items()
                if "don't archive me" in subject
            )
            other_closed = next(
                state for subject, state in by_subject.items()
                if "archive me" in subject and "don't" not in subject
            )
            assert pinned_open == "open"
            assert other_closed == "closed"
        finally:
            store.close()

    def test_archive_read_rejects_combined_with_match(self, db_path):
        result = runner.invoke(
            inbox_app,
            [
                "archive", "--read", "--match", "X*",
                "--db", db_path,
            ],
        )
        assert result.exit_code == 2, result.output
        assert "only one bulk archive mode" in result.output
