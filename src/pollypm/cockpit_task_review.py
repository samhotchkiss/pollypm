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
import subprocess
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


@dataclass(slots=True)
class ReviewDiffFile:
    path: str
    patch: str
    line_count: int


@dataclass(slots=True)
class ReviewDiffBundle:
    source_label: str
    files: list[ReviewDiffFile]
    total_lines: int


_PLAN_REVIEW_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("Project Plan", "docs/project-plan.md"),
    ("Canonical Plan", "docs/plan/plan.md"),
    ("Planning Session Log", "docs/planning-session-log.md"),
)

_CONFIDENCE_PATTERN = re.compile(
    r"(?im)\bconfidence\s*[:=-]?\s*(10|[0-9])\s*/\s*10\b"
)
_DIFF_FENCE_PATTERN = re.compile(r"```diff\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)
_DIFF_HEADING_PATTERN = re.compile(r"(?im)^(diff --git .+|--- .+\n\+\+\+ .+|@@ .+ @@)")
_DIFF_PATH_PATTERN = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_DIFF_COLLAPSE_LINES = 500

_DIFF_LINK_KEYS = (
    "github_pr",
    "github_pr_number",
    "pull_request",
    "pr_number",
    "branch",
    "branch_name",
    "head_branch",
    "head_ref",
    "head_sha",
    "base_branch",
    "base_ref",
    "base_sha",
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


def load_task_review_diff(
    task,
    project_path: Path | None,
    *,
    review_artifact: ReviewArtifact | None = None,
    active_branch: str | None = None,
) -> ReviewDiffBundle | None:
    """Load a review diff, preferring captured review artifacts over git."""
    artifact = review_artifact
    if artifact is None and project_path is not None:
        artifact = load_task_review_artifact(task, project_path)
    artifact_diff = _artifact_review_diff(artifact)
    if artifact_diff is not None:
        return artifact_diff
    refs = _linked_diff_refs(task, active_branch=active_branch)
    if refs is None or project_path is None:
        return None
    base_ref, head_ref = refs
    diff_text = _git_review_diff(project_path, base_ref=base_ref, head_ref=head_ref)
    if not diff_text:
        return None
    return _bundle_from_diff_text(
        diff_text,
        source_label=f"git diff {base_ref}..{head_ref}",
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


def review_diff_is_large(bundle: ReviewDiffBundle | None) -> bool:
    return bundle is not None and bundle.total_lines > _DIFF_COLLAPSE_LINES


def collapse_review_diff_file(
    diff_file: ReviewDiffFile,
    *,
    max_lines: int = _DIFF_COLLAPSE_LINES,
) -> str:
    lines = diff_file.patch.splitlines()
    if len(lines) <= max_lines:
        return diff_file.patch
    return "\n".join(lines[:max_lines])


def review_diff_hidden_line_count(
    diff_file: ReviewDiffFile,
    *,
    max_lines: int = _DIFF_COLLAPSE_LINES,
) -> int:
    return max(0, diff_file.line_count - max_lines)


def _artifact_review_diff(artifact: ReviewArtifact | None) -> ReviewDiffBundle | None:
    if artifact is None:
        return None
    chunks: list[str] = []
    for section in artifact.sections:
        body = (section.body or "").strip()
        if not body:
            continue
        suffix = section.path.suffix.lower()
        if suffix in {".diff", ".patch"}:
            chunks.append(body)
            continue
        fenced = [chunk.strip() for chunk in _DIFF_FENCE_PATTERN.findall(body) if chunk.strip()]
        if fenced:
            chunks.extend(fenced)
            continue
        if _looks_like_unified_diff(body):
            chunks.append(body)
    if not chunks:
        return None
    return _bundle_from_diff_text(
        "\n".join(chunks),
        source_label="Review Artifact Diff",
    )


def _looks_like_unified_diff(text: str) -> bool:
    if not text:
        return False
    return _DIFF_HEADING_PATTERN.search(text) is not None


def _bundle_from_diff_text(diff_text: str, *, source_label: str) -> ReviewDiffBundle | None:
    files = _parse_diff_files(diff_text)
    if not files:
        return None
    return ReviewDiffBundle(
        source_label=source_label,
        files=files,
        total_lines=sum(item.line_count for item in files),
    )


def _parse_diff_files(diff_text: str) -> list[ReviewDiffFile]:
    lines = diff_text.splitlines()
    if not lines:
        return []
    chunks: list[list[str]] = []
    start_indices = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    if start_indices:
        for offset, start in enumerate(start_indices):
            end = start_indices[offset + 1] if offset + 1 < len(start_indices) else len(lines)
            chunk = lines[start:end]
            if chunk:
                chunks.append(chunk)
    elif _looks_like_unified_diff(diff_text):
        chunks.append(lines)
    files: list[ReviewDiffFile] = []
    for index, chunk_lines in enumerate(chunks, start=1):
        patch = "\n".join(chunk_lines).rstrip()
        if not patch:
            continue
        files.append(
            ReviewDiffFile(
                path=_diff_file_path(chunk_lines, index=index),
                patch=patch,
                line_count=len(chunk_lines),
            )
        )
    return files


def _diff_file_path(chunk_lines: list[str], *, index: int) -> str:
    for line in chunk_lines:
        match = _DIFF_PATH_PATTERN.match(line)
        if match is None:
            continue
        before = _normalize_diff_path(match.group(1))
        after = _normalize_diff_path(match.group(2))
        return after or before or f"Diff {index}"
    for line in chunk_lines:
        if not line.startswith(("--- ", "+++ ")):
            continue
        path = _normalize_diff_path(line[4:].strip())
        if path:
            return path
    return f"Diff {index}"


def _normalize_diff_path(raw_path: str) -> str:
    if raw_path in {"", "/dev/null"}:
        return ""
    if raw_path.startswith(("a/", "b/")):
        return raw_path[2:]
    return raw_path


def _linked_diff_refs(task, *, active_branch: str | None = None) -> tuple[str, str] | None:
    refs = getattr(task, "external_refs", {}) or {}
    if not any(refs.get(key) for key in _DIFF_LINK_KEYS):
        return None
    base_ref = _first_ref(
        refs,
        "base_ref",
        "base_branch",
        "base_sha",
        "base_commit",
        "target_branch",
        "default_branch",
    )
    head_ref = _first_ref(
        refs,
        "head_ref",
        "head_branch",
        "branch",
        "branch_name",
        "head_sha",
    )
    if head_ref is None and active_branch and _first_ref(
        refs,
        "github_pr",
        "github_pr_number",
        "pull_request",
        "pr_number",
    ):
        head_ref = active_branch
    if base_ref is None and head_ref is not None and _first_ref(
        refs,
        "github_pr",
        "github_pr_number",
        "pull_request",
        "pr_number",
    ):
        base_ref = "origin/main"
    if base_ref is None or head_ref is None:
        return None
    return str(base_ref), str(head_ref)


def _first_ref(refs: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = refs.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _git_review_diff(
    project_path: Path,
    *,
    base_ref: str,
    head_ref: str,
) -> str | None:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(project_path),
                "diff",
                "--no-color",
                "--no-ext-diff",
                f"{base_ref}..{head_ref}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    if completed.returncode not in {0, 1}:
        return None
    text = (completed.stdout or "").strip()
    return text or None


__all__ = [
    "ReviewArtifact",
    "ReviewDiffBundle",
    "ReviewDiffFile",
    "collapse_review_diff_file",
    "ReviewSection",
    "extract_confidence_score",
    "load_task_review_diff",
    "load_task_review_artifact",
    "render_task_review_artifact",
    "review_diff_hidden_line_count",
    "review_diff_is_large",
]
