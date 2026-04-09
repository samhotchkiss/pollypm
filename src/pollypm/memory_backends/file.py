from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from pollypm.plugin_host import extension_host_for_root
from pollypm.projects import ensure_project_scaffold, project_artifacts_dir, project_dossier_dir
from pollypm.storage.state import StateStore

from pollypm.memory_backends.base import MemoryBackend, MemoryEntry, MemorySummary


class FileMemoryBackend(MemoryBackend):
    def __init__(self, project_path: Path, *, state_db: Path | None = None) -> None:
        self.project_path = project_path.expanduser().resolve()
        self.state_db = state_db or (self.project_path / ".pollypm" / "state.db")
        self.store = StateStore(self.state_db)
        self.plugins = extension_host_for_root(str(self.project_path))

    def root(self) -> Path:
        return self.project_path

    def exists(self) -> bool:
        return project_dossier_dir(self.project_path).exists()

    def ensure_memory(self) -> Path:
        ensure_project_scaffold(self.project_path)
        memory_root = project_dossier_dir(self.project_path) / "memory"
        memory_root.mkdir(parents=True, exist_ok=True)
        (project_artifacts_dir(self.project_path) / "memory").mkdir(parents=True, exist_ok=True)
        return memory_root

    def write_entry(
        self,
        *,
        scope: str,
        title: str,
        body: str,
        kind: str = "note",
        tags: list[str] | None = None,
        source: str = "manual",
    ) -> MemoryEntry:
        payload = {
            "scope": scope,
            "title": title,
            "body": body,
            "kind": kind,
            "tags": list(tags or []),
            "source": source,
        }
        result = self.plugins.run_filters("memory.before_write", payload, metadata={"scope": scope, "kind": kind})
        if result.action == "deny":
            raise PermissionError(result.reason or "Memory write denied by plugin")
        payload = result.payload if isinstance(result.payload, dict) else payload

        scope = str(payload.get("scope", scope))
        title = str(payload.get("title", title))
        body = str(payload.get("body", body))
        kind = str(payload.get("kind", kind))
        tags = [str(tag) for tag in payload.get("tags", tags or [])]
        source = str(payload.get("source", source))

        self.ensure_memory()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        slug = _slugify(title)
        scope_dir = project_dossier_dir(self.project_path) / "memory" / scope
        scope_dir.mkdir(parents=True, exist_ok=True)
        file_path = scope_dir / f"{stamp}-{slug}.md"
        summary_path = file_path
        content = _render_entry(scope=scope, title=title, body=body, kind=kind, tags=tags, source=source)
        file_path.write_text(content)

        record = self.store.record_memory_entry(
            scope=scope,
            kind=kind,
            title=title,
            body=body,
            tags=tags,
            source=source,
            file_path=str(file_path),
            summary_path=str(summary_path),
        )
        entry = MemoryEntry(
            entry_id=record.entry_id,
            scope=record.scope,
            kind=record.kind,
            title=record.title,
            body=record.body,
            tags=record.tags,
            source=record.source,
            file_path=Path(record.file_path),
            summary_path=Path(record.summary_path),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        self.plugins.run_observers("memory.after_write", entry, metadata={"scope": scope, "kind": kind})
        return entry

    def list_entries(
        self,
        *,
        scope: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[MemoryEntry]:
        entries = self.store.list_memory_entries(scope=scope, kind=kind, limit=limit)
        return [
            MemoryEntry(
                entry_id=item.entry_id,
                scope=item.scope,
                kind=item.kind,
                title=item.title,
                body=item.body,
                tags=item.tags,
                source=item.source,
                file_path=Path(item.file_path),
                summary_path=Path(item.summary_path),
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in entries
        ]

    def read_entry(self, entry_id: int) -> MemoryEntry | None:
        entry = self.store.get_memory_entry(entry_id)
        if entry is None:
            return None
        memory_entry = MemoryEntry(
            entry_id=entry.entry_id,
            scope=entry.scope,
            kind=entry.kind,
            title=entry.title,
            body=entry.body,
            tags=entry.tags,
            source=entry.source,
            file_path=Path(entry.file_path),
            summary_path=Path(entry.summary_path),
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        )
        self.plugins.run_observers("memory.after_read", memory_entry, metadata={"scope": entry.scope, "kind": entry.kind})
        return memory_entry

    def summarize(self, scope: str, *, limit: int = 20) -> str:
        entries = self.list_entries(scope=scope, limit=limit)
        summary = _summarize_entries(scope, entries)
        self.plugins.run_observers("memory.after_summarize", summary, metadata={"scope": scope, "entry_count": len(entries)})
        return summary

    def compact(self, scope: str, *, limit: int = 50) -> MemorySummary:
        entries = self.list_entries(scope=scope, limit=limit)
        summary_text = _summarize_entries(scope, entries)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        summary_dir = project_artifacts_dir(self.project_path) / "memory" / scope
        summary_dir.mkdir(parents=True, exist_ok=True)
        summary_path = summary_dir / f"{stamp}.md"
        summary_path.write_text(summary_text)
        record = self.store.record_memory_summary(
            scope=scope,
            summary_text=summary_text,
            summary_path=str(summary_path),
            entry_count=len(entries),
        )
        summary = MemorySummary(
            summary_id=record.summary_id,
            scope=record.scope,
            summary_text=record.summary_text,
            summary_path=Path(record.summary_path),
            entry_count=record.entry_count,
            created_at=record.created_at,
        )
        self.plugins.run_observers("memory.after_compact", summary, metadata={"scope": scope, "entry_count": len(entries)})
        return summary


def _render_entry(*, scope: str, title: str, body: str, kind: str, tags: list[str], source: str) -> str:
    tag_line = ", ".join(tags) if tags else "none"
    return "\n".join(
        [
            f"# {title}",
            "",
            f"- Scope: `{scope}`",
            f"- Kind: `{kind}`",
            f"- Source: `{source}`",
            f"- Tags: {tag_line}",
            "",
            body.rstrip(),
            "",
        ]
    )


def _summarize_entries(scope: str, entries: list[MemoryEntry]) -> str:
    lines = [
        f"# Memory Summary: {scope}",
        "",
        f"- Entries: {len(entries)}",
        "",
    ]
    if not entries:
        lines.append("No memory entries recorded yet.")
        lines.append("")
        return "\n".join(lines)

    lines.append("## Recent Entries")
    lines.append("")
    for entry in entries[:10]:
        snippet = " ".join(entry.body.split())[:140]
        lines.append(f"- {entry.title}: {snippet}")
    lines.append("")
    return "\n".join(lines)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "memory"
