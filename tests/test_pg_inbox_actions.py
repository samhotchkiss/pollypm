"""Pg re-coverage of work-service inbox interaction methods (#1794).

Replaces the sqlite-bound ``tests/test_inbox_actions.py`` that
Slice K-tests part 6 (#1795) deleted. The deleted module's body was
gated behind a module-level ``xfail`` referencing #1776 (PgWorkService
missing add_reply/list_replies/mark_read/archive_task). #1776 closed
in #1784 — these methods are live on PgWorkService — so the xfail can
come off and the assertions can run for real.

Covers the four methods the cockpit Textual inbox screen calls:
``add_reply``, ``list_replies``, ``mark_read``, ``archive_task``.

The deleted module also contained a ``TestResolveInboxWorkService``
class that exercised dual-DB resolution between workspace and per-
project sqlite state.dbs. That code path doesn't exist under pg
(``resolve_work_db_path`` was a sqlite-only seam) — there is no pg
equivalent to port, so that test class is intentionally not
re-added.
"""

from __future__ import annotations

import time

import pytest

from pollypm.inbox.kind import InboxItemKind
from pollypm.work.models import WorkStatus
from pollypm.work.service_support import (
    InvalidTransitionError,
    TaskNotFoundError,
    ValidationError,
)


def _inbox_task(svc, *, title: str = "Hello Sam", body: str = "Read me.") -> str:
    """Create a chat-flow task in the same shape ``pm notify`` does."""
    task = svc.create(
        title=title,
        description=body,
        type="task",
        project="demo",
        flow_template="chat",
        roles={"requester": "user", "operator": "polly"},
        priority="normal",
        created_by="polly",
    )
    return task.task_id


def _watchdog_dispatch_task(svc, *, body: str, title: str = "Watchdog") -> str:
    task = svc.create(
        title=title,
        description=body,
        type="task",
        project="demo",
        flow_template="chat",
        roles={"requester": "user", "operator": "user"},
        priority="high",
        created_by="audit_watchdog",
        labels=["notify", "watchdog", "notify_message:123"],
        kind=InboxItemKind.WATCHDOG_OPERATOR_DISPATCH.value,
    )
    return task.task_id


# ---------------------------------------------------------------------------
# add_reply
# ---------------------------------------------------------------------------


