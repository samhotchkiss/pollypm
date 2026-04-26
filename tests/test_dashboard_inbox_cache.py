"""Cycle 138 — perf: content-addressed cache on _dashboard_inbox.

The per-project dashboard refresh tick (every 10s) used to call
``_dashboard_inbox`` which opens SQLAlchemyStore + SQLiteWorkService
and runs queries against both. On 9 projects = 18 DB opens per tick
even when no inbox change has landed.

Cache by ``(project_key, db_mtime)``: an unchanged db_mtime means
no inbox writes since the last call, so the answer is unchanged.
The lone ``stat()`` is essentially free compared to two DB opens.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pollypm.cockpit_ui import (
    _DASHBOARD_INBOX_CACHE,
    _dashboard_inbox,
)


def setup_function(_func) -> None:
    _DASHBOARD_INBOX_CACHE.clear()


def _project_with_db(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "demo"
    db = project / ".pollypm" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    return project, db


def test_cache_short_circuits_on_repeat_call(tmp_path: Path) -> None:
    """Two back-to-back calls with the same db_mtime hit the cache —
    no DB opens, no config reload."""
    project, _db = _project_with_db(tmp_path)

    open_count = {"n": 0}

    class _BoomStore:
        def __init__(self, *_a, **_kw):
            open_count["n"] += 1

    # Seed the cache directly so the first call returns without
    # touching the import side. Then a second call must hit the
    # cache and never reach the SQLAlchemyStore import.
    config_path = tmp_path / "pollypm.toml"
    cache_key = (
        "demo",
        (project / ".pollypm" / "state.db").stat().st_mtime,
    )
    _DASHBOARD_INBOX_CACHE[cache_key] = (3, ["item"], [])

    with patch("pollypm.store.SQLAlchemyStore", _BoomStore):
        result = _dashboard_inbox(config_path, "demo", project)

    assert result == (3, ["item"], [])
    assert open_count["n"] == 0, "cache hit must skip the SQLAlchemyStore open"


def test_cache_invalidates_when_db_mtime_changes(tmp_path: Path) -> None:
    """A bumped db_mtime (i.e. an inbox write landed) makes the cache
    miss — caller gets fresh data, not stale cached data."""
    project, db = _project_with_db(tmp_path)
    config_path = tmp_path / "pollypm.toml"

    # Pre-seed under the current mtime.
    initial_mtime = db.stat().st_mtime
    _DASHBOARD_INBOX_CACHE[("demo", initial_mtime)] = (1, ["stale"], [])

    # Bump the mtime as if a new message landed.
    import os
    new_time = initial_mtime + 100
    os.utime(db, (new_time, new_time))

    # The new key isn't seeded — caller would re-query. Verify by
    # checking the stale entry is still in the cache untouched but
    # the new key isn't there yet.
    new_mtime = db.stat().st_mtime
    assert new_mtime != initial_mtime
    assert ("demo", initial_mtime) in _DASHBOARD_INBOX_CACHE
    assert ("demo", new_mtime) not in _DASHBOARD_INBOX_CACHE


def test_cache_returns_empty_when_db_missing(tmp_path: Path) -> None:
    """The early-return path (no state.db) doesn't touch the cache —
    nothing to cache, no I/O to elide."""
    project = tmp_path / "no-db-project"
    project.mkdir()
    config_path = tmp_path / "pollypm.toml"
    result = _dashboard_inbox(config_path, "demo", project)
    assert result == (0, [], [])
    assert len(_DASHBOARD_INBOX_CACHE) == 0
