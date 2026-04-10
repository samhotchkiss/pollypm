"""Project history import pipeline.

Five-stage pipeline that transforms raw project artifacts into structured
documentation:

1. Discover sources (JSONL transcripts, git commits, existing docs, configs)
2. Build chronological timeline
3. Extract understanding (decisions, goals, architecture, conventions)
4. Generate docs/ files
5. User interview (confirm/correct before locking)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pollypm.knowledge_extract import _sanitize_text
from pollypm.projects import project_transcripts_dir


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HAIKU_MODEL = "claude-3-5-haiku-latest"

PROVIDER_TRANSCRIPT_DIRS = (".claude", ".codex")

CONFIG_FILE_NAMES = (
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".github/workflows",
    "Gemfile",
    "go.mod",
    "tsconfig.json",
    "setup.py",
    "setup.cfg",
)

DOC_FILE_PATTERNS = (
    "README*",
    "CONTRIBUTING*",
    "ARCHITECTURE*",
    "CHANGELOG*",
    "docs/*.md",
)

GENERATED_DOC_FILES = (
    "project-overview.md",
    "decisions.md",
    "architecture.md",
    "history.md",
    "conventions.md",
)

# Maximum items per source type to avoid unbounded extraction
MAX_GIT_COMMITS = 500
MAX_JSONL_EVENTS = 1000
MAX_TIMELINE_EVENTS = 2000


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TimelineEntry:
    """Single entry on the chronological timeline."""

    timestamp: str
    source_type: str  # "git_commit", "jsonl_event", "doc_file", "config_file"
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    def sort_key(self) -> tuple[str, str]:
        return (self.timestamp, self.source_type)


@dataclass(slots=True)
class DiscoveredSources:
    """All sources found during stage 1."""

    jsonl_files: list[Path] = field(default_factory=list)
    git_available: bool = False
    git_commit_count: int = 0
    doc_files: list[Path] = field(default_factory=list)
    config_files: list[Path] = field(default_factory=list)
    provider_transcript_dirs: list[Path] = field(default_factory=list)

    def total_sources(self) -> int:
        count = len(self.jsonl_files) + len(self.doc_files) + len(self.config_files)
        if self.git_available:
            count += 1
        return count


@dataclass(slots=True)
class ExtractedUnderstanding:
    """Structured understanding extracted from the timeline."""

    project_name: str = ""
    overview: str = ""
    decisions: list[str] = field(default_factory=list)
    architecture: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ImportResult:
    """Result of the full import pipeline."""

    sources_found: int = 0
    timeline_events: int = 0
    docs_generated: int = 0
    provider_transcripts_copied: int = 0
    interview_questions: list[str] = field(default_factory=list)
    locked: bool = False


# ---------------------------------------------------------------------------
# Stage 1: Discover Sources
# ---------------------------------------------------------------------------


def discover_sources(project_root: Path) -> DiscoveredSources:
    """Scan the project for all available history sources."""
    sources = DiscoveredSources()

    # JSONL transcripts from .pollypm/transcripts/
    transcripts_dir = project_transcripts_dir(project_root)
    if transcripts_dir.exists():
        sources.jsonl_files = sorted(transcripts_dir.rglob("*.jsonl"))

    # Provider-specific transcript dirs (pre-PollyPM history)
    for dirname in PROVIDER_TRANSCRIPT_DIRS:
        provider_dir = project_root / dirname
        if provider_dir.is_dir():
            sources.provider_transcript_dirs.append(provider_dir)
            for jsonl_file in sorted(provider_dir.rglob("*.jsonl")):
                if jsonl_file not in sources.jsonl_files:
                    sources.jsonl_files.append(jsonl_file)

    # Git history
    sources.git_available = (project_root / ".git").is_dir()
    if sources.git_available:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-list", "--count", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip().isdigit():
            sources.git_commit_count = int(result.stdout.strip())

    # Existing documentation
    for pattern in DOC_FILE_PATTERNS:
        for match in sorted(project_root.glob(pattern)):
            if match.is_file() and match not in sources.doc_files:
                sources.doc_files.append(match)

    # Config files
    for name in CONFIG_FILE_NAMES:
        candidate = project_root / name
        if candidate.is_file():
            sources.config_files.append(candidate)
        elif candidate.is_dir():
            # e.g. .github/workflows directory
            for child in sorted(candidate.rglob("*.yml")) + sorted(candidate.rglob("*.yaml")):
                if child.is_file():
                    sources.config_files.append(child)

    return sources


# ---------------------------------------------------------------------------
# Stage 2: Build Timeline
# ---------------------------------------------------------------------------


def build_timeline(project_root: Path, sources: DiscoveredSources) -> list[TimelineEntry]:
    """Merge all discovered sources into a single chronological stream."""
    timeline: list[TimelineEntry] = []

    # Git commits
    if sources.git_available:
        timeline.extend(_git_commits_to_timeline(project_root))

    # JSONL events
    for jsonl_path in sources.jsonl_files:
        timeline.extend(_jsonl_file_to_timeline(jsonl_path))

    # Existing docs (use file mtime as timestamp)
    for doc_path in sources.doc_files:
        timeline.extend(_doc_file_to_timeline(doc_path, project_root))

    # Config files
    for config_path in sources.config_files:
        timeline.extend(_config_file_to_timeline(config_path, project_root))

    # Sort chronologically
    timeline.sort(key=lambda entry: entry.sort_key())

    # Bound the timeline
    if len(timeline) > MAX_TIMELINE_EVENTS:
        timeline = timeline[-MAX_TIMELINE_EVENTS:]

    return timeline


def _git_commits_to_timeline(project_root: Path) -> list[TimelineEntry]:
    """Extract git commits as timeline entries."""
    result = subprocess.run(
        [
            "git", "-C", str(project_root), "log",
            f"--max-count={MAX_GIT_COMMITS}",
            "--format=%H%x00%aI%x00%s%x00%b",
            "--reverse",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []

    entries: list[TimelineEntry] = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\x00", 3)
        if len(parts) < 3:
            continue
        commit_hash, timestamp, subject = parts[0], parts[1], parts[2]
        body = parts[3].strip() if len(parts) > 3 else ""
        entries.append(
            TimelineEntry(
                timestamp=timestamp,
                source_type="git_commit",
                summary=_sanitize_text(subject),
                details={
                    "hash": commit_hash[:12],
                    "body": _sanitize_text(body)[:500] if body else "",
                },
            )
        )
    return entries


def _jsonl_file_to_timeline(jsonl_path: Path) -> list[TimelineEntry]:
    """Parse JSONL file into timeline entries."""
    entries: list[TimelineEntry] = []
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="ignore") as handle:
            count = 0
            for line in handle:
                if count >= MAX_JSONL_EVENTS:
                    break
                raw = line.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                timestamp = obj.get("timestamp", "")
                event_type = obj.get("event_type") or obj.get("type", "unknown")
                payload = obj.get("payload", {})
                text = ""
                if isinstance(payload, dict):
                    text = str(payload.get("text", ""))[:300]
                elif isinstance(payload, str):
                    text = payload[:300]

                if not timestamp:
                    continue

                entries.append(
                    TimelineEntry(
                        timestamp=str(timestamp),
                        source_type="jsonl_event",
                        summary=_sanitize_text(f"[{event_type}] {text}".strip()),
                        details={"event_type": event_type, "source_file": str(jsonl_path.name)},
                    )
                )
                count += 1
    except OSError:
        pass
    return entries


def _doc_file_to_timeline(doc_path: Path, project_root: Path) -> list[TimelineEntry]:
    """Convert a doc file into a timeline entry using its mtime."""
    try:
        mtime = datetime.fromtimestamp(doc_path.stat().st_mtime, tz=UTC)
        rel_path = doc_path.relative_to(project_root)
    except (OSError, ValueError):
        return []
    try:
        content = doc_path.read_text(encoding="utf-8", errors="ignore")[:500]
    except OSError:
        content = ""
    return [
        TimelineEntry(
            timestamp=mtime.isoformat(),
            source_type="doc_file",
            summary=_sanitize_text(f"Documentation: {rel_path}"),
            details={"path": str(rel_path), "preview": _sanitize_text(content)},
        )
    ]


def _config_file_to_timeline(config_path: Path, project_root: Path) -> list[TimelineEntry]:
    """Convert a config file into a timeline entry using its mtime."""
    try:
        mtime = datetime.fromtimestamp(config_path.stat().st_mtime, tz=UTC)
        rel_path = config_path.relative_to(project_root)
    except (OSError, ValueError):
        return []
    try:
        content = config_path.read_text(encoding="utf-8", errors="ignore")[:300]
    except OSError:
        content = ""
    return [
        TimelineEntry(
            timestamp=mtime.isoformat(),
            source_type="config_file",
            summary=_sanitize_text(f"Config: {rel_path}"),
            details={"path": str(rel_path), "preview": _sanitize_text(content)},
        )
    ]


# ---------------------------------------------------------------------------
# Stage 3: Extract Understanding
# ---------------------------------------------------------------------------


def extract_understanding(
    timeline: list[TimelineEntry],
    project_name: str,
) -> ExtractedUnderstanding:
    """Walk timeline and extract structured understanding.

    Tries LLM extraction first, falls back to heuristic.
    """
    result = _extract_with_llm(timeline, project_name)
    if result is not None:
        return result
    return _heuristic_understanding(timeline, project_name)


def _extract_with_llm(
    timeline: list[TimelineEntry],
    project_name: str,
) -> ExtractedUnderstanding | None:
    """Use Haiku CLI to extract understanding from the timeline."""
    if shutil.which("claude") is None:
        return None

    # Build a compact representation of the timeline
    compact_entries: list[dict[str, str]] = []
    for entry in timeline[:500]:  # Limit to avoid huge prompts
        compact_entries.append({
            "ts": entry.timestamp,
            "type": entry.source_type,
            "summary": entry.summary[:200],
        })

    prompt_lines = [
        f"Analyze this project timeline for '{project_name}' and extract understanding.",
        "Return JSON with these keys:",
        "- overview: 2-5 sentence project overview",
        "- decisions: array of key decisions with rationale",
        "- architecture: array of architecture observations",
        "- history: array of chronological milestones",
        "- conventions: array of coding patterns and conventions",
        "- goals: array of stated or implied goals",
        "- open_questions: array of things that could not be determined",
        "Never include secrets, tokens, or credentials.",
        json.dumps(compact_entries),
    ]

    result = subprocess.run(
        ["claude", "-p", "\n".join(prompt_lines), "--model", HAIKU_MODEL],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        payload = json.loads(result.stdout)
    except Exception:  # noqa: BLE001
        return None

    if not isinstance(payload, dict):
        return None

    return ExtractedUnderstanding(
        project_name=project_name,
        overview=_sanitize_text(str(payload.get("overview", ""))),
        decisions=_sanitize_items(payload.get("decisions")),
        architecture=_sanitize_items(payload.get("architecture")),
        history=_sanitize_items(payload.get("history")),
        conventions=_sanitize_items(payload.get("conventions")),
        goals=_sanitize_items(payload.get("goals")),
        open_questions=_sanitize_items(payload.get("open_questions")),
    )


def _heuristic_understanding(
    timeline: list[TimelineEntry],
    project_name: str,
) -> ExtractedUnderstanding:
    """Extract understanding heuristically from the timeline."""
    understanding = ExtractedUnderstanding(project_name=project_name)

    git_commits: list[TimelineEntry] = []
    jsonl_events: list[TimelineEntry] = []
    doc_entries: list[TimelineEntry] = []
    config_entries: list[TimelineEntry] = []

    for entry in timeline:
        if entry.source_type == "git_commit":
            git_commits.append(entry)
        elif entry.source_type == "jsonl_event":
            jsonl_events.append(entry)
        elif entry.source_type == "doc_file":
            doc_entries.append(entry)
        elif entry.source_type == "config_file":
            config_entries.append(entry)

    # Overview from doc files and git history
    overview_parts: list[str] = []
    if doc_entries:
        overview_parts.append(f"Project has {len(doc_entries)} documentation file(s).")
    if git_commits:
        overview_parts.append(f"Git history contains {len(git_commits)} commit(s).")
    if jsonl_events:
        overview_parts.append(f"Found {len(jsonl_events)} transcript event(s).")
    if config_entries:
        overview_parts.append(f"Found {len(config_entries)} configuration file(s).")
    understanding.overview = " ".join(overview_parts) or f"{project_name} project."

    # History from git commits
    for commit in git_commits:
        understanding.history.append(f"[{commit.details.get('hash', '?')}] {commit.summary}")

    # Decisions, conventions, architecture from JSONL text
    for event in jsonl_events:
        text = event.summary.lower()
        if any(kw in text for kw in ("decided", "decision", "we will", "choose", "selected")):
            understanding.decisions.append(event.summary)
        if any(kw in text for kw in ("architecture", "refactor", "pipeline", "schema", "design")):
            understanding.architecture.append(event.summary)
        if any(kw in text for kw in ("convention", "prefer", "always", "naming", "standard", "pattern")):
            understanding.conventions.append(event.summary)
        if any(kw in text for kw in ("goal", "priority", "focus", "scope", "roadmap")):
            understanding.goals.append(event.summary)

    # Architecture from config files
    for config_entry in config_entries:
        path = config_entry.details.get("path", "")
        understanding.architecture.append(f"Config: {path}")

    # Deduplicate
    understanding.decisions = _dedupe(understanding.decisions)
    understanding.architecture = _dedupe(understanding.architecture)
    understanding.history = _dedupe(understanding.history)
    understanding.conventions = _dedupe(understanding.conventions)
    understanding.goals = _dedupe(understanding.goals)

    return understanding


# ---------------------------------------------------------------------------
# Stage 4: Generate Documentation
# ---------------------------------------------------------------------------


def generate_docs(
    project_root: Path,
    understanding: ExtractedUnderstanding,
    *,
    timestamp: str | None = None,
) -> int:
    """Write extracted understanding into docs/ files. Returns count of docs generated."""
    docs_dir = project_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    ts = timestamp or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    generated = 0

    # project-overview.md
    overview_content = _render_overview(understanding, ts)
    (docs_dir / "project-overview.md").write_text(overview_content)
    generated += 1

    # decisions.md
    decisions_content = _render_list_doc(
        "Decisions",
        "Key decisions made during the project, with rationale and context.",
        understanding.decisions,
        ts,
    )
    (docs_dir / "decisions.md").write_text(decisions_content)
    generated += 1

    # architecture.md
    arch_content = _render_list_doc(
        "Architecture",
        "System design, components, boundaries, data flow, and dependencies.",
        understanding.architecture,
        ts,
    )
    (docs_dir / "architecture.md").write_text(arch_content)
    generated += 1

    # history.md
    history_content = _render_list_doc(
        "History",
        "Chronological narrative of how the project evolved.",
        understanding.history,
        ts,
    )
    (docs_dir / "history.md").write_text(history_content)
    generated += 1

    # conventions.md
    conventions_content = _render_list_doc(
        "Conventions",
        "Coding patterns, naming conventions, testing approaches, and tooling preferences.",
        understanding.conventions,
        ts,
    )
    (docs_dir / "conventions.md").write_text(conventions_content)
    generated += 1

    return generated


def _render_overview(understanding: ExtractedUnderstanding, timestamp: str) -> str:
    """Render project-overview.md."""
    lines = [
        f"# {understanding.project_name or 'Project'} Overview",
        "",
        "## Summary",
        "",
        _sanitize_text(understanding.overview) or "Project overview pending.",
        "",
    ]

    if understanding.goals:
        lines.append("## Goals")
        lines.append("")
        for item in understanding.goals:
            lines.append(f"- {_sanitize_text(item)}")
        lines.append("")

    if understanding.architecture:
        lines.append("## Architecture")
        lines.append("")
        lines.append("See [architecture.md](architecture.md) for details.")
        lines.append("")

    if understanding.conventions:
        lines.append("## Conventions")
        lines.append("")
        lines.append("See [conventions.md](conventions.md) for details.")
        lines.append("")

    if understanding.decisions:
        lines.append("## Key Decisions")
        lines.append("")
        lines.append("See [decisions.md](decisions.md) for the full record.")
        lines.append("")

    lines.append(f"*Last updated: {timestamp}*")
    lines.append("")
    return "\n".join(lines)


def _render_list_doc(
    title: str,
    summary: str,
    items: list[str],
    timestamp: str,
) -> str:
    """Render a doc with a summary and bullet list."""
    lines = [
        f"# {title}",
        "",
        "## Summary",
        "",
        summary,
        "",
        f"## {title}",
        "",
    ]
    if items:
        for item in items:
            lines.append(f"- {_sanitize_text(item)}")
    else:
        lines.append("- None recorded yet.")
    lines.append("")
    lines.append(f"*Last updated: {timestamp}*")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage 5: User Interview
# ---------------------------------------------------------------------------


def build_interview_questions(understanding: ExtractedUnderstanding) -> list[str]:
    """Build questions for the user interview stage."""
    questions: list[str] = []

    questions.append(
        f"Here is the generated project overview:\n\n{understanding.overview}\n\n"
        "Is this accurate? Any corrections or additions?"
    )

    if understanding.open_questions:
        for question in understanding.open_questions:
            questions.append(question)

    if not understanding.decisions:
        questions.append(
            "No key decisions were found in the project history. "
            "Can you describe any important decisions that shaped this project?"
        )

    if not understanding.conventions:
        questions.append(
            "No coding conventions were detected. "
            "Are there specific patterns or conventions this project follows?"
        )

    if not understanding.goals:
        questions.append(
            "No explicit goals were found. "
            "What are the main goals or priorities for this project?"
        )

    return questions


# ---------------------------------------------------------------------------
# Copy provider transcripts
# ---------------------------------------------------------------------------


def copy_provider_transcripts(project_root: Path, sources: DiscoveredSources) -> int:
    """Copy pre-PollyPM provider transcripts into .pollypm/transcripts/."""
    transcripts_dir = project_transcripts_dir(project_root)
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    copied = 0

    for provider_dir in sources.provider_transcript_dirs:
        provider_name = provider_dir.name.lstrip(".")
        for jsonl_file in sorted(provider_dir.rglob("*.jsonl")):
            try:
                rel = jsonl_file.relative_to(provider_dir)
            except ValueError:
                continue
            dest = transcripts_dir / f"imported-{provider_name}" / rel
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(jsonl_file, dest)
            copied += 1

    return copied


# ---------------------------------------------------------------------------
# Import state (checkpoint)
# ---------------------------------------------------------------------------


def _import_state_path(project_root: Path) -> Path:
    return project_root / ".pollypm" / "history-import" / "state.json"


def load_import_state(project_root: Path) -> dict[str, Any]:
    """Load the import state checkpoint."""
    path = _import_state_path(project_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_import_state(project_root: Path, state: dict[str, Any]) -> None:
    """Save the import state checkpoint."""
    path = _import_state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


def import_project_history(
    project_root: Path,
    project_name: str,
    *,
    skip_interview: bool = False,
    timestamp: str | None = None,
) -> ImportResult:
    """Run the full five-stage import pipeline.

    Args:
        project_root: Path to the project root directory.
        project_name: Human-readable project name.
        skip_interview: If True, skip the user interview stage (for testing).
        timestamp: Override timestamp for generated docs.

    Returns:
        ImportResult with statistics about the import.
    """
    result = ImportResult()

    # Stage 1: Discover
    sources = discover_sources(project_root)
    result.sources_found = sources.total_sources()

    # Copy provider transcripts into canonical location
    result.provider_transcripts_copied = copy_provider_transcripts(project_root, sources)

    # Re-discover after copy so timeline includes copied transcripts
    if result.provider_transcripts_copied > 0:
        sources = discover_sources(project_root)

    # Stage 2: Build timeline
    timeline = build_timeline(project_root, sources)
    result.timeline_events = len(timeline)

    if not timeline:
        save_import_state(project_root, {
            "status": "completed",
            "sources_found": result.sources_found,
            "timeline_events": 0,
            "docs_generated": 0,
        })
        return result

    # Stage 3: Extract understanding
    understanding = extract_understanding(timeline, project_name)

    # Stage 5: User interview (build questions; actual interview is external)
    result.interview_questions = build_interview_questions(understanding)

    # Stage 4: Generate docs
    result.docs_generated = generate_docs(
        project_root, understanding, timestamp=timestamp,
    )

    # Stage 6: Lock (mark as complete)
    result.locked = skip_interview  # Only auto-lock if interview is skipped
    save_import_state(project_root, {
        "status": "locked" if result.locked else "pending_review",
        "sources_found": result.sources_found,
        "timeline_events": result.timeline_events,
        "docs_generated": result.docs_generated,
        "provider_transcripts_copied": result.provider_transcripts_copied,
    })

    return result


def lock_import(project_root: Path) -> None:
    """Lock the import after user interview confirmation."""
    state = load_import_state(project_root)
    state["status"] = "locked"
    save_import_state(project_root, state)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return _dedupe([_sanitize_text(str(item)) for item in value if str(item).strip()])


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered
