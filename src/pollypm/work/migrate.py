"""Migration tool — import existing issues/ directories into the work service.

Scans the file-based issue directory structure and creates corresponding
tasks in the work service. Idempotent: skips tasks that already exist.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from pollypm.work.models import WorkStatus

logger = logging.getLogger(__name__)

# Folder name → work status mapping (reverse of file adapter)
FOLDER_TO_STATUS: dict[str, str] = {
    "00-not-ready": WorkStatus.DRAFT.value,
    "01-ready": WorkStatus.QUEUED.value,
    "02-in-progress": WorkStatus.IN_PROGRESS.value,
    "03-needs-review": WorkStatus.REVIEW.value,
    "04-in-review": WorkStatus.REVIEW.value,
    "05-completed": WorkStatus.DONE.value,
}


@dataclass
class MigrationResult:
    """Summary of a migration run."""

    created: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_filename(filename: str) -> tuple[int, str] | None:
    """Parse '<number>-<slug>.md' into (task_number, slug).

    Returns None if the filename doesn't match the expected pattern.
    """
    match = re.match(r"^(\d+)-(.+)\.md$", filename)
    if not match:
        return None
    task_number = int(match.group(1))
    slug = match.group(2)
    return task_number, slug


def _slug_to_title(slug: str) -> str:
    """Convert a slug back to a human-readable title."""
    return slug.replace("-", " ").strip().title()


def _parse_content(text: str) -> tuple[str, str]:
    """Parse markdown content into (title, description).

    First H1 line is the title; everything after is the description.
    """
    lines = text.split("\n")
    title = ""
    desc_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# ") and not title:
            title = stripped[2:].strip()
            desc_start = i + 1
            break

    if not title:
        # No H1 found; use all content as description
        return "", text.strip()

    # Skip blank lines after the title
    while desc_start < len(lines) and not lines[desc_start].strip():
        desc_start += 1

    description = "\n".join(lines[desc_start:]).strip()
    return title, description


def migrate_issues(
    issues_dir: Path,
    work_service,
    project: str,
    flow: str = "standard",
) -> MigrationResult:
    """Scan issues/ directory and create tasks in the work service.

    Maps folder position to work_status:
    - 00-not-ready -> draft
    - 01-ready -> queued
    - 02-in-progress -> queued (can't auto-claim)
    - 03-needs-review -> queued
    - 04-in-review -> queued
    - 05-completed -> done

    Idempotent: skips tasks that already exist (by task_number).
    """
    result = MigrationResult()

    if not issues_dir.is_dir():
        result.errors.append(f"Issues directory not found: {issues_dir}")
        return result

    # Collect existing task numbers to check for duplicates
    try:
        existing_tasks = work_service.list_tasks(project=project)
        existing_numbers = {t.task_number for t in existing_tasks}
    except Exception:
        existing_numbers = set()

    # Scan all state directories
    for state_dir in sorted(issues_dir.iterdir()):
        if not state_dir.is_dir():
            continue

        folder_name = state_dir.name
        target_status = FOLDER_TO_STATUS.get(folder_name)
        if target_status is None:
            logger.debug("Skipping unrecognised folder: %s", folder_name)
            continue

        for md_file in sorted(state_dir.iterdir()):
            if not md_file.is_file() or md_file.suffix != ".md":
                continue

            parsed = _parse_filename(md_file.name)
            if parsed is None:
                result.errors.append(f"Could not parse filename: {md_file.name}")
                continue

            task_number, slug = parsed

            # Idempotency check
            if task_number in existing_numbers:
                result.skipped += 1
                continue

            # Parse content
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception as e:
                result.errors.append(f"Could not read {md_file}: {e}")
                continue

            title, description = _parse_content(text)
            if not title:
                title = _slug_to_title(slug)

            # Create task in draft state
            try:
                task = work_service.create(
                    title=title,
                    description=description,
                    type="task",
                    project=project,
                    flow_template=flow,
                    roles={"worker": "migrated", "reviewer": "migrated"},
                    priority="normal",
                    created_by="migration",
                )

                # Advance task to target status if needed
                _advance_to_status(work_service, task, target_status)

                existing_numbers.add(task.task_number)
                result.created += 1
                logger.info(
                    "Migrated %s → %s/%d (%s)",
                    md_file.name,
                    project,
                    task.task_number,
                    target_status,
                )
            except Exception as e:
                result.errors.append(f"Failed to create task from {md_file.name}: {e}")

    return result


def _advance_to_status(work_service, task, target_status: str) -> None:
    """Advance a draft task to the target work status.

    Uses the work service lifecycle methods to properly transition
    through states. For completed tasks, we go through the full flow.
    """
    task_id = task.task_id

    if target_status == WorkStatus.DRAFT.value:
        return  # Already in draft

    if target_status == WorkStatus.QUEUED.value:
        work_service.queue(task_id, "migration")
        return

    if target_status in (
        WorkStatus.IN_PROGRESS.value,
        WorkStatus.REVIEW.value,
    ):
        # Queue it — we can't auto-claim or advance further safely
        work_service.queue(task_id, "migration")
        return

    if target_status == WorkStatus.DONE.value:
        # Full lifecycle: draft -> queue -> claim -> node_done -> approve
        # Use "migrated" as actor for claim/node_done since that matches
        # the worker role assigned during migration.
        work_service.queue(task_id, "migration")
        work_service.claim(task_id, "migrated")
        work_service.node_done(
            task_id,
            "migrated",
            {
                "type": "mixed",
                "summary": "Migrated from file-based issues",
                "artifacts": [
                    {"kind": "note", "description": "Migrated from file-based issue system"}
                ],
            },
        )
        work_service.approve(task_id, "migrated", "Migrated as completed")
        return
