"""In-process project-state cache (Move A).

See ``docs/design/move-a-state-cache.md`` for the design (issue
[#1664](https://github.com/samhotchkiss/pollypm/issues/1664)).

This package ships the entry dataclass, cache class,
audit-log-driven refresher, and the env-flag-gated module-level
accessor. As of PR
[#2029](https://github.com/samhotchkiss/pollypm/issues/2029) the
cache is **authoritative when on** and five call sites are wired:
``cockpit_inbox`` list + count, ``dashboard.operator_view`` rollup,
``cockpit_rail`` rollups, and ``project_state_map_from_config``.

Current contract:

* :envvar:`POLLYPM_STATE_CACHE` defaults **ON** (PR 4 flip). Set
  it to a falsy value (``0`` / ``false`` / ``no`` / ``off``) as a
  kill-switch to fall through to the direct facade — emergency
  rollback is a process restart with the env var set.
* When the flag is on, :func:`get_cache` returns the real cache;
  the refresher starts on the first :func:`get_cache` call (import
  itself is still side-effect-free).
* When the kill-switch is set, :func:`get_cache` returns a no-op
  shim whose ``snapshot()`` is ``{}`` and ``get()`` is ``None``,
  and routed call sites short-circuit via :func:`is_enabled` to
  the direct path before touching the cache.
* The cache is authoritative when on; there is no in-process
  divergence sampling. Parity between the cached and direct
  facades is enforced by the test suite (bulk equivalence checks),
  not by runtime sampling. See ``divergence.py`` for the
  historical helpers retained for those tests.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Protocol

from pollypm.state_cache.divergence import (
    DIVERGENCE_SAMPLE_RATE,
    DivergenceCounter,
    compare_awaits_user_lists,
    compare_state_maps,
    log_divergence,
)
from pollypm.state_cache.entry import ProjectStateCacheEntry, empty_entry
from pollypm.state_cache.project_state_cache import ProjectStateCache, RefreshFn
from pollypm.state_cache.refresh_impl import (
    build_refresh_fn,
    compute_entry_for_project,
)
from pollypm.state_cache.refresher import StateCacheRefresher, stub_refresh_fn

logger = logging.getLogger(__name__)

# Env flag — single source of truth for "is the cache live?". Read
# on every :func:`get_cache` call so tests can toggle without having
# to reload the module.
ENV_FLAG = "POLLYPM_STATE_CACHE"

__all__ = [
    "DIVERGENCE_SAMPLE_RATE",
    "DivergenceCounter",
    "ENV_FLAG",
    "ProjectStateCache",
    "ProjectStateCacheEntry",
    "RefreshFn",
    "StateCacheLike",
    "StateCacheRefresher",
    "build_refresh_fn",
    "compare_awaits_user_lists",
    "compare_state_maps",
    "compute_entry_for_project",
    "empty_entry",
    "get_cache",
    "get_refresher",
    "is_enabled",
    "log_divergence",
    "reset_for_test",
]


# ── flag plumbing ──────────────────────────────────────────────────


def _flag_falsy(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"0", "false", "no", "off"}


def is_enabled() -> bool:
    """Return True iff the cache is enabled by env at call time.

    Move A PR 4 (#1664, design §6.4): default is now **ON**. The env
    var stays as a kill-switch — ``POLLYPM_STATE_CACHE=0`` (or any
    other falsy spelling: ``false`` / ``no`` / ``off``) still
    disables for one release. PR 1-3 callers all preserve a
    flag-off fall-through, so an emergency rollback is a process
    restart with the kill-switch set.

    Unset / empty / unknown values → enabled.
    """

    raw = os.environ.get(ENV_FLAG)
    if _flag_falsy(raw):
        return False
    return True


# ── shim — used when the flag is off ───────────────────────────────


class StateCacheLike(Protocol):
    """Structural type satisfied by both the real cache and the shim.

    Call sites are coded against this protocol so they can hold a
    reference returned from :func:`get_cache` without knowing whether
    the flag was on or off when they acquired it.
    """

    def get(self, project_key: str) -> ProjectStateCacheEntry | None: ...

    def snapshot(self) -> dict[str, ProjectStateCacheEntry]: ...

    def version(self, project_key: str) -> int: ...

    def global_version(self) -> int: ...

    def invalidate(self, project_key: str | None = None) -> None: ...


class _DisabledStateCache:
    """No-op cache returned when the env flag is off.

    Matches :class:`StateCacheLike` so call sites can be written
    flag-agnostic; the shim short-circuits every read and absorbs
    invalidation calls. Cheap to construct and shared as a singleton.
    """

    __slots__ = ()

    def get(self, project_key: str) -> ProjectStateCacheEntry | None:
        return None

    def snapshot(self) -> dict[str, ProjectStateCacheEntry]:
        return {}

    def version(self, project_key: str) -> int:
        return 0

    def global_version(self) -> int:
        return 0

    def invalidate(self, project_key: str | None = None) -> None:
        return None


_DISABLED = _DisabledStateCache()


# ── lazy singleton ─────────────────────────────────────────────────


_singleton_lock = threading.Lock()
_cache_singleton: ProjectStateCache | None = None
_refresher_singleton: StateCacheRefresher | None = None


def get_cache() -> StateCacheLike:
    """Return the shared cache — real cache when enabled, shim otherwise.

    The real cache + refresher are constructed lazily on first call
    when the flag is on. The shim is a cheap module-level singleton.
    """

    if not is_enabled():
        return _DISABLED

    global _cache_singleton, _refresher_singleton
    if _cache_singleton is not None:
        return _cache_singleton

    with _singleton_lock:
        if _cache_singleton is None:
            # PR 2: wire the real per-project refresh through a
            # ``load_config``-backed provider. A failure to import
            # ``pollypm.config`` (e.g. during a circular-import
            # bootstrap, very unlikely at this leaf module's depth)
            # falls back to the PR 1 stub so the cache still loads.
            project_keys_provider: Callable[[], list[str]] | None = None
            try:
                from pollypm.config import DEFAULT_CONFIG_PATH, load_config

                def _config_provider() -> object:
                    return load_config(DEFAULT_CONFIG_PATH)

                def _project_keys_provider() -> list[str]:
                    # Re-read the config on every full-refresh / boot so
                    # newly-added projects are picked up without having
                    # to bounce the cache. Failures degrade to the empty
                    # list (matches the provider-less PR 1 behavior).
                    try:
                        config = _config_provider()
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "state_cache: project_keys provider — "
                            "config load failed",
                        )
                        return []
                    try:
                        projects = getattr(config, "projects", {}) or {}
                        return list(projects.keys())
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "state_cache: project_keys provider — "
                            "projects access failed",
                        )
                        return []

                refresh_fn = build_refresh_fn(_config_provider)
                project_keys_provider = _project_keys_provider
            except Exception:  # noqa: BLE001
                logger.exception(
                    "state_cache: real refresh wiring failed; "
                    "falling back to stub",
                )
                refresh_fn = stub_refresh_fn
            cache = ProjectStateCache(refresh_fn=refresh_fn)
            refresher = StateCacheRefresher(
                cache, project_keys=project_keys_provider,
            )
            try:
                refresher.start()
            except Exception:  # noqa: BLE001
                # The cache itself is still usable even if the
                # refresher fails to start. Better to serve an empty
                # cache than to crash a caller that just asked for a
                # snapshot.
                logger.exception(
                    "state_cache: refresher failed to start; "
                    "serving an unattended cache",
                )
            _cache_singleton = cache
            _refresher_singleton = refresher
    return _cache_singleton


def get_refresher() -> StateCacheRefresher | None:
    """Return the running refresher, or ``None`` when the flag is off.

    Lazy: calling :func:`get_cache` first ensures the refresher is
    constructed.
    """

    return _refresher_singleton


def reset_for_test() -> None:
    """Drop the cached singletons (test-only).

    Tests that flip the env flag between cases need a way to force
    the next :func:`get_cache` call to re-evaluate. Production code
    does not call this.
    """

    global _cache_singleton, _refresher_singleton
    with _singleton_lock:
        refresher = _refresher_singleton
        _cache_singleton = None
        _refresher_singleton = None
    if refresher is not None:
        try:
            refresher.stop()
        except Exception:  # noqa: BLE001
            logger.exception("state_cache: refresher.stop() raised on reset")
