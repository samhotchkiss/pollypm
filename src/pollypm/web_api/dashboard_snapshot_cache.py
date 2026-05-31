"""Stale-while-refresh cache for expensive dashboard snapshots."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class DashboardSnapshot:
    generated_at: datetime
    projects: tuple[Any, ...]
    data: Any
    refreshed_at_monotonic: float


class DashboardSnapshotCache:
    """Bounded stale-while-refresh cache for dashboard API snapshots."""

    def __init__(
        self,
        *,
        stale_after_seconds: float = 10.0,
        max_stale_seconds: float = 60.0,
    ) -> None:
        if stale_after_seconds < 0:
            raise ValueError("stale_after_seconds must be non-negative")
        if max_stale_seconds < stale_after_seconds:
            raise ValueError("max_stale_seconds must be >= stale_after_seconds")
        self._stale_after_seconds = stale_after_seconds
        self._max_stale_seconds = max_stale_seconds
        self._snapshots: dict[int, DashboardSnapshot] = {}
        self._refreshing: set[int] = set()
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._snapshots.clear()
            self._refreshing.clear()

    def requires_sync_refresh(self, config: Any) -> bool:
        """Return True when ``get_or_refresh`` would block on a reload."""
        key = id(config)
        with self._lock:
            snapshot = self._snapshots.get(key)
            if snapshot is None:
                return True
            age = time.monotonic() - snapshot.refreshed_at_monotonic
            return age > self._max_stale_seconds

    async def get_or_refresh(
        self,
        config: Any,
        loader: Callable[[Any], DashboardSnapshot],
    ) -> DashboardSnapshot:
        """Return a cached snapshot, refreshing in the background when stale.

        The first call, or a call after the bounded stale window has
        expired, waits for ``loader`` and propagates failures. Calls with
        a stale-but-usable snapshot return immediately while one daemon
        thread updates the cache.
        """
        key = id(config)
        with self._lock:
            snapshot = self._snapshots.get(key)
            if snapshot is not None:
                age = time.monotonic() - snapshot.refreshed_at_monotonic
                if age <= self._stale_after_seconds:
                    return snapshot
                if age <= self._max_stale_seconds:
                    self._start_background_refresh_locked(key, config, loader)
                    return snapshot

        return await asyncio.to_thread(self._refresh_sync, key, config, loader)

    def _start_background_refresh_locked(
        self,
        key: int,
        config: Any,
        loader: Callable[[Any], DashboardSnapshot],
    ) -> None:
        if key in self._refreshing:
            return
        self._refreshing.add(key)
        thread = threading.Thread(
            target=self._refresh_background,
            name="dashboard-snapshot-refresh",
            args=(key, config, loader),
            daemon=True,
        )
        thread.start()

    def _refresh_background(
        self,
        key: int,
        config: Any,
        loader: Callable[[Any], DashboardSnapshot],
    ) -> None:
        try:
            self._refresh_sync(key, config, loader)
        except Exception:
            logger.warning(
                "dashboard snapshot refresh failed for cache key %r",
                key,
                exc_info=True,
            )
        finally:
            with self._lock:
                self._refreshing.discard(key)

    def _refresh_sync(
        self,
        key: int,
        config: Any,
        loader: Callable[[Any], DashboardSnapshot],
    ) -> DashboardSnapshot:
        snapshot = loader(config)
        with self._lock:
            self._snapshots[key] = snapshot
            self._refreshing.discard(key)
        return snapshot


__all__ = ["DashboardSnapshot", "DashboardSnapshotCache"]
