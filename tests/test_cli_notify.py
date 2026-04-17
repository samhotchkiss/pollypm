"""Tests for ``pm notify`` — routes writes into the work-service inbox.

The notification must land as a task that :mod:`pollypm.work.inbox_view`
considers an inbox member, so ``pm inbox`` surfaces it through the same
read path operators use.

Tests cover:
  * create creates a work-service task (not a legacy ``inbox_messages`` row)
  * created task appears in ``pm inbox list`` via the canonical read path
  * ``--actor`` is recorded on the task
  * ``-`` body reads from stdin
  * empty subject yields non-zero exit
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.cli import app as root_app
from pollypm.storage.state import StateStore
from pollypm.work.cli import task_app
from pollypm.work.inbox_cli import inbox_app


runner = CliRunner()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


def _invoke_notify(db_path: str, *args: str, input_text: str | None = None):
    return runner.invoke(
        root_app,
        ["notify", *args, "--db", db_path],
        input=input_text,
    )


class TestNotifyCreatesWorkServiceTask:
    def test_notify_creates_task_visible_to_pm_inbox(self, db_path):
        result = _invoke_notify(
            db_path,
            "Deploy blocked",
            "Needs verification email click.",
        )
        assert result.exit_code == 0, result.output
        task_id = result.output.strip().splitlines()[-1]
        assert "/" in task_id, f"expected project/number task_id, got {task_id!r}"

        # The task must show up via the same read path pm inbox uses.
        inbox_result = runner.invoke(
            inbox_app, ["--db", db_path, "--json"]
        )
        assert inbox_result.exit_code == 0, inbox_result.output
        payload = json.loads(inbox_result.output)
        assert payload["assigned_count"] == 1
        assert payload["tasks"][0]["task_id"] == task_id
        assert payload["tasks"][0]["title"] == "Deploy blocked"
        assert payload["tasks"][0]["description"] == (
            "Needs verification email click."
        )

    def test_notify_writes_to_work_tasks_not_legacy_inbox_messages(
        self, db_path,
    ):
        """Regression: the previous attempt at #258 wrote to the legacy
        ``inbox_messages`` table, which ``pm inbox`` does not read from.
        Ensure we wrote to ``work_tasks`` and *not* to ``inbox_messages``.
        """
        result = _invoke_notify(db_path, "Subject", "Body")
        assert result.exit_code == 0, result.output

        # inbox_messages table is part of the legacy schema and gets
        # created whenever StateStore opens the db — we must not have
        # inserted a row there.
        store = StateStore(Path(db_path))
        try:
            rows = store.list_inbox_messages()
            assert rows == [], (
                "pm notify must not write to the deprecated inbox_messages "
                "table — route through work-service instead; got: " + str(rows)
            )
        finally:
            store.close()

    def test_actor_flag_is_recorded_on_task(self, db_path):
        result = _invoke_notify(
            db_path,
            "Heads up",
            "Something happened.",
            "--actor", "morning-briefing",
        )
        assert result.exit_code == 0, result.output
        task_id = result.output.strip().splitlines()[-1]

        # created_by + roles.operator both carry the actor.
        get_result = runner.invoke(
            task_app,
            ["get", task_id, "--db", db_path, "--json"],
        )
        assert get_result.exit_code == 0, get_result.output
        task = json.loads(get_result.output)
        assert task["roles"].get("operator") == "morning-briefing"
        # Canonical user-mark — this is what makes it an inbox item.
        assert task["roles"].get("requester") == "user"

    def test_body_from_stdin_via_dash(self, db_path):
        result = _invoke_notify(
            db_path,
            "Long body",
            "-",
            input_text="line 1\nline 2\nline 3\n",
        )
        assert result.exit_code == 0, result.output
        task_id = result.output.strip().splitlines()[-1]

        get_result = runner.invoke(
            task_app,
            ["get", task_id, "--db", db_path, "--json"],
        )
        assert get_result.exit_code == 0, get_result.output
        task = json.loads(get_result.output)
        assert "line 1" in task["description"]
        assert "line 3" in task["description"]

    def test_empty_subject_exits_nonzero(self, db_path):
        result = _invoke_notify(db_path, "", "non-empty body")
        assert result.exit_code != 0, result.output