class TestAddReply:
    def test_reply_persisted_as_reply_entry_type(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        entry = svc.add_reply(task_id, "Thanks for the update.", actor="user")
        assert entry.entry_type == "reply"
        assert entry.actor == "user"
        assert entry.text == "Thanks for the update."

    def test_reply_strips_whitespace(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        entry = svc.add_reply(task_id, "  hi  ", actor="user")
        assert entry.text == "hi"

    def test_empty_reply_rejected(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        with pytest.raises(ValidationError):
            svc.add_reply(task_id, "   ", actor="user")

    def test_reply_to_missing_task_raises(self, pg_work_service):
        svc = pg_work_service
        with pytest.raises(TaskNotFoundError):
            svc.add_reply("demo/999", "ping", actor="user")

    def test_multiple_replies_are_independent_rows(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        svc.add_reply(task_id, "one", actor="user")
        svc.add_reply(task_id, "two", actor="user")
        svc.add_reply(task_id, "three", actor="user")
        entries = svc.list_replies(task_id)
        assert [e.text for e in entries] == ["one", "two", "three"]


# ---------------------------------------------------------------------------
# list_replies
# ---------------------------------------------------------------------------


class TestListReplies:
    def test_returns_replies_oldest_first(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        svc.add_reply(task_id, "first", actor="user")
        time.sleep(0.01)
        svc.add_reply(task_id, "second", actor="user")
        entries = svc.list_replies(task_id)
        assert [e.text for e in entries] == ["first", "second"]

    def test_excludes_non_reply_context(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        svc.add_context(task_id, "system", "a note")
        svc.add_reply(task_id, "a reply", actor="user")
        entries = svc.list_replies(task_id)
        # Only the reply row surfaces in list_replies.
        assert [e.text for e in entries] == ["a reply"]
        assert all(e.entry_type == "reply" for e in entries)

    def test_empty_when_no_replies(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        assert svc.list_replies(task_id) == []


# ---------------------------------------------------------------------------
# mark_read
# ---------------------------------------------------------------------------


class TestMarkRead:
    def test_first_read_writes_marker(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        assert svc.mark_read(task_id, actor="user") is True

    def test_repeat_read_is_idempotent(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        assert svc.mark_read(task_id, actor="user") is True
        # Second call must not write a duplicate row and must return
        # False so callers can gate event emission on it.
        assert svc.mark_read(task_id, actor="user") is False

    def test_marker_lives_as_read_entry_type(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        svc.mark_read(task_id, actor="user")
        reads = svc.get_context(task_id, entry_type="read")
        assert len(reads) == 1
        assert reads[0].entry_type == "read"

    def test_read_markers_do_not_pollute_replies(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        svc.mark_read(task_id, actor="user")
        svc.add_reply(task_id, "hey", actor="user")
        # Reply list ignores the read marker; mark_read is idempotent
        # so no duplicate read rows exist either.
        assert len(svc.list_replies(task_id)) == 1
        assert len(svc.get_context(task_id, entry_type="read")) == 1

    def test_mark_read_on_missing_task_raises(self, pg_work_service):
        svc = pg_work_service
        with pytest.raises(TaskNotFoundError):
            svc.mark_read("demo/404", actor="user")


# ---------------------------------------------------------------------------
# archive_task
# ---------------------------------------------------------------------------


class TestArchiveTask:
    def test_archive_flips_status_to_done(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        archived = svc.archive_task(task_id, actor="user")
        assert archived.work_status == WorkStatus.DONE

    def test_archive_records_transition(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        svc.archive_task(task_id, actor="user")
        task = svc.get(task_id)
        # The transition row is written with actor="user" and a
        # recognisable reason tag so consumers can tell it apart from
        # the standard mark_done path.
        assert any(
            tr.to_state == WorkStatus.DONE.value
            and tr.actor == "user"
            and (tr.reason or "").startswith("inbox.archive")
            for tr in task.transitions
        )

    def test_archive_is_idempotent(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        first = svc.archive_task(task_id, actor="user")
        second = svc.archive_task(task_id, actor="user")
        assert first.work_status == WorkStatus.DONE
        assert second.work_status == WorkStatus.DONE
        # Second call must not append a second transition — otherwise
        # dashboard counts would double-count an archive click.
        task = svc.get(task_id)
        archive_transitions = [
            tr for tr in task.transitions
            if tr.to_state == WorkStatus.DONE.value
            and (tr.reason or "").startswith("inbox.archive")
        ]
        assert len(archive_transitions) == 1

    def test_archive_on_missing_task_raises(self, pg_work_service):
        svc = pg_work_service
        with pytest.raises(TaskNotFoundError):
            svc.archive_task("demo/777", actor="user")

    # ------------------------------------------------------------------
    # strict=True (#2060) — atomic concurrent-archive contract
    # ------------------------------------------------------------------

    def test_archive_strict_flips_status(self, pg_work_service):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        archived = svc.archive_task(task_id, actor="user", strict=True)
        assert archived.work_status == WorkStatus.DONE

    def test_archive_strict_raises_when_already_terminal(
        self, pg_work_service,
    ):
        svc = pg_work_service
        task_id = _inbox_task(svc)
        svc.archive_task(task_id, actor="user")
        # Second archiver under strict mode must NOT silently succeed
        # — that was the race surfaced by #2060 (Codex P0 #3).
        with pytest.raises(InvalidTransitionError):
            svc.archive_task(task_id, actor="user", strict=True)

    def test_archive_strict_concurrent_only_one_winner(self, pg_work_service):
        """Two threads race the same archive; exactly one wins.

        Regression test for the #2060 race: under the old pre-check
        version both threads observed the row in ``in_progress``, both
        called the idempotent ``archive_task``, and both returned 200
        — losing the documented ``invalid_state`` contract. With
        ``strict=True`` the conditional UPDATE serialises the writers
        so the loser sees rowcount==0 and raises
        ``InvalidTransitionError``.
        """
        import threading

        svc = pg_work_service
        task_id = _inbox_task(svc)
        barrier = threading.Barrier(2)
        results: list[object] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def attempt() -> None:
            try:
                barrier.wait(timeout=5)
                outcome = svc.archive_task(task_id, actor="racer", strict=True)
                with lock:
                    results.append(outcome)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Exactly one winner, one InvalidTransitionError loser.
        assert len(results) == 1, (results, errors)
        assert len(errors) == 1, (results, errors)
        assert isinstance(errors[0], InvalidTransitionError)

        # And the canonical writer only appended ONE archive
        # transition row, matching the contract for idempotent /
        # serialised archivers.
        task = svc.get(task_id)
        archive_transitions = [
            tr for tr in task.transitions
            if tr.to_state == WorkStatus.DONE.value
            and (tr.reason or "").startswith("inbox.archive")
        ]
        assert len(archive_transitions) == 1


# ---------------------------------------------------------------------------
# stale watchdog dispatch cleanup
# ---------------------------------------------------------------------------


class TestWatchdogDispatchCleanup:
    def test_terminal_subject_archives_related_watchdog_dispatch(
        self, pg_work_service,
    ):
        svc = pg_work_service
        source_id = _inbox_task(svc, title="Real work")
        dispatch_id = _watchdog_dispatch_task(
            svc,
            body=f"Tier handoff for completed task {source_id}.",
        )

        svc.mark_done(source_id, actor="worker")

        assert svc.get(dispatch_id).work_status == WorkStatus.DONE

    def test_terminal_subject_cleanup_requires_exact_task_ref(
        self, pg_work_service,
    ):
        svc = pg_work_service
        source_id = _inbox_task(svc, title="Real work")
        dispatch_id = _watchdog_dispatch_task(
            svc,
            body=f"Tier handoff for a different task {source_id}0.",
        )

        svc.mark_done(source_id, actor="worker")

        assert svc.get(dispatch_id).work_status != WorkStatus.DONE


# ---------------------------------------------------------------------------
# latest_snoozes_bulk (#2060 round-2) — single-query snooze lookup for the
# inbox-list scan path. Replaces the per-task
# ``get_context(entry_type='snooze', limit=1)`` loop in
# ``_active_snoozed_ids`` (Codex round-2 blocker #2 on PR #2060).
# ---------------------------------------------------------------------------


class TestLatestSnoozesBulk:
    """Bulk snooze fetch — one SQL for N tasks, latest row per task.

    The helper's docstring promises a single statement; verifying the
    actual statement count would require monkeypatching the pool, but
    we can lock the contract behaviour (correct latest row per task,
    absent when no snooze, no leakage of other entry_types) so a
    regression that splits into N queries still ships obviously
    correct data while the bulk-path test in
    ``tests/test_inbox_writes_endpoint.py`` pins the call-count
    behaviour at the API integration layer.
    """

    def test_empty_input_short_circuits(self, pg_work_service):
        # ``[]`` returns ``{}`` without hitting the DB (the helper
        # branches early). Asserting the result shape is enough — a
        # regression that hits the DB with an empty IN-list would
        # raise on the bind, not silently misbehave.
        assert pg_work_service.latest_snoozes_bulk([]) == {}

    def test_returns_latest_snooze_per_task(self, pg_work_service):
        svc = pg_work_service
        t1 = _inbox_task(svc, title="task one")
        t2 = _inbox_task(svc, title="task two")
        # t1: two snoozes — bulk must return the SECOND (latest by id).
        svc.add_context(t1, "api", "until_iso=2026-06-01T00:00:00+00:00",
                        entry_type="snooze")
        svc.add_context(t1, "api", "until_iso=2026-07-01T00:00:00+00:00",
                        entry_type="snooze")
        # t2: one snooze.
        svc.add_context(t2, "api", "until_iso=2026-08-01T00:00:00+00:00",
                        entry_type="snooze")
        keys = [
            (t1.split("/")[0], int(t1.split("/")[1])),
            (t2.split("/")[0], int(t2.split("/")[1])),
        ]
        out = svc.latest_snoozes_bulk(keys)
        assert set(out.keys()) == set(keys)
        # t1's latest is the July snooze.
        assert "2026-07-01" in out[keys[0]].text
        assert "2026-08-01" in out[keys[1]].text

    def test_absent_when_task_has_no_snooze(self, pg_work_service):
        svc = pg_work_service
        t1 = _inbox_task(svc, title="snoozed")
        t2 = _inbox_task(svc, title="not snoozed")
        svc.add_context(t1, "api", "until_iso=2026-06-01T00:00:00+00:00",
                        entry_type="snooze")
        keys = [
            (t1.split("/")[0], int(t1.split("/")[1])),
            (t2.split("/")[0], int(t2.split("/")[1])),
        ]
        out = svc.latest_snoozes_bulk(keys)
        # Only t1 appears; t2 has no snooze row so it's absent.
        assert (t1.split("/")[0], int(t1.split("/")[1])) in out
        assert (t2.split("/")[0], int(t2.split("/")[1])) not in out

    def test_ignores_non_snooze_entry_types(self, pg_work_service):
        """A ``reply`` or ``read`` row must not leak into the result."""
        svc = pg_work_service
        t1 = _inbox_task(svc, title="mixed")
        svc.add_reply(t1, "hi", actor="user")
        svc.add_context(t1, "api", "read", entry_type="read")
        keys = [(t1.split("/")[0], int(t1.split("/")[1]))]
        out = svc.latest_snoozes_bulk(keys)
        # No snooze row exists, so the helper returns ``{}`` — proving
        # the WHERE filter is on entry_type='snooze', not "any context
        # row".
        assert out == {}

    def test_legacy_snoozed_until_text_round_trips(self, pg_work_service):
        """Rows written before the structured marker landed still load.

        The bulk helper returns the raw ``ContextEntry``; the snooze
        text parser (``pollypm.work.inbox_snooze.parse_snooze_until``)
        accepts both shapes. This test just confirms the bulk path
        doesn't filter or mangle the legacy text.
        """
        svc = pg_work_service
        t1 = _inbox_task(svc, title="legacy")
        svc.add_context(
            t1, "api",
            "snoozed until 2026-06-01T12:00:00+00:00",
            entry_type="snooze",
        )
        keys = [(t1.split("/")[0], int(t1.split("/")[1]))]
        out = svc.latest_snoozes_bulk(keys)
        assert keys[0] in out
        assert "snoozed until 2026-06-01" in out[keys[0]].text
