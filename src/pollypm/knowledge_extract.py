from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
import json
import re
from typing import Any

from pollypm.llm_runner import HAIKU_MODEL, run_haiku_json
from pollypm.memory_backends import get_memory_backend

EXTRACTION_INTERVAL_SECONDS = 15 * 60
SUMMARY_HEADER = "## Summary"
SECTION_ORDER = (
    "## Goals",
    "## Architecture Changes",
    "## Convention Shifts",
    "## Decisions",
    "## Risks",
    "## Ideas",
)


@dataclass(slots=True)
class KnowledgeDelta:
    goals: list[str] = field(default_factory=list)
    architecture_changes: list[str] = field(default_factory=list)
    convention_shifts: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    ideas: list[str] = field(default_factory=list)

    def extend(self, other: "KnowledgeDelta") -> None:
        self.goals.extend(other.goals)
        self.architecture_changes.extend(other.architecture_changes)
        self.convention_shifts.extend(other.convention_shifts)
        self.decisions.extend(other.decisions)
        self.risks.extend(other.risks)
        self.ideas.extend(other.ideas)

    def is_empty(self) -> bool:
        return not any(
            (
                self.goals,
                self.architecture_changes,
                self.convention_shifts,
                self.decisions,
                self.risks,
                self.ideas,
            )
        )


def extract_knowledge_once(config) -> dict[str, int]:
    updated_docs = 0
    processed_events = 0
    memory_entries = 0
    for project_root in _all_project_roots(config):
        events, checkpoint = _read_new_events(project_root)
        if not events:
            continue
        processed_events += len(events)
        delta = _extract_with_haiku_or_fallback(events)
        updated_docs += _apply_docs_delta(project_root, delta)
        memory_entries += _store_memory_entries(config, project_root, delta)
        _save_checkpoint(project_root, checkpoint)
    return {"processed_events": processed_events, "updated_docs": updated_docs, "memory_entries": memory_entries}


def store_snapshot_learnings(
    config,
    *,
    project_root: Path,
    scope: str,
    snapshot_text: str,
    memory_backend_name: str = "file",
    source: str = "heartbeat",
) -> int:
    text = _sanitize_text(snapshot_text).strip()
    if not text:
        return 0
    delta = _extract_with_haiku_or_fallback(
        [
            {
                "event_type": "heartbeat_snapshot",
                "payload": {"text": text},
            }
        ]
    )
    if delta.is_empty():
        return 0
    try:
        backend = get_memory_backend(project_root, memory_backend_name)
    except Exception:  # noqa: BLE001
        return 0

    count = 0
    kind_map = {
        "decision": delta.decisions,
        "goal": delta.goals,
        "risk": delta.risks,
        "idea": delta.ideas,
        "architecture": delta.architecture_changes,
        "convention": delta.convention_shifts,
    }
    for kind, items in kind_map.items():
        if not items:
            continue
        existing_titles = {
            entry.title.strip()
            for entry in backend.list_entries(scope=scope, kind=kind, limit=500)
        }
        for item in items:
            title = item.strip()
            if not title or title in existing_titles:
                continue
            backend.write_entry(
                scope=scope,
                title=title,
                body=title,
                kind=kind,
                tags=[scope, kind, source],
                source=source,
            )
            existing_titles.add(title)
            count += 1
    if count > 0:
        try:
            backend.compact(scope)
        except Exception:  # noqa: BLE001
            pass
    return count


def _store_memory_entries(config, project_root: Path, delta: "KnowledgeDelta") -> int:
    """Store extracted knowledge as memory entries in SQLite."""
    from pollypm.storage.state import StateStore
    try:
        store = StateStore(config.project.state_db)
    except Exception:  # noqa: BLE001
        return 0
    count = 0
    project_name = project_root.name
    kind_map = {
        "decision": delta.decisions,
        "goal": delta.goals,
        "risk": delta.risks,
        "idea": delta.ideas,
        "architecture": delta.architecture_changes,
        "convention": delta.convention_shifts,
    }
    summary_paths = {
        "decision": project_root / "docs" / "decisions.md",
        "goal": project_root / "docs" / "project-overview.md",
        "risk": project_root / "docs" / "risks.md",
        "idea": project_root / "docs" / "ideas.md",
        "architecture": project_root / "docs" / "project-overview.md",
        "convention": project_root / "docs" / "project-overview.md",
    }
    for kind, items in kind_map.items():
        summary_path = summary_paths[kind]
        for item in items:
            # Check for duplicates by title+scope
            existing = store.list_memory_entries(scope=project_name, kind=kind, limit=200)
            if any(e.title == item for e in existing):
                continue
            store.record_memory_entry(
                scope=project_name,
                kind=kind,
                title=item,
                body="",
                tags=[project_name, kind],
                source="knowledge_extract",
                file_path=str(summary_path),
                summary_path=str(summary_path),
            )
            count += 1
    store.close()
    return count


