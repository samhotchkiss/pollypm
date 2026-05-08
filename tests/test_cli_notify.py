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
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from typer.testing import CliRunner
from pollypm.work.sqlite_service import SQLiteWorkService

from pollypm.cli import app as root_app
from pollypm.store import SQLAlchemyStore
from pollypm.work.models import WorkStatus
from pollypm.work.sqlite_service import SQLiteWorkService


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
        task_id = result.output.strip().splitlines()[-1]
        assert task_id == "inbox/1"

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["type"] == "notify"
        assert row["tier"] == "immediate"
        # Title contract auto-stamps [Action] for immediate notify.
        assert row["subject"].startswith("[Action]")
        assert "Deploy blocked" in row["subject"]
        assert row["body"] == "Needs verification email click."
        assert row["state"] == "closed"
        assert row["recipient"] == "user"
        # sender defaults to 'polly'; payload carries actor + project.
        payload = json.loads(row["payload_json"])
        assert payload["actor"] == "polly"
        assert payload["project"] == "inbox"
        assert payload["task_id"] == task_id

        svc = SQLiteWorkService(db_path=db_path)
        try:
            task = svc.get(task_id)
        finally:
            svc.close()
        assert task.title == "Deploy blocked"
        assert task.roles["requester"] == "user"
        assert "notify" in task.labels
        assert f"notify_message:{row['id']}" in task.labels

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

    def test_user_prompt_json_with_summary_passes(self, db_path):
        """A non-empty user_prompt with at least one of summary/steps/
        question is the contract — accept and persist."""
        prompt = json.dumps(
            {"summary": "A full plan is ready for your review."}
        )
        result = _invoke_notify(
            db_path,
            "Plan ready",
            "Review this plan.",
            "--priority", "immediate",
            "--user-prompt-json", prompt,
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_messages(db_path)
        payload = json.loads(rows[0]["payload_json"])
        assert payload["user_prompt"]["summary"] == (
            "A full plan is ready for your review."
        )

    def test_user_prompt_json_invalid_json_exits_nonzero(self, db_path):
        result = _invoke_notify(
            db_path,
            "Plan ready",
            "Review this plan.",
            "--user-prompt-json", "{not valid",
        )
        assert result.exit_code != 0
        assert "not valid JSON" in (result.output + (result.stderr or ""))

    def test_user_prompt_json_must_be_object_not_array(self, db_path):
        result = _invoke_notify(
            db_path,
            "Plan ready",
            "Review this plan.",
            "--user-prompt-json", "[]",
        )
        assert result.exit_code != 0
        assert "must decode to an object" in (
            result.output + (result.stderr or "")
        )

    def test_user_prompt_json_empty_object_exits_nonzero(self, db_path):
        """An empty user_prompt has nothing for the dashboard's Action
        Needed card to render — that's a producer-side bug we should
        catch immediately, not let it silently degrade to body
        heuristics in the dashboard hours later."""
        result = _invoke_notify(
            db_path,
            "Plan ready",
            "Review this plan.",
            "--user-prompt-json", "{}",
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr or "")
        assert "must include at least one of" in combined
        assert "summary" in combined
        assert "steps" in combined
        assert "question" in combined

    def test_user_prompt_json_action_kind_validated(self, db_path):
        """Unknown action ``kind`` values silently fall through to
        record-response in the dashboard — the operator clicks a
        button and nothing happens. Reject at producer time."""
        prompt = json.dumps(
            {
                "summary": "A plan is ready.",
                "actions": [
                    {"label": "Review", "kind": "approveTask"},  # camelCase typo
                ],
            }
        )
        result = _invoke_notify(
            db_path,
            "Plan ready",
            "Review this plan.",
            "--user-prompt-json", prompt,
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr or "")
        assert "unknown kind 'approveTask'" in combined
        # Error names every supported kind so the producer can pick the
        # right one without grepping the source.
        for known in (
            "approve_task",
            "review_plan",
            "open_task",
            "open_inbox",
            "discuss_pm",
            "record_response",
        ):
            assert known in combined

    def test_user_prompt_json_action_missing_label_rejected(self, db_path):
        prompt = json.dumps(
            {
                "summary": "X",
                "actions": [{"kind": "approve_task"}],  # no label
            }
        )
        result = _invoke_notify(
            db_path,
            "Plan ready",
            "Review.",
            "--user-prompt-json", prompt,
        )
        assert result.exit_code != 0
        assert "missing a non-empty 'label'" in (
            result.output + (result.stderr or "")
        )

    def test_user_prompt_json_action_missing_kind_rejected(self, db_path):
        prompt = json.dumps(
            {
                "summary": "X",
                "actions": [{"label": "Approve"}],  # no kind
            }
        )
        result = _invoke_notify(
            db_path,
            "Plan ready",
            "Review.",
            "--user-prompt-json", prompt,
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr or "")
        assert "missing a non-empty 'kind'" in combined
        # The label is named so the producer can find which action
        # tripped the validator without re-counting array indices.
        assert "Approve" in combined

    def test_immediate_user_notify_without_user_prompt_warns(self, db_path):
        """An immediate-priority user-facing notify without ``--user-
        prompt-json`` is accepted (back-compat for legacy callers) but
        produces a stderr warning so the operator knows the dashboard
        will degrade to body heuristics. Pairs with the scan-side
        ``user_action_message_missing_user_prompt`` invariant — this
        is the emit-side reminder.
        """
        result = _invoke_notify(
            db_path,
            "Plan ready",
            "Review this plan.",
            "--priority", "immediate",
        )
        # Exit 0 — warning, not rejection. Existing scripts keep working.
        assert result.exit_code == 0, result.output
        combined = result.output + (result.stderr or "")
        assert "Warning" in combined
        assert "--user-prompt-json" in combined
        assert "Action Needed" in combined or "heuristic" in combined.lower()
        # Message still landed.
        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        assert rows[0]["tier"] == "immediate"

    def test_immediate_user_notify_with_user_prompt_does_not_warn(self, db_path):
        """When the producer DOES pass ``--user-prompt-json``, no
        warning fires — the contract is satisfied."""
        prompt = json.dumps({"summary": "Plan is ready."})
        result = _invoke_notify(
            db_path,
            "Plan ready",
            "Review this plan.",
            "--priority", "immediate",
            "--user-prompt-json", prompt,
        )
        assert result.exit_code == 0, result.output
        combined = result.output + (result.stderr or "")
        # No warning about missing user_prompt — contract met.
        assert "Warning" not in combined or "user-prompt-json" not in combined

    def test_digest_priority_does_not_warn_about_user_prompt(self, db_path):
        """The user_prompt contract only applies to immediate-tier
        notifies; digest messages bucket up at flush time and don't
        render Action Needed cards directly."""
        result = _invoke_notify(
            db_path,
            "FYI: weekly digest",
            "Body content here.",
            "--priority", "digest",
        )
        assert result.exit_code == 0, result.output
        combined = result.output + (result.stderr or "")
        assert "Warning" not in combined or "user-prompt-json" not in combined

    def test_polly_recipient_does_not_warn_about_user_prompt(self, db_path):
        """The contract targets the user inbox specifically. Polly's
        own inbox doesn't render Action Needed cards the same way, so
        skipping the warning here keeps the noise floor down."""
        result = _invoke_notify(
            db_path,
            "Plan ready for fast-track",
            "Body content here.",
            "--priority", "immediate",
            "--requester", "polly",
        )
        assert result.exit_code == 0, result.output
        combined = result.output + (result.stderr or "")
        assert "Warning" not in combined or "user-prompt-json" not in combined

    def test_user_prompt_json_action_must_be_object(self, db_path):
        prompt = json.dumps(
            {
                "summary": "X",
                "actions": ["not an object"],
            }
        )
        result = _invoke_notify(
            db_path,
            "Plan ready",
            "Review.",
            "--user-prompt-json", prompt,
        )
        assert result.exit_code != 0
        assert "must be an object" in (
            result.output + (result.stderr or "")
        )

    def test_user_prompt_json_known_kinds_pass(self, db_path):
        """All currently-supported action kinds must pass validation
        — guards against the kind set drifting from the dashboard's
        dispatch table."""
        for kind in (
            "approve_task",
            "review_plan",
            "open_task",
            "open_inbox",
            "discuss_pm",
            "record_response",
        ):
            prompt = json.dumps(
                {
                    "summary": "A plan is ready.",
                    "actions": [{"label": "X", "kind": kind}],
                }
            )
            result = _invoke_notify(
                db_path,
                "Plan ready",
                "Review.",
                "--user-prompt-json", prompt,
            )
            assert result.exit_code == 0, (
                f"kind={kind!r} should validate: {result.output}"
            )

    def test_user_prompt_json_steps_only_passes(self, db_path):
        """Steps alone is enough — the dashboard renders 'What to do'
        from steps even without summary."""
        prompt = json.dumps(
            {"steps": ["Open the plan review surface", "Approve"]}
        )
        result = _invoke_notify(
            db_path,
            "Plan ready",
            "Review this plan.",
            "--priority", "immediate",
            "--user-prompt-json", prompt,
        )
        assert result.exit_code == 0, result.output

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


def _review_ready_service(tmp_path: Path):
    db_path = tmp_path / "work.db"
    svc = SQLiteWorkService(db_path=db_path)
    task = svc.create(
        title="Needs review",
        description="Exercise reviewer escalation hold",
        type="task",
        project="proj",
        flow_template="standard",
        roles={"worker": "agent-1", "reviewer": "reviewer"},
        priority="normal",
        created_by="tester",
    )
    svc.queue(task.task_id, "pm")
    svc.claim(task.task_id, "agent-1")
    svc.node_done(
        task.task_id,
        "agent-1",
        {
            "type": "code_change",
            "summary": "Implemented the feature",
            "artifacts": [
                {
                    "kind": "commit",
                    "description": "feat: implementation",
                    "ref": "abc123",
                }
            ],
        },
    )
    return svc, task.task_id, db_path


class TestNotifyReviewerEscalations:
    def test_infer_notify_actor_from_current_reviewer_window(self, monkeypatch, tmp_path: Path):
        from pollypm.cli_features.session_runtime import _infer_notify_actor

        class _Tmux:
            def current_session_name(self):
                return "pollypm-storage-closet"

            def current_window_index(self):
                return "2"

            def list_windows(self, _session_name):
                return [SimpleNamespace(index=2, name="pm-reviewer")]

        config = SimpleNamespace(
            sessions={
                "reviewer": SimpleNamespace(window_name="pm-reviewer"),
            }
        )

        monkeypatch.setattr(
            "pollypm.session_services.create_tmux_client",
            lambda: _Tmux(),
        )
        monkeypatch.setattr(
            "pollypm.config.load_config",
            lambda _path=None: config,
        )

        actor, session_name = _infer_notify_actor(
            tmp_path / "pollypm.toml",
            "polly",
        )

        assert actor == "reviewer"
        assert session_name == "reviewer"

    def test_reviewer_notify_keeps_review_task_in_review(self, monkeypatch, tmp_path: Path):
        from pollypm.cli_features.session_runtime import _hold_review_tasks_for_notify

        svc, task_id, db_path = _review_ready_service(tmp_path)
        monkeypatch.setattr(
            "pollypm.work.cli._resolve_db_path",
            lambda db, project=None: db_path,
        )

        held = _hold_review_tasks_for_notify(
            actor="reviewer",
            current_session_name="reviewer",
            priority="immediate",
            subject=f"{task_id} needs operator help",
            body="Waiting on deploy credentials.",
        )

        assert held == []
        task = svc.get(task_id)
        assert task.work_status == WorkStatus.REVIEW

    def test_reviewer_review_ready_notify_does_not_hold_review_task(
        self, monkeypatch, tmp_path: Path,
    ):
        from pollypm.cli_features.session_runtime import _hold_review_tasks_for_notify

        svc, task_id, db_path = _review_ready_service(tmp_path)
        monkeypatch.setattr(
            "pollypm.work.cli._resolve_db_path",
            lambda db, project=None: db_path,
        )

        held = _hold_review_tasks_for_notify(
            actor="reviewer",
            current_session_name="reviewer",
            priority="immediate",
            subject=f"Done: {task_id} handed to review",
            body="Press A to approve.",
        )

        assert held == []
        task = svc.get(task_id)
        assert task.work_status == WorkStatus.REVIEW


def _patch_tmux_session(monkeypatch, *, window_name: str, sessions: dict[str, object]):
    """Wire up a fake tmux client + ``load_config`` for project-inference tests.

    The fake tmux always reports a single window with ``window_name`` so
    the session match is deterministic; ``sessions`` mirrors the
    ``SessionConfig`` shape (``window_name`` + ``project``) the inference
    helpers consume. Returns nothing — the monkeypatch installs both
    seams in-place.
    """

    class _Tmux:
        def current_session_name(self):
            return "pollypm-storage-closet"

        def current_window_index(self):
            return "2"

        def list_windows(self, _session_name):
            return [SimpleNamespace(index=2, name=window_name)]

    config = SimpleNamespace(sessions=sessions)
    monkeypatch.setattr(
        "pollypm.session_services.create_tmux_client",
        lambda: _Tmux(),
    )
    monkeypatch.setattr(
        "pollypm.config.load_config",
        lambda _path=None: config,
    )


class TestNotifyDefaultProjectInference:
    """Issue #1425 — ``pm notify`` from a project-scoped pane should
    default-route to that project, not the synthetic global ``inbox``
    bucket. Regression caught when reviewer-savethenovel's substantive
    review feedback landed at ``project=inbox`` and was invisible from
    every project-filtered surface."""

    def test_infer_notify_project_from_reviewer_window(
        self, monkeypatch, tmp_path: Path,
    ):
        from pollypm.cli_features.session_runtime import _infer_notify_project

        _patch_tmux_session(
            monkeypatch,
            window_name="reviewer-savethenovel",
            sessions={
                "reviewer_savethenovel": SimpleNamespace(
                    window_name="reviewer-savethenovel",
                    project="savethenovel",
                ),
            },
        )

        inferred = _infer_notify_project(tmp_path / "pollypm.toml")

        assert inferred == "savethenovel"

    def test_infer_notify_project_returns_none_without_match(
        self, monkeypatch, tmp_path: Path,
    ):
        from pollypm.cli_features.session_runtime import _infer_notify_project

        _patch_tmux_session(
            monkeypatch,
            window_name="some-unrelated-window",
            sessions={
                "reviewer_savethenovel": SimpleNamespace(
                    window_name="reviewer-savethenovel",
                    project="savethenovel",
                ),
            },
        )

        assert _infer_notify_project(tmp_path / "pollypm.toml") is None

    def test_notify_without_project_uses_inferred_project(
        self, monkeypatch, db_path,
    ):
        """Reviewer-savethenovel calls ``pm notify`` (no ``--project``).
        The message + task should land at ``project=savethenovel``."""
        monkeypatch.setattr(
            "pollypm.cli_features.session_runtime._infer_notify_project",
            lambda _config_path: "savethenovel",
        )

        result = _invoke_notify(
            db_path,
            "Rejecting savethenovel/11",
            "Footer.astro:20-22 still has placeholder copy.",
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        row = rows[0]
        # Message scope + payload.project both reflect the inferred key.
        assert row["scope"] == "savethenovel"
        payload = json.loads(row["payload_json"])
        assert payload["project"] == "savethenovel"

        # The work-service inbox task created for immediate-priority
        # notifies inherits the same project.
        svc = SQLiteWorkService(db_path=db_path)
        try:
            task = svc.get(payload["task_id"])
        finally:
            svc.close()
        assert task.project == "savethenovel"

    def test_notify_explicit_project_wins_over_inference(
        self, monkeypatch, db_path,
    ):
        """``--project inbox`` (or any explicit value) must override the
        inferred project so global broadcasts still work from a
        project-scoped pane."""
        monkeypatch.setattr(
            "pollypm.cli_features.session_runtime._infer_notify_project",
            lambda _config_path: "savethenovel",
        )

        result = _invoke_notify(
            db_path,
            "Site-wide announcement",
            "Cockpit reboot in 5m.",
            "--project", "inbox",
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_messages(db_path)
        assert rows[0]["scope"] == "inbox"
        payload = json.loads(rows[0]["payload_json"])
        assert payload["project"] == "inbox"

    def test_notify_falls_back_to_inbox_without_inferred_project(
        self, monkeypatch, db_path,
    ):
        """Outside a managed pane (no tmux match), ``pm notify`` keeps
        its legacy ``project=inbox`` default so existing scripts and
        ad-hoc operator invocations don't silently change semantics."""
        monkeypatch.setattr(
            "pollypm.cli_features.session_runtime._infer_notify_project",
            lambda _config_path: None,
        )

        result = _invoke_notify(
            db_path,
            "Operator note",
            "Posted from a bare shell.",
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_messages(db_path)
        assert rows[0]["scope"] == "inbox"
        payload = json.loads(rows[0]["payload_json"])
        assert payload["project"] == "inbox"

    def test_reviewer_savethenovel_integration(self, monkeypatch, db_path):
        """Integration: simulate the exact reviewer-savethenovel pane
        from issue #1425. The reviewer calls ``pm notify`` (no flags)
        and the resulting message + inbox task carry
        ``project=savethenovel`` — visible from
        ``pm message list --project savethenovel`` and the cockpit's
        project-filtered inbox."""
        _patch_tmux_session(
            monkeypatch,
            window_name="reviewer-savethenovel",
            sessions={
                "reviewer_savethenovel": SimpleNamespace(
                    window_name="reviewer-savethenovel",
                    project="savethenovel",
                ),
            },
        )

        result = _invoke_notify(
            db_path,
            "Rejecting savethenovel/11",
            "Footer.astro:20-22 still has placeholder copy. "
            "Needs operator decision before re-queue.",
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        row = rows[0]
        # Sender flips from default ``polly`` to the matched session
        # name (existing actor inference behavior).
        assert row["sender"] == "reviewer_savethenovel"
        # And the project routing — the regression — lands at
        # ``savethenovel`` rather than the synthetic ``inbox`` bucket.
        assert row["scope"] == "savethenovel"
        payload = json.loads(row["payload_json"])
        assert payload["project"] == "savethenovel"
        assert payload["actor"] == "reviewer_savethenovel"
