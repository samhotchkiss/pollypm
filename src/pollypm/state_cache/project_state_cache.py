"""In-process per-project state cache.

See ``docs/design/move-a-state-cache.md`` §3.3 / §3.6 for the design
and threading model.

Contract:

* **Reads are lock-free.** :meth:`ProjectStateCache.get` and
  :meth:`snapshot` do an atomic dict get / copy. Entries are frozen
  dataclasses (see :mod:`pollypm.state_cache.entry`) so the payload
  itself is safe to hand back without a lock.
* **Writes are RLock-guarded.** :meth:`refresh` and :meth:`invalidate`
  take the RLock to bump versions atomically with the entry-dict swap.
* **Versioning.** :attr:`entry.version` bumps per-project on every
  refresh. ``global_version`` is the sum of all per-project bumps,
  monotone, used by readers to short-circuit ("nothing changed since
  last tick").

PR 1 ships the data structure + version semantics. The refresher
thread that drives :meth:`refresh` from audit-log events lives in
:mod:`pollypm.state_cache.refresher`.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from pollypm.state_cache.entry import ProjectStateCacheEntry, empty_entry

logger = logging.getLogger(__name__)

__all__ = ["ProjectStateCache", "RefreshFn"]


# The refresher provides this callable. It is the only path from
# audit-event → recomputed entry. Kept as a callable injection point
# (rather than a hard import of the per-project query) so PR 2 can
# swap the real implementation in without touching the cache class.
RefreshFn = Callable[[str], Optional[ProjectStateCacheEntry]]


def _default_refresh(project_key: str) -> Optional[ProjectStateCacheEntry]:
    """Stub refresh used until PR 2 wires the real per-project query.

    Returns an empty entry so the version-bump bookkeeping still
    works. The env-flag-off shim short-circuits before any of this
    runs in production (see :func:`pollypm.state_cache.get_cache`).
    """

    return empty_entry(project_key)


class ProjectStateCache:
    """The shared in-process project-state cache.

    A single instance lives at module scope (see
    :func:`pollypm.state_cache.get_cache`). The cockpit, rail, and
    dashboard panes all consume the same instance.
    """

    def __init__(self, refresh_fn: RefreshFn | None = None) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, ProjectStateCacheEntry] = {}
        # Monotone counter: bumps on every successful refresh().
        # Readers compare this to a remembered value to decide
        # whether to skip a render cycle.
        self._global_version = 0
        # Per-project version bumps so callers can detect a single
        # project changing without diffing the whole snapshot.
        self._versions: dict[str, int] = {}
        # Pending invalidation set — projects whose next read should
        # force a refresh. Coalesced: N invalidations between
        # refreshes collapse to one. The refresher drains this.
        self._pending: set[str] = set()
        self._refresh_fn: RefreshFn = refresh_fn or _default_refresh

    # ── reads — lock-free ───────────────────────────────────────────

    def get(self, project_key: str) -> ProjectStateCacheEntry | None:
        """Return the current entry for ``project_key`` or ``None``.

        Lock-free dict get. The returned entry (if any) is frozen, so
        the caller can hold it across the next refresh without
        observing a torn read.
        """

        return self._entries.get(project_key)

    def snapshot(self) -> dict[str, ProjectStateCacheEntry]:
        """Return a shallow copy of the current entry dict.

        O(N) where N is the number of cached projects. Returned dict
        is independent of the cache's internal storage; the values
        remain shared (and immutable). Lock-free: dict.copy() on
        CPython is GIL-atomic for our access pattern.
        """

        return self._entries.copy()

    def version(self, project_key: str) -> int:
        """Per-project monotone version. ``0`` if never refreshed."""

        return self._versions.get(project_key, 0)

    def global_version(self) -> int:
        """Sum of all per-project version bumps. Monotone."""

        return self._global_version

    def pending_invalidations(self) -> frozenset[str]:
        """Diagnostic — read of the pending set without draining."""

        with self._lock:
            return frozenset(self._pending)

    # ── writes — RLock-guarded ──────────────────────────────────────

    def invalidate(self, project_key: str | None = None) -> None:
        """Mark ``project_key`` (or all known projects) as needing refresh.

        Coalesced: subsequent invalidations of the same key before
        the refresher drains the queue collapse to one refresh.

        ``project_key=None`` enqueues every currently-known project
        (used for workspace-wide events with empty project payloads).
        Brand-new projects are picked up the next time a refresh
        discovers them — invalidate does not synthesize unknown keys.
        """

        with self._lock:
            if project_key is None:
                # Workspace-scoped invalidation. Enqueue every project
                # we already know about; per §9.3 the refresher
                # rediscovers the candidate set on every full pass,
                # so unknown projects join on the next refresh.
                self._pending.update(self._entries.keys())
                self._pending.update(self._versions.keys())
                return
            if not project_key:
                # Empty-string guard — never invalidate the "" key.
                return
            self._pending.add(project_key)

    def refresh(self, project_key: str) -> ProjectStateCacheEntry | None:
        """Recompute and atomically install the entry for ``project_key``.

        Calls the injected :data:`RefreshFn`; on success the new
        entry is installed and the per-project + global version
        counters bump. On failure (refresh function returns ``None``
        or raises) the existing entry is left in place and the
        pending flag is cleared (failures don't loop indefinitely;
        the next invalidation re-enqueues).
        """

        with self._lock:
            # Drop the pending flag up front. If the refresh raises
            # we still want to advance — the next legitimate event
            # will re-enqueue.
            self._pending.discard(project_key)
            try:
                entry = self._refresh_fn(project_key)
            except Exception:  # noqa: BLE001 — refresher MUST be best-effort
                logger.exception(
                    "state_cache: refresh failed for %s", project_key,
                )
                return None
            if entry is None:
                return None
            next_version = self._versions.get(project_key, 0) + 1
            # Re-stamp the entry's version so readers see a payload
            # whose ``entry.version`` matches the cache's per-project
            # counter. The stub may have set version=0; this is the
            # canonical place where the bump becomes authoritative.
            installed = _restamp_version(entry, next_version)
            self._entries[project_key] = installed
            self._versions[project_key] = next_version
            self._global_version += 1
            return installed

    def drain_pending(self) -> list[str]:
        """Pop and return all currently-pending project keys.

        Used by the refresher's worker tick. RLock-guarded so a
        concurrent :meth:`invalidate` either lands in this drain or
        in the next one — never lost.
        """

        with self._lock:
            drained = sorted(self._pending)
            self._pending.clear()
            return drained

    # ── test support ───────────────────────────────────────────────

    def _install_for_test(
        self, project_key: str, entry: ProjectStateCacheEntry,
    ) -> None:
        """Direct entry install bypassing the refresh function.

        Test-only helper; production code goes through :meth:`refresh`.
        """

        with self._lock:
            next_version = self._versions.get(project_key, 0) + 1
            self._entries[project_key] = _restamp_version(entry, next_version)
            self._versions[project_key] = next_version
            self._global_version += 1


def _restamp_version(
    entry: ProjectStateCacheEntry, version: int,
) -> ProjectStateCacheEntry:
    """Return a copy of ``entry`` with its ``version`` field updated.

    Frozen dataclasses can't be mutated; ``dataclasses.replace``
    constructs a fresh instance sharing the rest of the payload.
    """

    from dataclasses import replace

    return replace(entry, version=version)
