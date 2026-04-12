"""GitHub issue backend using the gh CLI.

Maps PollyPM issue states to GitHub labels prefixed with ``polly:``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pollypm.task_backends.base import TaskBackend, TaskRecord, TRACKER_STATES, validate_task_transition

logger = logging.getLogger(__name__)

# Maps internal state names to GitHub label names
STATE_TO_LABEL: dict[str, str] = {
    "00-not-ready": "polly:not-ready",
    "01-ready": "polly:ready",
    "02-in-progress": "polly:in-progress",
    "03-needs-review": "polly:needs-review",
    "04-in-review": "polly:in-review",
    "05-completed": "polly:completed",
}

LABEL_TO_STATE: dict[str, str] = {v: k for k, v in STATE_TO_LABEL.items()}

ALL_POLLY_LABELS = list(STATE_TO_LABEL.values())


@dataclass(slots=True)
class GitHubTaskBackendValidation:
    passed: bool
    checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a gh CLI command and return the result."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}... failed: {result.stderr.strip()[:200]}")
    return result


class GitHubTaskBackend(TaskBackend):
    """Issue tracker backed by GitHub Issues with polly:* labels."""

    def __init__(self, project_path: Path, *, repo: str | None = None) -> None:
        self.project_path = project_path.expanduser().resolve()
        self._repo = repo  # e.g. "samhotchkiss/pollypm"; auto-detected if None

    @property
    def repo(self) -> str:
        if self._repo:
            return self._repo
        result = _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        self._repo = result.stdout.strip()
        return self._repo

    def issues_root(self) -> Path:
        """Not meaningful for GitHub — return project root for compatibility."""
        return self.project_path

    def exists(self) -> bool:
        try:
            _gh("repo", "view", "--json", "name")
            return True
        except (RuntimeError, FileNotFoundError):
            return False

    def validate(self) -> GitHubTaskBackendValidation:
        checks: list[str] = []
        errors: list[str] = []

        try:
            _gh("repo", "view", "--json", "nameWithOwner", "--repo", self.repo)
            checks.append("repo_accessible")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"repo_accessible: {exc}")
            return GitHubTaskBackendValidation(passed=False, checks=checks, errors=errors)

        try:
            self.ensure_tracker()
            checks.append("labels_ensured")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"labels_ensured: {exc}")
            return GitHubTaskBackendValidation(passed=False, checks=checks, errors=errors)

        validation_task: TaskRecord | None = None
        try:
            validation_task = self.create_task(
                title="PollyPM GitHub backend validation",
                body="Temporary validation issue created by PollyPM.",
                state="01-ready",
            )
            checks.append("create_task")
            self.move_task(validation_task.task_id, "02-in-progress")
            checks.append("move_task")
            self.get_task(validation_task.task_id)
            checks.append("get_task")
            self.append_note(validation_task.task_id, "Validation comment from PollyPM.")
            checks.append("append_note")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"roundtrip: {exc}")
        finally:
            if validation_task is not None:
                try:
                    self.append_note(validation_task.task_id, "Validation cleanup: closing temporary issue.")
                    self.move_task(validation_task.task_id, "03-needs-review")
                    self.move_task(validation_task.task_id, "04-in-review")
                    self.move_task(validation_task.task_id, "05-completed")
                    checks.append("cleanup")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"cleanup: {exc}")

        return GitHubTaskBackendValidation(passed=not errors, checks=checks, errors=errors)

    def ensure_tracker(self) -> Path:
        """Ensure all polly:* labels exist on the repo."""
        existing = set()
        try:
            result = _gh("label", "list", "--json", "name", "-q", ".[].name", "--repo", self.repo)
            existing = {name.strip() for name in result.stdout.strip().splitlines()}
        except RuntimeError:
            pass
        for label in ALL_POLLY_LABELS:
            if label not in existing:
                try:
                    _gh("label", "create", label, "--repo", self.repo, "--force", check=False)
                except RuntimeError:
                    pass
        return self.project_path

    def list_tasks(self, *, states: list[str] | None = None) -> list[TaskRecord]:
        wanted_states = states or TRACKER_STATES
        wanted_labels = [STATE_TO_LABEL[s] for s in wanted_states if s in STATE_TO_LABEL]
        if not wanted_labels:
            return []

        tasks: list[TaskRecord] = []
        for label in wanted_labels:
            state = LABEL_TO_STATE[label]
            try:
                result = _gh(
                    "issue", "list",
                    "--label", label,
                    "--state", "all",
                    "--json", "number,title,state",
                    "--repo", self.repo,
                    "--limit", "200",
                )
                issues = json.loads(result.stdout) if result.stdout.strip() else []
            except (RuntimeError, json.JSONDecodeError):
                continue
            for issue in issues:
                tasks.append(TaskRecord(
                    task_id=str(issue["number"]),
                    title=issue["title"],
                    state=state,
                    path=self.project_path / f"#{issue['number']}",
                ))
        return tasks

    def get_task(self, task_id: str) -> TaskRecord:
        result = _gh(
            "issue", "view", task_id,
            "--json", "number,title,labels",
            "--repo", self.repo,
        )
        issue = json.loads(result.stdout)
        state = "01-ready"
        for label in issue.get("labels", []):
            name = label.get("name")
            if isinstance(name, str) and name in LABEL_TO_STATE:
                state = LABEL_TO_STATE[name]
                break
        issue_id = str(issue["number"])
        return TaskRecord(
            task_id=issue_id,
            title=str(issue["title"]),
            state=state,
            path=self.project_path / f"#{issue_id}",
        )

    def next_available(self) -> TaskRecord | None:
        result = _gh(
            "issue", "list",
            "--label", STATE_TO_LABEL["01-ready"],
            "--state", "all",
            "--json", "number,title,state",
            "--repo", self.repo,
            "--limit", "1",
        )
        issues = json.loads(result.stdout) if result.stdout.strip() else []
        if not issues:
            return None
        issue = issues[0]
        issue_id = str(issue["number"])
        return TaskRecord(
            task_id=issue_id,
            title=str(issue["title"]),
            state="01-ready",
            path=self.project_path / f"#{issue_id}",
        )

    def task_history(self, task_id: str) -> list[str]:
        result = _gh(
            "issue", "view", task_id,
            "--json", "comments",
            "--repo", self.repo,
        )
        issue = json.loads(result.stdout)
        comments = issue.get("comments", [])
        history: list[str] = []
        for item in comments:
            author = item.get("author", {}).get("login", "unknown")
            body = str(item.get("body", "")).strip()
            if body:
                history.append(f"{author}: {body}")
        return history

    def create_task(self, *, title: str, body: str = "", state: str = "01-ready") -> TaskRecord:
        label = STATE_TO_LABEL.get(state, "polly:ready")
        args = [
            "issue", "create",
            "--title", title,
            "--body", body or "Created by PollyPM",
            "--label", label,
            "--repo", self.repo,
        ]
        result = _gh(*args)
        # gh issue create outputs the URL; extract the issue number
        url = result.stdout.strip()
        match = re.search(r"/issues/(\d+)", url)
        if not match:
            raise RuntimeError(f"Could not parse issue number from: {url}")
        task_id = match.group(1)
        return TaskRecord(
            task_id=task_id,
            title=title,
            state=state,
            path=self.project_path / f"#{task_id}",
        )

    def move_task(self, task_id: str, to_state: str, *, strict: bool = False) -> TaskRecord:
        # Get current issue info
        result = _gh(
            "issue", "view", task_id,
            "--json", "title,labels",
            "--repo", self.repo,
        )
        issue = json.loads(result.stdout)
        title = issue["title"]
        current_labels = [l["name"] for l in issue.get("labels", [])]

        # Find current polly state
        current_state = None
        for lbl in current_labels:
            if lbl in LABEL_TO_STATE:
                current_state = LABEL_TO_STATE[lbl]
                break

        if current_state:
            validate_task_transition(current_state, to_state)

        # Remove all polly:* labels, add the new one
        new_label = STATE_TO_LABEL.get(to_state)
        if not new_label:
            raise ValueError(f"Unknown state: {to_state}")

        for lbl in current_labels:
            if lbl.startswith("polly:"):
                _gh("issue", "edit", task_id, "--remove-label", lbl, "--repo", self.repo, check=False)

        _gh("issue", "edit", task_id, "--add-label", new_label, "--repo", self.repo)

        # Close the issue if moving to completed
        if to_state == "05-completed":
            _gh("issue", "close", task_id, "--repo", self.repo, check=False)
        elif to_state != "05-completed":
            # Reopen if moving out of completed
            _gh("issue", "reopen", task_id, "--repo", self.repo, check=False)

        return TaskRecord(
            task_id=task_id,
            title=title,
            state=to_state,
            path=self.project_path / f"#{task_id}",
        )

    def read_task(self, task: TaskRecord) -> str:
        result = _gh(
            "issue", "view", task.task_id,
            "--json", "title,body,number",
            "--repo", self.repo,
        )
        issue = json.loads(result.stdout)
        body = str(issue.get("body", "")).strip()
        title = str(issue.get("title", task.title)).strip()
        return f"# {task.task_id} {title}\n\n{body}".rstrip() + "\n"

    def append_note(self, name: str, text: str) -> Path:
        """Append a comment to a GitHub issue. 'name' should be the issue number."""
        # If name looks like an issue number, comment on it
        match = re.match(r"#?(\d+)", name)
        if match:
            _gh(
                "issue", "comment", match.group(1),
                "--body", text,
                "--repo", self.repo,
            )
            return self.project_path / f"#{match.group(1)}"
        # Fallback: write to a local file
        target = self.project_path / name
        existing = target.read_text() if target.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        target.write_text(existing + text)
        return target

    def latest_issue_number(self) -> int:
        try:
            result = _gh(
                "issue", "list",
                "--state", "all",
                "--json", "number",
                "--repo", self.repo,
                "--limit", "1",
                "-q", ".[0].number",
            )
            return int(result.stdout.strip() or "0")
        except (RuntimeError, ValueError):
            return 0

    def state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for state, label in STATE_TO_LABEL.items():
            try:
                result = _gh(
                    "issue", "list",
                    "--label", label,
                    "--state", "all",
                    "--json", "number",
                    "--repo", self.repo,
                    "-q", "length",
                )
                counts[state] = int(result.stdout.strip() or "0")
            except (RuntimeError, ValueError):
                counts[state] = 0
        return counts
