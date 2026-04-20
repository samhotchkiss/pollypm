"""File sync adapter — maintains issues/ folder as a read-only projection.

Maps work service tasks to markdown files in state-named subdirectories
(00-not-ready through 05-completed).
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from pollypm.work.models import Task, WorkStatus

logger = logging.getLogger(__name__)

# Work status → folder name mapping
STATUS_TO_FOLDER: dict[str, str] = {
    WorkStatus.DRAFT.value: "00-not-ready",
    WorkStatus.QUEUED.value: "01-ready",
    WorkStatus.IN_PROGRESS.value: "02-in-progress",
    WorkStatus.REVIEW.value: "03-needs-review",
    WorkStatus.DONE.value: "05-completed",
    WorkStatus.CANCELLED.value: "05-completed",
    WorkStatus.BLOCKED.value: "02-in-progress",
    WorkStatus.ON_HOLD.value: "00-not-ready",
}


def _slugify(title: str) -> str:
    """Convert a title to a filename-safe slug."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "untitled"


def _filename(task: Task) -> str:
    """Generate the filename for a task: <number>-<slug>.md."""
    slug = _slugify(task.title)
    return f"{task.task_number:04d}-{slug}.md"


def _render_markdown(task: Task) -> str:
    """Render task content as markdown."""
    lines: list[str] = []

    title = task.title
    if task.work_status == WorkStatus.CANCELLED:
        title = f"[CANCELLED] {title}"

    lines.append(f"# {title}")
    lines.append("")

    if task.description:
        lines.append(task.description)
        lines.append("")

    if task.acceptance_criteria:
        lines.append("## Acceptance Criteria")
        lines.append("")
        lines.append(task.acceptance_criteria)
        lines.append("")

    return "\n".join(lines)


class FileSyncAdapter:
    """Sync adapter that maintains an issues/ folder structure."""

    name: str = "file"

    def __init__(self, issues_root: Path) -> None:
        self._root = issues_root

    def _folder_for_status(self, status: str) -> str:
        """Return the folder name for a work status value."""
        return STATUS_TO_FOLDER.get(status, "00-not-ready")

    def _ensure_dir(self, folder: str) -> Path:
        """Ensure the state directory exists and return its path."""
        dirpath = self._root / folder
        dirpath.mkdir(parents=True, exist_ok=True)
        return dirpath

    def _find_existing_file(self, task: Task) -> Path | None:
        """Find an existing file for this task across all state directories."""
        if not self._root.exists():
            return None
        prefix = f"{task.task_number:04d}-"
        for child in self._root.iterdir():
            if child.is_dir():
                for f in child.iterdir():
                    if f.name.startswith(prefix) and f.suffix == ".md":
                        return f
        return None

    def on_create(self, task: Task) -> None:
        """Write a markdown file to the appropriate state directory."""
        folder = self._folder_for_status(task.work_status.value)
        dirpath = self._ensure_dir(folder)
        filepath = dirpath / _filename(task)
        filepath.write_text(_render_markdown(task), encoding="utf-8")
        logger.info("File sync: created %s", filepath)

    def on_transition(self, task: Task, old_status: str, new_status: str) -> None:
        """Move the file between state directories based on new status."""
        old_folder = self._folder_for_status(old_status)
        new_folder = self._folder_for_status(new_status)

        # Find and move the existing file
        existing = self._find_existing_file(task)
        if existing is None:
            # File doesn't exist yet; create it
            self.on_create(task)
            return

        new_dir = self._ensure_dir(new_folder)
        new_path = new_dir / _filename(task)

        if existing != new_path:
            shutil.move(str(existing), str(new_path))

        # Rewrite content (e.g. to add [CANCELLED] prefix)
        new_path.write_text(_render_markdown(task), encoding="utf-8")
        logger.info("File sync: moved %s → %s", existing, new_path)

    def on_update(self, task: Task, changed_fields: list[str]) -> None:
        """Rewrite the markdown content."""
        existing = self._find_existing_file(task)
        if existing is None:
            self.on_create(task)
            return

        # If title changed, we may need to rename the file
        new_name = _filename(task)
        if existing.name != new_name:
            new_path = existing.parent / new_name
            shutil.move(str(existing), str(new_path))
            existing = new_path

        existing.write_text(_render_markdown(task), encoding="utf-8")
        logger.info("File sync: updated %s", existing)
