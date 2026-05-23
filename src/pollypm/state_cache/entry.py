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

__all__ = [
    "WORKSPACE_ENTRY_TTL_SECONDS",
    "WORKSPACE_PROJECT_KEY",
    "ProjectStateCacheEntry",
    "config_identity",
    "empty_entry",
]


# Sentinel project key for the "workspace-root inbox" entry (#2051).
# Workspace-root messages (``scope IN ('', 'inbox')``) carry
# ``project == "inbox"`` after :func:`message_row_to_inbox_entry`, so
# they don't associate with any tracked project key. The refresher
# emits a synthetic entry under this sentinel so the cache fast-path
# can surface those items alongside per-project ones. Matches the
# established ``__workspace__`` sentinel used by other inbox code paths
# (see ``cockpit_inbox_items._WORKSPACE_DB_KEY``).
#
# Lives in :mod:`pollypm.state_cache.entry` (a dependency-free leaf
# module) so :mod:`pollypm.state_cache.refresh_impl`,
# :mod:`pollypm.state_cache.refresher`, :mod:`pollypm.cockpit_inbox`,
# and the test suite all share one source of truth. Splitting the
# constant across modules (as in earlier rounds) risked drift when one
# call site moved off ``"__workspace__"`` without the other.
WORKSPACE_PROJECT_KEY = "__workspace__"


# Bounded-staleness TTL for the synthetic workspace sentinel (#2051
# round-4 Codex review). A non-empty ``__workspace__`` entry is only
# treated as authoritative for this many monotonic seconds after the
# refresher stamped it. After the window expires the cache-read
# boundary falls through to the direct sweep — see
# ``cockpit_inbox._maybe_cache_route_awaits_user`` and
# ``_maybe_cache_count_awaits_user``.
#
# Rationale: several message-store paths
# (``PgStore.close_message``, ``PgStore.clear_alert``,
# ``service_api.v1.clear_alert``) mutate workspace-root awaits-user
# rows but do NOT emit a ``state-cache`` audit event the refresher's
# ``_dispatch_event`` consumes. Without a TTL a cached non-empty
# sentinel can serve closed rows or an inflated count indefinitely.
#
# 10 seconds was picked as a compromise: short enough that any close-
# then-read sequence the operator notices stays inside one rail tick
# window (rail polls ~1s); long enough that the cache still absorbs
# bursty consumer reads inside a single refresh cycle. Full audit-
# event wiring for the message-store close/clear paths is deferred
# past v1 RC — this TTL is a documented bounded-staleness window,
# not an invariant.
WORKSPACE_ENTRY_TTL_SECONDS = 10.0


def config_identity(config: Any) -> str:
    """Return a canonical string identity for ``config``.

    PR #2026 v7 (Codex r7 blocker): the cache singleton can serve
    cross-config data when two ``PollyPMConfig`` objects share project
    keys but were loaded from different on-disk roots. Every cache
    lookup must compare this identity against the snapshot's stamped
    identity and decline on mismatch.

    Identity preference order:

    1. ``config.config_path`` (resolved) — when a future refactor adds
       this attribute, prefer it (the file on disk IS the identity).
       :func:`pollypm.supervisor.SupervisorWorker._reviewer_auto_provision`
       already probes for it via ``getattr`` for the same reason.
    2. ``config.project.workspace_root`` (resolved) — every loaded
       ``PollyPMConfig`` has one; it's the workspace root the config
       was parsed against. Two configs with overlapping project keys
       but different workspace roots are exactly the
       cross-config-leak case this guard exists to catch.
    3. Empty string — fall through. The guard treats two empties as
       equal so test fixtures that don't set either field still work;
       production configs always have a workspace_root.
    """

    try:
        cfg_path = getattr(config, "config_path", None)
        if cfg_path is not None:
            try:
                return str(Path(cfg_path).resolve())
            except Exception:  # noqa: BLE001
                return str(cfg_path)
    except Exception:  # noqa: BLE001
        pass
    try:
        project = getattr(config, "project", None)
        if project is not None:
            wsr = getattr(project, "workspace_root", None)
            if wsr:
                try:
                    return str(Path(wsr).resolve())
                except Exception:  # noqa: BLE001
                    return str(wsr)
    except Exception:  # noqa: BLE001
        pass
    return ""


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
    # #2049 — ``actionable_key`` is the rail-route id
    # (``project:<key>:issues``) that ``rollup_project_state`` returns
    # when an alerted task drives the rollup. Stored on the entry so the
    # cache fast-path can serve a rollup that matches the direct path
    # even when a live ``stuck_on_task:`` / ``no_session_for_assignment:``
    # alert is present. Without this field the alert overlay had to be
    # re-applied at read time (which requires task-status info the entry
    # didn't carry) — the PR #2026 workaround declined the cache for any
    # render with a tracked-project actionable alert. The refresher now
    # folds alerts into the entry at compute time so the read path is
    # authoritative.
    actionable_key: str | None = None
    # #2049 follow-up (Codex blocker on PR #2085): stamp whether the
    # alert snapshot used to compute ``rail_state`` / ``actionable_key``
    # was successfully read. ``_open_alerts_for`` previously turned every
    # supervisor/store/open-alerts failure into an empty list, which
    # meant a transient refresher-side alert read failure could install
    # a no-alert WORKING/NONE rollup; a later render whose
    # ``supervisor.open_alerts()`` succeeds would still serve that
    # stale-from-failure entry and hide the actionable RED alert. When
    # this flag is ``False`` the cache fast-path
    # (:meth:`_maybe_cache_route_rollups`) MUST decline — the entry
    # can't speak to the alert state, so falling through to the direct
    # path is the only way to honour the live alert read. Defaults to
    # ``True`` so legacy / hand-rolled fixtures (and the success path)
    # serve normally.
    alerts_snapshot_valid: bool = True

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

    # ── config identity (PR #2026 v7) ─────────────────────────────
    # Stamped by the refresher at compute time (see
    # ``refresh_impl.compute_entry_for_project``). Every cache-routed
    # call site MUST compare this against the live config's identity
    # (via :func:`config_identity`) before consuming the entry — when
    # they disagree the entry was computed against a different config
    # and serving it would leak cross-config data. Defaults to ``""``
    # so legacy fixtures that construct entries by hand still satisfy
    # the guard against a config with no identity (e.g. unit tests).
    config_identity: str = ""


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
