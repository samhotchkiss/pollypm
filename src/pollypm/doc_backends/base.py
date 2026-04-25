"""Documentation backend interface definition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(slots=True)
class DocEntry:
    """A single documentation entry."""

    name: str  # e.g. "project-overview", "decisions"
    title: str
    content: str
    path: Path | None = None
    last_updated: str = ""
    summary: str = ""


class DocBackend(Protocol):
    """Interface for documentation storage backends.

    Methods:
        write_document: Write or overwrite a document by name.
        read_document: Read a document by name.
        read_summary: Read just the summary section of a document.
        list_documents: List all documents.
        append_entry: Append a new entry to an existing document.
        get_injection_context: Get project-overview content for prompt assembly.
    """

    def write_document(
        self,
        *,
        name: str,
        title: str,
        content: str,
        last_updated: str | None = None,
    ) -> DocEntry: ...

    def read_document(self, name: str) -> DocEntry | None: ...

    def read_summary(self, name: str) -> str: ...

    def list_documents(self) -> list[DocEntry]: ...

    def append_entry(
        self,
        *,
        name: str,
        heading: str,
        items: list[str],
    ) -> DocEntry | None: ...

    def get_injection_context(self) -> str: ...
