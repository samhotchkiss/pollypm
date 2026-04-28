"""Tests for flow progression — node_done, approve, reject, block, get_execution."""

from __future__ import annotations

import json
import subprocess

import pytest
from unittest.mock import MagicMock

from pollypm.rejection_feedback import (
    feedback_target_task_id,
    is_rejection_feedback_task,
    rejection_feedback_preview,
)
from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    Decision,
    ExecutionStatus,
    OutputType,
    WorkOutput,
    WorkStatus,
)
from pollypm.work.sqlite_service import (
    InvalidTransitionError,
    SQLiteWorkService,
    ValidationError,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    db_path = tmp_path / "work.db"
    return SQLiteWorkService(db_path=db_path)


def _create_task(svc, flow="standard", **kwargs):
    defaults = dict(
        title="Test task",
        description="A test task",
        type="task",
        project="proj",
        flow_template=flow,
        roles={"worker": "pete", "reviewer": "polly"},
        priority="normal",
        created_by="tester",
    )
    defaults.update(kwargs)
    return svc.create(**defaults)


def _create_spike_task(svc, **kwargs):
    defaults = dict(
        title="Spike task",
        description="Research something",
        type="spike",
        project="proj",
        flow_template="spike",
        roles={"worker": "pete"},
        priority="normal",
        created_by="tester",
    )
    defaults.update(kwargs)
    return svc.create(**defaults)


def _valid_work_output():
    return WorkOutput(
        type=OutputType.CODE_CHANGE,
        summary="Implemented the feature",
        artifacts=[
            Artifact(
                kind=ArtifactKind.COMMIT,
                description="feat: add new feature",
                ref="abc123",
            ),
        ],
    )


def _claim_task(svc, task):
    """Queue and claim a task, returning the claimed task."""
    svc.queue(task.task_id, "pm")
    return svc.claim(task.task_id, "pete")


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _git_stdout(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_review_task_on_git_repo(tmp_path):
    repo = _git_repo(tmp_path)
    svc = SQLiteWorkService(db_path=tmp_path / "work.db", project_path=repo)
    task = _create_task(svc)
    _claim_task(svc, task)

    current_branch = _git_stdout(repo, "rev-parse", "--abbrev-ref", "HEAD")
    task_branch = f"task/{task.project}-{task.task_number}"
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", task_branch], check=True)
    (repo / "feature.txt").write_text("done\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "feature.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "feat: worker change"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", current_branch], check=True)

    svc.node_done(task.task_id, "pete", _valid_work_output())
    return repo, svc, task, task_branch


# ---------------------------------------------------------------------------
# node_done
# ---------------------------------------------------------------------------


class TestNodeDone:
    def test_node_done_advances_to_review(self, svc):
        task = _create_task(svc)
        claimed = _claim_task(svc, task)
        assert claimed.work_status == WorkStatus.IN_PROGRESS
        assert claimed.current_node_id == "implement"

        result = svc.node_done(task.task_id, "pete", _valid_work_output())
        assert result.work_status == WorkStatus.REVIEW
        assert result.current_node_id == "code_review"

        # The implement execution should be completed
        execs = svc.get_execution(task.task_id, node_id="implement")
        assert len(execs) == 1
        assert execs[0].status == ExecutionStatus.COMPLETED
        assert execs[0].completed_at is not None

        # A new code_review execution should be active
        review_execs = svc.get_execution(task.task_id, node_id="code_review")
        assert len(review_execs) == 1
        assert review_execs[0].status == ExecutionStatus.ACTIVE

    def test_node_done_without_work_output_rejected(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)

        # After wg03, the error message is longer (three-question rule)
        # but still mentions --output, which is the actionable fix.
        with pytest.raises(ValidationError, match="--output"):
            svc.node_done(task.task_id, "pete", None)

    def test_node_done_with_empty_artifacts_rejected(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)

        bad_output = WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="Did something",
            artifacts=[],
        )
        with pytest.raises(ValidationError, match="at least one artifact"):
            svc.node_done(task.task_id, "pete", bad_output)

    def test_node_done_wrong_actor_rejected(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)

        with pytest.raises(ValidationError, match="does not match role"):
            svc.node_done(task.task_id, "polly", _valid_work_output())

    def test_node_done_not_in_progress_rejected(self, svc):
        task = _create_task(svc)
        # Task is in draft state
        with pytest.raises(InvalidTransitionError, match="draft"):
            svc.node_done(task.task_id, "pete", _valid_work_output())


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


class TestApprove:
    def test_approve_advances_to_done(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())

        result = svc.approve(task.task_id, "polly")
        assert result.work_status == WorkStatus.DONE
        assert result.current_node_id is None

    def test_approve_wrong_actor_rejected(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())

        with pytest.raises(ValidationError, match="does not match role"):
            svc.approve(task.task_id, "pete")

    def test_approve_not_in_review_rejected(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)
        # Task is in_progress, not review
        with pytest.raises(InvalidTransitionError, match="in_progress"):
            svc.approve(task.task_id, "polly")

    def test_approve_at_terminal_makes_done(self, svc):
        """Standard flow: after code_review approve, task is done."""
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())
        result = svc.approve(task.task_id, "polly", reason="LGTM")

        assert result.work_status == WorkStatus.DONE

        # Check the review execution has approved decision
        review_execs = svc.get_execution(task.task_id, node_id="code_review")
        assert len(review_execs) == 1
        assert review_execs[0].decision == Decision.APPROVED
        assert review_execs[0].decision_reason == "LGTM"

    def test_approve_auto_merges_task_branch_into_repo(self, tmp_path):
        repo, svc, task, task_branch = _create_review_task_on_git_repo(tmp_path)

        result = svc.approve(task.task_id, "polly")

        assert result.work_status == WorkStatus.DONE
        assert (repo / "feature.txt").read_text(encoding="utf-8") == "done\n"
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", task_branch, "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert merged.returncode == 0

    def test_approve_refuses_auto_merge_when_repo_dirty(self, tmp_path):
        repo, svc, task, _task_branch = _create_review_task_on_git_repo(tmp_path)
        (repo / "README.md").write_text("dirty\n", encoding="utf-8")
        session_mgr = MagicMock()
        svc.set_session_manager(session_mgr)

        with pytest.raises(ValidationError, match="uncommitted changes"):
            svc.approve(task.task_id, "polly")

        assert svc.get(task.task_id).work_status == WorkStatus.REVIEW
        session_mgr.teardown_worker.assert_not_called()

    def _build_addadd_repo(
        self,
        tmp_path,
        rel_path: str,
        main_text: str,
        task_text: str,
    ):
        """Create a repo where ``rel_path`` is independently added on both
        main and the task branch (classic add/add conflict)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=repo, check=True
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True
        )
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

        svc = SQLiteWorkService(db_path=tmp_path / "work.db", project_path=repo)
        task = _create_task(svc)
        _claim_task(svc, task)
        current_branch = _git_stdout(repo, "rev-parse", "--abbrev-ref", "HEAD")
        task_branch = f"task/{task.project}-{task.task_number}"

        # Worker branch independently adds rel_path with task_text.
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-q", "-b", task_branch],
            check=True,
        )
        (repo / rel_path).write_text(task_text, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", rel_path], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", f"feat: add {rel_path}"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-q", current_branch],
            check=True,
        )

        # Main branch independently adds rel_path with main_text.
        (repo / rel_path).write_text(main_text, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", rel_path], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", f"chore: add {rel_path}"],
            check=True,
        )

        svc.node_done(task.task_id, "pete", _valid_work_output())
        return repo, svc, task, task_branch

    def test_approve_unions_disjoint_gitignore_addadd(self, tmp_path):
        repo, svc, task, task_branch = self._build_addadd_repo(
            tmp_path,
            ".gitignore",
            ".pollypm/\nnode_modules/\n",
            "dist/\n.env\n",
        )

        result = svc.approve(task.task_id, "polly")

        assert result.work_status == WorkStatus.DONE
        merged_lines = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
        # Union of both sides, dedupe-preserving order; ours-first.
        assert merged_lines == [
            ".pollypm/",
            "node_modules/",
            "dist/",
            ".env",
        ]
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", task_branch, "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert merged.returncode == 0
        # Repo must be clean after the merge — no lingering MERGE_HEAD, no
        # stray conflict markers.
        assert not (repo / ".git" / "MERGE_HEAD").exists()
        text = (repo / ".gitignore").read_text(encoding="utf-8")
        assert "<<<<<<<" not in text and "=======" not in text

    def test_approve_unions_overlapping_gitignore_addadd(self, tmp_path):
        repo, svc, task, _task_branch = self._build_addadd_repo(
            tmp_path,
            ".gitignore",
            ".pollypm/\nnode_modules/\ndist/\n",
            "node_modules/\ndist/\n.env\n",
        )

        result = svc.approve(task.task_id, "polly")

        assert result.work_status == WorkStatus.DONE
        merged_lines = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
        # Each line appears at most once, ours-first ordering.
        assert merged_lines == [
            ".pollypm/",
            "node_modules/",
            "dist/",
            ".env",
        ]

    def test_approve_surfaces_friendly_error_for_unsafe_addadd(self, tmp_path):
        repo, svc, task, _task_branch = self._build_addadd_repo(
            tmp_path,
            "README.md",  # not on the safelist
            "# Project\n\nMain version.\n",
            "# Project\n\nWorker version.\n",
        )

        with pytest.raises(ValidationError) as excinfo:
            svc.approve(task.task_id, "polly")

        msg = str(excinfo.value)
        assert "README.md" in msg
        assert "--resume" in msg
        # The merge must have been aborted cleanly — no MERGE_HEAD, no
        # in-tree conflict markers, working tree restored.
        assert not (repo / ".git" / "MERGE_HEAD").exists()
        assert (repo / "README.md").read_text(encoding="utf-8") == (
            "# Project\n\nMain version.\n"
        )
        # Status should be back to clean (no UU entries).
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "UU" not in status.stdout
        # Task is still in review since approve raised before mutation.
        assert svc.get(task.task_id).work_status == WorkStatus.REVIEW

    def test_approve_resume_after_manual_merge(self, tmp_path):
        """If the user hand-merges + commits the task branch, then runs
        approve --resume, approval proceeds without re-attempting the merge."""
        repo, svc, task, task_branch = self._build_addadd_repo(
            tmp_path,
            "README.md",  # not on safelist — first attempt will surface error
            "# Project\n\nMain version.\n",
            "# Project\n\nWorker version.\n",
        )

        # First attempt surfaces the friendly error, aborts cleanly.
        with pytest.raises(ValidationError):
            svc.approve(task.task_id, "polly")

        # User completes the merge by hand.
        subprocess.run(
            ["git", "-C", str(repo), "merge", "--no-ff", "--no-edit", task_branch],
            check=False,
            capture_output=True,
        )
        # add/add still conflicts here — resolve manually by taking ours.
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "--ours", "README.md"],
            check=True,
        )
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "--no-edit", "-q"],
            check=True,
        )

        # --resume should detect that task_branch is already an ancestor of
        # HEAD and let approval proceed.
        result = svc.approve(task.task_id, "polly", resume_merge=True)
        assert result.work_status == WorkStatus.DONE

    def test_approve_allows_pollypm_import_scaffold_dirt(self, tmp_path):
        repo, svc, task, task_branch = _create_review_task_on_git_repo(tmp_path)
        (repo / ".gitignore").write_text(".pollypm/\n", encoding="utf-8")
        docs = repo / "docs"
        docs.mkdir()
        for name in (
            "project-overview.md",
            "decisions.md",
            "architecture.md",
            "history.md",
            "conventions.md",
        ):
            (docs / name).write_text(
                f"# {name}\n\nGenerated by import.\n\n*Last updated: test*\n",
                encoding="utf-8",
            )

        result = svc.approve(task.task_id, "polly")

        assert result.work_status == WorkStatus.DONE
        assert (repo / "feature.txt").read_text(encoding="utf-8") == "done\n"
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", task_branch, "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert merged.returncode == 0

    def test_approve_allows_untracked_issues_dir(self, tmp_path):
        """#930: ``FileSyncAdapter`` writes per-task markdown to
        ``<project>/issues/<phase>/<n>-<slug>.md`` at every status
        transition without committing. On a fresh project the entire
        ``issues/`` tree shows up as ``?? issues/`` in
        ``git status --porcelain``. The approve dirty-tree gate must
        treat that as scaffold-only and proceed with the auto-merge —
        otherwise every approve after the very first ``pm task create``
        would bounce on uncommitted changes."""
        repo, svc, task, task_branch = _create_review_task_on_git_repo(tmp_path)
        # Simulate FileSyncAdapter's writes mid-cycle.
        issues_review = repo / "issues" / "03-needs-review"
        issues_review.mkdir(parents=True)
        (issues_review / f"{task.task_number:04d}-test-task.md").write_text(
            "# Test task\n\nA test task\n", encoding="utf-8",
        )
        # The seeded helper files FileTaskBackend.ensure_tracker writes.
        (repo / "issues" / ".latest_issue_number").write_text("1\n")
        (repo / "issues" / "notes.md").write_text("# Notes\n")
        (repo / "issues" / "progress-log.md").write_text("# Progress Log\n")

        # Sanity check: tree looks dirty but only because of issues/.
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout
        assert "issues" in status

        result = svc.approve(task.task_id, "polly")

        assert result.work_status == WorkStatus.DONE
        assert (repo / "feature.txt").read_text(encoding="utf-8") == "done\n"
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", task_branch, "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert merged.returncode == 0

    def test_approve_allows_modified_tracked_issues_files(self, tmp_path):
        """#930: existing projects (registered before the gitignore
        fix) have ``issues/`` files already tracked. When a status
        transition rewrites those tracked files, the approve gate must
        still pass — the markdown snapshots are pollypm-managed
        regardless of whether they're tracked or untracked."""
        repo, svc, task, task_branch = _create_review_task_on_git_repo(tmp_path)
        # Pretend the user already committed a stale issues/ snapshot
        # (mirrors counter-trainer / blackjack-trainer).
        issues_review = repo / "issues" / "03-needs-review"
        issues_review.mkdir(parents=True)
        snapshot = issues_review / f"{task.task_number:04d}-test-task.md"
        snapshot.write_text("# stale\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "issues/"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "track issues"],
            check=True,
        )
        # Now FileSyncAdapter rewrites the tracked file at the next
        # transition — modified tracked file, dirty tree.
        snapshot.write_text("# fresh\n\nUpdated content\n", encoding="utf-8")

        result = svc.approve(task.task_id, "polly")
        assert result.work_status == WorkStatus.DONE
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", task_branch, "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert merged.returncode == 0

    def test_approve_still_blocks_user_dirt_with_issues_clean(self, tmp_path):
        """#930 sanity check: widening the gate for issues/ must NOT
        widen it for unrelated user files. A modified ``src.py`` plus
        an untracked ``issues/`` tree should still bounce the approve."""
        repo, svc, task, _task_branch = _create_review_task_on_git_repo(tmp_path)
        # FileSyncAdapter scribbles into issues/.
        (repo / "issues" / "01-ready").mkdir(parents=True)
        (repo / "issues" / "01-ready" / "0001-foo.md").write_text(
            "# foo\n", encoding="utf-8",
        )
        # User has uncommitted src.py changes (dirty tracked file).
        (repo / "README.md").write_text("dirty user edit\n", encoding="utf-8")

        with pytest.raises(ValidationError, match="uncommitted changes"):
            svc.approve(task.task_id, "polly")
        assert svc.get(task.task_id).work_status == WorkStatus.REVIEW

    # ------------------------------------------------------------------
    # #945 — itsalive scaffold files must not block approve
    # ------------------------------------------------------------------

    def test_approve_allows_untracked_itsalive_config(self, tmp_path):
        """#945: ``pm itsalive`` writes ``.itsalive`` (deployToken JSON)
        into the project root. The file is exclusively PollyPM-managed
        and should never bounce ``pm task approve``."""
        repo, svc, task, task_branch = _create_review_task_on_git_repo(tmp_path)
        (repo / ".itsalive").write_text(
            json.dumps({"deployToken": "tok", "domain": "x.itsalive.app"}) + "\n",
            encoding="utf-8",
        )

        result = svc.approve(task.task_id, "polly")

        assert result.work_status == WorkStatus.DONE
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", task_branch, "HEAD"],
            check=False, capture_output=True, text=True,
        )
        assert merged.returncode == 0

    def test_approve_allows_untracked_generated_itsalive_md(self, tmp_path):
        """#945: ``ITSALIVE.md`` with the ``Generated by PollyPM's
        itsalive integration`` marker is PollyPM-generated docs and
        must not block approve."""
        repo, svc, task, _task_branch = _create_review_task_on_git_repo(tmp_path)
        (repo / "ITSALIVE.md").write_text(
            "<!--\n"
            "  DO NOT EDIT THIS FILE\n"
            "  Generated by PollyPM's itsalive integration.\n"
            "-->\n\n# itsalive.co Integration\n",
            encoding="utf-8",
        )

        result = svc.approve(task.task_id, "polly")
        assert result.work_status == WorkStatus.DONE

    def test_approve_blocks_user_edited_itsalive_md_without_marker(self, tmp_path):
        """#945: an ``ITSALIVE.md`` that lacks the PollyPM marker is
        treated as user content — the gate must keep blocking so we
        don't silently swallow a hand-written file."""
        repo, svc, task, _task_branch = _create_review_task_on_git_repo(tmp_path)
        (repo / "ITSALIVE.md").write_text(
            "# My own itsalive notes\n\nNothing PollyPM-shaped here.\n",
            encoding="utf-8",
        )

        with pytest.raises(ValidationError, match="uncommitted changes"):
            svc.approve(task.task_id, "polly")
        assert svc.get(task.task_id).work_status == WorkStatus.REVIEW

    def test_approve_allows_pointer_stub_claude_md(self, tmp_path):
        """#945: when ``pm itsalive`` runs with no pre-existing
        ``CLAUDE.md`` it writes a one-line pointer stub. That tiny
        stub must not block approve."""
        repo, svc, task, _task_branch = _create_review_task_on_git_repo(tmp_path)
        (repo / "CLAUDE.md").write_text(
            "See ITSALIVE.md for itsalive.co deployment and API documentation.\n",
            encoding="utf-8",
        )

        result = svc.approve(task.task_id, "polly")
        assert result.work_status == WorkStatus.DONE

    def test_approve_blocks_large_user_authored_claude_md(self, tmp_path):
        """#945: a 5KB user-authored ``CLAUDE.md`` (no pointer marker,
        well over the 200-byte stub cap) is real signal — keep
        blocking so we don't shadow the user's instructions file."""
        repo, svc, task, _task_branch = _create_review_task_on_git_repo(tmp_path)
        body = "## Project notes\n\n" + ("Detailed user-authored guidance.\n" * 200)
        assert len(body.encode("utf-8")) >= 5000
        (repo / "CLAUDE.md").write_text(body, encoding="utf-8")

        with pytest.raises(ValidationError, match="uncommitted changes"):
            svc.approve(task.task_id, "polly")
        assert svc.get(task.task_id).work_status == WorkStatus.REVIEW

    def test_approve_allows_full_itsalive_scaffold_bundle(self, tmp_path):
        """#945 end-to-end: the actual mix that bounced uno/2 — all
        three itsalive files plus the existing ``issues/`` allow from
        #930 should pass through together."""
        repo, svc, task, task_branch = _create_review_task_on_git_repo(tmp_path)
        (repo / ".itsalive").write_text("{}\n", encoding="utf-8")
        (repo / "ITSALIVE.md").write_text(
            "<!--\n"
            "  DO NOT EDIT THIS FILE\n"
            "  Generated by PollyPM's itsalive integration.\n"
            "-->\n",
            encoding="utf-8",
        )
        (repo / "CLAUDE.md").write_text(
            "See ITSALIVE.md for itsalive.co deployment and API documentation.\n",
            encoding="utf-8",
        )
        (repo / "issues" / "01-ready").mkdir(parents=True)
        (repo / "issues" / "01-ready" / "0001-foo.md").write_text(
            "# foo\n", encoding="utf-8",
        )

        result = svc.approve(task.task_id, "polly")
        assert result.work_status == WorkStatus.DONE
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", task_branch, "HEAD"],
            check=False, capture_output=True, text=True,
        )
        assert merged.returncode == 0

    # ------------------------------------------------------------------
    # #946 — pre-stage PollyPM-managed untracked files before merge
    # ------------------------------------------------------------------

    def _build_worker_commits_file_repo(
        self,
        tmp_path,
        rel_path: str,
        worker_text: str,
    ):
        """Create a repo where the worker branch commits ``rel_path``
        with ``worker_text`` and main has no such file. Caller is
        responsible for writing the colliding untracked copy in the
        project root before calling approve."""
        repo = _git_repo(tmp_path)
        svc = SQLiteWorkService(db_path=tmp_path / "work.db", project_path=repo)
        task = _create_task(svc)
        _claim_task(svc, task)

        current_branch = _git_stdout(repo, "rev-parse", "--abbrev-ref", "HEAD")
        task_branch = f"task/{task.project}-{task.task_number}"
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-q", "-b", task_branch],
            check=True,
        )
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(worker_text, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", rel_path], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", f"feat: add {rel_path}"],
            check=True,
        )
        # Worker also leaves a benign companion change so node_done has
        # something to reference. Not strictly required for the merge
        # itself but keeps the fixture shape close to the real case.
        (repo / "feature.txt").write_text("done\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "feature.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "feat: worker change"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-q", current_branch],
            check=True,
        )
        # Worker branch must vanish from the working tree on main.
        target_after_checkout = repo / rel_path
        if target_after_checkout.exists():
            target_after_checkout.unlink()

        svc.node_done(task.task_id, "pete", _valid_work_output())
        return repo, svc, task, task_branch

    def test_approve_pre_stages_untracked_itsalive_when_worker_commits_it(
        self, tmp_path
    ):
        """#946: worker branch commits ``.itsalive``; project root has
        an untracked ``.itsalive`` from a prior ``pm itsalive deploy``.
        Approve must pre-stage the untracked file so ``git merge``
        doesn't bounce on "untracked working tree files would be
        overwritten"."""
        repo, svc, task, task_branch = self._build_worker_commits_file_repo(
            tmp_path,
            ".itsalive",
            json.dumps({"deployToken": "branch-tok"}) + "\n",
        )
        # Project root has the stale untracked deploy token (different
        # content; merge would otherwise overwrite it).
        (repo / ".itsalive").write_text(
            json.dumps({"deployToken": "stale-tok"}) + "\n",
            encoding="utf-8",
        )

        result = svc.approve(task.task_id, "polly")

        assert result.work_status == WorkStatus.DONE
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", task_branch, "HEAD"],
            check=False, capture_output=True, text=True,
        )
        assert merged.returncode == 0
        # Worker's content wins (it was committed on the branch).
        assert json.loads((repo / ".itsalive").read_text(encoding="utf-8")) == {
            "deployToken": "branch-tok"
        }
        # No conflict markers, no MERGE_HEAD.
        assert not (repo / ".git" / "MERGE_HEAD").exists()

    def test_approve_pre_stages_untracked_itsalive_md_when_worker_commits_it(
        self, tmp_path
    ):
        """#946: worker branch commits a fresh ``ITSALIVE.md`` (with
        marker); project root has a stale marker-bearing ``ITSALIVE.md``
        from a previous deploy. Approve must auto-merge."""
        marker = (
            "<!--\n"
            "  DO NOT EDIT THIS FILE\n"
            "  Generated by PollyPM's itsalive integration.\n"
            "-->\n"
        )
        repo, svc, task, task_branch = self._build_worker_commits_file_repo(
            tmp_path,
            "ITSALIVE.md",
            marker + "\n# itsalive.co Integration (worker)\n",
        )
        (repo / "ITSALIVE.md").write_text(
            marker + "\n# itsalive.co Integration (stale main copy)\n",
            encoding="utf-8",
        )

        result = svc.approve(task.task_id, "polly")
        assert result.work_status == WorkStatus.DONE
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", task_branch, "HEAD"],
            check=False, capture_output=True, text=True,
        )
        assert merged.returncode == 0
        # Worker's content wins.
        assert "(worker)" in (repo / "ITSALIVE.md").read_text(encoding="utf-8")

    def test_approve_does_not_pre_stage_user_authored_claude_md(self, tmp_path):
        """#946: a 5KB user-written ``CLAUDE.md`` (no pointer marker)
        is NOT on the allowlist — the dirty-tree gate must keep
        bouncing approve, and we must not blanket-stage it. Preserves
        user signal."""
        repo, svc, task, _task_branch = self._build_worker_commits_file_repo(
            tmp_path,
            "CLAUDE.md",
            "See ITSALIVE.md for itsalive.co deployment and API documentation.\n",
        )
        big_user_body = "## Project notes\n\n" + (
            "Detailed user-authored guidance.\n" * 200
        )
        assert len(big_user_body.encode("utf-8")) >= 5000
        (repo / "CLAUDE.md").write_text(big_user_body, encoding="utf-8")

        with pytest.raises(ValidationError, match="uncommitted changes"):
            svc.approve(task.task_id, "polly")

        # Task remains in review — approve never mutated state.
        assert svc.get(task.task_id).work_status == WorkStatus.REVIEW
        # User's CLAUDE.md is untouched on disk.
        assert (
            (repo / "CLAUDE.md").read_text(encoding="utf-8") == big_user_body
        )

    def test_approve_pre_stages_untracked_issues_files_no_regression(
        self, tmp_path
    ):
        """#946: ensure the #930 untracked-``issues/`` path still
        works when the worker branch ALSO commits an ``issues/`` file
        with a colliding path. Both the modified (untracked) project-
        root file AND the worker's commit reference the same path,
        so the merge must pre-stage and complete cleanly."""
        repo, svc, task, task_branch = self._build_worker_commits_file_repo(
            tmp_path,
            "issues/03-needs-review/0042-task.md",
            "# Task on worker branch\n\nWorker copy.\n",
        )
        # Project root has an untracked stale snapshot at the same
        # path (matches what FileSyncAdapter writes pre-approve).
        review_dir = repo / "issues" / "03-needs-review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "0042-task.md").write_text(
            "# Task on main\n\nStale snapshot.\n", encoding="utf-8",
        )

        result = svc.approve(task.task_id, "polly")
        assert result.work_status == WorkStatus.DONE
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", task_branch, "HEAD"],
            check=False, capture_output=True, text=True,
        )
        assert merged.returncode == 0
        # Worker's content wins.
        assert "Worker copy" in (
            repo / "issues" / "03-needs-review" / "0042-task.md"
        ).read_text(encoding="utf-8")

    def test_approve_preserves_untracked_itsalive_when_worker_does_not_commit_it(
        self, tmp_path
    ):
        """#947: ``pm itsalive deploy`` writes ``.itsalive`` to the
        project root before any worker branch commits it. A subsequent
        ``pm task approve`` for an unrelated task (worker branch does
        NOT touch ``.itsalive``) must preserve the file rather than
        silently deleting the deploy config. The fix stages + commits
        the file on the current branch before the merge so it ends up
        tracked on main."""
        repo = _git_repo(tmp_path)
        svc = SQLiteWorkService(db_path=tmp_path / "work.db", project_path=repo)
        task = _create_task(svc)
        _claim_task(svc, task)

        # Worker branch makes an unrelated change — does NOT commit
        # ``.itsalive``.
        current_branch = _git_stdout(repo, "rev-parse", "--abbrev-ref", "HEAD")
        task_branch = f"task/{task.project}-{task.task_number}"
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-q", "-b", task_branch],
            check=True,
        )
        (repo / "feature.txt").write_text("worker work\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "feature.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "feat: feature work"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-q", current_branch],
            check=True,
        )
        svc.node_done(task.task_id, "pete", _valid_work_output())

        # Mirror the real-world chord-trainer/1 repro: ``pm itsalive
        # deploy`` left an untracked ``.itsalive`` at root.
        deploy_token = json.dumps(
            {"deployToken": "tok-947", "domain": "ct.itsalive.app"}
        ) + "\n"
        (repo / ".itsalive").write_text(deploy_token, encoding="utf-8")

        result = svc.approve(task.task_id, "polly")

        assert result.work_status == WorkStatus.DONE
        # File still exists on disk with original content.
        assert (repo / ".itsalive").exists(), (
            ".itsalive was deleted during approve (#947 regression)"
        )
        assert (repo / ".itsalive").read_text(encoding="utf-8") == deploy_token
        # And it's tracked in the merge result, not just sitting
        # untracked in the working tree again.
        ls_files = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--", ".itsalive"],
            check=False, capture_output=True, text=True,
        )
        assert ls_files.stdout.strip() == ".itsalive", (
            ".itsalive should be tracked on the current branch after approve"
        )
        # Merge completed cleanly.
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", task_branch, "HEAD"],
            check=False, capture_output=True, text=True,
        )
        assert merged.returncode == 0
        assert not (repo / ".git" / "MERGE_HEAD").exists()


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


