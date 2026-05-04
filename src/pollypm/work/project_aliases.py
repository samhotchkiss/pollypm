"""Project-name aliases for work DB lookups."""

from __future__ import annotations

from pathlib import Path


def project_storage_aliases(config: object, project_key: str) -> list[str]:
    """Return every project-name form the work-service may have stored."""
    aliases: list[str] = []

    def _add(value: object) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text or text in aliases:
            return
        aliases.append(text)

    _add(project_key)
    project = (getattr(config, "projects", None) or {}).get(project_key)
    if project is not None:
        _add(getattr(project, "name", None))
        path = getattr(project, "path", None)
        if path is not None:
            try:
                _add(Path(path).name)
            except TypeError:
                pass
    _add(project_key.replace("_", "-"))
    _add(project_key.replace("-", "_"))

    try:
        from pollypm.config import _normalize_project_display_name

        _add(_normalize_project_display_name(project_key, None))
    except Exception:  # noqa: BLE001
        pass

    for existing in list(aliases):
        _add(existing.lower())
        _add(existing.casefold())
        _add(existing.title())
    return aliases


__all__ = ["project_storage_aliases"]
