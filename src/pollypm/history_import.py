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
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pollypm.knowledge_extract import _sanitize_text
from pollypm.llm_runner import run_haiku_json
from pollypm.projects import project_transcripts_dir

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
class DeprecatedFact:
    """A fact that was believed and later superseded."""

    category: str  # e.g. "overview", "architecture", "conventions"
    superseded_at_chunk: int  # which chunk caused the replacement
    old_value: str
    new_value: str


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
    deprecated_facts: list[DeprecatedFact] = field(default_factory=list)


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

    # Filter out heartbeat/monitoring noise before extraction
    _NOISE_PATTERNS = [
        "heartbeat sweep completed",
        "token_ledger",
        "Standing by",
        "You appear stalled",
        "You appear idle",
        "Supervision check",
        "nudge",
        "[token_usage]",
    ]
    filtered: list[TimelineEntry] = []
    for entry in timeline:
        summary_lower = entry.summary.lower()
        if any(noise.lower() in summary_lower for noise in _NOISE_PATTERNS):
            continue
        filtered.append(entry)

    # Sort chronologically
    filtered.sort(key=lambda entry: entry.sort_key())

    # Bound the timeline
    if len(filtered) > MAX_TIMELINE_EVENTS:
        filtered = filtered[-MAX_TIMELINE_EVENTS:]

    return filtered


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
    *,
    chunk_size: int = 50,
) -> ExtractedUnderstanding | None:
    """Walk the timeline chronologically, oldest to newest, in chunks.

    Each chunk updates the accumulated understanding. Newer information
    supersedes older — if the project was renamed, the new name wins.
    """
    import logging
    logger = logging.getLogger(__name__)

    # Sort oldest first (should already be, but ensure)
    sorted_timeline = sorted(timeline, key=lambda e: e.timestamp)

    # Build compact entries
    compact_entries: list[dict[str, str]] = []
    for entry in sorted_timeline:
        compact_entries.append({
            "ts": entry.timestamp,
            "type": entry.source_type,
            "summary": entry.summary[:200],
        })

    # Break into chunks
    chunks = [compact_entries[i:i + chunk_size] for i in range(0, len(compact_entries), chunk_size)]
    if not chunks:
        return None

    # Walk chronologically, accumulating understanding
    accumulated: dict[str, Any] = {
        "overview": "",
        "decisions": [],
        "architecture": [],
        "history": [],
        "conventions": [],
        "goals": [],
        "open_questions": [],
    }
    deprecated_facts: list[DeprecatedFact] = []

    for i, chunk in enumerate(chunks):
        is_first = i == 0
        is_last = i == len(chunks) - 1

        if is_first:
            prompt = "\n".join([
                f"You are analyzing the history of project '{project_name}', reading events chronologically.",
                f"This is chunk {i+1} of {len(chunks)} (the earliest events).",
                "Extract your initial understanding.",
                "Return ONLY valid JSON (no markdown fences) with keys:",
                "  overview, decisions, architecture, history, conventions, goals, open_questions",
                "Each value must be an array of strings, except overview which is a string.",
                "Be specific and concrete. Never include secrets or tokens.",
                "",
                json.dumps(chunk),
            ])
        else:
            prompt = "\n".join([
                f"You are analyzing the history of project '{project_name}', reading events chronologically.",
                f"This is chunk {i+1} of {len(chunks)}." + (" This is the FINAL chunk — your output is the definitive understanding." if is_last else ""),
                "",
                "Here is your current accumulated understanding:",
                json.dumps(accumulated),
                "",
                "Here are the next chronological events. UPDATE your understanding:",
                "- If the project was renamed, use the NEW name",
                "- If architecture changed, describe the CURRENT architecture, not the old one",
                "- If conventions changed, describe the CURRENT conventions",
                "- Add new decisions, milestones, and goals as discovered",
                "- Remove or update anything that was superseded by newer events",
                "- The overview should describe the project AS IT IS NOW",
                "",
                "Return ONLY valid JSON (no markdown fences) with the same keys.",
                "Never include secrets or tokens.",
                "",
                json.dumps(chunk),
            ])

        payload = run_haiku_json(prompt)
        if payload is None:
            logger.warning("Chunk %d/%d failed, continuing with accumulated state", i+1, len(chunks))
            continue

        # Update accumulated state — newer chunks override
        # Track what was superseded
        if isinstance(payload.get("overview"), str) and payload["overview"]:
            old_overview = accumulated["overview"]
            if old_overview and old_overview != payload["overview"]:
                deprecated_facts.append(DeprecatedFact(
                    category="overview",
                    superseded_at_chunk=i + 1,
                    old_value=old_overview,
                    new_value=payload["overview"],
                ))
            accumulated["overview"] = payload["overview"]
        for key in ("decisions", "architecture", "history", "conventions", "goals", "open_questions"):
            val = payload.get(key)
            if isinstance(val, list) and val:
                old_val = accumulated[key]
                # Detect items that were dropped (superseded)
                if old_val:
                    old_set = set(str(item) for item in old_val)
                    new_set = set(str(item) for item in val)
                    dropped = old_set - new_set
                    for item in dropped:
                        deprecated_facts.append(DeprecatedFact(
                            category=key,
                            superseded_at_chunk=i + 1,
                            old_value=item,
                            new_value="(removed or replaced in later events)",
                        ))
                accumulated[key] = val  # Replace with latest understanding

        event_word = "event" if len(chunk) == 1 else "events"
        logger.info(
            "Chunk %d/%d processed (%d %s)",
            i + 1, len(chunks), len(chunk), event_word,
        )

    # Check we got something meaningful
    if not accumulated["overview"]:
        return None

    return ExtractedUnderstanding(
        project_name=project_name,
        overview=_sanitize_text(accumulated["overview"]),
        decisions=_sanitize_items(accumulated.get("decisions")),
        architecture=_sanitize_items(accumulated.get("architecture")),
        history=_sanitize_items(accumulated.get("history")),
        conventions=_sanitize_items(accumulated.get("conventions")),
        goals=_sanitize_items(accumulated.get("goals")),
        open_questions=_sanitize_items(accumulated.get("open_questions")),
        deprecated_facts=deprecated_facts,
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
    def _plural(n: int, sing: str, plur: str | None = None) -> str:
        return f"{n} {sing if n == 1 else (plur or sing + 's')}"

    overview_parts: list[str] = []
    if doc_entries:
        overview_parts.append(
            f"Project has {_plural(len(doc_entries), 'documentation file')}."
        )
    if git_commits:
        overview_parts.append(
            f"Git history contains {_plural(len(git_commits), 'commit')}."
        )
    if jsonl_events:
        overview_parts.append(
            f"Found {_plural(len(jsonl_events), 'transcript event')}."
        )
    if config_entries:
        overview_parts.append(
            f"Found {_plural(len(config_entries), 'configuration file')}."
        )
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
    """Write extracted understanding into docs/ files via the doc backend.

    Uses the pluggable doc backend so docs can be stored in markdown,
    a wiki, or any other supported format. Only overwrites PollyPM-managed
    docs (identified by the *Last updated:* marker). User-created docs
    in docs/ are left untouched.
    """
    from pollypm.doc_backends import get_doc_backend

    backend = get_doc_backend(project_root)
    ts = timestamp or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    generated = 0

    docs_to_write = [
        ("project-overview", "Project Overview", _render_overview(understanding, ts)),
        ("decisions", "Decisions", _render_list_doc(
            "Decisions",
            "Key decisions made during the project, with rationale and context.",
            understanding.decisions, ts,
        )),
        ("architecture", "Architecture", _render_list_doc(
            "Architecture",
            "System design, components, boundaries, data flow, and dependencies.",
            understanding.architecture, ts,
        )),
        ("history", "History", _render_list_doc(
            "History",
            "Chronological narrative of how the project evolved.",
            understanding.history, ts,
        )),
        ("conventions", "Conventions", _render_list_doc(
            "Conventions",
            "Coding patterns, naming conventions, testing approaches, and tooling preferences.",
            understanding.conventions, ts,
        )),
    ]
    if understanding.deprecated_facts:
        # Cap deprecated facts to avoid enormous files (otter_camp generated 320KB)
        capped = understanding.deprecated_facts[:100]
        docs_to_write.append(
            ("deprecated-facts", "Deprecated Facts", _render_deprecated_facts(capped, ts))
        )

    for name, title, content in docs_to_write:
        # Check if an existing doc is user-created (no PollyPM marker)
        existing = backend.read_document(name)
        if existing is not None and "*Last updated:" not in existing.content:
            # User-created doc — don't overwrite, write to a separate file
            backend.write_document(name=f"{name}-pollypm", title=title, content=content, last_updated=ts)
        else:
            backend.write_document(name=name, title=title, content=content, last_updated=ts)
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


def _render_deprecated_facts(facts: list[DeprecatedFact], timestamp: str) -> str:
    """Render deprecated-facts.md — a log of beliefs that were superseded."""
    lines = [
        "# Deprecated Facts",
        "",
        "## Summary",
        "",
        "Facts that were believed at earlier points in the project timeline",
        "but were later superseded by newer information. This log exists so",
        "that future agents and humans can understand what changed and why.",
        "",
        "## Deprecated Facts",
        "",
    ]
    for fact in facts:
        lines.append(f"### {fact.category} (superseded at chunk {fact.superseded_at_chunk})")
        lines.append("")
        lines.append(f"**Was:** {_sanitize_text(fact.old_value)}")
        lines.append("")
        lines.append(f"**Became:** {_sanitize_text(fact.new_value)}")
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
        parsed = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return {}
    # Defend against a checkpoint corrupted to a non-dict shape (list,
    # null, string). The function's annotation promises ``dict[str, Any]``
    # and ``lock_import`` later does ``state["status"] = "locked"`` —
    # that assignment would raise ``TypeError`` if the parse returned
    # a list, silently breaking the lock-import flow.
    return parsed if isinstance(parsed, dict) else {}


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

    # Stage 5b: Persist interview questions alongside the generated docs
    # for async review. The legacy inbox has been retired — the questions
    # live on disk next to the generated docs so the user can read them
    # without a running session.
    if result.interview_questions and not skip_interview:
        try:
            questions_path = project_root / "docs" / "history-import-questions.md"
            questions_path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                f"# Review: {project_name} history import",
                "",
                (
                    f"The history import generated {result.docs_generated} docs. "
                    f"Review them in `{project_root}/docs/` and answer the questions below."
                ),
                "",
            ]
            for i, question in enumerate(result.interview_questions, 1):
                lines.append(f"{i}. {question}")
            lines.append("")
            lines.append("Run `pm import --lock` once you're satisfied.")
            questions_path.write_text("\n".join(lines) + "\n")
        except Exception:  # noqa: BLE001
            pass  # history-import review is best-effort

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