class TestReject:
    def test_reject_loops_back(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())

        result = svc.reject(task.task_id, "polly", "Needs more tests")
        # #777 — reviewer rejection now lands the task in an
        # explicit REWORK state instead of bouncing back to
        # IN_PROGRESS. The rework node + assignee are still active
        # so a worker can re-claim and continue, but the cockpit /
        # inbox can now distinguish "fresh implement" from "rework
        # after rejection".
        assert result.work_status == WorkStatus.REWORK
        assert result.current_node_id == "implement"

        # New execution at implement with visit=2
        impl_execs = svc.get_execution(task.task_id, node_id="implement")
        assert len(impl_execs) == 2
        assert impl_execs[0].visit == 1
        assert impl_execs[0].status == ExecutionStatus.COMPLETED
        assert impl_execs[1].visit == 2
        assert impl_execs[1].status == ExecutionStatus.ACTIVE

    def test_rework_can_advance_via_node_done(self, svc):
        """#777 — after rejection, the worker re-runs the implement
        node and calls node_done. REWORK must be a valid source
        state for the node-done transition (otherwise the worker
        gets a "task must be in_progress" error and can't recover).
        """
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())
        rejected = svc.reject(task.task_id, "polly", "needs more tests")
        assert rejected.work_status == WorkStatus.REWORK

        # Worker re-does the implement node and signals done. The
        # transition should succeed — the previously-rejecting
        # status (REWORK) is a legitimate source for node_done.
        re_done = svc.node_done(task.task_id, "pete", _valid_work_output())
        # Next node is review again, so status moves to REVIEW.
        assert re_done.work_status == WorkStatus.REVIEW

    def test_reject_without_reason_rejected(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())

        with pytest.raises(ValidationError, match="Reason is required"):
            svc.reject(task.task_id, "polly", "")

    def test_reject_creates_feedback_inbox_item(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())

        svc.reject(task.task_id, "polly", "Needs better rollback coverage")

        feedback_tasks = [
            candidate
            for candidate in svc.list_tasks(project="proj")
            if is_rejection_feedback_task(candidate)
        ]
        assert len(feedback_tasks) == 1
        feedback = feedback_tasks[0]
        assert feedback_target_task_id(feedback) == task.task_id
        assert rejection_feedback_preview(feedback) == "Needs better rollback coverage"

    def test_full_rejection_cycle(self, svc):
        """implement(v1) -> review -> reject -> implement(v2) -> review -> approve -> done"""
        task = _create_task(svc)
        _claim_task(svc, task)

        # v1: implement -> review -> reject
        svc.node_done(task.task_id, "pete", _valid_work_output())
        svc.reject(task.task_id, "polly", "Needs work")

        # v2: implement -> review -> approve
        wo2 = WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="Fixed the issues",
            artifacts=[
                Artifact(
                    kind=ArtifactKind.COMMIT,
                    description="fix: address review feedback",
                    ref="def456",
                ),
            ],
        )
        svc.node_done(task.task_id, "pete", wo2)
        result = svc.approve(task.task_id, "polly")

        assert result.work_status == WorkStatus.DONE

        # Should have 4 execution records total:
        # implement v1, code_review v1, implement v2, code_review v2(?)
        all_execs = svc.get_execution(task.task_id)
        assert len(all_execs) == 4

        impl_execs = svc.get_execution(task.task_id, node_id="implement")
        assert len(impl_execs) == 2
        assert impl_execs[0].visit == 1
        assert impl_execs[1].visit == 2

        review_execs = svc.get_execution(task.task_id, node_id="code_review")
        assert len(review_execs) == 2
        assert review_execs[0].visit == 1
        assert review_execs[0].decision == Decision.REJECTED
        assert review_execs[1].visit == 2
        assert review_execs[1].decision == Decision.APPROVED