def _all_project_roots(config) -> list[Path]:
    roots = OrderedDict()
    roots[config.project.root_dir.resolve()] = None
    for project in config.projects.values():
        roots[project.path.resolve()] = None
    return list(roots.keys())


def _transcript_root(project_root: Path) -> Path:
    return project_root / ".pollypm" / "transcripts"


def _checkpoint_path(project_root: Path) -> Path:
    return _transcript_root(project_root) / ".knowledge-extraction-state.json"


def _load_checkpoint(project_root: Path) -> dict[str, int]:
    path = _checkpoint_path(project_root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return {}
    files = payload.get("files", {})
    if not isinstance(files, dict):
        return {}
    return {str(key): int(value or 0) for key, value in files.items()}


def _save_checkpoint(project_root: Path, checkpoint: dict[str, int]) -> None:
    path = _checkpoint_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"files": checkpoint, "model": HAIKU_MODEL}, indent=2) + "\n")


def _read_new_events(project_root: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    root = _transcript_root(project_root)
    checkpoint = _load_checkpoint(project_root)
    events: list[dict[str, Any]] = []
    if not root.exists():
        return events, checkpoint
    for path in sorted(root.glob("*/events.jsonl")):
        key = str(path.relative_to(root))
        offset = checkpoint.get(key, 0)
        size = path.stat().st_size
        if size < offset:
            offset = 0
        with path.open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(event, dict):
                    events.append(event)
            checkpoint[key] = handle.tell()
    events.sort(key=lambda item: (str(item.get("timestamp", "")), str(item.get("source_path", "")), int(item.get("source_offset", 0) or 0)))
    return events, checkpoint


def _extract_with_haiku_or_fallback(events: list[dict[str, Any]]) -> KnowledgeDelta:
    result = _extract_with_haiku(events)
    if result is not None:
        return result
    return _heuristic_extract(events)


def _extract_with_haiku(events: list[dict[str, Any]], *, max_events: int = 200) -> KnowledgeDelta | None:
    truncated = events[:max_events]
    prompt = "\n".join([
        "Extract project knowledge from transcript events as compact JSON.",
        "Return ONLY valid JSON (no markdown fences) with keys: goals, architecture_changes, convention_shifts, decisions, risks, ideas.",
        "Each value must be an array of short bullet strings.",
        "Be specific and concrete — extract actual project details.",
        "Never include secrets or tokens.",
        json.dumps(truncated, indent=2),
    ])
    payload = run_haiku_json(prompt)
    if payload is None:
        return None
    return KnowledgeDelta(
        goals=_sanitize_items(payload.get("goals")),
        architecture_changes=_sanitize_items(payload.get("architecture_changes")),
        convention_shifts=_sanitize_items(payload.get("convention_shifts")),
        decisions=_sanitize_items(payload.get("decisions")),
        risks=_sanitize_items(payload.get("risks")),
        ideas=_sanitize_items(payload.get("ideas")),
    )


def _heuristic_extract(events: list[dict[str, Any]]) -> KnowledgeDelta:
    delta = KnowledgeDelta()
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        text = str(payload.get("text", "")).strip()
        if not text:
            continue
        for sentence in _sentences(_sanitize_text(text)):
            lowered = sentence.lower()
            if any(token in lowered for token in ("decided", "decision", "we will", "choose", "selected")):
                delta.decisions.append(sentence)
            if any(token in lowered for token in ("architecture", "refactor", "split", "pipeline", "migrate", "schema")):
                delta.architecture_changes.append(sentence)
            if any(token in lowered for token in ("convention", "prefer", "always", "naming", "standard")):
                delta.convention_shifts.append(sentence)
            if any(token in lowered for token in ("goal", "priority", "focus", "scope", "roadmap")):
                delta.goals.append(sentence)
            if any(token in lowered for token in ("risk", "blocker", "concern", "danger", "unsafe")):
                delta.risks.append(sentence)
            if any(token in lowered for token in ("idea", "maybe", "consider", "future", "could")):
                delta.ideas.append(sentence)
    delta.goals = _dedupe(delta.goals)
    delta.architecture_changes = _dedupe(delta.architecture_changes)
    delta.convention_shifts = _dedupe(delta.convention_shifts)
    delta.decisions = _dedupe(delta.decisions)
    delta.risks = _dedupe(delta.risks)
    delta.ideas = _dedupe(delta.ideas)
    return delta


def _sanitize_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value:
        text = _sanitize_text(str(item)).strip()
        if not text:
            continue
        # Reject items that are raw JSON, escaped strings, or transcript noise
        if text.startswith("{") or text.startswith("[") or text.startswith("```"):
            continue
        if "\\\\" in text or "\\n" in text:
            continue
        if '"timestamp"' in text or '"event_type"' in text or '"payload"' in text:
            continue
        if text.startswith("I'm ready to extract") or text.startswith("For example"):
            continue
        if len(text) > 500:
            text = text[:500]
        cleaned.append(text)
    return _dedupe(cleaned)


def _sanitize_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"\b(sk|ghp)_[A-Za-z0-9_\-]{12,}\b", "[redacted-secret]", cleaned)
    cleaned = re.sub(r"\b[A-Fa-f0-9]{32,}\b", "[redacted-secret]", cleaned)
    cleaned = re.sub(r"\b[A-Za-z0-9+/]{40,}={0,2}\b", "[redacted-secret]", cleaned)
    return cleaned


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.replace("\n", " ").strip())
    return [part.strip() for part in parts if part.strip()]


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


