"""``pm inbox`` post-#341 shows messages-table rows alongside work-tasks.

Issue #340 collapsed ``pm notify`` onto :meth:`Store.enqueue_message`,
so items written by ``pm notify`` never reach the work_tasks chat flow.
Issue #341 completes the migration on the reader side: ``pm inbox``
now UNIONs the messages table (via the legacy bridge) with the
existing work-service inbox query. This test file pins the
notify-then-inbox round trip so a regression wouldn't silently hide
every ``pm notify`` the user makes.

Run with ``HOME=/tmp/pytest-storage-e uv run pytest -x
tests/test_inbox_messages_reader.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.cli import app as root_app
from pollypm.work.inbox_cli import inbox_app


runner = CliRunner()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


def _invoke_notify(db_path: str, *args: str):
    return runner.invoke(
        root_app,
        ["notify", *args, "--db", db_path],
    )


class TestNotifyVisibleInInbox:
    def test_immediate_notify_surfaces_in_inbox_json(self, db_path):
        result = _invoke_notify(
            db_path, "Deploy blocked", "Needs verification email click.",
        )
        assert result.exit_code == 0, result.output

        inbox = runner.invoke(inbox_app, ["--db", db_path, "--json"])
        assert inbox.exit_code == 0, inbox.output
        payload = json.loads(inbox.output)
        assert payload["assigned_count"] >= 1
        messages = payload["messages"]
        assert len(messages) == 1
        msg = messages[0]
        assert msg["type"] == "notify"
        assert "[Action]" in msg["title"]
        assert "Deploy blocked" in msg["title"]
        # id is prefixed so it never collides with a project/number task id.
        assert msg["id"].startswith("msg:")

    def test_immediate_notify_shows_in_text_inbox(self, db_path):
        result = _invoke_notify(
            db_path, "Ready to review", "Check the latest build.",
        )
        assert result.exit_code == 0, result.output
        inbox = runner.invoke(inbox_app, ["--db", db_path])
        assert inbox.exit_code == 0, inbox.output
        assert "Inbox:" in inbox.output
        assert "[Action]" in inbox.output
        assert "Ready to review" in inbox.output

    def test_digest_notify_does_not_show_until_flushed(self, db_path):
        # Digest-priority notifies land with state='staged' and must not
        # surface in the inbox until the milestone-flush sweep runs.
        result = _invoke_notify(
            db_path,
            "status update",
            "task shipped",
            "--priority", "digest",
        )
        assert result.exit_code == 0, result.output
        inbox = runner.invoke(inbox_app, ["--db", db_path, "--json"])
        assert inbox.exit_code == 0, inbox.output
        payload = json.loads(inbox.output)
        assert payload["messages"] == []

    def test_silent_notify_does_not_show(self, db_path):
        # Silent tier is closed on write — never visible in the inbox.
        result = _invoke_notify(
            db_path,
            "audit entry",
            "background housekeeping",
            "--priority", "silent",
        )
        assert result.exit_code == 0, result.output
        inbox = runner.invoke(inbox_app, ["--db", db_path, "--json"])
        assert inbox.exit_code == 0, inbox.output
        payload = json.loads(inbox.output)
        assert payload["messages"] == []

    def test_project_filter_narrows_messages(self, db_path):
        runner.invoke(
            root_app,
            [
                "notify", "Subject A", "Body A",
                "--db", db_path, "--project", "alpha",
            ],
        )
        runner.invoke(
            root_app,
            [
                "notify", "Subject B", "Body B",
                "--db", db_path, "--project", "beta",
            ],
        )
        inbox = runner.invoke(
            inbox_app, ["--db", db_path, "--project", "alpha", "--json"],
        )
        payload = json.loads(inbox.output)
        assert len(payload["messages"]) == 1
        assert "Subject A" in payload["messages"][0]["title"]
