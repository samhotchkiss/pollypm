"""``pm inbox`` shows user-assigned task rows created by ``pm notify``.

Actionable inbox items are tasks assigned to the user. Store messages remain
audit/activity infrastructure and may link back to those tasks, but they are
not the source of truth for user action.

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
        assert payload["messages"] == []
        tasks = payload["tasks"]
        assert len(tasks) == 1
        task = tasks[0]
        assert task["task_id"] == "inbox/1"
        assert "Deploy blocked" in task["title"]
        assert "notify" in task["labels"]

    def test_immediate_notify_shows_in_text_inbox(self, db_path):
        result = _invoke_notify(
            db_path, "Ready to review", "Check the latest build.",
        )
        assert result.exit_code == 0, result.output
        inbox = runner.invoke(inbox_app, ["--db", db_path])
        assert inbox.exit_code == 0, inbox.output
        assert "Inbox:" in inbox.output
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
        assert payload["messages"] == []
        assert len(payload["tasks"]) == 1
        assert "Subject A" in payload["tasks"][0]["title"]
