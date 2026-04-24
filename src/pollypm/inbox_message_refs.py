"""Helpers for explicit project references in user-facing inbox messages.

These helpers intentionally inspect structured fields only. Free-text
subjects/bodies can mention old project names coincidentally, so cleanup and
badge filtering must be driven by scope, payload, and labels.
"""

from __future__ import annotations

from typing import Any


_NON_PROJECT_SCOPES = {"", "inbox", "workspace", "global", "cockpit"}


def _json_dict(value: object) -> dict[str, object]:
    import json as _json

    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = _json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: object) -> list[object]:
    import json as _json

    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = _json.loads(value)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def row_project_refs(row: Any) -> set[str]:
    """Return explicit project refs carried by a Store message row."""
    if isinstance(row, dict):
        scope = str(row.get("scope") or "")
        payload = _json_dict(row.get("payload") or row.get("payload_json"))
        labels = _json_list(row.get("labels"))
    else:
        scope = str(getattr(row, "scope", "") or "")
        payload = _json_dict(
            getattr(row, "payload", None) or getattr(row, "payload_json", None)
        )
        labels = _json_list(getattr(row, "labels", None))

    refs: set[str] = set()
    if scope not in _NON_PROJECT_SCOPES:
        refs.add(scope)
    for key in ("project", "task_project"):
        value = payload.get(key)
        if isinstance(value, str) and value not in _NON_PROJECT_SCOPES:
            refs.add(value)
    for label in labels:
        if isinstance(label, str) and label.startswith("project:"):
            value = label.split(":", 1)[1]
            if value not in _NON_PROJECT_SCOPES:
                refs.add(value)
    return refs


def unknown_project_refs(row: Any, known_projects: set[str]) -> set[str]:
    """Return explicit row refs not present in the active project registry."""
    return {ref for ref in row_project_refs(row) if ref not in known_projects}
