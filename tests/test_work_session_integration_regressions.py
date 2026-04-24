"""Regression coverage for review approval vs worker-session teardown."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from pollypm.work.models import WorkStatus
from pollypm.work.sqlite_service import SQLiteWorkService


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo,
        check=True,
    )
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


def _create_review_task(svc):
    task = svc.create(
        title="Review task",
        description="Exercise approval lifecycle",
        type="task",
        project="proj",
        flow_template="standard",
        roles={"worker": "pete", "reviewer": "polly"},
        priority="normal",
        created_by="tester",
    )
    svc.queue(task.task_id, "pm")
    svc.claim(task.task_id, "pete")
    return task


def _prepare_review_task(tmp_path, *, stay_on_task_branch: bool):
    repo = _git_repo(tmp_path)
    svc = SQLiteWorkService(db_path=tmp_path / "work.db", project_path=repo)
    session_mgr = MagicMock()
    svc.set_session_manager(session_mgr)
    task = _create_review_task(svc)

    main_branch = _git_stdout(repo, "rev-parse", "--abbrev-ref", "HEAD")
    task_branch = f"task/{task.project}-{task.task_number}"
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", task_branch],
        check=True,
    )
    (repo / "feature.txt").write_text("done\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "feature.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "feat: worker change"],
        check=True,
    )
    if not stay_on_task_branch:
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-q", main_branch],
            check=True,
        )

    svc.node_done(
        task.task_id,
        "pete",
        {
            "type": "code_change",
            "summary": "Implemented feature X",
            "artifacts": [
                {
                    "kind": "commit",
                    "description": "feat: worker change",
                    "ref": "abc123",
                }
            ],
        },
    )
    return repo, svc, task, session_mgr


def test_approve_keeps_session_alive_until_work_is_integrated(tmp_path):
    repo, svc, task, session_mgr = _prepare_review_task(
        tmp_path,
        stay_on_task_branch=True,
    )

    result = svc.approve(task.task_id, "polly")

    assert result.work_status == WorkStatus.DONE
    assert _git_stdout(repo, "rev-parse", "--abbrev-ref", "HEAD") == (
        f"task/{task.project}-{task.task_number}"
    )
    session_mgr.teardown_worker.assert_not_called()


def test_approve_tears_down_session_after_auto_merge_integration(tmp_path):
    _repo, svc, task, session_mgr = _prepare_review_task(
        tmp_path,
        stay_on_task_branch=False,
    )

    result = svc.approve(task.task_id, "polly")

    assert result.work_status == WorkStatus.DONE
    session_mgr.teardown_worker.assert_called_once_with(task.task_id)
