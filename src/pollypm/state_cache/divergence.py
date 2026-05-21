"""Cache-vs-direct divergence sampler.

Per ``docs/design/move-a-state-cache.md`` §6.2 (last bullet) + §7
(cache-mismatch risk row), each routed call site samples 1-in-N
calls and runs BOTH the cached path and the direct (uncached) path,
then compares the results. A mismatch logs a WARN line so the
divergence shows up in telemetry — the cache is held to a strict
"never silently drift from the source of truth" bar.

The sample-rate constant lives here so the §6.2 PR 4 telemetry gate
("0 WARN lines for 24h") has exactly one tuning knob. The default
N=50 matches the design's "1-in-50 sampled call" recommendation —
enough samples to catch sustained drift inside a single refresh
cycle (~250ms tail tick × 50 = ~12.5s coverage window) without
doubling the per-tick cost.

This module deliberately holds no state about which call site fired:
each routed helper owns its own ``_should_sample()`` counter so a
quiet site doesn't piggyback on a busy site's samples. The sampler
returns a deterministic bool from a thread-safe counter so the test
suite can inject mismatches at known intervals without flakiness.
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


# 1-in-N sampling cadence. Constant so the §8 acceptance gate ("0
# WARN lines for 24h") has a single, reviewable knob. Tests override
# by constructing their own :class:`DivergenceCounter`.
DIVERGENCE_SAMPLE_RATE = 50


class DivergenceCounter:
    """Thread-safe 1-in-N sampler used by each routed call site.

    A fresh counter per call site keeps the sampling independent —
    a quiet helper doesn't borrow samples from a busy one. The first
    call after construction does NOT sample (we want the sample to
    land mid-cycle, not on cold start when the cache is empty by
    construction and a divergence would be expected).

    Move A PR 4 (#1664, design §6.4): the parity-debugging window is
    over once the flag default flips to ON — the cache is now the
    authoritative source. :meth:`should_sample` returns False
    whenever the kill-switch is NOT set (i.e. cache is the default),
    so routed call sites stop paying the cost of running the direct
    path alongside the cache. Tests pin the always-sample path by
    constructing a counter with ``always=True`` or by setting the
    kill-switch env var.
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
        # Test override: when set, ``should_sample`` ignores the
        # PR 4 "no-op when cache authoritative" suppression and
        # samples every Nth call as before. Production code never
        # passes this — only PR 2's parity tests do.
        self._always = always

    def should_sample(self) -> bool:
        """Return True every Nth call. Thread-safe.

        Move A PR 4: returns False whenever the cache is
        authoritative (kill-switch not set). Pass ``always=True`` at
        construction to force the legacy always-sample behavior for
        tests.
        """

        if not self._always and not _kill_switch_set():
            # Cache is authoritative — skip the parity sample.
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


def _kill_switch_set() -> bool:
    """Return True iff ``POLLYPM_STATE_CACHE`` is set to a falsy value.

    Move A PR 4: the cache default flipped to ON, so the divergence
    sampler is silent EXCEPT when the operator has explicitly set
    the kill-switch. That's the only condition under which we still
    care about parity telemetry — the operator is presumably
    debugging a cache vs direct mismatch, and we want both paths to
    run + compare while they triage.

    Defined locally (no import from ``pollypm.state_cache``) to
    avoid the leaf-module circular import.
    """

    import os

    raw = os.environ.get("POLLYPM_STATE_CACHE")
    if raw is None:
        return False
    return raw.strip().lower() in {"0", "false", "no", "off"}


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
    (``state_cache: divergence at <site>: <reason>``). PR 4 will
    promote this to an alert once it has been silent for 14 days.
    """

    logger.warning("state_cache: divergence at %s: %s", call_site, reason)
