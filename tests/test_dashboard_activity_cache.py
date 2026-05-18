"""Cycle 139 — perf: cache _dashboard_activity by (project, db_mtime, limit).

The per-project dashboard refresh tick used to rebuild the activity-
feed projector and walk the messages table on every call — pure
repeated work when no event had landed since the last call. Cache
by db_mtime so an idle project pays one stat() per tick instead.

Sister to ``_DASHBOARD_INBOX_CACHE`` (cycle 138) and
``_PLAN_STALENESS_CACHE`` (cycle 133).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pollypm.cockpit_ui import (
    _DASHBOARD_ACTIVITY_CACHE,
    _dashboard_activity,
)


def setup_function(_func) -> None:
    _DASHBOARD_ACTIVITY_CACHE.clear()


def _project_with_db(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "demo"
    db = project / ".pollypm" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    return project, db


def _config(project_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        projects={"demo": SimpleNamespace(path=project_path)}
    )


def test_cache_short_circuits_on_repeat_call(tmp_path: Path) -> None:
    """A second call within the same db_mtime hits the cache; the
    projector is never rebuilt."""
    project, db = _project_with_db(tmp_path)
    config_path = tmp_path / "pollypm.toml"

    # Pre-seed the cache so the first call returns instantly.
    cache_key = ("demo", db.stat().st_mtime, 10)
    _DASHBOARD_ACTIVITY_CACHE[cache_key] = [{"summary": "cached"}]

    build_count = {"n": 0}

    def boom(_config):
        build_count["n"] += 1
        return None

    with patch("pollypm.cockpit_ui.load_config", lambda _: _config(project)), patch(
        "pollypm.activity_projector_registry.build_activity_projector", boom,
    ):
        result = _dashboard_activity(config_path, "demo", limit=10)

    assert result == [{"summary": "cached"}]
    assert build_count["n"] == 0, "cache hit must skip build_activity_projector"


def test_cache_invalidates_when_db_mtime_changes(tmp_path: Path) -> None:
    """A bumped db_mtime makes the cache miss — caller falls through
    to the projector. Verifies the key includes db_mtime."""
    project, db = _project_with_db(tmp_path)
    config_path = tmp_path / "pollypm.toml"

    initial_mtime = db.stat().st_mtime
    _DASHBOARD_ACTIVITY_CACHE[("demo", initial_mtime, 10)] = [{"summary": "stale"}]

    import os
    new_time = initial_mtime + 100
    os.utime(db, (new_time, new_time))

    new_mtime = db.stat().st_mtime
    assert new_mtime != initial_mtime
    assert ("demo", initial_mtime, 10) in _DASHBOARD_ACTIVITY_CACHE
    assert ("demo", new_mtime, 10) not in _DASHBOARD_ACTIVITY_CACHE


def test_cache_key_separates_limit_values(tmp_path: Path) -> None:
    """Two calls with different ``limit`` values must use distinct cache
    entries — the projection length depends on limit."""
    project, db = _project_with_db(tmp_path)
    mtime = db.stat().st_mtime
    _DASHBOARD_ACTIVITY_CACHE[("demo", mtime, 5)] = [{"summary": "five"}]
    _DASHBOARD_ACTIVITY_CACHE[("demo", mtime, 10)] = [{"summary": "ten"}]
    assert _DASHBOARD_ACTIVITY_CACHE[("demo", mtime, 5)] != \
        _DASHBOARD_ACTIVITY_CACHE[("demo", mtime, 10)]