def _apply_docs_delta(project_root: Path, delta: KnowledgeDelta) -> int:
    docs_root = project_root / "docs"
    docs_root.mkdir(parents=True, exist_ok=True)
    updated = 0
    updated += int(
        _update_doc(
            docs_root / "project-overview.md",
            "Project Overview",
            {
                "## Goals": delta.goals,
                "## Architecture Changes": delta.architecture_changes,
                "## Convention Shifts": delta.convention_shifts,
            },
        )
    )
    updated += int(_update_doc(docs_root / "decisions.md", "Decisions", {"## Decisions": delta.decisions}))
    updated += int(_update_doc(docs_root / "risks.md", "Risks", {"## Risks": delta.risks}))
    updated += int(_update_doc(docs_root / "ideas.md", "Ideas", {"## Ideas": delta.ideas}))
    return updated


def _update_doc(path: Path, title: str, updates: dict[str, list[str]]) -> bool:
    existing = path.read_text() if path.exists() else f"# {title}\n"
    sections = _parse_sections(existing)
    changed = False
    for heading, items in updates.items():
        if not items:
            continue
        current_items = _parse_bullets(sections.get(heading, ""))
        merged = _dedupe(current_items + items)
        new_body = _render_bullets(merged)
        if sections.get(heading, "") != new_body:
            sections[heading] = new_body
            changed = True
    if not changed:
        return False
    sections[SUMMARY_HEADER] = _render_summary(sections)
    path.write_text(_render_doc(title, sections))
    return True


def _parse_sections(content: str) -> OrderedDict[str, str]:
    sections: OrderedDict[str, str] = OrderedDict()
    current = "__preamble__"
    bucket: list[str] = []
    for line in content.splitlines():
        if line.startswith("## "):
            sections[current] = "\n".join(bucket).rstrip()
            current = line.strip()
            bucket = []
            continue
        bucket.append(line)
    sections[current] = "\n".join(bucket).rstrip()
    ordered: OrderedDict[str, str] = OrderedDict()
    ordered["__preamble__"] = sections.get("__preamble__", "").strip()
    if SUMMARY_HEADER in sections:
        ordered[SUMMARY_HEADER] = sections[SUMMARY_HEADER]
    for heading in SECTION_ORDER:
        if heading in sections:
            ordered[heading] = sections[heading]
    for heading, body in sections.items():
        if heading in ordered or heading == "__preamble__":
            continue
        ordered[heading] = body
    return ordered


def _parse_bullets(content: str) -> list[str]:
    items: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return items


def _render_bullets(items: list[str]) -> str:
    if not items:
        return "- None yet."
    return "\n".join(f"- {item}" for item in items)


def _render_summary(sections: OrderedDict[str, str]) -> str:
    lines: list[str] = []
    for heading in SECTION_ORDER:
        items = _parse_bullets(sections.get(heading, ""))
        if not items or items == ["None yet."]:
            continue
        lines.append(f"- {heading[3:]}: {items[0]}")
    return "\n".join(lines[:3]) or "- No extracted updates yet."


def _render_doc(title: str, sections: OrderedDict[str, str]) -> str:
    lines = [f"# {title}", ""]
    preamble = sections.get("__preamble__", "").strip()
    if preamble:
        lines.append(preamble)
        lines.append("")
    if SUMMARY_HEADER not in sections:
        sections = OrderedDict([(SUMMARY_HEADER, "- No extracted updates yet."), *[(k, v) for k, v in sections.items() if k != "__preamble__"]])
    for heading, body in sections.items():
        if heading == "__preamble__":
            continue
        lines.append(heading)
        lines.append(body.strip() if body.strip() else "- None yet.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
