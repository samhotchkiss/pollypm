"""Unit tests for documentation plugin backend interface."""

from pathlib import Path

from pollypm.doc_backends import get_doc_backend
from pollypm.doc_backends.base import DocBackend, DocEntry
from pollypm.doc_backends.markdown import (
    MarkdownDocBackend,
    _extract_last_updated,
    _extract_summary,
    _extract_title,
)


# ---------------------------------------------------------------------------
# DocEntry
# ---------------------------------------------------------------------------


class TestDocEntry:
    def test_defaults(self) -> None:
        entry = DocEntry(name="test", title="Test", content="Hello")
        assert entry.path is None
        assert entry.last_updated == ""
        assert entry.summary == ""


# ---------------------------------------------------------------------------
# get_doc_backend
# ---------------------------------------------------------------------------


class TestGetDocBackend:
    def test_markdown_backend(self, tmp_path: Path) -> None:
        backend = get_doc_backend(tmp_path, "markdown")
        assert isinstance(backend, MarkdownDocBackend)

    def test_default_is_markdown(self, tmp_path: Path) -> None:
        backend = get_doc_backend(tmp_path)
        assert isinstance(backend, MarkdownDocBackend)

    def test_unknown_raises(self, tmp_path: Path) -> None:
        import pytest
        with pytest.raises(ValueError, match="Unknown doc backend"):
            get_doc_backend(tmp_path, "notion")


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------


class TestExtractTitle:
    def test_extracts_h1(self) -> None:
        assert _extract_title("# My Project\nContent") == "My Project"

    def test_no_h1(self) -> None:
        assert _extract_title("No heading here") == ""


class TestExtractSummary:
    def test_extracts_summary_section(self) -> None:
        content = "# Title\n\n## Summary\n\nThis is the summary.\n\n## Details\n\nMore stuff."
        assert _extract_summary(content) == "This is the summary."

    def test_no_summary(self) -> None:
        assert _extract_summary("# Title\n\nNo summary section.") == ""


class TestExtractLastUpdated:
    def test_extracts_timestamp(self) -> None:
        content = "# Title\n\n*Last updated: 2026-04-10T00:00:00Z*\n"
        assert _extract_last_updated(content) == "2026-04-10T00:00:00Z"

    def test_no_timestamp(self) -> None:
        assert _extract_last_updated("# Title\n") == ""


# ---------------------------------------------------------------------------
# MarkdownDocBackend
# ---------------------------------------------------------------------------


class TestMarkdownDocBackend:
    def test_write_and_read_document(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        entry = backend.write_document(
            name="test-doc",
            title="Test Document",
            content="# Test Document\n\n## Summary\n\nA test.\n",
            last_updated="2026-04-10T00:00:00Z",
        )

        assert entry.name == "test-doc"
        assert entry.path.exists()
        assert "2026-04-10T00:00:00Z" in entry.content

        read_back = backend.read_document("test-doc")
        assert read_back is not None
        assert read_back.title == "Test Document"
        assert read_back.summary == "A test."

    def test_read_missing_document(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        assert backend.read_document("nonexistent") is None

    def test_read_summary(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        backend.write_document(
            name="overview",
            title="Overview",
            content="# Overview\n\n## Summary\n\nProject overview text.\n\n## Details\n\nMore info.\n",
        )

        summary = backend.read_summary("overview")
        assert summary == "Project overview text."

    def test_read_summary_missing(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        assert backend.read_summary("nope") == ""

    def test_list_documents(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        backend.write_document(name="a", title="A", content="# A\n")
        backend.write_document(name="b", title="B", content="# B\n")

        docs = backend.list_documents()
        assert len(docs) == 2
        names = [d.name for d in docs]
        assert "a" in names
        assert "b" in names

    def test_list_documents_empty(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        assert backend.list_documents() == []

    def test_append_entry_existing_heading(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        backend.write_document(
            name="decisions",
            title="Decisions",
            content="# Decisions\n\n## Summary\n\nKey decisions.\n\n## Decisions\n\n- Use SQLite\n",
        )

        result = backend.append_entry(
            name="decisions",
            heading="Decisions",
            items=["Use Python 3.12"],
        )

        assert result is not None
        content = result.content
        assert "Use SQLite" in content
        assert "Use Python 3.12" in content

    def test_append_entry_new_heading(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        backend.write_document(
            name="doc",
            title="Doc",
            content="# Doc\n\nContent.\n",
        )

        result = backend.append_entry(
            name="doc",
            heading="New Section",
            items=["Item 1", "Item 2"],
        )

        assert result is not None
        assert "## New Section" in result.content
        assert "Item 1" in result.content

    def test_append_entry_missing_doc(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        result = backend.append_entry(
            name="nope",
            heading="Section",
            items=["Item"],
        )
        assert result is None

    def test_get_injection_context(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        backend.write_document(
            name="project-overview",
            title="Project Overview",
            content="# My Project\n\n## Summary\n\nA great project.\n",
        )

        context = backend.get_injection_context()
        assert "My Project" in context
        assert "great project" in context

    def test_get_injection_context_missing(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        assert backend.get_injection_context() == ""

    def test_write_adds_timestamp(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        entry = backend.write_document(
            name="doc",
            title="Doc",
            content="# Doc\n\nContent.\n",
        )
        assert "*Last updated:" in entry.content

    def test_write_does_not_double_timestamp(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        entry = backend.write_document(
            name="doc",
            title="Doc",
            content="# Doc\n\nContent.\n\n*Last updated: 2026-04-10T00:00:00Z*\n",
        )
        assert entry.content.count("*Last updated:") == 1

    def test_injection_context_truncated(self, tmp_path: Path) -> None:
        backend = MarkdownDocBackend(tmp_path)
        backend.write_document(
            name="project-overview",
            title="Overview",
            content="# Overview\n\n" + "x" * 10000 + "\n",
        )
        context = backend.get_injection_context()
        assert len(context) <= 4000
