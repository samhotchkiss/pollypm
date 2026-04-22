"""Task-review artifact helpers for the cockpit tasks screen.

Contract:
- Inputs: a work-service task plus its owning project root.
- Outputs: structured review artifacts and a plain-text rendering.
- Side effects: reads review files from the project workspace only.
- Invariants: review-artifact lookup is independent of the Textual app
  so it can be tested without UI state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ReviewSection:
    title: str
    path: Path
    body: str


@dataclass(slots=True)
class ReviewArtifact:
    title: str
    summary: str
    sections: list[ReviewSection]
    confidence: int | None = None


_PLAN_REVIEW_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("Project Plan", "docs/project-plan.md"),
    ("Canonical Plan", "docs/plan/plan.md"),
    ("Planning Session Log", "docs/planning-session-log.md"),
)

_CONFIDENCE_PATTERN = re.compile(
    r"(?im)\bconfidence\s*[:=-]?\s*(10|[0-9])\s*/\s*10\b"
)


def extract_confidence_score(text: str | None) -> int | None:
    """Return an explicit 0-10 confidence score when one is present."""
    if not text:
        return None
    match = _CONFIDENCE_PATTERN.search(text)
    if match is None:
        return None
    score = int(match.group(1))
    return score if 0 <= score <= 10 else None


def load_task_review_artifact(task, project_path: Path | None) -> ReviewArtifact | None:
    """Load reviewable project artifacts for ``task`` if they exist."""
    if project_path is None:
        return None
    sections: list[ReviewSection] = []
    confidence: int | None = None
    seen: set[Path] = set()
    for title, path in _candidate_review_paths(task, project_path):
        try:
            resolved = path.resolve()
        except Exception:  # noqa: BLE001
            resolved = path
        if resolved in seen or not path.exists() or not path.is_file():
            continue
        try:
            body = path.read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            continue
        if not body:
            continue
        if confidence is None:
            confidence = extract_confidence_score(body)
        sections.append(ReviewSection(title=title, path=path, body=body))
        seen.add(resolved)
    if not sections:
        return None
    summary = "Review the current artifact before approving this task."
    if getattr(task, "flow_template_id", "") == "plan_project":
        summary = "Review the project plan and planning log before approving."
    return ReviewArtifact(
        title="Review Artifact",
        summary=summary,
        sections=sections,
        confidence=confidence,
    )


def render_task_review_artifact(artifact: ReviewArtifact | None) -> str:
    """Render a review artifact bundle as scrollable plain text."""
    if artifact is None:
        return "No review artifact is available for this task yet."
    lines = [artifact.title, "", artifact.summary]
    for section in artifact.sections:
        lines.extend(
            [
                "",
                f"{section.title}",
                f"{section.path}",
                "",
                section.body,
            ]
        )
    return "\n".join(lines).rstrip()


def _candidate_review_paths(
    task, project_path: Path,
) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    if getattr(task, "flow_template_id", "") == "plan_project":
        for title, rel_path in _PLAN_REVIEW_DEFAULTS:
            candidates.append((title, project_path / rel_path))
    for rel_path in getattr(task, "relevant_files", []) or []:
        title = Path(rel_path).name or "Relevant File"
        path = Path(rel_path)
        candidates.append((title, path if path.is_absolute() else project_path / path))
    return candidates


__all__ = [
    "ReviewArtifact",
    "ReviewSection",
    "extract_confidence_score",
    "load_task_review_artifact",
    "render_task_review_artifact",
]
