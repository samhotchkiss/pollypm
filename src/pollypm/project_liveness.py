"""Project classification helpers for operator-facing live surfaces."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping, TypeVar


_SYNTHETIC_PROJECT_KEYS = frozenset(
    {
        "inbox",
        "myproj",
        "smoketest",
        "pm_test",
        "queuestorm",
        "test_5_8_x",
        "polly_e2e_proj",
        "pollypm_cycle_ux_scratch",
        "demo_polly",
    }
)
_SYNTHETIC_PROJECT_RES = (
    re.compile(r"(?:^|[-_])wave[-_]\d+$"),
    re.compile(r"^pr\d+[-_]drift[-_].+"),
)
_SYNTHETIC_PROJECT_PREFIXES = (
    "pm_test_",
    "pm-test-",
    "queuestorm_",
    "queuestorm-",
    "pr2316_drift_",
    "pr2316-drift-",
    "race_proj_",
    "race-proj-",
    "testpause_",
    "testpause-",
)
_SYNTHETIC_PATH_PREFIXES = ("/private/tmp/",)

_ProjectKey = TypeVar("_ProjectKey")
_ProjectValue = TypeVar("_ProjectValue")


def _normalized_project_key(value: object) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[\s-]+", "_", text)


def project_key_looks_synthetic(project_key: object) -> bool:
    """Return True for known harness/debris project key shapes."""

    raw = str(project_key or "").strip().lower()
    normalized = _normalized_project_key(raw)
    if raw in _SYNTHETIC_PROJECT_KEYS or normalized in _SYNTHETIC_PROJECT_KEYS:
        return True
    for prefix in _SYNTHETIC_PROJECT_PREFIXES:
        if raw.startswith(prefix) or normalized.startswith(_normalized_project_key(prefix)):
            return True
    for pattern in _SYNTHETIC_PROJECT_RES:
        if pattern.search(raw) or pattern.search(normalized):
            return True
    return False


def project_path_looks_synthetic(path: object) -> bool:
    """Return True when a project root is an ephemeral test workspace."""

    if not path:
        return False
    parsed = Path(path) if isinstance(path, str | Path) else Path(str(path))
    text = str(parsed).strip()
    if text == "/private/tmp":
        return True
    if not any(text.startswith(prefix) for prefix in _SYNTHETIC_PATH_PREFIXES):
        return False
    return any(project_key_looks_synthetic(part) for part in parsed.parts)


def is_real_operator_project(
    project_key: object,
    project: object,
    *,
    default_tracked: bool = True,
) -> bool:
    """Return True when a project may headline operator-facing live content."""

    if default_tracked and not getattr(project, "tracked", default_tracked):
        return False
    if project_key_looks_synthetic(project_key):
        return False
    if project_path_looks_synthetic(getattr(project, "path", None)):
        return False
    return True


def real_operator_project_keys(
    projects: Mapping[object, object],
    *,
    default_tracked: bool = True,
) -> frozenset[str]:
    """Return tracked, non-synthetic project keys."""

    return frozenset(
        str(key)
        for key, project in projects.items()
        if is_real_operator_project(
            key,
            project,
            default_tracked=default_tracked,
        )
    )


def real_operator_project_items(
    projects: Mapping[_ProjectKey, _ProjectValue],
    *,
    default_tracked: bool = True,
    fallback_to_all: bool = True,
) -> tuple[tuple[_ProjectKey, _ProjectValue], ...]:
    """Return tracked, non-synthetic project items for operator-facing UI.

    Test and fixture-heavy workspaces can contain only synthetic keys
    such as ``myproj``. In that case ``fallback_to_all=True`` keeps
    the surface usable instead of rendering an empty rail.
    """

    items = tuple(projects.items())
    real_items = tuple(
        (key, project)
        for key, project in items
        if is_real_operator_project(
            key,
            project,
            default_tracked=default_tracked,
        )
    )
    if real_items or not fallback_to_all:
        return real_items
    return items


__all__ = [
    "is_real_operator_project",
    "project_key_looks_synthetic",
    "project_path_looks_synthetic",
    "real_operator_project_items",
    "real_operator_project_keys",
]
