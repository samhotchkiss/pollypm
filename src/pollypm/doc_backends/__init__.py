"""Documentation plugin backend system.

Provides a pluggable interface for reading and writing project
documentation. The default backend writes markdown files to docs/.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.doc_backends.base import DocBackend, DocEntry  # noqa: F401  (DocEntry re-exported as part of the public API)
from pollypm.doc_backends.markdown import MarkdownDocBackend


def get_doc_backend(project_root: Path, backend_name: str = "markdown") -> DocBackend:
    """Get a documentation backend by name."""
    if backend_name == "markdown":
        return MarkdownDocBackend(project_root)
    raise ValueError(f"Unknown doc backend: {backend_name}")
