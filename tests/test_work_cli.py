"""Tests for the work service CLI commands."""

from __future__ import annotations

import json
import importlib.resources
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.work.cli import task_app, flow_app


runner = CliRunner()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def _create_task(db_path, title="Test task", project="proj", priority="normal",
                 roles=None, description="A test task", flow="standard", task_type="task"):
    roles = roles or ["worker=agent-1", "reviewer=agent-2"]
    args = [
        "create", title,
        "--project", project,
        "--flow", flow,
        "--priority", priority,
        "--description", description,
        "--type", task_type,
        "--db", db_path,
    ]
    for r in roles:
        args.extend(["--role", r])
    result = runner.invoke(task_app, args)
    assert result.exit_code == 0, f"create failed: {result.output}"
    return result


# ---------------------------------------------------------------------------
# Task CLI tests
# ---------------------------------------------------------------------------


class TestCliCreate:
    def test_cli_create_and_get(self, db_path):
        result = _create_task(db_path, title="My new task")
        assert "Created proj/1" in result.output

        # Get it back
        result = runner.invoke(task_app, ["get", "proj/1", "--db", db_path])
        assert result.exit_code == 0
        assert "My new task" in result.output
        assert "draft" in result.output


class TestCliList:
    def test_cli_list(self, db_path):
        _create_task(db_path, title="Task A")
        _create_task(db_path, title="Task B")

        result = runner.invoke(task_app, ["list", "--db", db_path])
        assert result.exit_code == 0
        assert "Task A" in result.output
        assert "Task B" in result.output

    def test_cli_list_hides_pm_notify_inbox_tasks(self, db_path):
        """``pm notify`` materialises a chat-flow task carrying the
        ``notify`` label so the cockpit inbox can surface the
        architect's plan_review handoff. Those rows must NOT appear
        in the canonical task list — they have no node-level
        transition affordance and clutter the work view (#1003).
        """
        _create_task(db_path, title="Real work task")
        # Mirror cli_features/session_runtime.py::notify task creation:
        # chat-flow + ``notify`` label + plan_review labels.
        runner.invoke(
            task_app,
            [
                "create",
                "Plan ready for review: proj",
                "--project", "proj",
                "--flow", "chat",
                "--description", "Press A to approve.",
                "--db", db_path,
                "--label", "notify",
                "--label", "notify_message:42",
                "--label", "plan_review",
                "--label", "plan_task:proj/1",
            ],
        )

        # Default: notify-backed inbox row is hidden.
        result = runner.invoke(task_app, ["list", "--db", db_path])
        assert result.exit_code == 0
        assert "Real work task" in result.output
        assert "Plan ready for review" not in result.output

        # Opt-in: ``--include-inbox`` resurfaces them for debugging.
        result = runner.invoke(
            task_app, ["list", "--include-inbox", "--db", db_path],
        )
        assert result.exit_code == 0
        assert "Real work task" in result.output
        assert "Plan ready for review" in result.output


