"""Tests for ``pm notify`` — writes into the unified ``messages`` table.

Issue #340 collapsed the three-branched ``pm notify`` path (silent /
digest / immediate, each hitting a different table) into a single
:meth:`Store.enqueue_message` call. The test surface moved with it:

- ``test_cli_notify.py`` now asserts on the ``messages`` table.
- ``pm inbox`` visibility lives under Issue E (reader migration) — the
  writer-side smoke is what this file owns.

Run with ``HOME=/tmp/pytest-storage-d uv run pytest -x
tests/test_cli_notify.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from pollypm.cli import app as root_app
from pollypm.store import SQLAlchemyStore


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


def _fetch_messages(db_path: str) -> list[dict]:
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        with store.read_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM messages ORDER BY id ASC")
            ).mappings().all()
        return [dict(r) for r in rows]
    finally:
        store.close()


class TestNotifyWritesToMessages:
    def test_immediate_message_lands_in_messages_table(self, db_path):
        result = _invoke_notify(
            db_path,
            "Deploy blocked",
            "Needs verification email click.",
        )
        assert result.exit_code == 0, result.output
        row_id = result.output.strip().splitlines()[-1]
        assert row_id.isdigit(), (
            f"immediate tier should emit an integer row id, got {row_id!r}"
        )

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["type"] == "notify"
        assert row["tier"] == "immediate"
        # Title contract auto-stamps [Action] for immediate notify.
        assert row["subject"].startswith("[Action]")
        assert "Deploy blocked" in row["subject"]
        assert row["body"] == "Needs verification email click."
        assert row["state"] == "open"
        assert row["recipient"] == "user"
        # sender defaults to 'polly'; payload carries actor + project.
        payload = json.loads(row["payload_json"])
        assert payload["actor"] == "polly"
        assert payload["project"] == "inbox"

    def test_digest_tier_lands_staged(self, db_path):
        # ``done`` + ``merged`` → classifier picks digest.
        result = _invoke_notify(
            db_path,
            "Task done",
            "PR merged cleanly.",
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip().startswith("digest:")

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        assert rows[0]["tier"] == "digest"
        assert rows[0]["state"] == "staged"
        # Digest tier auto-stamps [FYI].
        assert rows[0]["subject"].startswith("[FYI]")

    def test_silent_tier_lands_closed(self, db_path):
        result = _invoke_notify(
            db_path,
            "Audit trace",
            "Recorded for the log.",
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "silent"

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        assert rows[0]["tier"] == "silent"
        assert rows[0]["state"] == "closed"
        # Silent-tier gets the [Audit] tag.
        assert rows[0]["subject"].startswith("[Audit]")

    def test_actor_flag_is_recorded_on_message(self, db_path):
        result = _invoke_notify(
            db_path,
            "Heads up",
            "Something happened.",
            "--actor", "morning-briefing",
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        assert rows[0]["sender"] == "morning-briefing"
        payload = json.loads(rows[0]["payload_json"])
        assert payload["actor"] == "morning-briefing"

    def test_body_from_stdin_via_dash(self, db_path):
        result = _invoke_notify(
            db_path,
            "Long body",
            "-",
            input_text="line 1\nline 2\nline 3\n",
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        body = rows[0]["body"]
        assert "line 1" in body
        assert "line 3" in body

    def test_empty_subject_exits_nonzero(self, db_path):
        result = _invoke_notify(db_path, "", "non-empty body")
        assert result.exit_code != 0, result.output

    def test_requester_polly_routes_to_polly_inbox(self, db_path):
        result = _invoke_notify(
            db_path,
            "Plan ready",
            "Review this plan.",
            "--requester", "polly",
            "--priority", "immediate",
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        assert rows[0]["recipient"] == "polly"

    def test_labels_are_attached(self, db_path):
        result = _invoke_notify(
            db_path,
            "Plan ready",
            "Please review",
            "--priority", "immediate",
            "--label", "plan_review",
            "--label", "plan_task:demo/5",
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_messages(db_path)
        labels = json.loads(rows[0]["labels"])
        assert "plan_review" in labels
        assert "plan_task:demo/5" in labels