# ---------------------------------------------------------------------------
# block
# ---------------------------------------------------------------------------


class TestBlock:
    def test_block_sets_status(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)

        blocker = _create_task(svc, title="Blocker task")

        result = svc.block(task.task_id, "pm", blocker.task_id)
        assert result.work_status == WorkStatus.BLOCKED

        # Execution should be blocked
        execs = svc.get_execution(task.task_id, node_id="implement")
        assert len(execs) == 1
        assert execs[0].status == ExecutionStatus.BLOCKED

    def test_block_persists_dependency_row(self, svc):
        """block() must INSERT a blocks row into work_task_dependencies so
        auto-unblock can find it when the blocker reaches done (issue #133)."""
        task = _create_task(svc)
        _claim_task(svc, task)

        blocker = _create_task(svc, title="Blocker task")

        svc.block(task.task_id, "pm", blocker.task_id)

        # The dependency row is on the task — blocked_by should reflect it
        blocked = svc.get(task.task_id)
        assert (blocker.project, blocker.task_number) in blocked.blocked_by

        # dependents() from the blocker's side should list the blocked task
        deps = svc.dependents(blocker.task_id)
        assert any(d.task_id == task.task_id for d in deps)

    def test_block_then_blocker_done_auto_unblocks(self, svc):
        """After block(), marking the blocker done should auto-unblock the
        task via _check_auto_unblock (issue #133)."""
        task = _create_task(svc)
        _claim_task(svc, task)

        blocker = _create_task(svc, title="Blocker task")

        svc.block(task.task_id, "pm", blocker.task_id)
        assert svc.get(task.task_id).work_status == WorkStatus.BLOCKED

        # Move blocker to done — auto-unblock should fire
        svc.mark_done(blocker.task_id, "agent-1")

        # Task was IN_PROGRESS before block; auto-unblock returns it to queued.
        unblocked = svc.get(task.task_id)
        assert unblocked.work_status == WorkStatus.QUEUED

    def test_block_fires_sync_adapters(self, svc, tmp_path):
        """block() must call _sync_transition so adapters see the blocked
        state (issue #136)."""
        from pollypm.work.sync import SyncManager

        events: list[tuple[str, str, str]] = []

        class RecordingAdapter:
            name = "recorder"

            def on_create(self, task):
                events.append(("create", task.task_id, ""))

            def on_transition(self, task, old_status, new_status):
                events.append(("transition", old_status, new_status))

            def on_update(self, task, changed_fields):
                events.append(("update", task.task_id, ",".join(changed_fields)))

        mgr = SyncManager()
        mgr.register(RecordingAdapter())

        db_path = tmp_path / "sync.db"
        svc2 = SQLiteWorkService(db_path=db_path, sync_manager=mgr)

        task = _create_task(svc2)
        _claim_task(svc2, task)
        blocker = _create_task(svc2, title="Blocker task")

        events.clear()
        svc2.block(task.task_id, "pm", blocker.task_id)

        # Must have fired a transition event with new_status == 'blocked'
        transition_events = [e for e in events if e[0] == "transition"]
        assert any(
            new == WorkStatus.BLOCKED.value for _, _, new in transition_events
        ), f"Expected blocked transition in {transition_events}"


