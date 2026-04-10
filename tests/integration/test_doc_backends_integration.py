"""Integration tests for documentation plugin backend."""

from pathlib import Path

from pollypm.doc_backends import get_doc_backend
from pollypm.doc_backends.markdown import MarkdownDocBackend


def test_full_document_lifecycle(tmp_path: Path) -> None:
    """Test write, read, list, append, and injection context."""
    backend = get_doc_backend(tmp_path, "markdown")

    # Write project overview
    overview = backend.write_document(
        name="project-overview",
        title="MyProject Overview",
        content="# MyProject Overview\n\n## Summary\n\nA CLI tool for managing projects.\n\n## Goals\n\n- Ship v1\n",
        last_updated="2026-04-10T00:00:00Z",
    )
    assert overview.path.exists()
    assert overview.summary == "A CLI tool for managing projects."

    # Write decisions
    decisions = backend.write_document(
        name="decisions",
        title="Decisions",
        content="# Decisions\n\n## Summary\n\nKey project decisions.\n\n## Decisions\n\n- Use Python with Typer\n",
        last_updated="2026-04-10T00:00:00Z",
    )

    # List documents
    docs = backend.list_documents()
    assert len(docs) == 2
    names = {d.name for d in docs}
    assert "project-overview" in names
    assert "decisions" in names

    # Read back
    read_overview = backend.read_document("project-overview")
    assert read_overview is not None
    assert "MyProject" in read_overview.title
    assert read_overview.last_updated == "2026-04-10T00:00:00Z"

    # Append to decisions
    updated = backend.append_entry(
        name="decisions",
        heading="Decisions",
        items=["Use SQLite for state", "Use markdown for docs"],
    )
    assert updated is not None
    assert "SQLite" in updated.content
    assert "markdown" in updated.content
    assert "Typer" in updated.content  # Original item preserved

    # Injection context
    context = backend.get_injection_context()
    assert "MyProject" in context
    assert "CLI tool" in context


def test_read_summary_for_different_docs(tmp_path: Path) -> None:
    """Test reading summaries from multiple docs."""
    backend = get_doc_backend(tmp_path)

    backend.write_document(
        name="architecture",
        title="Architecture",
        content="# Architecture\n\n## Summary\n\nPlugin-based system.\n\n## Components\n\n- Core\n",
    )
    backend.write_document(
        name="conventions",
        title="Conventions",
        content="# Conventions\n\n## Summary\n\nUse snake_case.\n\n## Details\n\n- Python 3.12\n",
    )

    assert backend.read_summary("architecture") == "Plugin-based system."
    assert backend.read_summary("conventions") == "Use snake_case."


def test_overwrite_preserves_no_data_loss(tmp_path: Path) -> None:
    """Overwriting a document should completely replace it."""
    backend = get_doc_backend(tmp_path)

    backend.write_document(
        name="test",
        title="Test V1",
        content="# Test V1\n\nOriginal content.\n",
    )

    backend.write_document(
        name="test",
        title="Test V2",
        content="# Test V2\n\nUpdated content.\n",
    )

    doc = backend.read_document("test")
    assert doc is not None
    assert "V2" in doc.title
    assert "Updated content" in doc.content
    assert "Original content" not in doc.content


def test_backend_protocol_compliance(tmp_path: Path) -> None:
    """Verify MarkdownDocBackend satisfies the DocBackend protocol."""
    backend = MarkdownDocBackend(tmp_path)

    # All required methods exist and are callable
    assert callable(backend.write_document)
    assert callable(backend.read_document)
    assert callable(backend.read_summary)
    assert callable(backend.list_documents)
    assert callable(backend.append_entry)
    assert callable(backend.get_injection_context)