class TestCliLifecycle:
    def test_cli_lifecycle(self, db_path):
        # Create
        _create_task(db_path, title="Lifecycle task", roles=["worker=pete", "reviewer=polly"])

        # Queue
        result = runner.invoke(task_app, ["queue", "proj/1", "--db", db_path])
        assert result.exit_code == 0
        assert "Queued" in result.output

        # Claim
        result = runner.invoke(task_app, ["claim", "proj/1", "--actor", "pete", "--db", db_path])
        assert result.exit_code == 0
        assert "Claimed" in result.output

        # Done (node_done with work output)
        wo = json.dumps({
            "type": "code_change",
            "summary": "Implemented the feature",
            "artifacts": [{"kind": "commit", "description": "abc123", "ref": "abc123"}],
        })
        result = runner.invoke(task_app, ["done", "proj/1", "--output", wo, "--actor", "pete", "--db", db_path])
        assert result.exit_code == 0
        assert "Node done" in result.output

        # Approve
        result = runner.invoke(task_app, ["approve", "proj/1", "--actor", "polly", "--db", db_path])
        assert result.exit_code == 0
        assert "Approved" in result.output

        # Verify final state
        result = runner.invoke(task_app, ["get", "proj/1", "--db", db_path, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["work_status"] == "done"

    def test_cli_approve_without_actor_uses_bound_reviewer(self, db_path):
        _create_task(
            db_path,
            title="Auto approve",
            roles=["worker=pete", "reviewer=polly"],
        )
        assert runner.invoke(task_app, ["queue", "proj/1", "--db", db_path]).exit_code == 0
        assert runner.invoke(
            task_app, ["claim", "proj/1", "--actor", "pete", "--db", db_path],
        ).exit_code == 0
        wo = json.dumps(
            {
                "type": "code_change",
                "summary": "Implemented the feature",
                "artifacts": [
                    {"kind": "commit", "description": "abc123", "ref": "abc123"},
                ],
            }
        )
        assert runner.invoke(
            task_app, ["done", "proj/1", "--output", wo, "--actor", "pete", "--db", db_path],
        ).exit_code == 0

        result = runner.invoke(task_app, ["approve", "proj/1", "--db", db_path])
        assert result.exit_code == 0, result.output
        assert "Approved proj/1" in result.output

    def test_cli_approve_without_actor_uses_human_for_user_review(self, db_path):
        _create_task(
            db_path,
            title="Human approve",
            flow="user-review",
            roles=["worker=pete"],
        )
        assert runner.invoke(task_app, ["queue", "proj/1", "--db", db_path]).exit_code == 0
        assert runner.invoke(
            task_app, ["claim", "proj/1", "--actor", "pete", "--db", db_path],
        ).exit_code == 0
        wo = json.dumps(
            {
                "type": "code_change",
                "summary": "Implemented the feature",
                "artifacts": [
                    {"kind": "commit", "description": "abc123", "ref": "abc123"},
                ],
            }
        )
        assert runner.invoke(
            task_app, ["done", "proj/1", "--output", wo, "--actor", "pete", "--db", db_path],
        ).exit_code == 0

        result = runner.invoke(task_app, ["approve", "proj/1", "--db", db_path])
        assert result.exit_code == 0, result.output
        assert "Approved proj/1" in result.output


class TestCliDoneOutputValidation:
    """``pm task done --output`` validates the work-output payload.

    The work_service is permissive — missing fields default silently —
    so producer-side typos used to slip into the task history with an
    empty work_output. Validate at the CLI so workers see a clear
    contract failure.
    """

    def _setup_claimed_task(self, db_path):
        _create_task(
            db_path,
            title="Lifecycle task",
            roles=["worker=pete", "reviewer=polly"],
        )
        runner.invoke(task_app, ["queue", "proj/1", "--db", db_path])
        runner.invoke(
            task_app,
            ["claim", "proj/1", "--actor", "pete", "--db", db_path],
        )

    def test_done_with_invalid_json_errors(self, db_path):
        self._setup_claimed_task(db_path)
        result = runner.invoke(
            task_app,
            [
                "done", "proj/1", "--output", "{not valid",
                "--actor", "pete", "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        assert "not valid JSON" in (result.output + (result.stderr or ""))

    def test_done_with_non_object_output_errors(self, db_path):
        self._setup_claimed_task(db_path)
        result = runner.invoke(
            task_app,
            [
                "done", "proj/1", "--output", "[]",
                "--actor", "pete", "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr or "")
        assert "must decode to an object" in combined

    def test_done_without_summary_errors(self, db_path):
        self._setup_claimed_task(db_path)
        wo = json.dumps({"type": "code_change", "artifacts": []})
        result = runner.invoke(
            task_app,
            [
                "done", "proj/1", "--output", wo,
                "--actor", "pete", "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr or "")
        assert "non-empty 'summary'" in combined

    def test_done_with_empty_summary_errors(self, db_path):
        self._setup_claimed_task(db_path)
        wo = json.dumps({"type": "code_change", "summary": "   "})
        result = runner.invoke(
            task_app,
            [
                "done", "proj/1", "--output", wo,
                "--actor", "pete", "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        assert "non-empty 'summary'" in (result.output + (result.stderr or ""))

    def test_done_with_unknown_type_errors(self, db_path):
        self._setup_claimed_task(db_path)
        wo = json.dumps({"type": "magic_change", "summary": "Tried something"})
        result = runner.invoke(
            task_app,
            [
                "done", "proj/1", "--output", wo,
                "--actor", "pete", "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr or "")
        assert "type" in combined and "magic_change" in combined
        # Error names every supported type so worker can pick the right one.
        for known in ("code_change", "action", "document", "mixed"):
            assert known in combined

    def test_done_artifact_must_have_kind(self, db_path):
        self._setup_claimed_task(db_path)
        wo = json.dumps(
            {
                "type": "code_change",
                "summary": "Implemented X",
                "artifacts": [{"description": "commit"}],  # no kind
            }
        )
        result = runner.invoke(
            task_app,
            [
                "done", "proj/1", "--output", wo,
                "--actor", "pete", "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr or "")
        assert "missing 'kind'" in combined
        # Error names supported kinds so the worker can pick.
        for known in ("commit", "file_change", "action", "note"):
            assert known in combined

    def test_done_artifact_unknown_kind_rejected(self, db_path):
        self._setup_claimed_task(db_path)
        wo = json.dumps(
            {
                "type": "code_change",
                "summary": "Implemented X",
                "artifacts": [
                    {"kind": "diff", "description": "patch"},
                ],
            }
        )
        result = runner.invoke(
            task_app,
            [
                "done", "proj/1", "--output", wo,
                "--actor", "pete", "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr or "")
        assert "diff" in combined
        for known in ("commit", "file_change", "action", "note"):
            assert known in combined

    def test_done_artifact_missing_description_rejected(self, db_path):
        self._setup_claimed_task(db_path)
        wo = json.dumps(
            {
                "type": "code_change",
                "summary": "Implemented X",
                "artifacts": [{"kind": "commit", "ref": "deadbeef"}],
            }
        )
        result = runner.invoke(
            task_app,
            [
                "done", "proj/1", "--output", wo,
                "--actor", "pete", "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        assert "non-empty 'description'" in (
            result.output + (result.stderr or "")
        )

    def test_done_artifacts_must_be_list(self, db_path):
        self._setup_claimed_task(db_path)
        wo = json.dumps(
            {
                "type": "code_change",
                "summary": "Implemented X",
                "artifacts": "not a list",
            }
        )
        result = runner.invoke(
            task_app,
            [
                "done", "proj/1", "--output", wo,
                "--actor", "pete", "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        assert "must be a list" in (
            result.output + (result.stderr or "")
        )

    def test_done_with_well_formed_artifacts_passes(self, db_path):
        """A correctly-shaped payload still succeeds end-to-end —
        regression guard against the new validation accidentally
        rejecting valid output."""
        self._setup_claimed_task(db_path)
        wo = json.dumps(
            {
                "type": "code_change",
                "summary": "Implemented the feature",
                "artifacts": [
                    {"kind": "commit", "description": "abc123", "ref": "abc123"},
                ],
            }
        )
        result = runner.invoke(
            task_app,
            [
                "done", "proj/1", "--output", wo,
                "--actor", "pete", "--db", db_path,
            ],
        )
        assert result.exit_code == 0, result.output


class TestCliRejectValidation:
    """``pm task reject --reason`` enforces Russell's contract.

    The reviewer prompt requires SPECIFIC, actionable rejection
    reasons. Typer's ``...`` makes ``--reason`` required but allows
    empty strings — exactly the bad-rejection-message shape the
    contract warns against. Reject empty/whitespace reasons at the
    CLI so workers always have actionable feedback to address.
    """

    def _setup_review_task(self, db_path):
        _create_task(
            db_path,
            title="Review me",
            roles=["worker=pete", "reviewer=polly"],
        )
        runner.invoke(task_app, ["queue", "proj/1", "--db", db_path])
        runner.invoke(
            task_app,
            ["claim", "proj/1", "--actor", "pete", "--db", db_path],
        )
        wo = json.dumps(
            {
                "type": "code_change",
                "summary": "Implemented the feature",
                "artifacts": [
                    {"kind": "commit", "description": "abc", "ref": "abc"},
                ],
            }
        )
        runner.invoke(
            task_app,
            [
                "done", "proj/1", "--output", wo,
                "--actor", "pete", "--db", db_path,
            ],
        )

    def test_reject_with_empty_reason_errors(self, db_path):
        self._setup_review_task(db_path)
        result = runner.invoke(
            task_app,
            [
                "reject", "proj/1", "--reason", "",
                "--actor", "polly", "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr or "")
        assert "non-empty string" in combined
        # The contract's voice rule is named so reviewers reading
        # the error see what kind of reason is expected.
        assert "specific" in combined.lower()

    def test_reject_with_whitespace_reason_errors(self, db_path):
        self._setup_review_task(db_path)
        result = runner.invoke(
            task_app,
            [
                "reject", "proj/1", "--reason", "   ",
                "--actor", "polly", "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        assert "non-empty" in (result.output + (result.stderr or ""))

    def test_reject_with_real_reason_passes(self, db_path):
        """Regression guard: the validator must not reject valid
        rejection reasons end-to-end."""
        self._setup_review_task(db_path)
        result = runner.invoke(
            task_app,
            [
                "reject", "proj/1",
                "--reason",
                "Criterion 3 (CSV export) not verified. Add the export "
                "subcommand and resubmit.",
                "--actor", "polly", "--db", db_path,
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Rejected proj/1" in result.output


class TestCliCancelHoldReasonValidation:
    """``pm task cancel --reason`` and ``pm task hold --reason``
    enforce non-empty content (mirrors the reject contract from the
    previous cycle). Cancellation needs an audit trail; hold reasons
    surface verbatim in the dashboard's On Hold section."""

    def test_cancel_with_empty_reason_errors(self, db_path):
        _create_task(db_path, title="Doomed task")
        result = runner.invoke(
            task_app,
            [
                "cancel", "proj/1", "--reason", "",
                "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr or "")
        assert "non-empty" in combined
        assert "audit" in combined.lower()

    def test_cancel_with_whitespace_reason_errors(self, db_path):
        _create_task(db_path, title="Doomed task")
        result = runner.invoke(
            task_app,
            [
                "cancel", "proj/1", "--reason", "   ",
                "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        assert "non-empty" in (result.output + (result.stderr or ""))

    def test_cancel_with_real_reason_passes(self, db_path):
        _create_task(db_path, title="Doomed task")
        result = runner.invoke(
            task_app,
            [
                "cancel", "proj/1",
                "--reason", "Superseded by proj/2",
                "--db", db_path,
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Cancelled proj/1" in result.output

    def test_hold_with_explicit_empty_reason_errors(self, db_path):
        """If the operator passes ``--reason`` it must carry content;
        otherwise the dashboard On Hold section renders a blank
        ``paused:`` line."""
        _create_task(
            db_path,
            title="Holdable",
            roles=["worker=pete", "reviewer=russell"],
        )
        runner.invoke(task_app, ["queue", "proj/1", "--db", db_path])
        runner.invoke(
            task_app,
            ["claim", "proj/1", "--actor", "pete", "--db", db_path],
        )
        result = runner.invoke(
            task_app,
            [
                "hold", "proj/1", "--reason", "",
                "--actor", "polly", "--db", db_path,
            ],
        )
        assert result.exit_code != 0
        combined = result.output + (result.stderr or "")
        assert "empty" in combined
        # The error names the dashboard surface that uses the field
        # so the operator understands the downstream impact.
        assert "On Hold" in combined or "on hold" in combined

    def test_hold_without_reason_flag_passes(self, db_path):
        """Reason is optional on hold — omitting the flag entirely is
        still a valid call."""
        _create_task(
            db_path,
            title="Holdable",
            roles=["worker=pete", "reviewer=russell"],
        )
        runner.invoke(task_app, ["queue", "proj/1", "--db", db_path])
        runner.invoke(
            task_app,
            ["claim", "proj/1", "--actor", "pete", "--db", db_path],
        )
        result = runner.invoke(
            task_app,
            [
                "hold", "proj/1", "--actor", "polly", "--db", db_path,
            ],
        )
        assert result.exit_code == 0, result.output

    def test_hold_with_real_reason_passes(self, db_path):
        _create_task(
            db_path,
            title="Holdable",
            roles=["worker=pete", "reviewer=russell"],
        )
        runner.invoke(task_app, ["queue", "proj/1", "--db", db_path])
        runner.invoke(
            task_app,
            ["claim", "proj/1", "--actor", "pete", "--db", db_path],
        )
        result = runner.invoke(
            task_app,
            [
                "hold", "proj/1",
                "--reason", "Waiting on Sam to confirm contract",
                "--actor", "polly", "--db", db_path,
            ],
        )
        assert result.exit_code == 0, result.output


class TestCliNext:
    def test_cli_next(self, db_path):
        _create_task(db_path, title="High task", priority="high")
        _create_task(db_path, title="Critical task", priority="critical")

        runner.invoke(task_app, ["queue", "proj/1", "--db", db_path])
        runner.invoke(task_app, ["queue", "proj/2", "--db", db_path])

        result = runner.invoke(task_app, ["next", "--db", db_path])
        assert result.exit_code == 0
        assert "Critical task" in result.output

    def test_cli_next_none(self, db_path):
        result = runner.invoke(task_app, ["next", "--db", db_path])
        assert result.exit_code == 0
        assert "No tasks available" in result.output


class TestCliJsonOutput:
    def test_cli_json_output(self, db_path):
        _create_task(db_path, title="JSON task")

        result = runner.invoke(task_app, ["get", "proj/1", "--db", db_path, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["title"] == "JSON task"
        assert data["task_id"] == "proj/1"


class TestCliErrors:
    def test_cli_create_missing_roles_shows_fix_without_traceback(self, db_path):
        result = runner.invoke(
            task_app,
            [
                "create",
                "Missing roles",
                "--project",
                "proj",
                "--flow",
                "standard",
                "--db",
                db_path,
            ],
        )

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "✗ Required task roles are missing." in result.output
        assert "Why: flow 'standard' requires worker, reviewer." in result.output
        # Tightened post-savethenovel: the fix-suggestion now spells out
        # legal agent values and explicitly warns against ``user``.
        assert "--role worker=<agent> --role reviewer=<agent>" in result.output
        assert "architect, reviewer, worker, polly, russell, triage" in result.output
        assert "NOT `user`" in result.output

    def test_cli_get_missing_task_includes_why_fix_and_suggestion(self, db_path):
        _create_task(db_path, title="Only task")

        result = runner.invoke(task_app, ["get", "proj/9", "--db", db_path])

        assert result.exit_code == 1
        assert "✗ Task proj/9 not found." in result.output
        assert "Why: project 'proj' does not have task number 9." in result.output
        assert "Fix: run `pm task list --project proj` to see available task ids." in result.output
        assert "Did you mean proj/1?" in result.output

    def test_cli_get_invalid_task_id_includes_example_fix(self, db_path):
        result = runner.invoke(task_app, ["get", "bogus", "--db", db_path])

        assert result.exit_code == 1
        assert "✗ Task id 'bogus' is invalid." in result.output
        assert "Why: work-service task ids must use the form `project/number`." in result.output
        assert "Fix: pass a task id like `demo/1`." in result.output

    def test_cli_update_without_fields_includes_fix(self, db_path):
        _create_task(db_path, title="Needs update")

        result = runner.invoke(task_app, ["update", "proj/1", "--db", db_path])

        assert result.exit_code == 1
        assert "✗ No updatable fields provided." in result.output
        assert "Why: `pm task update` only changes fields you pass as flags." in result.output
        assert "--title" in result.output
        assert "--relevant-files" in result.output


class TestCliCreateRoleAgentValidation:
    """savethenovel forensic — Polly typed ``--role worker=user --role
    reviewer=user`` while planning a project, and the work service
    happily stored ``roles={"worker":"user","reviewer":"user"}``. The
    resulting worker session ran with ``Assignee: user`` for ~37
    seconds before she self-cancelled. The validation gate must
    reject ``user`` (and similar non-agent values) on roles that
    drive autonomous-agent execution nodes.
    """

    def _create(self, db_path, roles, flow="standard"):
        args = [
            "create",
            "Plan task",
            "--project",
            "proj",
            "--flow",
            flow,
            "--db",
            db_path,
        ]
        for r in roles:
            args.extend(["--role", r])
        return runner.invoke(task_app, args)

    def test_worker_user_is_rejected_with_clear_error(self, db_path):
        result = self._create(db_path, ["worker=user", "reviewer=user"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert "✗ Invalid agent value for an autonomous role." in result.output
        assert "worker='user'" in result.output
        assert "reviewer='user'" in result.output
        # The error must list legal options so the next try works.
        assert "architect" in result.output
        assert "polly" in result.output
        # And it must point at the project-plan escape hatch.
        assert "pm project plan" in result.output

    def test_human_and_placeholder_values_are_also_rejected(self, db_path):
        # Personal names ("sam") are NOT rejected — existing fixtures use
        # them as opaque worker IDs. Only canonical human-markers and
        # obvious placeholders trip the gate.
        for bad in ("human", "  USER  ", "nobody", "tbd", "?"):
            result = self._create(db_path, [f"worker={bad}", "reviewer=russell"])
            assert result.exit_code == 1, f"{bad!r} should be rejected: {result.output}"
            assert "✗ Invalid agent value for an autonomous role." in result.output

    def test_legitimate_agent_values_succeed(self, db_path):
        # Canonical role-contract names.
        result = self._create(
            db_path, ["worker=architect", "reviewer=russell"]
        )
        assert result.exit_code == 0, result.output
        assert "Created proj/" in result.output

    def test_agent_dash_id_pattern_still_succeeds(self, db_path):
        # The existing test convention uses ``agent-1`` / ``agent-2`` — those
        # opaque worker IDs must remain valid (the fix is a blacklist, not a
        # whitelist).
        result = self._create(
            db_path, ["worker=agent-1", "reviewer=agent-2"]
        )
        assert result.exit_code == 0, result.output

    def test_requester_user_remains_legal(self, db_path):
        # Metadata-only roles like ``requester`` are NOT autonomous — the
        # inbox view explicitly relies on ``requester=user`` to mark a
        # task as user-facing (pollypm.work.inbox_view._roles_match_user).
        result = self._create(
            db_path,
            ["worker=worker", "reviewer=russell", "requester=user"],
        )
        assert result.exit_code == 0, result.output

    def test_required_role_gate_still_fires_for_missing_role(self, db_path):
        # When ``--role`` is omitted entirely we should still hit the
        # "Required task roles are missing" gate, not the new
        # invalid-agent gate.
        result = self._create(db_path, [])
        assert result.exit_code == 1
        assert "✗ Required task roles are missing." in result.output


class TestCliContext:
    def test_cli_context(self, db_path):
        _create_task(db_path, title="Context task")

        result = runner.invoke(task_app, ["context", "proj/1", "Hello context", "--db", db_path])
        assert result.exit_code == 0
        assert "Added context" in result.output

        # Verify context shows in get
        result = runner.invoke(task_app, ["get", "proj/1", "--db", db_path])
        assert result.exit_code == 0
        assert "Hello context" in result.output


class TestCliSweeperContextFiltering:
    """``pm task get`` / ``pm task status`` should hide infrastructure
    sweeper-ping context rows by default — they drown user-visible
    signal like review summaries (#1035). ``--show-internal`` opts in.
    """

    def _seed_mixed_context(self, db_path: str) -> None:
        """Insert a realistic mix: 5 sweeper pings around 1 review summary,
        mirroring the bikepath/13 pattern from the issue body.
        """
        from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
            SWEEPER_PING_CONTEXT_ENTRY_TYPE,
        )
        from pollypm.work.sqlite_service import SQLiteWorkService

        svc = SQLiteWorkService(db_path=db_path)
        try:
            # Older sweeper churn.
            for _ in range(3):
                svc.add_context(
                    "proj/1", "sweeper",
                    "task_assignment.sweep:deduped",
                    entry_type=SWEEPER_PING_CONTEXT_ENTRY_TYPE,
                )
            # The one row a user actually wants to see.
            svc.add_context(
                "proj/1", "review_summary",
                "Approve if quality is acceptable; 16 specs pass.",
                entry_type="note",
            )
            # Newer sweeper churn bracketing the useful row.
            for _ in range(2):
                svc.add_context(
                    "proj/1", "sweeper",
                    "task_assignment.sweep:sent",
                    entry_type=SWEEPER_PING_CONTEXT_ENTRY_TYPE,
                )
        finally:
            svc.close()

    def test_task_get_hides_sweeper_pings_by_default(self, db_path):
        _create_task(db_path, title="Bikepath-style task")
        self._seed_mixed_context(db_path)

        result = runner.invoke(task_app, ["get", "proj/1", "--db", db_path])
        assert result.exit_code == 0, result.output
        # Useful row surfaces.
        assert "Approve if quality is acceptable" in result.output
        # Sweeper noise hidden.
        assert "task_assignment.sweep:sent" not in result.output
        assert "task_assignment.sweep:deduped" not in result.output
        # Hidden-count breadcrumb tells the user noise was filtered.
        assert "5 internal sweeper entries hidden" in result.output

    def test_task_get_show_internal_resurfaces_pings(self, db_path):
        _create_task(db_path, title="Bikepath-style task")
        self._seed_mixed_context(db_path)

        result = runner.invoke(
            task_app, ["get", "proj/1", "--show-internal", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert "task_assignment.sweep:sent" in result.output
        assert "task_assignment.sweep:deduped" in result.output
        assert "Approve if quality is acceptable" in result.output
        # No "hidden" footer when nothing was filtered.
        assert "hidden" not in result.output

    def test_task_get_json_filters_context_by_default(self, db_path):
        _create_task(db_path, title="JSON sweeper")
        self._seed_mixed_context(db_path)

        result = runner.invoke(
            task_app, ["get", "proj/1", "--db", db_path, "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # Filtered context only contains the user-visible row.
        texts = [c["text"] for c in data.get("context", [])]
        assert any("Approve if quality" in t for t in texts)
        assert not any("task_assignment.sweep" in t for t in texts)
        assert data.get("hidden_internal_context_count") == 5

    def test_task_get_json_show_internal_returns_all(self, db_path):
        _create_task(db_path, title="JSON show-internal")
        self._seed_mixed_context(db_path)

        result = runner.invoke(
            task_app,
            ["get", "proj/1", "--show-internal", "--db", db_path, "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        texts = [c["text"] for c in data.get("context", [])]
        assert sum(1 for t in texts if "task_assignment.sweep" in t) == 5
        assert any("Approve if quality" in t for t in texts)
        # No hidden-count key when nothing was filtered.
        assert "hidden_internal_context_count" not in data

    def test_task_status_hides_sweeper_pings_and_overfetches(self, db_path):
        """The pre-fix bug: ``task_status`` fetched only the last 5 rows,
        so a task with >=5 trailing sweeper pings would show *zero*
        useful context. The fix over-fetches before filtering."""
        _create_task(db_path, title="Status sweeper")
        self._seed_mixed_context(db_path)

        result = runner.invoke(task_app, ["status", "proj/1", "--db", db_path])
        assert result.exit_code == 0, result.output
        assert "Recent context:" in result.output
        # The actually-useful row makes it through, even though it sits
        # behind 2 newer sweeper pings (would be filtered out of a raw
        # last-5 window).
        assert "Approve if quality is acceptable" in result.output
        assert "task_assignment.sweep" not in result.output

    def test_task_status_show_internal_returns_literal_recent_5(self, db_path):
        """``--show-internal`` opts back into the historic last-5 window —
        no over-fetch, no filtering."""
        _create_task(db_path, title="Status show-internal")
        self._seed_mixed_context(db_path)

        result = runner.invoke(
            task_app,
            ["status", "proj/1", "--show-internal", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert "task_assignment.sweep" in result.output

    def test_legacy_sweeper_rows_without_typed_marker_still_filtered(self, db_path):
        """Older DBs predate ``entry_type='sweeper_ping'`` and write the
        sweeper row with the default ``"note"`` type. The fallback
        actor+text sniff in ``_is_sweeper_internal`` keeps them hidden.
        """
        from pollypm.work.sqlite_service import SQLiteWorkService

        _create_task(db_path, title="Legacy sweeper rows")
        svc = SQLiteWorkService(db_path=db_path)
        try:
            # Legacy: actor="sweeper", text matches sweep ping shape, but
            # entry_type is the default "note".
            svc.add_context(
                "proj/1", "sweeper", "task_assignment.sweep:sent",
            )
            svc.add_context(
                "proj/1", "review_summary",
                "Real signal that should survive the filter.",
            )
        finally:
            svc.close()

        result = runner.invoke(task_app, ["get", "proj/1", "--db", db_path])
        assert result.exit_code == 0, result.output
        assert "Real signal that should survive" in result.output
        assert "task_assignment.sweep:sent" not in result.output

    def test_is_sweeper_internal_predicate(self):
        """Direct unit test on the predicate so future refactors of the
        rendering layer can't silently break the contract."""
        from datetime import UTC, datetime

        from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
            SWEEPER_PING_CONTEXT_ENTRY_TYPE,
        )
        from pollypm.work.cli import _is_sweeper_internal
        from pollypm.work.models import ContextEntry

        ts = datetime.now(UTC)
        sweeper_typed = ContextEntry(
            actor="sweeper", timestamp=ts,
            text="task_assignment.sweep:sent",
            entry_type=SWEEPER_PING_CONTEXT_ENTRY_TYPE,
        )
        sweeper_legacy = ContextEntry(
            actor="sweeper", timestamp=ts,
            text="task_assignment.sweep:deduped",
            entry_type="note",
        )
        review = ContextEntry(
            actor="review_summary", timestamp=ts,
            text="Approve if quality is acceptable.",
            entry_type="note",
        )
        worker_note = ContextEntry(
            actor="worker", timestamp=ts,
            text="Started implementation.",
            entry_type="note",
        )
        # Worker writes "sent" in a normal note — must NOT be filtered
        # (different actor than "sweeper").
        worker_with_colon_sent = ContextEntry(
            actor="worker", timestamp=ts,
            text="email:sent",
            entry_type="note",
        )

        assert _is_sweeper_internal(sweeper_typed) is True
        assert _is_sweeper_internal(sweeper_legacy) is True
        assert _is_sweeper_internal(review) is False
        assert _is_sweeper_internal(worker_note) is False
        assert _is_sweeper_internal(worker_with_colon_sent) is False


class TestCliCounts:
    def test_cli_counts(self, db_path):
        _create_task(db_path, title="Task 1")
        _create_task(db_path, title="Task 2")
        runner.invoke(task_app, ["queue", "proj/1", "--db", db_path])

        result = runner.invoke(task_app, ["counts", "--db", db_path])
        assert result.exit_code == 0
        assert "draft" in result.output
        assert "queued" in result.output

    def test_cli_counts_json(self, db_path):
        _create_task(db_path, title="Task 1")

        result = runner.invoke(task_app, ["counts", "--db", db_path, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["draft"] == 1


# ---------------------------------------------------------------------------
# Flow CLI tests
# ---------------------------------------------------------------------------


class TestCliFlowList:
    def test_cli_flow_list(self, db_path):
        result = runner.invoke(flow_app, ["list", "--db", db_path])
        assert result.exit_code == 0
        assert "standard" in result.output

    def test_cli_flow_list_json(self, db_path):
        result = runner.invoke(flow_app, ["list", "--db", db_path, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        names = [f["name"] for f in data]
        assert "standard" in names


class TestCliFlowValidate:
    def test_cli_flow_validate(self, tmp_path):
        # Use a built-in flow file
        ref = importlib.resources.files("pollypm.work") / "flows" / "standard.yaml"
        flow_path = str(ref)

        result = runner.invoke(flow_app, ["validate", flow_path])
        assert result.exit_code == 0
        assert "Valid" in result.output

    def test_cli_flow_validate_by_registered_name(self):
        """#1041: `pm flow validate standard` should resolve via the registry."""
        result = runner.invoke(flow_app, ["validate", "standard"])
        assert result.exit_code == 0, result.output
        assert "Valid" in result.output
        # Source path should be reported when resolved via name.
        assert "standard.yaml" in result.output

    def test_cli_flow_validate_by_registered_name_json(self):
        """JSON output for name-resolution should include the source path."""
        result = runner.invoke(flow_app, ["validate", "standard", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["valid"] is True
        assert data["name"] == "standard"
        assert "source" in data
        assert data["source"].endswith("standard.yaml")

    def test_cli_flow_validate_invalid(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("name: bad\n")  # Missing required fields

        result = runner.invoke(flow_app, ["validate", str(bad)])
        assert result.exit_code == 1
        assert f"✗ Flow {bad} is invalid." in result.output
        assert "Why:" in result.output
        assert f"Fix: edit {bad} to satisfy the reported constraint" in result.output

    def test_cli_flow_validate_unknown_arg_fails(self, tmp_path):
        """Unknown name/path: error mentions both fallback paths."""
        missing = tmp_path / "missing.yaml"

        result = runner.invoke(flow_app, ["validate", str(missing)])

        assert result.exit_code == 1
        assert f"✗ Flow '{missing}' not found." in result.output
        # New error wording reflects dual-resolution behavior.
        assert "registered flow name" in result.output
        assert "path to a YAML file" in result.output

    def test_cli_flow_validate_unknown_name_lists_known_flows(self):
        """Unknown bare name should hint at registered alternatives."""
        result = runner.invoke(flow_app, ["validate", "nope-not-a-flow"])
        assert result.exit_code == 1
        assert "✗ Flow 'nope-not-a-flow' not found." in result.output
        # The known-flows hint should mention at least the built-in 'standard'.
        assert "standard" in result.output


# ---------------------------------------------------------------------------
# #919 — workspace-DB claims must still spawn per-task workers
# ---------------------------------------------------------------------------


class TestSvcSessionManagerWiring:
    """``_svc()`` must wire SessionManager when the project is registered.

    The bug (#919): tasks created against the workspace-root state.db
    for a project whose checkout is a sibling repo would silently skip
    SessionManager wiring because ``project_root`` was derived from
    ``db_path.parent.parent`` (the workspace dir, no ``.git``). Claim
    succeeded as a DB-only no-op — no tmux window, no log dir.
    """

    def test_svc_uses_configured_project_path_for_session_manager(
        self, tmp_path, monkeypatch,
    ):
        """When project hint resolves to a registered project, the
        SessionManager must use the *project's* configured path even if
        the DB lives at a different (workspace-root) location."""
        from types import SimpleNamespace

        from pollypm.models import KnownProject, ProjectKind

        # Workspace-root DB (the ``.parent.parent`` is workspace, no .git)
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        workspace_db_dir = workspace_root / ".pollypm"
        workspace_db_dir.mkdir()
        workspace_db = workspace_db_dir / "state.db"

        # Project repo with .git, registered under a *different* key shape
        project_path = tmp_path / "blackjack-trainer"
        project_path.mkdir()
        (project_path / ".git").mkdir()

        registered_key = "blackjack_trainer"  # underscore (slugified key)
        task_id_slug = "blackjack-trainer"  # hyphen (project name in task ids)

        fake_config = SimpleNamespace(
            projects={
                registered_key: KnownProject(
                    key=registered_key,
                    path=project_path,
                    name=task_id_slug,
                    kind=ProjectKind.GIT,
                ),
            },
            project=SimpleNamespace(
                tmux_session="pollypm",
                state_db=workspace_db,
            ),
            accounts={},
            pollypm=SimpleNamespace(controller_account=""),
        )

        monkeypatch.setattr(
            "pollypm.config.load_config",
            lambda *args, **kwargs: fake_config,
        )

        # Capture SessionManager constructor args.
        captured: dict = {}

        class FakeSessionManager:
            def __init__(self, *args, **kwargs):
                captured["kwargs"] = kwargs

            def ensure_worker_session_schema(self):  # pragma: no cover
                return None

        monkeypatch.setattr(
            "pollypm.work.session_manager.SessionManager",
            FakeSessionManager,
        )
        monkeypatch.setattr(
            "pollypm.session_services.create_tmux_client",
            lambda: object(),
        )

        # Stub the optional StateStore + TmuxSessionService to avoid
        # touching real disk / tmux during the test.
        monkeypatch.setattr(
            "pollypm.session_services.tmux.TmuxSessionService",
            lambda **kwargs: object(),
        )
        monkeypatch.setattr(
            "pollypm.storage.state.StateStore",
            lambda *a, **kw: object(),
        )

        from pollypm.work.cli import _svc

        svc = _svc(str(workspace_db), project=task_id_slug)
        try:
            assert "kwargs" in captured, (
                "SessionManager was not constructed — _svc() failed to wire "
                "spawn for a workspace-DB-with-registered-project task. "
                "This is the regression that left pm task claim a DB-only "
                "no-op for projects whose state.db lives at the workspace "
                "root."
            )
            assert captured["kwargs"]["project_path"] == project_path
        finally:
            svc.close()
