"""GitHub sync adapter — one-way push to GitHub Issues via gh CLI.

Creates and updates GitHub issues, managing polly: labels to reflect
work service state. Failures are logged but never raised.
"""

from __future__ import annotations

import json
import logging
import subprocess

from pollypm.work.models import Task, WorkStatus

logger = logging.getLogger(__name__)

# Work status → GitHub label mapping
STATUS_TO_LABEL: dict[str, str] = {
    WorkStatus.DRAFT.value: "polly:not-ready",
    WorkStatus.QUEUED.value: "polly:ready",
    WorkStatus.IN_PROGRESS.value: "polly:in-progress",
    WorkStatus.REVIEW.value: "polly:needs-review",
    WorkStatus.DONE.value: "polly:completed",
    WorkStatus.CANCELLED.value: "polly:completed",
    WorkStatus.BLOCKED.value: "polly:blocked",
    WorkStatus.ON_HOLD.value: "polly:on-hold",
}


def _run_gh(args: list[str], repo: str) -> subprocess.CompletedProcess[str]:
    """Run a gh CLI command, raising on failure."""
    cmd = ["gh"] + args + ["--repo", repo]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


class GitHubSyncAdapter:
    """Sync adapter that pushes state to GitHub Issues via gh CLI."""

    name: str = "github"

    def __init__(self, repo: str) -> None:
        self._repo = repo

    def _get_gh_issue_number(self, task: Task) -> str | None:
        """Get the GitHub issue number from external_refs."""
        return task.external_refs.get("github_issue")

    def on_create(self, task: Task) -> None:
        """Create a GitHub issue with the appropriate label."""
        label = STATUS_TO_LABEL.get(task.work_status.value, "polly:not-ready")

        body = task.description or ""
        if task.acceptance_criteria:
            body += f"\n\n## Acceptance Criteria\n\n{task.acceptance_criteria}"

        try:
            result = _run_gh(
                [
                    "issue",
                    "create",
                    "--title",
                    task.title,
                    "--body",
                    body,
                    "--label",
                    label,
                ],
                self._repo,
            )
            # gh issue create outputs the URL; extract the issue number
            url = result.stdout.strip()
            if url:
                # URL is like https://github.com/owner/repo/issues/123
                issue_number = url.rstrip("/").rsplit("/", 1)[-1]
                task.external_refs["github_issue"] = issue_number
                logger.info(
                    "GitHub sync: created issue #%s for %s",
                    issue_number,
                    task.task_id,
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error(
                "GitHub sync: failed to create issue for %s: %s",
                task.task_id,
                e,
            )

    def on_transition(self, task: Task, old_status: str, new_status: str) -> None:
        """Swap labels on the GitHub issue to reflect the new status."""
        issue_number = self._get_gh_issue_number(task)
        if not issue_number:
            logger.warning(
                "GitHub sync: no issue number for %s, skipping transition",
                task.task_id,
            )
            return

        old_label = STATUS_TO_LABEL.get(old_status, "polly:not-ready")
        new_label = STATUS_TO_LABEL.get(new_status, "polly:not-ready")

        if old_label == new_label:
            return

        try:
            _run_gh(
                [
                    "issue",
                    "edit",
                    issue_number,
                    "--remove-label",
                    old_label,
                    "--add-label",
                    new_label,
                ],
                self._repo,
            )
            logger.info(
                "GitHub sync: transitioned issue #%s from %s to %s",
                issue_number,
                old_label,
                new_label,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error(
                "GitHub sync: failed to transition issue #%s for %s: %s",
                issue_number,
                task.task_id,
                e,
            )

    def on_update(self, task: Task, changed_fields: list[str]) -> None:
        """Update the GitHub issue title and/or body."""
        issue_number = self._get_gh_issue_number(task)
        if not issue_number:
            logger.warning(
                "GitHub sync: no issue number for %s, skipping update",
                task.task_id,
            )
            return

        args = ["issue", "edit", issue_number]

        if "title" in changed_fields:
            args.extend(["--title", task.title])

        if "description" in changed_fields or "acceptance_criteria" in changed_fields:
            body = task.description or ""
            if task.acceptance_criteria:
                body += f"\n\n## Acceptance Criteria\n\n{task.acceptance_criteria}"
            args.extend(["--body", body])

        # Only call gh if we have something to update
        if len(args) <= 3:
            return

        try:
            _run_gh(args, self._repo)
            logger.info(
                "GitHub sync: updated issue #%s for %s",
                issue_number,
                task.task_id,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error(
                "GitHub sync: failed to update issue #%s for %s: %s",
                issue_number,
                task.task_id,
                e,
            )
