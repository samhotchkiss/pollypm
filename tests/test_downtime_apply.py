"""Tests for dt06 — inbox notification + approve/reject → commit/archive routing.

Covers:

* ``render_notification`` shape + marker line (gated by dt02's
  ``inbox_notification_sent``).
* ``dispatch_notification`` writes to the service's context log.
* ``build_apply_plan`` produces the right action set per (kind, approval)
  pair. One happy-path + one reject-path assertion per category.
* ``execute_plan`` does the right file moves / report stamps / branch
  archives on disk.
* Security-scan refusal: :func:`plan_security_scan` rejects with
  ``refused=True`` when the explorer touched files outside the report
  dir, regardless of approval verdict.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pollypm.plugins_builtin.downtime.gates.inbox_notification_sent import (
    InboxNotificationSent,
)
from pollypm.plugins_builtin.downtime.handlers.apply import (
    ApplyAction,
    ApplyPlan,
    build_apply_plan,
    execute_plan,
    plan_audit_docs,
    plan_build_speculative,
    plan_security_scan,
    plan_spec_feature,
    plan_try_alt_approach,
    run_apply,
)
from pollypm.plugins_builtin.downtime.handlers.notify import (
    NOTIFICATION_KIND,
    NOTIFICATION_MARKER,
    dispatch_notification,
    render_notification,
)
from pollypm.work.models import ContextEntry, Task, TaskType
from datetime import datetime, timezone


GIT_AVAILABLE = shutil.which("git") is not None


# ---------------------------------------------------------------------------
# Stub service + test helpers
# ---------------------------------------------------------------------------


class _StubService:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str]] = []

    def add_context(self, task_id: str, actor: str, text: str) -> Any:
        self.entries.append((task_id, actor, text))
        return object()


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "downtime@test.local"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Downtime"], cwd=path, check=True)
    (path / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True
    )


# ---------------------------------------------------------------------------
# Notification rendering
# ---------------------------------------------------------------------------


class TestRenderNotification:
    def test_first_line_is_marker(self) -> None:
        body = render_notification(
            task_id="fix/1",
            kind="spec_feature",
            result={"summary": "ok", "artifact_path": "docs/ideas/x.md"},
        )
        first = body.splitlines()[0]
        assert first.startswith(NOTIFICATION_MARKER)
        assert "kind=" + NOTIFICATION_KIND in first

    def test_includes_artifact_and_affordances(self) -> None:
        body = render_notification(
            task_id="fix/2",
            kind="build_speculative",
            result={
                "summary": "built a prototype",
                "branch_name": "downtime/proto",
                "commit_sha": "abcdef1234567890",
                "tests_added": 3,
                "tests_pass": True,
            },
        )
        assert "downtime/proto" in body
        assert "abcdef123456" in body
        assert "Tests added: 3" in body
        assert "pm task approve fix/2" in body
        assert "pm task reject fix/2" in body

    def test_unlocks_gate(self) -> None:
        """A task with this notification in context passes the dt02 gate."""
        body = render_notification(
            task_id="fix/3",
            kind="audit_docs",
            result={"summary": "doc drift", "branch_name": "downtime/docs-x"},
        )
        task = Task(
            project="fix",
            task_number=3,
            title="t",
            type=TaskType.TASK,
        )
        task.context = [
            ContextEntry(actor="downtime", timestamp=datetime.now(timezone.utc), text=body)
        ]
        gate = InboxNotificationSent()
        assert gate.check(task).passed is True


class TestDispatchNotification:
    def test_writes_to_service(self) -> None:
        svc = _StubService()
        dispatch_notification(
            service=svc,
            task_id="proj/1",
            kind="spec_feature",
            result={"summary": "ok", "artifact_path": "docs/ideas/x.md"},
        )
        assert len(svc.entries) == 1
        task_id, actor, text = svc.entries[0]
        assert task_id == "proj/1"
        assert actor == "downtime"
        assert text.startswith(NOTIFICATION_MARKER)


# ---------------------------------------------------------------------------
# Apply plans per category
# ---------------------------------------------------------------------------


class TestPlanSpecFeature:
    def test_approve_moves_to_specs_and_opens_pr(self) -> None:
        plan = plan_spec_feature(
            approved=True,
            result={
                "slug": "shiny",
                "artifact_path": "docs/ideas/shiny.md",
                "branch_name": "downtime/spec-shiny",
            },
        )
        verbs = [a.verb for a in plan.actions]
        assert "move_file" in verbs
        assert "pr_create" in verbs

    def test_reject_archives(self) -> None:
        plan = plan_spec_feature(
            approved=False,
            result={
                "slug": "shiny",
                "artifact_path": "docs/ideas/shiny.md",
                "branch_name": "downtime/spec-shiny",
            },
        )
        verbs = [a.verb for a in plan.actions]
        assert "archive_file" in verbs
        assert "archive_branch" in verbs


class TestPlanBuildSpeculative:
    def test_approve_creates_pr_no_auto_merge(self) -> None:
        plan = plan_build_speculative(
            approved=True, result={"branch_name": "downtime/proto"}
        )
        assert [a.verb for a in plan.actions] == ["pr_create"]
        # Spec §6: "Don't auto-merge — leave for user review."
        assert "auto-merge" not in plan.actions[0].detail.lower() or "user can" in plan.actions[0].detail.lower()

    def test_reject_archives_branch(self) -> None:
        plan = plan_build_speculative(
            approved=False, result={"branch_name": "downtime/proto"}
        )
        assert plan.actions[0].verb == "archive_branch"


class TestPlanAuditDocs:
    def test_approve_flips_draft_pr_to_ready(self) -> None:
        plan = plan_audit_docs(
            approved=True,
            result={
                "branch_name": "downtime/docs-x",
                "pr_number": 42,
                "pr_title": "t",
                "pr_body": "b",
            },
        )
        assert plan.actions[0].verb == "pr_ready"

    def test_reject_closes_pr(self) -> None:
        plan = plan_audit_docs(
            approved=False,
            result={"branch_name": "downtime/docs-x", "pr_number": 42},
        )
        assert plan.actions[0].verb == "pr_close"


class TestPlanSecurityScan:
    def test_approve_stamps_reviewed(self) -> None:
        plan = plan_security_scan(
            approved=True,
            result={"report_path": ".pollypm/security-reports/x.md"},
            changed_paths=[".pollypm/security-reports/x.md"],
        )
        assert plan.refused is False
        assert plan.actions[0].verb == "stamp_report"
        assert "reviewed" in plan.actions[0].detail.lower()

    def test_reject_stamps_dismissed(self) -> None:
        plan = plan_security_scan(
            approved=False,
            result={"report_path": ".pollypm/security-reports/x.md"},
            changed_paths=[".pollypm/security-reports/x.md"],
        )
        assert plan.actions[0].verb == "stamp_report"
        assert "dismissed" in plan.actions[0].detail.lower()

    def test_refuses_when_non_report_file_changed(self) -> None:
        plan = plan_security_scan(
            approved=True,
            result={"report_path": ".pollypm/security-reports/x.md"},
            changed_paths=[
                ".pollypm/security-reports/x.md",
                "src/pollypm/cli.py",
            ],
        )
        assert plan.refused is True
        assert "src/pollypm/cli.py" in plan.refusal_reason


class TestPlanTryAltApproach:
    def test_approve_keeps_branch(self) -> None:
        plan = plan_try_alt_approach(
            approved=True, result={"branch_name": "downtime/alt"}
        )
        assert plan.actions[0].verb == "keep_branch"

    def test_reject_archives_branch(self) -> None:
        plan = plan_try_alt_approach(
            approved=False, result={"branch_name": "downtime/alt"}
        )
        assert plan.actions[0].verb == "archive_branch"


# ---------------------------------------------------------------------------
# build_apply_plan dispatch
# ---------------------------------------------------------------------------


class TestBuildApplyPlan:
    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ValueError):
            build_apply_plan(kind="bogus", approved=True, result={})

    def test_passes_changed_paths_to_security(self) -> None:
        plan = build_apply_plan(
            kind="security_scan",
            approved=True,
            result={"report_path": ".pollypm/security-reports/x.md"},
            changed_paths=["unrelated/file.py"],
        )
        assert plan.refused is True


# ---------------------------------------------------------------------------
# execute_plan — disk side effects
# ---------------------------------------------------------------------------


class TestExecutePlan:
    def test_move_file_action(self, tmp_path: Path) -> None:
        src = tmp_path / "docs" / "ideas" / "x.md"
        src.parent.mkdir(parents=True)
        src.write_text("spec body")
        plan = ApplyPlan(
            approved=True,
            actions=(
                ApplyAction(
                    verb="move_file",
                    detail="move",
                    src="docs/ideas/x.md",
                    dst="docs/specs/x.md",
                ),
            ),
        )
        summaries = execute_plan(plan, project_root=tmp_path)
        assert not (tmp_path / "docs" / "ideas" / "x.md").exists()
        assert (tmp_path / "docs" / "specs" / "x.md").read_text() == "spec body"
        assert summaries[0].startswith("ok:")

    def test_archive_file_action(self, tmp_path: Path) -> None:
        src = tmp_path / "docs" / "ideas" / "x.md"
        src.parent.mkdir(parents=True)
        src.write_text("draft")
        plan = ApplyPlan(
            approved=False,
            actions=(
                ApplyAction(
                    verb="archive_file",
                    detail="archive",
                    src="docs/ideas/x.md",
                    dst=".pollypm/archive/specs/x.md",
                ),
            ),
        )
        summaries = execute_plan(plan, project_root=tmp_path)
        assert (tmp_path / ".pollypm/archive/specs/x.md").exists()
        assert summaries[0].startswith("ok:")

    def test_stamp_report_action(self, tmp_path: Path) -> None:
        rpt = tmp_path / ".pollypm" / "security-reports" / "r.md"
        rpt.parent.mkdir(parents=True)
        rpt.write_text("# Findings\n")
        plan = ApplyPlan(
            approved=True,
            actions=(
                ApplyAction(
                    verb="stamp_report",
                    detail="stamp",
                    src=".pollypm/security-reports/r.md",
                    extras={"stamp_text": "\n> Reviewed on 2026-04-16\n"},
                ),
            ),
        )
        summaries = execute_plan(plan, project_root=tmp_path)
        content = rpt.read_text()
        assert "Reviewed on 2026-04-16" in content
        assert summaries[0].startswith("ok:")

    def test_refused_plan_reports_refusal(self, tmp_path: Path) -> None:
        plan = ApplyPlan(approved=True, refused=True, refusal_reason="nope")
        summaries = execute_plan(plan, project_root=tmp_path)
        assert summaries == ["refused: nope"]

    @pytest.mark.skipif(not GIT_AVAILABLE, reason="git not on PATH")
    def test_archive_branch_renames(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        subprocess.run(
            ["git", "switch", "-c", "downtime/to-archive"],
            cwd=tmp_path, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "switch", "main"], cwd=tmp_path, check=True, capture_output=True,
        )
        plan = ApplyPlan(
            approved=False,
            actions=(
                ApplyAction(
                    verb="archive_branch",
                    detail="archive",
                    src="downtime/to-archive",
                ),
            ),
        )
        summaries = execute_plan(plan, project_root=tmp_path)
        assert summaries[0].startswith("ok:")
        # Renamed branch should exist.
        out = subprocess.run(
            ["git", "branch", "--list", "archive/downtime/to-archive"],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        ).stdout
        assert "archive/downtime/to-archive" in out


class TestRunApply:
    def test_integration_spec_feature_reject(self, tmp_path: Path) -> None:
        (tmp_path / "docs" / "ideas").mkdir(parents=True)
        (tmp_path / "docs" / "ideas" / "x.md").write_text("draft")
        plan, summaries = run_apply(
            kind="spec_feature",
            approved=False,
            result={
                "slug": "x",
                "artifact_path": "docs/ideas/x.md",
                "branch_name": "downtime/spec-x",
            },
            project_root=tmp_path,
        )
        assert plan.approved is False
        # File archived.
        assert (tmp_path / ".pollypm" / "archive" / "specs" / "x.md").exists()
        assert any("archived" in s for s in summaries)
