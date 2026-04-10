"""Default markdown documentation backend.

Writes markdown files to <project>/docs/ and reads them back
for injection into agent prompts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pollypm.doc_backends.base import DocBackend, DocEntry


class MarkdownDocBackend:
    """Default documentation backend: markdown files in docs/."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root
        self._docs_dir = project_root / "docs"

    @property
    def docs_dir(self) -> Path:
        return self._docs_dir

    def write_document(
        self,
        *,
        name: str,
        title: str,
        content: str,
        last_updated: str | None = None,
    ) -> DocEntry:
        """Write or overwrite a document."""
        self._docs_dir.mkdir(parents=True, exist_ok=True)
        ts = last_updated or _utc_now()
        path = self._docs_dir / f"{name}.md"

        # Add timestamp footer if not already present
        if f"*Last updated:" not in content:
            full_content = content.rstrip() + f"\n\n*Last updated: {ts}*\n"
        else:
            full_content = content

        path.write_text(full_content)

        summary = _extract_summary(full_content)
        return DocEntry(
            name=name,
            title=title,
            content=full_content,
            path=path,
            last_updated=ts,
            summary=summary,
        )

    def read_document(self, name: str) -> DocEntry | None:
        """Read a document by name."""
        path = self._docs_dir / f"{name}.md"
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8")
        title = _extract_title(content)
        summary = _extract_summary(content)
        last_updated = _extract_last_updated(content)
        return DocEntry(
            name=name,
            title=title,
            content=content,
            path=path,
            last_updated=last_updated,
            summary=summary,
        )

    def read_summary(self, name: str) -> str:
        """Read just the summary section of a document."""
        doc = self.read_document(name)
        if doc is None:
            return ""
        return doc.summary

    def list_documents(self) -> list[DocEntry]:
        """List all documents in docs/."""
        if not self._docs_dir.exists():
            return []
        entries: list[DocEntry] = []
        for path in sorted(self._docs_dir.glob("*.md")):
            name = path.stem
            content = path.read_text(encoding="utf-8")
            entries.append(DocEntry(
                name=name,
                title=_extract_title(content),
                content=content,
                path=path,
                last_updated=_extract_last_updated(content),
                summary=_extract_summary(content),
            ))
        return entries

    def append_entry(
        self,
        *,
        name: str,
        heading: str,
        items: list[str],
    ) -> DocEntry | None:
        """Append items under a heading in an existing document."""
        doc = self.read_document(name)
        if doc is None:
            return None

        content = doc.content
        # Find the heading and append items
        heading_line = f"## {heading}" if not heading.startswith("#") else heading
        if heading_line in content:
            # Insert items after the heading
            parts = content.split(heading_line, 1)
            existing_section = parts[1].split("\n## ", 1)
            section_content = existing_section[0]
            new_items = "\n".join(f"- {item}" for item in items)
            updated_section = section_content.rstrip() + "\n" + new_items + "\n"
            if len(existing_section) > 1:
                new_content = parts[0] + heading_line + updated_section + "\n## " + existing_section[1]
            else:
                new_content = parts[0] + heading_line + updated_section
        else:
            # Add new heading at the end
            new_items = "\n".join(f"- {item}" for item in items)
            new_content = content.rstrip() + f"\n\n{heading_line}\n\n{new_items}\n"

        return self.write_document(
            name=name,
            title=doc.title,
            content=new_content,
        )

    def get_injection_context(self) -> str:
        """Get project-overview content for prompt assembly."""
        doc = self.read_document("project-overview")
        if doc is None:
            return ""
        return doc.content[:4000]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _extract_title(content: str) -> str:
    """Extract the H1 title from markdown content."""
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _extract_summary(content: str) -> str:
    """Extract the Summary section content."""
    in_summary = False
    lines: list[str] = []
    for line in content.splitlines():
        if line.strip() == "## Summary":
            in_summary = True
            continue
        if in_summary:
            if line.startswith("## ") or line.startswith("*Last updated:"):
                break
            lines.append(line)
    return "\n".join(lines).strip()


def _extract_last_updated(content: str) -> str:
    """Extract the last updated timestamp."""
    for line in content.splitlines():
        if line.startswith("*Last updated:") and line.endswith("*"):
            return line[len("*Last updated:"):].rstrip("*").strip()
    return ""
