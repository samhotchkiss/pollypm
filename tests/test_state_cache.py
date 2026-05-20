"""Tests for :class:`pollypm.state_cache.ProjectStateCache`.

Covers the read / write / version semantics promised in
``docs/design/move-a-state-cache.md`` §3.3 / §3.6:

* Reads (`get` / `snapshot` / `version` / `global_version`) are
  lock-free dict ops and return immutable payloads.
* Writes (`invalidate` / `refresh`) bump per-project + global
  versions monotonically and are safe under concurrent readers.
* Failed refreshes (raises / returns ``None``) leave the existing
  entry intact and don't bump versions.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from pollypm.state_cache.entry import ProjectStateCacheEntry, empty_entry
from pollypm.state_cache.project_state_cache import ProjectStateCache


def _entry(key: str, *, glyph: str = "●") -> ProjectStateCacheEntry:
    base = empty_entry(key, Path(f"/tmp/{key}"))
    # Vary a single field so equality checks below catch identity
    # vs value confusion.
    return ProjectStateCacheEntry(
        project_key=base.project_key,
        project_path=base.project_path,
        glyph=glyph,
    )


def test_get_returns_none_when_unknown() -> None:
    cache = ProjectStateCache()
    assert cache.get("missing") is None


def test_snapshot_is_independent_copy() -> None:
    cache = ProjectStateCache(refresh_fn=lambda k: _entry(k))
    cache.refresh("alpha")
    snap = cache.snapshot()
    assert "alpha" in snap
    # Mutating the snapshot dict must not affect the cache itself.
    snap["evil"] = _entry("evil")
    assert "evil" not in cache.snapshot()


def test_refresh_bumps_per_project_and_global_versions() -> None:
    cache = ProjectStateCache(refresh_fn=lambda k: _entry(k))
    assert cache.global_version() == 0
    assert cache.version("alpha") == 0

    e1 = cache.refresh("alpha")
    assert e1 is not None
    assert cache.version("alpha") == 1
    assert e1.version == 1
    assert cache.global_version() == 1

    e2 = cache.refresh("alpha")
    assert e2 is not None
    assert cache.version("alpha") == 2
    assert e2.version == 2
    assert cache.global_version() == 2


def test_global_version_sums_across_projects() -> None:
    cache = ProjectStateCache(refresh_fn=lambda k: _entry(k))
    cache.refresh("alpha")
    cache.refresh("beta")
    cache.refresh("alpha")
    assert cache.version("alpha") == 2
    assert cache.version("beta") == 1
    assert cache.global_version() == 3


def test_refresh_failure_leaves_existing_entry() -> None:
    """A raising refresh function must not corrupt the cache."""

    calls = {"n": 0}

    def flaky(key: str) -> ProjectStateCacheEntry:
        calls["n"] += 1
        if calls["n"] == 1:
            return _entry(key, glyph="A")
        raise RuntimeError("boom")

    cache = ProjectStateCache(refresh_fn=flaky)
    first = cache.refresh("alpha")
    assert first is not None and first.glyph == "A"
    assert cache.version("alpha") == 1
    assert cache.global_version() == 1

    # Second refresh raises — entry + versions unchanged.
    second = cache.refresh("alpha")
    assert second is None
    assert cache.get("alpha") is first
    assert cache.version("alpha") == 1
    assert cache.global_version() == 1


def test_refresh_none_return_leaves_existing_entry() -> None:
    """Refresh fn returning None signals "skip" — no version bump."""

    state = {"return_entry": True}

    def maybe(key: str) -> ProjectStateCacheEntry | None:
        if state["return_entry"]:
            return _entry(key)
        return None

    cache = ProjectStateCache(refresh_fn=maybe)
    cache.refresh("alpha")
    assert cache.version("alpha") == 1
    state["return_entry"] = False
    assert cache.refresh("alpha") is None
    assert cache.version("alpha") == 1


def test_invalidate_coalesces_per_project() -> None:
    cache = ProjectStateCache()
    cache.invalidate("alpha")
    cache.invalidate("alpha")
    cache.invalidate("beta")
    pending = cache.pending_invalidations()
    assert pending == frozenset({"alpha", "beta"})


def test_invalidate_none_enqueues_known_projects_only() -> None:
    cache = ProjectStateCache(refresh_fn=lambda k: _entry(k))
    cache.refresh("alpha")
    cache.refresh("beta")
    cache.invalidate(None)
    pending = cache.pending_invalidations()
    assert pending == frozenset({"alpha", "beta"})


def test_invalidate_empty_string_is_noop() -> None:
    cache = ProjectStateCache()
    cache.invalidate("")
    assert cache.pending_invalidations() == frozenset()


def test_drain_pending_returns_sorted_and_clears() -> None:
    cache = ProjectStateCache()
    cache.invalidate("beta")
    cache.invalidate("alpha")
    cache.invalidate("gamma")
    drained = cache.drain_pending()
    assert drained == ["alpha", "beta", "gamma"]
    assert cache.pending_invalidations() == frozenset()


def test_refresh_clears_pending_for_key() -> None:
    cache = ProjectStateCache(refresh_fn=lambda k: _entry(k))
    cache.invalidate("alpha")
    cache.invalidate("beta")
    cache.refresh("alpha")
    assert cache.pending_invalidations() == frozenset({"beta"})


def test_returned_entry_is_immutable() -> None:
    cache = ProjectStateCache(refresh_fn=lambda k: _entry(k))
    entry = cache.refresh("alpha")
    assert entry is not None
    with pytest.raises(Exception):  # FrozenInstanceError on dataclass
        entry.glyph = "X"  # type: ignore[misc]


def test_concurrent_invalidations_no_lost_updates() -> None:
    """Stress test §3.6 RLock-guarded write path under N invalidators."""

    cache = ProjectStateCache(refresh_fn=lambda k: _entry(k))
    threads: list[threading.Thread] = []
    keys = [f"p{i}" for i in range(20)]

    def hammer() -> None:
        for k in keys:
            cache.invalidate(k)

    for _ in range(10):
        t = threading.Thread(target=hammer)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    assert cache.pending_invalidations() == frozenset(keys)


def test_concurrent_reads_during_refresh_never_tear() -> None:
    """Readers under a writer storm always see a coherent entry."""

    cache = ProjectStateCache(refresh_fn=lambda k: _entry(k))
    # Seed the initial entry so readers see something on first tick.
    cache.refresh("alpha")
    seeded_version = cache.version("alpha")

    stop = threading.Event()
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            while not stop.is_set():
                snap = cache.snapshot()
                entry = snap.get("alpha")
                if entry is not None:
                    # Frozen invariant — touch every field, must not raise.
                    _ = entry.project_key, entry.glyph, entry.version
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def writer() -> None:
        try:
            for _ in range(200):
                cache.refresh("alpha")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    writers = [threading.Thread(target=writer) for _ in range(2)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join()

    assert not errors
    # 2 writers × 200 refreshes on top of the seed.
    expected = seeded_version + 2 * 200
    assert cache.version("alpha") == expected
    assert cache.global_version() == expected


def test_install_for_test_helper() -> None:
    cache = ProjectStateCache()
    cache._install_for_test("alpha", _entry("alpha", glyph="Z"))
    entry = cache.get("alpha")
    assert entry is not None
    assert entry.glyph == "Z"
    assert entry.version == 1
    assert cache.version("alpha") == 1


def test_computed_at_advances_between_refreshes() -> None:
    cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
    e1 = cache.refresh("alpha")
    time.sleep(0.005)
    e2 = cache.refresh("alpha")
    assert e1 is not None and e2 is not None
    assert e2.computed_at >= e1.computed_at
