"""Storage path resolution for advisor handlers."""

from __future__ import annotations

from pathlib import Path


def resolve_state_db(project_path: Path) -> Path | None:
    """Return the on-disk ``state.db`` that backs ``project_path``."""
    project_path = Path(project_path)
    per_project = project_path / ".pollypm" / "state.db"
    if per_project.exists():
        return per_project

    try:
        resolved = project_path.resolve()
    except OSError:
        resolved = project_path
    for ancestor in [resolved, *resolved.parents]:
        candidate = ancestor / ".pollypm" / "state.db"
        if candidate.exists():
            return candidate
    return None


__all__ = ["resolve_state_db"]
