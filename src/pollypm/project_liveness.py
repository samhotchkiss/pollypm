"""Project classification helpers for operator-facing live surfaces."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Mapping, TypeVar


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
RECENT_REAL_WORK_WINDOW = timedelta(days=7)

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


def task_status_key(task: object) -> str:
    """Return a task's work-status value as a plain string."""

    status = getattr(task, "work_status", "") or ""
    status_value = getattr(status, "value", status)
    return str(status_value or "")


def coerce_utc_datetime(value: object) -> datetime | None:
    """Coerce an ISO/datetime value to UTC, returning ``None`` on parse failure."""

    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def task_is_recent_done_work(
    task: object,
    *,
    cutoff: datetime,
) -> bool:
    """Return True when ``task`` is recently completed real work."""

    if task_status_key(task) != "done":
        return False
    stamped = coerce_utc_datetime(
        getattr(task, "updated_at", None) or getattr(task, "created_at", None)
    )
    return stamped is None or stamped >= cutoff


def recent_real_work_project_keys(
    projects: Mapping[object, object],
    tasks_for_project: Callable[[object], Iterable[object]],
    *,
    now: datetime | None = None,
    recency_window: timedelta = RECENT_REAL_WORK_WINDOW,
) -> frozenset[str]:
    """Return real operator projects with recent completed task work.

    This is the liveness signal used by operator-facing alert demotion
    and sweep-side watchdog gating. It intentionally ignores queued /
    recently touched rows because watchdog sweeps themselves can refresh
    those timestamps.
    """

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    cutoff = current - recency_window
    recent: set[str] = set()
    for project_key, project in projects.items():
        if not is_real_operator_project(project_key, project):
            continue
        for task in tasks_for_project(project_key):
            if task_is_recent_done_work(task, cutoff=cutoff):
                recent.add(str(project_key))
                break
    return frozenset(recent)


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
    "RECENT_REAL_WORK_WINDOW",
    "coerce_utc_datetime",
    "is_real_operator_project",
    "project_key_looks_synthetic",
    "project_path_looks_synthetic",
    "real_operator_project_items",
    "real_operator_project_keys",
    "recent_real_work_project_keys",
    "task_is_recent_done_work",
    "task_status_key",
]
