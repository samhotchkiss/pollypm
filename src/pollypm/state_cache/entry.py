"""Frozen per-project state cache entry.

See ``docs/design/move-a-state-cache.md`` §3.2 for the design.

The entry is the **immutable snapshot** a reader sees. Refreshers
construct a fresh entry and atomically swap it into the cache dict;
readers never mutate one in place. The frozen-dataclass + tuple /
frozenset payload shape means a reader holding a reference cannot
observe a torn read, even without taking the cache lock.

In PR 1 (this slice) only the dataclass shape ships. PR 2 wires the
real per-project query that populates the rollup / inbox / heartbeat
fields. Until then, refreshers emit an :func:`empty_entry` for each
project key — the env-flag-off shim already returns ``None`` from
:meth:`ProjectStateCache.get`, so no production code consumes the
empty payload yet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["ProjectStateCacheEntry", "empty_entry"]


@dataclass(frozen=True, slots=True)
class ProjectStateCacheEntry:
    """Immutable per-project snapshot used by rail + dashboard readers.

    The shape mirrors §3.2 of the Move A design doc. Concrete types
    are kept loose (``Any`` / generic tuples) here because the
    consumer types (``InboxEntry``, ``WorkerSessionRow``,
    ``HeartbeatRecord``, ``ProjectState``, ``ProjectRailState``) live
    in modules that import a lot of the cockpit / dashboard surface;
    importing them from a leaf ``state_cache`` package would create
    cycles. Call sites that read entries already hold their own
    typed references and can narrow on consumption — the entry is a
    transport object, not the authoritative type definition.

    All payload fields default to the "no data yet" sentinel so the
    PR 1 stub refresher can produce a usable entry without yet
    knowing the call-site contracts. PR 2 fills in real values.
    """

    project_key: str
    project_path: Path
    tracked: bool = False
    db_paths: tuple[Path, ...] = ()

    # ── categorization output ──────────────────────────────────────
    state: Any = None              # ProjectState | None
    glyph: str = ""
    detail: str = ""

    # ── rail rollup output ─────────────────────────────────────────
    rail_state: Any = None         # ProjectRailState | None
    rail_badge: str | None = None
    rail_sort_rank: int = 0
    rail_reason: str = ""
    approvals_pending: int = 0
    plan_blocked: bool = False

    # ── awaits-user list (the expensive one) ───────────────────────
    awaits_user_count: int = 0
    awaits_user_items: tuple[Any, ...] = ()

    # ── what_working() inputs (only meaningful when state == WORKING)
    working_agent_name: str = ""
    working_task_title: str = ""

    # ── heartbeats / live workers ──────────────────────────────────
    live_worker_sessions: tuple[Any, ...] = ()
    # Populated when #2050 lands a heartbeat invalidation contract.
    latest_heartbeat_by_session: dict[str, Any] = field(default_factory=dict)

    # ── task statuses (recompute rail glyphs without re-opening) ───
    task_status_counts: dict[str, int] = field(default_factory=dict)
    on_hold_task_ids: frozenset[str] = frozenset()
    review_task_ids: frozenset[str] = frozenset()

    # ── versioning ────────────────────────────────────────────────
    version: int = 0
    computed_at: float = 0.0
    source_db_path: Path | None = None


def empty_entry(
    project_key: str,
    project_path: Path | None = None,
    *,
    version: int = 0,
) -> ProjectStateCacheEntry:
    """Construct a deterministic "no data yet" entry for ``project_key``.

    Used by the PR 1 stub refresher. The entry is fully formed
    (frozen, hashable-by-identity-of-fields where applicable) so
    downstream code can treat it like a real entry once PR 2 lands.
    """

    return ProjectStateCacheEntry(
        project_key=project_key,
        project_path=project_path if project_path is not None else Path(""),
        version=version,
        computed_at=time.monotonic(),
    )
