from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(slots=True)
class MemoryEntry:
    entry_id: int
    scope: str
    kind: str
    title: str
    body: str
    tags: tuple[str, ...]
    source: str
    file_path: Path
    summary_path: Path
    created_at: str
    updated_at: str


@dataclass(slots=True)
class MemorySummary:
    summary_id: int
    scope: str
    summary_text: str
    summary_path: Path
    entry_count: int
    created_at: str


class MemoryBackend(Protocol):
    def root(self) -> Path: ...

    def exists(self) -> bool: ...

    def ensure_memory(self) -> Path: ...

    def write_entry(
        self,
        *,
        scope: str,
        title: str,
        body: str,
        kind: str = "note",
        tags: list[str] | None = None,
        source: str = "manual",
    ) -> MemoryEntry: ...

    def list_entries(
        self,
        *,
        scope: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[MemoryEntry]: ...

    def read_entry(self, entry_id: int) -> MemoryEntry | None: ...

    def summarize(self, scope: str, *, limit: int = 20) -> str: ...

    def compact(self, scope: str, *, limit: int = 50) -> MemorySummary: ...
