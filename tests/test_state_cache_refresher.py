"""Tests for :class:`pollypm.state_cache.refresher.StateCacheRefresher`.

PR 1 scope: the refresher tails the central audit-log directory,
maps event names to ``cache.invalidate`` calls, and the worker
drains pending invalidations onto ``cache.refresh``. Per §9.1, the
tail starts at seek-to-end with an initial full refresh on startup.

The real per-project query lands in PR 2 — these tests use a
fake refresh function so the assertions stay tight to PR 1's
responsibility surface (tail + dispatch + drain + shutdown).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from pollypm.state_cache.entry import empty_entry
from pollypm.state_cache.project_state_cache import ProjectStateCache
from pollypm.state_cache.refresher import (
    StateCacheRefresher,
    _await_quiescence,
    _drain_for_test,
)


def _write_event(path: Path, *, event: str, project: str) -> None:
    record = {
        "ts": "2026-05-19T00:00:00+00:00",
        "project": project,
        "event": event,
        "subject": f"{project}/0",
        "actor": "test",
        "status": "ok",
        "metadata": {},
        "schema": 1,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


@pytest.fixture()
def audit_dir(tmp_path: Path) -> Path:
    d = tmp_path / "audit"
    d.mkdir()
    return d


def test_tail_starts_seek_to_end_skipping_history(audit_dir: Path) -> None:
    """§9.1 — events appended before start() must NOT cause invalidation."""

    log = audit_dir / "alpha.jsonl"
    _write_event(log, event="task.created", project="alpha")

    cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
    refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
    refresher.start()
    try:
        _await_quiescence(refresher, timeout=0.5)
        assert cache.pending_invalidations() == frozenset()
        # global_version is 0 because there are no candidate keys —
        # the initial full refresh is empty (PR 1 has no provider).
        assert cache.global_version() == 0
    finally:
        refresher.stop()


def test_appended_event_triggers_invalidation(audit_dir: Path) -> None:
    log = audit_dir / "alpha.jsonl"
    log.touch()

    cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
    refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
    refresher.start()
    try:
        _write_event(log, event="task.status_changed", project="alpha")
        # Wait for the tail thread to pick it up + the worker thread
        # to refresh.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if cache.get("alpha") is not None:
                break
            time.sleep(0.02)
        assert cache.get("alpha") is not None
        assert cache.version("alpha") == 1
    finally:
        refresher.stop()


def test_non_invalidating_event_is_ignored(audit_dir: Path) -> None:
    log = audit_dir / "alpha.jsonl"
    log.touch()
    cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
    refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
    refresher._seek_to_end()  # synchronous setup

    _write_event(log, event="work_db.opened", project="alpha")
    _drain_for_test(refresher)

    assert cache.pending_invalidations() == frozenset()
    assert cache.get("alpha") is None


def test_heartbeat_tick_event_invalidates_project(audit_dir: Path) -> None:
    """#2050 — ``heartbeat.tick`` is now in ``_INVALIDATING_EVENTS``.

    The audit watchdog emits one ``heartbeat.tick`` per sweep; the
    refresher must invalidate the affected project entry so the next
    refresh repopulates ``latest_heartbeat_by_session`` from a fresh
    pg query and the rail's snapshot can never go indefinitely stale.
    """

    log = audit_dir / "alpha.jsonl"
    log.touch()
    cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
    refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
    refresher._seek_to_end()

    _write_event(log, event="heartbeat.tick", project="alpha")
    _drain_for_test(refresher)

    assert cache.get("alpha") is not None
    assert cache.version("alpha") == 1


def test_workspace_scoped_heartbeat_tick_invalidates_all_known(
    audit_dir: Path,
) -> None:
    """``heartbeat.tick`` fires with ``project=""`` from
    ``audit_watchdog.emit_heartbeat_tick`` — must enqueue every known
    project (matches the ``work_table.cleared`` workspace-scope path).
    """

    log = audit_dir / "_workspace.jsonl"
    log.touch()
    cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
    cache._install_for_test("alpha", empty_entry("alpha"))
    cache._install_for_test("beta", empty_entry("beta"))

    refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
    refresher._seek_to_end()

    _write_event(log, event="heartbeat.tick", project="")
    _drain_for_test(refresher)

    assert cache.version("alpha") == 2
    assert cache.version("beta") == 2


def test_workspace_scoped_event_invalidates_known_projects(
    audit_dir: Path,
) -> None:
    log = audit_dir / "_workspace.jsonl"
    log.touch()
    cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
    # Seed two known projects so invalidate(None) has something to enqueue.
    cache._install_for_test("alpha", empty_entry("alpha"))
    cache._install_for_test("beta", empty_entry("beta"))

    refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
    refresher._seek_to_end()

    _write_event(log, event="work_table.cleared", project="")
    _drain_for_test(refresher)

    # The worker drained alpha + beta and re-refreshed each.
    assert cache.get("alpha") is not None
    assert cache.get("beta") is not None
    assert cache.version("alpha") == 2
    assert cache.version("beta") == 2


def test_coalesces_multiple_events_for_same_project(audit_dir: Path) -> None:
    log = audit_dir / "alpha.jsonl"
    log.touch()
    refresh_calls: list[str] = []

    def counting_refresh(key: str):
        refresh_calls.append(key)
        return empty_entry(key)

    cache = ProjectStateCache(refresh_fn=counting_refresh)
    refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
    refresher._seek_to_end()

    # 5 events for the same project between drains — must collapse
    # to a single refresh per key. #2051 (Codex review) adds the
    # synthetic ``__workspace__`` sentinel to every invalidation, so
    # the drain refreshes both keys once — still coalesced (no
    # duplicates per key).
    for _ in range(5):
        _write_event(log, event="task.status_changed", project="alpha")
    _drain_for_test(refresher)

    assert refresh_calls == ["__workspace__", "alpha"]


def test_tail_handles_truncation(audit_dir: Path) -> None:
    log = audit_dir / "alpha.jsonl"
    log.touch()
    cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
    refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
    refresher._seek_to_end()

    # Write two events so the post-rotation file is strictly smaller
    # than the pre-rotation offset (single event vs two).
    _write_event(log, event="task.created", project="alpha")
    _write_event(log, event="task.created", project="alpha")
    _drain_for_test(refresher)
    assert cache.version("alpha") == 1  # coalesced to one refresh

    # Simulate rotation — replace the file with a strictly smaller one
    # carrying a fresh event. The tail must replay from offset 0.
    log.unlink()
    log.touch()
    _write_event(log, event="task.deleted", project="alpha")
    _drain_for_test(refresher)
    assert cache.version("alpha") == 2


def test_malformed_json_lines_are_skipped(audit_dir: Path) -> None:
    log = audit_dir / "alpha.jsonl"
    log.touch()
    cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
    refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
    refresher._seek_to_end()

    with open(log, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write(
            json.dumps({"event": "task.created", "project": "alpha"}) + "\n"
        )
        fh.write("partial-truncated-line-no-newline")
    _drain_for_test(refresher)

    # The valid middle line landed; the malformed lines didn't crash
    # the tail and didn't trigger spurious invalidations.
    assert cache.version("alpha") == 1


def test_initial_full_refresh_uses_provider(audit_dir: Path) -> None:
    """§9.1 — start() force-refreshes every known project once."""

    cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
    refresher = StateCacheRefresher(
        cache,
        audit_dir=audit_dir,
        project_keys=lambda: ["alpha", "beta"],
    )
    refresher.start()
    try:
        # Initial refresh is synchronous in start().
        assert cache.version("alpha") == 1
        assert cache.version("beta") == 1
    finally:
        refresher.stop()


def test_stop_joins_threads_cleanly(audit_dir: Path) -> None:
    cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
    refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
    refresher.start()
    assert refresher.running
    refresher.stop()
    assert not refresher.running
    # stop() is idempotent
    refresher.stop()


def test_new_project_log_picked_up_mid_session(audit_dir: Path) -> None:
    """A log file appearing after start() must be tailed seek-to-end."""

    cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
    refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
    refresher._seek_to_end()

    new_log = audit_dir / "gamma.jsonl"
    _write_event(new_log, event="task.created", project="gamma")
    # First tail tick discovers the file, seeks to its EOF — does NOT
    # replay the historical event (consistent with §9.1 semantics).
    _drain_for_test(refresher)
    assert cache.get("gamma") is None

    # A second event after discovery must trigger invalidation.
    _write_event(new_log, event="task.status_changed", project="gamma")
    _drain_for_test(refresher)
    assert cache.version("gamma") == 1


class TestWorkspaceRootInvalidation:
    """Codex review of #2051 — every invalidating event must also keep
    the synthetic ``__workspace__`` sentinel fresh.

    Before this contract landed, workspace-root awaits-user rows could
    be created or closed after startup without refreshing the synthetic
    entry — the rail badge / inbox count could serve a stale zero until
    the next full workspace invalidation or process restart.
    """

    def test_project_inbox_event_invalidates_workspace_sentinel(
        self, audit_dir: Path,
    ) -> None:
        """``project="inbox"`` events are workspace-root signals — they
        MUST invalidate ``__workspace__`` so the next cache read picks
        up the new row.
        """
        log = audit_dir / "inbox.jsonl"
        log.touch()
        cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
        refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
        refresher._seek_to_end()

        _write_event(log, event="task.created", project="inbox")
        _drain_for_test(refresher)

        # The synthetic sentinel was refreshed (entry installed +
        # version bumped to 1). Before the fix, only the literal
        # "inbox" key was invalidated and the sentinel stayed stale.
        assert cache.get("__workspace__") is not None
        assert cache.version("__workspace__") == 1

    def test_project_inbox_status_change_invalidates_workspace_sentinel(
        self, audit_dir: Path,
    ) -> None:
        """``task.status_changed`` with ``project="inbox"`` (workspace-root
        row resolved / closed) MUST also bump the sentinel so the badge
        sees the closure.
        """
        log = audit_dir / "inbox.jsonl"
        log.touch()
        cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
        # Seed an existing sentinel so we can observe the version bump
        # on the close event.
        cache._install_for_test("__workspace__", empty_entry("__workspace__"))
        refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
        refresher._seek_to_end()

        baseline = cache.version("__workspace__")
        _write_event(log, event="task.status_changed", project="inbox")
        _drain_for_test(refresher)

        assert cache.version("__workspace__") > baseline

    def test_tracked_project_event_also_refreshes_workspace_sentinel(
        self, audit_dir: Path,
    ) -> None:
        """A tracked-project ``task.*`` event ALSO refreshes the sentinel.

        Conservative: tracked-project mutations can cross-post into the
        workspace-root inbox (notifications cc'd from a project task);
        the cache-read boundary now refuses to serve when the sentinel
        is missing, so we always refresh it alongside the project entry.
        """
        log = audit_dir / "alpha.jsonl"
        log.touch()
        cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
        refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
        refresher._seek_to_end()

        _write_event(log, event="task.created", project="alpha")
        _drain_for_test(refresher)

        assert cache.version("alpha") == 1
        # The sentinel was refreshed too.
        assert cache.get("__workspace__") is not None
        assert cache.version("__workspace__") == 1

    def test_non_invalidating_event_leaves_sentinel_alone(
        self, audit_dir: Path,
    ) -> None:
        """Ignored events (``work_db.opened``, etc.) MUST NOT touch the
        sentinel — the workspace invalidation is gated on the
        ``_INVALIDATING_EVENTS`` membership check.
        """
        log = audit_dir / "alpha.jsonl"
        log.touch()
        cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
        refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
        refresher._seek_to_end()

        _write_event(log, event="work_db.opened", project="alpha")
        _drain_for_test(refresher)

        assert cache.pending_invalidations() == frozenset()
        assert cache.get("__workspace__") is None

    def test_workspace_scoped_event_still_invalidates_sentinel(
        self, audit_dir: Path,
    ) -> None:
        """Empty-project events (workspace-scoped) MUST invalidate the
        sentinel via ``invalidate(None)`` — already covered by the
        project-keys provider including ``__workspace__`` at the
        singleton boundary, pinned here so the refresher itself can't
        regress the contract.
        """
        log = audit_dir / "_workspace.jsonl"
        log.touch()
        cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
        cache._install_for_test("__workspace__", empty_entry("__workspace__"))
        cache._install_for_test("alpha", empty_entry("alpha"))
        refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
        refresher._seek_to_end()

        _write_event(log, event="work_table.cleared", project="")
        _drain_for_test(refresher)

        # Both keys refreshed by the workspace-scoped invalidation.
        assert cache.version("__workspace__") == 2
        assert cache.version("alpha") == 2


def test_concurrent_writes_and_reads_during_tail(audit_dir: Path) -> None:
    """Stress: writers + tail thread + readers — no exceptions, no torn reads."""

    log = audit_dir / "alpha.jsonl"
    log.touch()
    cache = ProjectStateCache(refresh_fn=lambda k: empty_entry(k))
    refresher = StateCacheRefresher(cache, audit_dir=audit_dir)
    refresher.start()
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for _ in range(50):
                _write_event(log, event="task.status_changed", project="alpha")
                time.sleep(0.005)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                cache.snapshot()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        readers = [threading.Thread(target=reader) for _ in range(3)]
        for r in readers:
            r.start()
        w = threading.Thread(target=writer)
        w.start()
        w.join()
        stop.set()
        for r in readers:
            r.join()
        _await_quiescence(refresher, timeout=2.0)
    finally:
        refresher.stop()

    assert not errors
    assert cache.version("alpha") >= 1
