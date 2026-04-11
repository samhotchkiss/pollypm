"""GitHub issue backend using the gh CLI.

Maps PollyPM issue states to GitHub labels prefixed with ``polly:``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from pollypm.task_backends.base import TaskBackend, TaskRecord

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

TRACKER_STATES = list(STATE_TO_LABEL.keys())

ALL_POLLY_LABELS = list(STATE_TO_LABEL.values())


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

        # Validate transition
        if strict and current_state and current_state in TRACKER_STATES and to_state in TRACKER_STATES:
            from_idx = TRACKER_STATES.index(current_state)
            to_idx = TRACKER_STATES.index(to_state)
            if to_idx > from_idx + 1:
                skipped = TRACKER_STATES[from_idx + 1 : to_idx]
                raise ValueError(
                    f"Issue {task_id} skipping states {current_state} → {to_state} (skipped: {', '.join(skipped)})"
                )

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
