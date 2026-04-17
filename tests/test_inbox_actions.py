"""Work-service unit tests for inbox interaction methods.

Covers ``add_reply``, ``archive_task``, ``mark_read``, ``list_replies`` —
the four methods the cockpit Textual inbox screen calls to drive chat
threads, read-markers, and archival from the TUI.
"""

from __future__ import annotations

import time

import pytest

from pollypm.work.models import WorkStatus
from pollypm.work.sqlite_service import (
    SQLiteWorkService,
    TaskNotFoundError,
    ValidationError,
)


@pytest.fixture
def svc(tmp_path):
    db_path = tmp_path / "inbox.db"
    return SQLiteWorkService(db_path=db_path)


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


# ---------------------------------------------------------------------------
# add_reply
# ---------------------------------------------------------------------------


class TestAddReply:
    def test_reply_persisted_as_reply_entry_type(self, svc):
        task_id = _inbox_task(svc)
        entry = svc.add_reply(task_id, "Thanks for the update.", actor="user")
        assert entry.entry_type == "reply"
        assert entry.actor == "user"
        assert entry.text == "Thanks for the update."

    def test_reply_strips_whitespace(self, svc):
        task_id = _inbox_task(svc)
        entry = svc.add_reply(task_id, "  hi  ", actor="user")
        assert entry.text == "hi"

    def test_empty_reply_rejected(self, svc):
        task_id = _inbox_task(svc)
        with pytest.raises(ValidationError):
            svc.add_reply(task_id, "   ", actor="user")

    def test_reply_to_missing_task_raises(self, svc):
        with pytest.raises(TaskNotFoundError):
            svc.add_reply("demo/999", "ping", actor="user")

    def test_multiple_replies_are_independent_rows(self, svc):
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
    def test_returns_replies_oldest_first(self, svc):
        task_id = _inbox_task(svc)
        svc.add_reply(task_id, "first", actor="user")
        time.sleep(0.01)
        svc.add_reply(task_id, "second", actor="user")
        entries = svc.list_replies(task_id)
        assert [e.text for e in entries] == ["first", "second"]

    def test_excludes_non_reply_context(self, svc):
        task_id = _inbox_task(svc)
        svc.add_context(task_id, "system", "a note")
        svc.add_reply(task_id, "a reply", actor="user")
        entries = svc.list_replies(task_id)
        # Only the reply row surfaces in list_replies.
        assert [e.text for e in entries] == ["a reply"]
        assert all(e.entry_type == "reply" for e in entries)

    def test_empty_when_no_replies(self, svc):
        task_id = _inbox_task(svc)
        assert svc.list_replies(task_id) == []


# ---------------------------------------------------------------------------
# mark_read
# ---------------------------------------------------------------------------


class TestMarkRead:
    def test_first_read_writes_marker(self, svc):
        task_id = _inbox_task(svc)
        assert svc.mark_read(task_id, actor="user") is True

    def test_repeat_read_is_idempotent(self, svc):
        task_id = _inbox_task(svc)
        assert svc.mark_read(task_id, actor="user") is True
        # Second call must not write a duplicate row and must return False
        # so callers can gate event emission on it.
        assert svc.mark_read(task_id, actor="user") is False

    def test_marker_lives_as_read_entry_type(self, svc):
        task_id = _inbox_task(svc)
        svc.mark_read(task_id, actor="user")
        reads = svc.get_context(task_id, entry_type="read")
        assert len(reads) == 1
        assert reads[0].entry_type == "read"

    def test_read_markers_do_not_pollute_replies(self, svc):
        task_id = _inbox_task(svc)
        svc.mark_read(task_id, actor="user")
        svc.add_reply(task_id, "hey", actor="user")
        # Reply list must ignore the read marker, and mark_read is
        # idempotent so no duplicate read rows exist either.
        assert len(svc.list_replies(task_id)) == 1
        assert len(svc.get_context(task_id, entry_type="read")) == 1

    def test_mark_read_on_missing_task_raises(self, svc):
        with pytest.raises(TaskNotFoundError):
            svc.mark_read("demo/404", actor="user")


# ---------------------------------------------------------------------------
# archive_task
# ---------------------------------------------------------------------------


class TestArchiveTask:
    def test_archive_flips_status_to_done(self, svc):
        task_id = _inbox_task(svc)
        archived = svc.archive_task(task_id, actor="user")
        assert archived.work_status == WorkStatus.DONE

    def test_archive_records_transition(self, svc):
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

    def test_archive_is_idempotent(self, svc):
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

    def test_archive_on_missing_task_raises(self, svc):
        with pytest.raises(TaskNotFoundError):
            svc.archive_task("demo/777", actor="user")
