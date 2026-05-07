"""Tests for #1375 — bound the mtime-keyed dashboard caches.

Each ``state.db`` write bumps the mtime, which produces a fresh
cache key for each of:

* ``_PLAN_STALENESS_CACHE``
* ``_PROJECT_DASHBOARD_TASK_CACHE``
* ``_DASHBOARD_INBOX_CACHE``
* ``_DASHBOARD_ACTIVITY_CACHE``

Without bounds the caches grow monotonically over the cockpit's
lifetime. The fix wraps inserts through ``_dashboard_cache_set``
which evicts the least recently used entry past
``_DASHBOARD_CACHE_MAXSIZE`` (default 256).
"""

from __future__ import annotations

from collections import OrderedDict

import pytest

from pollypm.cockpit_ui import (
    _DASHBOARD_ACTIVITY_CACHE,
    _DASHBOARD_CACHE_MAXSIZE,
    _DASHBOARD_INBOX_CACHE,
    _PLAN_STALENESS_CACHE,
    _PROJECT_DASHBOARD_TASK_CACHE,
    _dashboard_cache_set,
)


def setup_function(_func) -> None:
    _PLAN_STALENESS_CACHE.clear()
    _PROJECT_DASHBOARD_TASK_CACHE.clear()
    _DASHBOARD_INBOX_CACHE.clear()
    _DASHBOARD_ACTIVITY_CACHE.clear()


def test_dashboard_cache_set_evicts_past_maxsize() -> None:
    """Pumping past ``maxsize`` keeps the cache size capped."""
    cache: OrderedDict[int, str] = OrderedDict()
    for i in range(10):
        _dashboard_cache_set(cache, i, f"v{i}", maxsize=4)
    assert len(cache) == 4
    # The four most-recent inserts should remain.
    assert list(cache.keys()) == [6, 7, 8, 9]


def test_dashboard_cache_set_refreshes_recency_on_reinsert() -> None:
    """Re-inserting an existing key moves it to the MRU end so it
    survives subsequent eviction passes."""
    cache: OrderedDict[int, str] = OrderedDict()
    for i in range(4):
        _dashboard_cache_set(cache, i, f"v{i}", maxsize=4)
    # Touch key 0 — should move to MRU end.
    _dashboard_cache_set(cache, 0, "v0-updated", maxsize=4)
    # Now insert 4 — should evict key 1 (LRU), not key 0.
    _dashboard_cache_set(cache, 4, "v4", maxsize=4)
    assert 0 in cache
    assert 1 not in cache
    assert cache[0] == "v0-updated"
    assert len(cache) == 4


def test_dashboard_cache_maxsize_is_generous() -> None:
    """Sanity: the cap is set generously so normal use never churns.

    9 projects * ~28 distinct mtimes is what we want to fit comfortably.
    """
    assert _DASHBOARD_CACHE_MAXSIZE >= 128


@pytest.mark.parametrize(
    "cache",
    [
        _PLAN_STALENESS_CACHE,
        _PROJECT_DASHBOARD_TASK_CACHE,
        _DASHBOARD_INBOX_CACHE,
        _DASHBOARD_ACTIVITY_CACHE,
    ],
)
def test_dashboard_caches_are_ordered_dicts(cache) -> None:
    """All four module-level caches are ``OrderedDict`` instances so
    LRU recency is preserved."""
    assert isinstance(cache, OrderedDict)


@pytest.mark.parametrize(
    "cache",
    [
        _PLAN_STALENESS_CACHE,
        _PROJECT_DASHBOARD_TASK_CACHE,
        _DASHBOARD_INBOX_CACHE,
        _DASHBOARD_ACTIVITY_CACHE,
    ],
)
def test_dashboard_caches_evict_past_maxsize(cache) -> None:
    """Each module-level cache stays bounded when pumped past the cap.

    Simulates the per-project refresh tick churning fresh ``db_mtime``
    keys (#1375). Without eviction this grows for the cockpit's
    lifetime; with it, size stays at ``_DASHBOARD_CACHE_MAXSIZE``.
    """
    overshoot = _DASHBOARD_CACHE_MAXSIZE + 50
    for i in range(overshoot):
        # Use distinct keys per cache shape — only the LRU bound is
        # under test, so any unique tuple works.
        key = ("demo", float(i), float(i))
        _dashboard_cache_set(cache, key, None)
    assert len(cache) == _DASHBOARD_CACHE_MAXSIZE