# ---------------------------------------------------------------------------
# spike flow (no review)
# ---------------------------------------------------------------------------


class TestSpikeFlow:
    def test_spike_flow_no_review(self, svc):
        task = _create_spike_task(svc)
        svc.queue(task.task_id, "pm")
        svc.claim(task.task_id, "pete")

        wo = WorkOutput(
            type=OutputType.DOCUMENT,
            summary="Research findings",
            artifacts=[
                Artifact(
                    kind=ArtifactKind.NOTE,
                    description="Found that X is better than Y",
                ),
            ],
        )
        result = svc.node_done(task.task_id, "pete", wo)
        assert result.work_status == WorkStatus.DONE
        assert result.current_node_id is None


# ---------------------------------------------------------------------------
# get_execution filters
# ---------------------------------------------------------------------------


class TestGetExecution:
    def test_execution_audit_trail(self, svc):
        """Full lifecycle with one rejection."""
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())
        svc.reject(task.task_id, "polly", "Not good enough")
        svc.node_done(task.task_id, "pete", _valid_work_output())
        svc.approve(task.task_id, "polly")

        all_execs = svc.get_execution(task.task_id)
        # implement v1, code_review v1, implement v2, code_review v2
        assert len(all_execs) == 4

        # All should be completed
        for ex in all_execs:
            assert ex.status == ExecutionStatus.COMPLETED

        # Check decisions
        review_execs = [e for e in all_execs if e.node_id == "code_review"]
        assert review_execs[0].decision == Decision.REJECTED
        assert review_execs[0].decision_reason == "Not good enough"
        assert review_execs[1].decision == Decision.APPROVED

    def test_work_output_stored_on_execution(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)

        wo = WorkOutput(
            type=OutputType.CODE_CHANGE,
            summary="Built the feature",
            artifacts=[
                Artifact(
                    kind=ArtifactKind.COMMIT,
                    description="feat: the thing",
                    ref="sha123",
                ),
                Artifact(
                    kind=ArtifactKind.FILE_CHANGE,
                    description="Modified src/main.py",
                    path="src/main.py",
                ),
            ],
        )
        svc.node_done(task.task_id, "pete", wo)

        execs = svc.get_execution(task.task_id, node_id="implement")
        assert len(execs) == 1
        stored = execs[0].work_output
        assert stored is not None
        assert stored.type == OutputType.CODE_CHANGE
        assert stored.summary == "Built the feature"
        assert len(stored.artifacts) == 2
        assert stored.artifacts[0].kind == ArtifactKind.COMMIT
        assert stored.artifacts[0].ref == "sha123"
        assert stored.artifacts[1].path == "src/main.py"

    def test_get_execution_reuses_decoded_work_output(self, svc, monkeypatch):
        task = _create_task(svc)
        _claim_task(svc, task)

        wo = _valid_work_output()
        svc.node_done(task.task_id, "pete", wo)
        svc.reject(task.task_id, "polly", "Redo it")
        svc.node_done(task.task_id, "pete", wo)
        svc._work_output_cache.clear()

        loads = 0
        original_loads = json.loads

        def counting_loads(raw, *args, **kwargs):
            nonlocal loads
            loads += 1
            return original_loads(raw, *args, **kwargs)

        monkeypatch.setattr("pollypm.work.sqlite_service.json.loads", counting_loads)

        first = svc.get_execution(task.task_id, node_id="implement")
        second = svc.get_execution(task.task_id, node_id="implement")

        assert len(first) == 2
        assert len(second) == 2
        assert loads == 1

    def test_get_execution_filters(self, svc):
        task = _create_task(svc)
        _claim_task(svc, task)
        svc.node_done(task.task_id, "pete", _valid_work_output())
        svc.reject(task.task_id, "polly", "Redo it")
        svc.node_done(task.task_id, "pete", _valid_work_output())
        svc.approve(task.task_id, "polly")

        # Filter by node_id
        impl_only = svc.get_execution(task.task_id, node_id="implement")
        assert len(impl_only) == 2

        review_only = svc.get_execution(task.task_id, node_id="code_review")
        assert len(review_only) == 2

        # Filter by visit
        visit1 = svc.get_execution(task.task_id, visit=1)
        assert all(e.visit == 1 for e in visit1)

        visit2 = svc.get_execution(task.task_id, visit=2)
        assert all(e.visit == 2 for e in visit2)

        # Filter by both
        impl_v2 = svc.get_execution(
            task.task_id, node_id="implement", visit=2
        )
        assert len(impl_v2) == 1
        assert impl_v2[0].node_id == "implement"
        assert impl_v2[0].visit == 2
