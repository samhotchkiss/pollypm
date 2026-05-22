"""Cache-vs-direct divergence sampler (historical / no-op).

Move A PR 4 (#1664, design §6.4) flipped ``POLLYPM_STATE_CACHE`` to
ON by default and made the cache authoritative. As of PR
[#2029](https://github.com/samhotchkiss/pollypm/issues/2029) this
module is kept for historical reference and possible future
re-enablement, but does NOTHING in production:

* :meth:`DivergenceCounter.should_sample` returns ``False``
  unconditionally — the in-process 1-in-N parity sampler has been
  removed. Routed call sites guard on ``is_enabled()`` and return
  early when the kill-switch is set, so the legacy "sample when
  kill-switch is set" branch was unreachable anyway.
* The ``compare_*`` helpers + :func:`log_divergence` are still
  exported because tests use them to verify bulk parity between the
  cached and direct facades; production never calls them.

Parity is now verified by the test suite (bulk equivalence checks on
fixture-backed configs), not by runtime sampling.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "DIVERGENCE_SAMPLE_RATE",
    "DivergenceCounter",
    "compare_awaits_user_lists",
    "compare_state_maps",
    "log_divergence",
]


# 1-in-N sampling cadence. Historical / test-only. Production no-ops
# as of PR4 (#2029). Tests override by constructing their own
# :class:`DivergenceCounter`.
DIVERGENCE_SAMPLE_RATE = 50


class DivergenceCounter:
    """Deterministic no-op sampler (PR #2029).

    Originally a thread-safe 1-in-N sampler that ran the direct path
    alongside the cache for parity telemetry. PR #2029 removed the
    sampling branch entirely:

    * Routed call sites (cockpit_inbox, operator_view, cockpit_rail,
      state_map) short-circuit via ``is_enabled()`` when the
      kill-switch is set, so the "sample when kill-switch is set"
      branch could never fire in production.
    * Bulk parity is verified by the test suite, not by in-process
      sampling.

    The class is preserved (and still constructable with ``rate=`` /
    ``always=``) so existing route-site sentinels and tests that
    instantiate it don't need to change. :meth:`should_sample` is
    deterministic-off in production. ``always=True`` is the test
    escape hatch that restores legacy 1-in-N behavior for parity
    tests that need to drive the comparison helpers.
    """

    __slots__ = ("_rate", "_counter", "_lock", "_always")

    def __init__(
        self, rate: int = DIVERGENCE_SAMPLE_RATE, *, always: bool = False,
    ) -> None:
        if rate < 1:
            raise ValueError(f"sample rate must be >= 1, got {rate!r}")
        self._rate = rate
        self._counter = 0
        self._lock = threading.Lock()
        # Test-only escape hatch: when True, restores the legacy
        # every-Nth-call sampling so parity tests can still drive the
        # comparison helpers. Production code never sets this.
        self._always = always

    def should_sample(self) -> bool:
        """Return False in production; legacy Nth-call when ``always=True``.

        PR #2029: deterministic-off in production. The kill-switch
        branch was removed because routed call sites already return
        early when the kill-switch is set, making that branch
        unreachable.
        """

        if not self._always:
            return False
        with self._lock:
            self._counter += 1
            # Sample the Nth call (not the 1st) so cold-start traffic
            # doesn't dominate the sample set.
            return self._counter % self._rate == 0

    def reset(self) -> None:
        """Test helper — reset the counter to 0."""

        with self._lock:
            self._counter = 0


# ── comparison helpers ─────────────────────────────────────────────


def _project_key(item: Any) -> str:
    """Best-effort project key from an inbox-entry-like object."""

    project = getattr(item, "project", "") or getattr(item, "scope", "")
    return str(project or "")


def _item_identity(item: Any) -> tuple[str, str, str]:
    """Identity tuple used to compare two awaits-user items.

    Items come from heterogeneous sources (tasks, store messages); we
    compare on the canonical (project, source, id) triple that both
    surfaces guarantee. Title / timestamps are intentionally excluded
    — they're populated by the same downstream code path in either
    branch, so a difference there would point at the populator, not
    at the cache.
    """

    project = _project_key(item)
    source = str(getattr(item, "source", "") or "")
    # Tasks carry ``task_id``; messages carry ``message_id``. Either
    # one is unique within (project, source); the other is None.
    ident = (
        getattr(item, "task_id", None)
        or getattr(item, "message_id", None)
        or ""
    )
    return (project, source, str(ident))


def compare_awaits_user_lists(
    cached: list[Any], direct: list[Any],
) -> tuple[bool, str]:
    """Return ``(matched, reason)`` for two awaits-user lists.

    Tolerant of ordering — the cached path concatenates per-project
    entry tuples while the direct path runs a workspace-wide sweep,
    so the order legitimately differs. Strict on contents: identity
    sets must match. A length mismatch is reported with both counts
    so the WARN line is debuggable without re-running.
    """

    cached_set = {_item_identity(item) for item in cached}
    direct_set = {_item_identity(item) for item in direct}
    if cached_set == direct_set:
        return True, ""
    missing_from_cache = direct_set - cached_set
    extra_in_cache = cached_set - direct_set
    parts = [
        f"len(cached)={len(cached)}",
        f"len(direct)={len(direct)}",
    ]
    if missing_from_cache:
        parts.append(f"missing_from_cache={sorted(missing_from_cache)[:5]}")
    if extra_in_cache:
        parts.append(f"extra_in_cache={sorted(extra_in_cache)[:5]}")
    return False, "; ".join(parts)


def compare_state_maps(
    cached: dict[str, Any], direct: dict[str, Any],
) -> tuple[bool, str]:
    """Return ``(matched, reason)`` for two ``{project: state}`` dicts.

    Equality on both key set and per-key value. State values may be
    enum-typed; we coerce to their ``.value`` (string) when present
    so a cache populated from the enum and a direct call returning
    the enum compare equal without object-identity coupling.
    """

    def _normalize(value: Any) -> str:
        return str(getattr(value, "value", value) or "")

    cached_n = {key: _normalize(val) for key, val in cached.items()}
    direct_n = {key: _normalize(val) for key, val in direct.items()}
    if cached_n == direct_n:
        return True, ""
    only_cached = set(cached_n) - set(direct_n)
    only_direct = set(direct_n) - set(cached_n)
    differing = {
        k for k in set(cached_n) & set(direct_n)
        if cached_n[k] != direct_n[k]
    }
    parts: list[str] = []
    if only_cached:
        parts.append(f"only_in_cache={sorted(only_cached)[:5]}")
    if only_direct:
        parts.append(f"only_in_direct={sorted(only_direct)[:5]}")
    if differing:
        sample = sorted(differing)[:5]
        parts.append(
            "differing="
            + ", ".join(
                f"{k}(cache={cached_n[k]!r},direct={direct_n[k]!r})"
                for k in sample
            )
        )
    return False, "; ".join(parts) or "maps differ"


def log_divergence(call_site: str, reason: str) -> None:
    """Emit the canonical WARN line for a sampled mismatch.

    Centralised so log scrapers can pin a single string template
    (``state_cache: divergence at <site>: <reason>``). Pre-PR4 design
    rationale; superseded — no runtime sampler in production.
    """

    logger.warning("state_cache: divergence at %s: %s", call_site, reason)
