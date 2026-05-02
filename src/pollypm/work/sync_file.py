"""File sync adapter — maintains issues/ folder as a read-only projection.

Maps work service tasks to markdown files in state-named subdirectories
(00-not-ready through 05-completed).

Issue #1022: each side-effect on ``issues/`` is auto-committed to the
project's git repo so the working tree never accumulates uncommitted
agent-generated state. Without this, every task transition (create,
move between lifecycle folders, content rewrite) leaves the project
root dirty, and the FIRST module task to reach ``code_review`` stalls
because ``pm task approve --auto-merge`` refuses on a dirty root.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from pollypm.work.models import Task, WorkStatus

logger = logging.getLogger(__name__)

# Work status → folder name mapping
STATUS_TO_FOLDER: dict[str, str] = {
    WorkStatus.DRAFT.value: "00-not-ready",
    WorkStatus.QUEUED.value: "01-ready",
    WorkStatus.IN_PROGRESS.value: "02-in-progress",
    # #777 — REWORK is "actively assigned, re-doing rejected work".
    # Same folder as IN_PROGRESS for filesystem-sync purposes; the
    # cockpit / inbox surfaces the rework provenance separately.
    WorkStatus.REWORK.value: "02-in-progress",
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


def _autocommit_issues(project_root: Path, message: str) -> bool:
    """Stage ``issues/`` and commit if anything changed there.

    Issue #1022. Best-effort; never raises. Returns ``True`` if a commit
    was created, ``False`` for any clean no-op or failure path. No-ops
    cleanly when:

    - the project is not a git repo (tests use bare temp dirs);
    - a merge is in progress (``.git/MERGE_HEAD`` exists) — committing
      then would clobber the half-finished merge;
    - ``issues/`` has no churn (``git diff --cached`` empty);
    - any git invocation fails (filesystem race, permissions, etc.).

    Uses inline ``-c user.name=...`` / ``-c user.email=...`` so the commit
    works in CI / fresh-checkout environments where the user identity
    isn't configured globally.

    Issue #1046. Failures are now surfaced at ``WARNING`` (not ``DEBUG``)
    so a regression like the one observed in #1046 — auto-commit
    silently failing and leaving ``issues/`` dirty across many project
    roots — is visible in operator logs immediately. Clean no-ops (no
    git repo, merge in progress, nothing to commit) stay at ``DEBUG``.
    """
    try:
        git_dir = project_root / ".git"
        if not git_dir.exists():
            return False
        # `.git` may be a file (worktree pointer); both forms are valid
        # repo markers. The MERGE_HEAD check below assumes a directory
        # layout — if `.git` is a file, skip the merge-in-progress
        # check and rely on the staging/commit no-oping when the index
        # is in an inconsistent state.
        if git_dir.is_dir() and (git_dir / "MERGE_HEAD").exists():
            logger.debug(
                "Skipping issues/ auto-commit: merge in progress at %s",
                project_root,
            )
            return False

        issues_dir = project_root / "issues"
        if not issues_dir.exists():
            return False

        base_cmd = [
            "git",
            "-C",
            str(project_root),
            "-c",
            "user.name=PollyPM",
            "-c",
            "user.email=pollypm@local",
        ]

        add = subprocess.run(
            [*base_cmd, "add", "--", "issues"],
            check=False,
            capture_output=True,
            text=True,
        )
        if add.returncode != 0:
            # #1046 — promoted from DEBUG. A failed ``git add`` here
            # means the lifecycle move / scaffold won't be captured and
            # the project root will stay dirty. Operator needs to see.
            logger.warning(
                "issues/ auto-commit: git add failed at %s: %s",
                project_root,
                add.stderr.strip() or add.stdout.strip(),
            )
            return False

        # If nothing is staged for issues/, skip the commit. Using
        # ``--quiet`` means a non-zero exit means "there is something
        # staged" — exactly what we want.
        diff = subprocess.run(
            [*base_cmd, "diff", "--cached", "--quiet", "--", "issues"],
            check=False,
            capture_output=True,
            text=True,
        )
        if diff.returncode == 0:
            # No staged changes under issues/ — nothing to commit.
            return False

        commit = subprocess.run(
            [*base_cmd, "commit", "-m", message, "--only", "--", "issues"],
            check=False,
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            # #1046 — promoted from DEBUG. ``git commit`` failure here
            # is exactly the silent-regression mode that #1046 calls
            # out: the rename has happened on disk but never landed in
            # a commit, so the working tree stays dirty.
            logger.warning(
                "issues/ auto-commit: git commit failed at %s: %s",
                project_root,
                commit.stderr.strip() or commit.stdout.strip(),
            )
            return False
        logger.info(
            "issues/ auto-commit: %s (root=%s)",
            message,
            project_root,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        # Never let a sync-side commit propagate. #1046 — promoted from
        # DEBUG so operators see the regression that this guard hides.
        logger.warning(
            "issues/ auto-commit suppressed at %s: %s",
            project_root,
            exc,
        )
        return False


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

    def _project_root(self) -> Path:
        """Return the directory containing ``issues/`` — the project root."""
        return self._root.parent

    def on_create(self, task: Task) -> None:
        """Write a markdown file to the appropriate state directory."""
        folder = self._folder_for_status(task.work_status.value)
        dirpath = self._ensure_dir(folder)
        filepath = dirpath / _filename(task)
        filepath.write_text(_render_markdown(task), encoding="utf-8")
        logger.info("File sync: created %s", filepath)
        _autocommit_issues(
            self._project_root(),
            f"chore(issues): add {task.task_id} ({task.work_status.value})",
        )

    def on_transition(self, task: Task, old_status: str, new_status: str) -> None:
        """Move the file between state directories based on new status."""
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
        _autocommit_issues(
            self._project_root(),
            f"chore(issues): move {task.task_id} {old_status} -> {new_status}",
        )

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
        _autocommit_issues(
            self._project_root(),
            f"chore(issues): update {task.task_id}",
        )
