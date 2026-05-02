"""``pm inbox`` shows user-assigned task rows created by ``pm notify``.

Actionable inbox items are tasks assigned to the user. Store messages remain
audit/activity infrastructure and may link back to those tasks, but they are
not the source of truth for user action.

Post-#1013 the ``notify``-labelled chat-flow tasks that ``pm notify``
materialises are hidden from ``pm inbox`` by default — they're stubs that
the cockpit inbox pane handles via its own structured-action affordances,
and the plain CLI listing buried genuinely actionable rows under them.
Pass ``--include-inbox`` to restore the pre-#1013 behaviour for
debugging.

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

        # Default ``pm inbox`` hides notify-backed stub tasks (#1013).
        # Resurface them with ``--include-inbox``.
        inbox = runner.invoke(
            inbox_app, ["--db", db_path, "--json", "--include-inbox"],
        )
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
        # ``--include-inbox`` resurfaces the stub for debugging.
        inbox = runner.invoke(
            inbox_app, ["--db", db_path, "--include-inbox"],
        )
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
        # ``--include-inbox`` is required to see the notify-backed
        # stub tasks (#1013); this test verifies the project filter
        # still narrows correctly when they're surfaced.
        inbox = runner.invoke(
            inbox_app,
            [
                "--db", db_path, "--project", "alpha",
                "--json", "--include-inbox",
            ],
        )
        payload = json.loads(inbox.output)
        assert payload["messages"] == []
        assert len(payload["tasks"]) == 1
        assert "Subject A" in payload["tasks"][0]["title"]


class TestNotifyHiddenWithoutOptIn:
    """#1013 — ``pm notify`` task rows are stubs hidden by default.

    The architect's stage-7 plan_review handoff lands as a chat-flow
    task carrying the ``notify`` label so the cockpit inbox pane can
    render it with structured action affordances. Those rows have no
    node-level transition affordance the CLI listing can act on, so
    listing them by default just buries genuinely actionable items
    (the original sin behind issue #1013, which had 12 of these stubs
    in a 59-item inbox).
    """

    def test_default_inbox_hides_notify_stub_tasks(self, db_path):
        """``pm inbox`` (no flags) hides ``pm notify``-derived task rows."""
        result = _invoke_notify(
            db_path, "Plan ready for review: bikepath",
            "The architect produced a plan; review and approve.",
            "--label", "plan_review",
            "--label", "plan_task:bikepath/1",
        )
        assert result.exit_code == 0, result.output

        inbox = runner.invoke(inbox_app, ["--db", db_path, "--json"])
        assert inbox.exit_code == 0, inbox.output
        payload = json.loads(inbox.output)
        # Stub task is hidden by default.
        assert payload["tasks"] == []

    def test_default_inbox_text_hides_notify_stub_tasks(self, db_path):
        """Text rendering also hides notify-backed stubs by default."""
        result = _invoke_notify(
            db_path, "Plan ready for review: smoketest",
            "The architect produced a plan; review and approve.",
            "--label", "plan_review",
        )
        assert result.exit_code == 0, result.output

        inbox = runner.invoke(inbox_app, ["--db", db_path])
        assert inbox.exit_code == 0, inbox.output
        # The stub title must NOT appear in the default text listing.
        assert "Plan ready for review" not in inbox.output

    def test_include_inbox_resurfaces_notify_stubs(self, db_path):
        """``--include-inbox`` opt-in restores the pre-#1013 behaviour."""
        result = _invoke_notify(
            db_path, "Plan ready for review: bikepath",
            "The architect produced a plan; review and approve.",
            "--label", "plan_review",
        )
        assert result.exit_code == 0, result.output

        inbox = runner.invoke(
            inbox_app, ["--db", db_path, "--json", "--include-inbox"],
        )
        assert inbox.exit_code == 0, inbox.output
        payload = json.loads(inbox.output)
        assert len(payload["tasks"]) == 1
        assert "Plan ready for review" in payload["tasks"][0]["title"]
        assert "notify" in payload["tasks"][0]["labels"]

    def test_real_work_tasks_remain_visible_with_filter(self, db_path):
        """A genuine user-facing chat task is NOT filtered by the
        notify-stub predicate — the filter is keyed on the ``notify``
        label specifically. A real chat-flow conversation (no ``notify``
        label) stays visible in ``pm inbox`` by default."""
        from pollypm.work.sqlite_service import SQLiteWorkService
        from pathlib import Path as _P

        svc = SQLiteWorkService(db_path=_P(db_path))
        try:
            svc.create(
                title="Real chat task",
                description="user-facing question",
                type="task",
                project="proj",
                flow_template="chat",
                roles={"requester": "user", "operator": "polly"},
                priority="normal",
                created_by="polly",
                # No ``notify`` label — this is a real conversation,
                # not an architect handoff stub.
            )
        finally:
            svc.close()

        # Default listing — no ``--include-inbox`` — shows the real task.
        inbox = runner.invoke(inbox_app, ["--db", db_path, "--json"])
        assert inbox.exit_code == 0, inbox.output
        payload = json.loads(inbox.output)
        titles = [t["title"] for t in payload["tasks"]]
        assert "Real chat task" in titles


