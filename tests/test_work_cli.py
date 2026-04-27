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
        assert "Fix: rerun with `--role worker=<agent> --role reviewer=<agent>`." in result.output

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

    def test_cli_flow_validate_invalid(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("name: bad\n")  # Missing required fields

        result = runner.invoke(flow_app, ["validate", str(bad)])
        assert result.exit_code == 1
        assert f"✗ Flow {bad} is invalid." in result.output
        assert "Why:" in result.output
        assert f"Fix: edit {bad} to satisfy the reported constraint" in result.output

    def test_cli_flow_validate_missing_file_includes_fix(self, tmp_path):
        missing = tmp_path / "missing.yaml"

        result = runner.invoke(flow_app, ["validate", str(missing)])

        assert result.exit_code == 1
        assert f"✗ Flow file {missing} not found." in result.output
        assert "Why: `pm flow validate` reads a YAML file from disk." in result.output
        assert "Fix: pass the path to an existing `.yaml` flow file." in result.output
