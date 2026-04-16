from __future__ import annotations

from datetime import UTC, datetime
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

from pollypm.task_backends.base import TaskBackend, TaskRecord, TRACKER_STATES, validate_task_transition


class FileTaskBackend(TaskBackend):
    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path.expanduser().resolve()

    def issues_root(self) -> Path:
        return self.project_path / "issues"

    def exists(self) -> bool:
        return self.issues_root().exists()

    def ensure_tracker(self) -> Path:
        issues_dir = self.issues_root()
        for name in TRACKER_STATES:
            (issues_dir / name).mkdir(parents=True, exist_ok=True)
        latest = issues_dir / ".latest_issue_number"
        if not latest.exists():
            latest.write_text("0\n")
        for file_name, default_content in {
            "notes.md": "# Notes\n",
            "progress-log.md": "# Progress Log\n",
        }.items():
            target = issues_dir / file_name
            if not target.exists():
                target.write_text(default_content)
        return issues_dir

    def list_tasks(self, *, states: list[str] | None = None) -> list[TaskRecord]:
        issues_dir = self.issues_root()
        wanted_states = states or TRACKER_STATES
        tasks: list[TaskRecord] = []
        for state in wanted_states:
            state_dir = issues_dir / state
            if not state_dir.exists():
                continue
            for path in sorted(state_dir.glob("*.md")):
                tasks.append(TaskRecord(
                    task_id=path.stem.split("-", 1)[0],
                    title=_task_title(path),
                    state=state,
                    path=path,
                ))
        return tasks

    def get_task(self, task_id: str) -> TaskRecord:
        for task in self.list_tasks():
            if task.task_id == task_id:
                return task
        raise FileNotFoundError(f"Unknown task id: {task_id}")

    def next_available(self) -> TaskRecord | None:
        tasks = self.list_tasks(states=["01-ready"])
        return tasks[0] if tasks else None

    def task_history(self, task_id: str) -> list[str]:
        task = self.get_task(task_id)
        history = [f"state={task.state}", f"title={task.title}"]
        for notes_path in [self.issues_root() / task_id, self.issues_root() / f"#{task_id}", self.issues_root() / "notes.md"]:
            if notes_path.exists():
                history.extend(line for line in notes_path.read_text().splitlines() if line.strip())
        return history

    def create_task(self, *, title: str, body: str = "", state: str = "01-ready") -> TaskRecord:
        self.ensure_tracker()
        # Use the higher of the counter file and the max existing issue ID
        # to avoid collisions when issues are created outside this API.
        counter_value = self.latest_issue_number()
        max_existing = counter_value
        for task in self.list_tasks():
            try:
                existing_num = int(task.task_id)
                max_existing = max(max_existing, existing_num)
            except ValueError:
                pass
        next_id = max(counter_value, max_existing) + 1
        task_id = f"{next_id:04d}"
        slug = _slugify(title)
        path = self.issues_root() / state / f"{task_id}-{slug}.md"
        path.write_text(f"# {task_id} {title}\n\n{body}".rstrip() + "\n")
        (self.issues_root() / ".latest_issue_number").write_text(f"{next_id}\n")
        return TaskRecord(task_id=task_id, title=title, state=state, path=path)

    def move_task(self, task_id: str, to_state: str) -> TaskRecord:
        for task in self.list_tasks():
            if task.task_id != task_id:
                continue
            validate_task_transition(task.state, to_state)
            destination = self.issues_root() / to_state / task.path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(task.path), str(destination))
            self._record_transition(task, to_state)
            return TaskRecord(task_id=task.task_id, title=task.title, state=to_state, path=destination)
        raise FileNotFoundError(f"Unknown task id: {task_id}")

    def read_task(self, task: TaskRecord) -> str:
        return task.path.read_text()

    def append_note(self, name: str, text: str) -> Path:
        self.ensure_tracker()
        target = self.issues_root() / name
        existing = target.read_text() if target.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        target.write_text(existing + text)
        return target

    def latest_issue_number(self) -> int:
        latest = self.issues_root() / ".latest_issue_number"
        if not latest.exists():
            return 0
        try:
            return int(latest.read_text().strip() or "0")
        except ValueError:
            return 0

    def state_counts(self) -> dict[str, int]:
        return {
            state: len(list((self.issues_root() / state).glob("*.md"))) if (self.issues_root() / state).exists() else 0
            for state in TRACKER_STATES
        }

    def _record_transition(self, task: TaskRecord, to_state: str) -> None:
        if task.state == to_state:
            return
        progress_log = self.issues_root() / "progress-log.md"
        stamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if task.state == "04-in-review" and to_state == "02-in-progress":
            action = "request-changes"
        elif task.state == "05-completed" and to_state == "02-in-progress":
            action = "reopen"
        else:
            action = "transition"
        entry = (
            f"- {stamp} {task.task_id} {action}: "
            f"{task.state} -> {to_state} ({task.title})\n"
        )
        existing = progress_log.read_text() if progress_log.exists() else "# Progress Log\n"
        if existing and not existing.endswith("\n"):
            existing += "\n"
        progress_log.write_text(existing + entry)


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-") or "task"


def _task_title(path: Path) -> str:
    text = path.read_text().splitlines()
    if not text:
        return path.stem
    first = text[0].lstrip("#").strip()
    return re.sub(r"^\d+\s+", "", first)