class TestNotifyMessagesHiddenByDefault:
    """#1027 — ``pm inbox`` text listing default-hides ``notify``-type
    messages so completion announcements / heartbeat alerts don't bury
    the single actionable row the user needs to look at. JSON output
    keeps the canonical full surface for tooling consumers; only the
    text rendering applies the lens.
    """

    def _seed_notify_message(self, db_path: str, *, subject: str) -> None:
        from pollypm.store import SQLAlchemyStore

        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            store.enqueue_message(
                type="notify",
                tier="immediate",
                recipient="user",
                sender="polly",
                subject=subject,
                body="background FYI",
                scope="inbox",
            )
        finally:
            store.close()

    def _seed_alert_message(self, db_path: str, *, subject: str) -> None:
        from pollypm.store import SQLAlchemyStore

        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            store.enqueue_message(
                type="alert",
                tier="immediate",
                recipient="user",
                sender="supervisor",
                subject=subject,
                body="actionable supervisor alert",
                scope="inbox",
            )
        finally:
            store.close()

    def test_default_text_inbox_hides_notify_messages(self, db_path):
        """``Done:`` style notify rows must not appear in the default text view."""
        self._seed_notify_message(db_path, subject="Done: Phase 2 rework resubmitted")
        inbox = runner.invoke(inbox_app, ["--db", db_path])
        assert inbox.exit_code == 0, inbox.output
        # Title was hidden behind the toggle.
        assert "Done: Phase 2 rework" not in inbox.output
        # Footer announces the hidden count + opt-in flag.
        assert "1 notification hidden" in inbox.output
        assert "--all" in inbox.output

    def test_default_text_inbox_keeps_alerts_visible(self, db_path):
        """Actionable ``alert`` rows stay visible — only ``notify`` is hidden."""
        self._seed_alert_message(db_path, subject="Worker stuck in bikepath/3")
        inbox = runner.invoke(inbox_app, ["--db", db_path])
        assert inbox.exit_code == 0, inbox.output
        assert "Worker stuck in bikepath/3" in inbox.output
        # No hidden-count footer when nothing is hidden.
        assert "notification hidden" not in inbox.output

    def test_all_flag_resurfaces_notify_messages(self, db_path):
        """``--all`` opts back in to the historical full listing."""
        self._seed_notify_message(db_path, subject="Done: Phase 2 rework resubmitted")
        inbox = runner.invoke(inbox_app, ["--db", db_path, "--all"])
        assert inbox.exit_code == 0, inbox.output
        assert "Done: Phase 2 rework" in inbox.output
        # No hidden-count footer when the user has explicitly opted in.
        assert "notification hidden" not in inbox.output

    def test_default_json_inbox_keeps_full_surface(self, db_path):
        """JSON consumers (cockpit, scripts) need the canonical merged
        list regardless of the text-rendering lens."""
        self._seed_notify_message(db_path, subject="Done: shipped")
        inbox = runner.invoke(inbox_app, ["--db", db_path, "--json"])
        assert inbox.exit_code == 0, inbox.output
        payload = json.loads(inbox.output)
        titles = [m["title"] for m in payload["messages"]]
        # Notify producers normalize the subject with an ``[Action]`` /
        # ``[FYI]`` prefix; assert by substring so the test is robust
        # to that contract without re-implementing it.
        assert any("Done: shipped" in t for t in titles)

    def test_pluralisation_when_multiple_hidden(self, db_path):
        """Footer copy uses the plural ``notifications`` for >1 hidden."""
        self._seed_notify_message(db_path, subject="Done: A")
        self._seed_notify_message(db_path, subject="Done: B")
        inbox = runner.invoke(inbox_app, ["--db", db_path])
        assert inbox.exit_code == 0, inbox.output
        assert "2 notifications hidden" in inbox.output
        assert "--all" in inbox.output

    def test_only_notifications_renders_footer_alone(self, db_path):
        """When EVERY row is a hidden notification, the table is replaced
        by the footer line so the user still knows there's something
        behind ``--all``.
        """
        self._seed_notify_message(db_path, subject="Done: nothing actionable")
        inbox = runner.invoke(inbox_app, ["--db", db_path])
        assert inbox.exit_code == 0, inbox.output
        assert "Inbox: 0 items" in inbox.output
        assert "1 notification hidden" in inbox.output
        # The column header is omitted because there's nothing to render.
        assert "ID" not in inbox.output.split("Inbox:")[1].splitlines()[0]
