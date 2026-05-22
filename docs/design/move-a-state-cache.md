# Move A — In-Process Project-State Cache with Epoch-Driven Invalidation

## Implementation drift (2026-05-22)

The shipped Move A behavior diverges from the original design in three places. This section is the source of truth; the original sections below are kept for historical context but should be read as superseded where they conflict.

- `latest_heartbeat_by_session` is reserved on `ProjectStateCacheEntry` but NOT populated. Rail heartbeat sites read `pollypm.storage.pg_heartbeats.latest_heartbeat` directly via `CockpitRouter._latest_heartbeat_cached`. Bulk prefetch + cache invalidation deferred to [#2050](https://github.com/samhotchkiss/pollypm/issues/2050).
- `_maybe_cache_route_rollups` declines (returns `None`) when any tracked project has a live actionable alert — cache cannot recompute the alert-to-rollup contract. Tracked in [#2049](https://github.com/samhotchkiss/pollypm/issues/2049).
- `_maybe_cache_route_awaits_user` / `_maybe_cache_count_awaits_user` decline when the workspace-root inbox has any open message (via `has_workspace_root_open_messages` probe). Workspace-root inbox isn't yet represented in cache entries. Tracked in [#2051](https://github.com/samhotchkiss/pollypm/issues/2051).

Design document for issue [#1664](https://github.com/samhotchkiss/pollypm/issues/1664).
Status: **proposed** (design only; implementation deferred to post-RC).
Authors: Sam, with research from the 2026-05-18 rail-perf review on [#1634](https://github.com/samhotchkiss/pollypm/issues/1634).

> **Note (2026-05-19, pre-PR-1):** This doc was written 2026-05-18, before
> the postgres cutover (#1737) completed. Where the prose says "sqlite open"
> or "60-100 sqlite opens per refresh," read it as **per-project postgres
> queries from the hot path**. The architecture (audit-log tail →
> in-process snapshot → lock-free reads) is unchanged; only the label on
> the bottleneck changed. The §2.1 hot-path table still names the right
> call sites — they now open a pg connection / run a per-project query
> instead of opening sqlite.

This document describes a concrete design that an implementor can pick up
post-v1-RC. It does **not** ship the cache. It is the resolution to the design
ticket; the implementation lands as a separate sequence of PRs (see §6).

---

## 1. Problem statement

The 2026-05-18 perf review on the rail-perf meta (#1634) traced the user-felt
cold-load and stuttery rail to **per-project query fanout from multiple
surfaces with no shared cache**. Concretely, a single rail rebuild on a
12-project workspace performs ~60-100 per-project work-service queries
(historically sqlite opens; post-#1737 postgres queries against the shared
state DB). Prior wedges (#1605, #1608, #1630) moved that work off the main
thread but did not reduce wall-clock cost; ~50% of asyncio-task time is
still parked on the RLock that hands worker-thread results back to the UI.

The review proposed three architectural moves prioritized A → C → B. **Move A**
is the smallest-scope, biggest user-felt win:

> In-process project-state cache with epoch-driven invalidation. ~2 days.
> Kills 60-100 per-project DB queries per refresh.

This doc is the design artifact for Move A. The output of #1664 is **this
document**; the implementation work is tracked separately as the PR sequence
in §6.

### Measured baseline (cited from the perf review)

| Surface | Cold load | Steady refresh | Per-project queries / refresh |
|---|---|---|---|
| Operator dashboard, 12 projects | ~12s under load, 1-2s quiet | ~600ms | 30-50 |
| Rail rebuild, 12 projects | ~800ms | ~250-400ms | 30-50 (overlap w/ dashboard) |
| `pm_inbox_awaits_user_list` | n/a | called per rail tick | 24 (= 2 × N projects) |

Acceptance targets (also in §8): dashboard cold mount <300ms median quiet,
<2s under load; rail rebuild issues zero per-project DB queries directly.

---

## 2. Current state — how project state is read today

All paths converge on a per-project `WorkService` (sqlalchemy or pg-backed)
created from a `Config` lookup. The construction of that service is the
expensive bit: `_open_work_service` walks a canonical-then-legacy candidate
list, `stat()`s each, and opens whichever wins.

### 2.1 Call sites that open per-project DBs from a hot path

The seven hottest paths, by file:line. These are the ones Move A optimizes.

| # | File:line | Helper | Per-refresh cost |
|---|---|---|---|
| 1 | `src/pollypm/cockpit_inbox.py:130-265` | `pm_inbox_awaits_user_list` | 1× `create_work_service` + 1× `SQLAlchemyStore` per project |
| 2 | `src/pollypm/cockpit_inbox.py:268-295` | `_count_inbox_tasks_for_label` | wraps #1; same fan-out |
| 3 | `src/pollypm/cockpit_inbox.py:298-…` | `pm_inbox_filtered_list` | same fan-out as #1, with a `kind_filter` |
| 4 | `src/pollypm/dashboard/operator_view.py:189-201` | `load_operator_view` / `load_operator_view_from_config` | `_parallel_scan_rows` opens 1-2 svc per project |
| 5 | `src/pollypm/dashboard/operator_view.py:359-400` | `project_state_map_from_config` | parallel fan-out, called every rail tick |
| 6 | `src/pollypm/cockpit_rail.py:1806-1855` | `_project_categorizations` | calls #5 behind a 2s TTL (`_PROJECT_CATEGORIZATIONS_TTL_SECONDS`) |
| 7 | `src/pollypm/cockpit_rail.py:1857-1880` | `_project_state_rollups` | calls `_project_tasks_for_rollup` per project |
| 8 | `src/pollypm/cockpit_rail.py:1882-2049` | `_project_tasks_for_rollup` | up to 3 DBs per project |
| 9 | `src/pollypm/cockpit_rail.py:1474-1490, 2200-2220` | `latest_heartbeat()` per project | one sqlite query × N |

### 2.2 Cold supporting helpers (not in the hot loop today; left alone in Move A)

- `_build_project_pm_primer` / `_build_operator_primer`
  (`src/pollypm/cockpit_rail.py:540, 657`) — fire-once on PM-chat mount.
- Editor / save paths in `src/pollypm/cockpit_ui.py:3982, 12173, 12383,
  13250, 13465, 13782, 13946, 17815` — interactive write paths; they emit
  audit events, which is what Move A consumes.
- `src/pollypm/update.py`, `src/pollypm/task_assignment_notify.py`,
  `src/pollypm/supervisor_alerts.py`, `src/pollypm/supervisor.py`,
  `src/pollypm/cockpit_project_settings.py` — out of the rail / dashboard
  hot path.

### 2.3 Why per-project queries are the bottleneck (and why a cache helps)

Each surface independently re-derives:

1. **Categorization output** (`src/pollypm/dashboard/categorization.py`) —
   `state ∈ {WAITING, WORKING, IDLE, PAUSED}`, glyph, `detail` from
   `why_waiting()` / `what_working()` / "Quiet".
2. **Rail rollup** — `rail_state`, `rail_badge`, sort rank, reason,
   approvals_pending, plan-blocked flag.
3. **Awaits-user inbox** — the most expensive per-project query.
4. **Live worker state** — sessions + latest heartbeat.
5. **Task status counts** — needed for rail glyphs.

Today these are recomputed from the work store per render, per surface,
with no sharing. The 2s TTL cache in `cockpit_rail.py:1806` is the only
existing mitigation, and it has the staleness pathology you'd expect: a
freshly completed task hangs around as "WORKING" until the TTL falls off.

A single in-process cache, populated by a refresher thread driven by the
audit log, eliminates the fan-out and the TTL staleness at the same time.

---

## 3. Proposed mechanism

### 3.1 Where it lives

```
src/pollypm/state_cache/
    __init__.py
    entry.py            # frozen dataclass: ProjectStateCacheEntry
    project_state_cache.py
    refresher.py        # the worker-thread refresh loop
```

Dedicated module. Module-level `get_cache()` returns a lazy singleton. A single
process holds one cache; the cockpit, rail, and dashboard panes share it.

### 3.2 Cache entry — one row per project

```python
# src/pollypm/state_cache/entry.py
@dataclass(frozen=True)
class ProjectStateCacheEntry:
    project_key: str
    project_path: Path
    tracked: bool                          # from project config
    db_paths: tuple[Path, ...]             # canonical → legacy order

    # ── categorization output ──────────────────────────────
    state: ProjectState                    # WAITING | WORKING | IDLE | PAUSED
    glyph: str                             # denormalized from _GLYPHS
    detail: str                            # why_waiting / what_working / "Quiet"

    # ── rail rollup output ────────────────────────────────
    rail_state: ProjectRailState           # RED | YELLOW | GREEN | WORKING | NONE
    rail_badge: str | None
    rail_sort_rank: int
    rail_reason: str
    approvals_pending: int                 # drives the "(N⚠)" suffix
    plan_blocked: bool                     # enforce_plan gate

    # ── awaits-user list (the expensive one) ──────────────
    awaits_user_count: int                 # rail badge consumes len()
    awaits_user_items: tuple[InboxEntry, ...]

    # ── what_working() inputs (only if state == WORKING) ──
    working_agent_name: str
    working_task_title: str

    # ── heartbeats / live workers ─────────────────────────
    live_worker_sessions: tuple[WorkerSessionRow, ...]
    latest_heartbeat_by_session: dict[str, HeartbeatRecord]

    # ── task statuses (recompute rail glyphs without re-opening) ──
    task_status_counts: dict[str, int]
    on_hold_task_ids: frozenset[str]
    review_task_ids: frozenset[str]

    # ── versioning ────────────────────────────────────────
    version: int                           # monotonic per project
    computed_at: float                     # monotonic timestamp
    source_db_path: Path                   # whichever candidate won
```

Notes:

- Frozen dataclass. Readers get an immutable snapshot — no torn reads.
- `awaits_user_items` is a `tuple`, not a list. Same reason.
- `db_paths` keeps the candidate set for diagnostic / divergence-sampler use.

### 3.3 Service API sketch

```python
# src/pollypm/state_cache/project_state_cache.py
class ProjectStateCache:
    def __init__(
        self,
        config_loader: Callable[[], Config],
        audit_tail: AuditTail,
    ) -> None: ...

    # reads — lock-free dict gets, atomic ──────────────────
    def get(self, project_key: str) -> ProjectStateCacheEntry | None: ...
    def snapshot(self) -> dict[str, ProjectStateCacheEntry]: ...
    def version(self, project_key: str) -> int: ...
    def global_version(self) -> int: ...

    # writes — RLock-guarded ──────────────────────────────
    def invalidate(self, project_key: str | None = None) -> None: ...
    def refresh(self, project_key: str) -> ProjectStateCacheEntry: ...

# module-level accessor
def get_cache() -> ProjectStateCache: ...
```

### 3.4 Reader pattern (rail tick / dashboard refresh)

```python
cache = get_cache()
gv = cache.global_version()
if gv == self._last_seen_global_version:
    return self._last_rendered_view     # nothing changed — short-circuit
self._last_seen_global_version = gv
snapshot = cache.snapshot()             # one O(1) dict copy
# render from snapshot — no DB opens
```

Same shape as the existing `_cockpit_state_version` pattern in
`src/pollypm/cockpit_ui.py:2049`, which today gates work on `epoch_mtime()`.
The in-process `global_version` is the syscall-free equivalent.

### 3.5 Why a class, not a module-level dict

- Constructor injection of `config_loader` and `audit_tail` is testable.
  The current "import the helper, mock it" pattern in `pm_inbox_awaits_user_list`
  is exactly why ~15 test files monkeypatch
  `pollypm.cockpit_inbox.pm_inbox_awaits_user_list` directly.
- A class supports a `disabled` mode (env-flag rollback, §6 PR 1) without
  scattering ifs through call sites.
- Test fixtures want multiple short-lived instances; module-level singletons
  fight pytest fixtures.
- The lazy `get_cache()` accessor preserves the "everyone shares one"
  invariant the cache needs.

Not recommended: threading a `ProjectStateCache` argument through every
caller. The 15+ call sites enumerated above have no clean injection point
today; the refactor cost is not worth it for Move A.

### 3.6 Threading

- Single `threading.RLock` around the entry-dict write path.
- Reads are lock-free dict `get()` calls on a `dict[str,
  ProjectStateCacheEntry]`. The atomicity is "you get an old version or a
  new one, not torn." Entries are frozen, so the payload itself is safe.
- The refresher runs on one dedicated worker thread. Refreshes are coalesced
  per project (N invalidations for one project between refreshes collapse
  to one refresh).

---

## 4. Invalidation triggers

Three triggers, ranked by primacy. Defense in depth — primary first; the
other two catch known failure modes of the primary.

### 4.1 Primary — audit-log tail

The dominant signal. `src/pollypm/audit/log.py` already defines stable
constants (`EVENT_TASK_CREATED`, `EVENT_TASK_STATUS_CHANGED`,
`EVENT_TASK_DELETED`, `EVENT_MARKER_CREATED`, `EVENT_MARKER_RELEASED`,
`EVENT_MARKER_CREATE_FAILED`, `EVENT_MARKER_LEAKED`), and every write path
already emits. Per-project audit-log paths live under `~/.pollypm/audit/`
(see the [Audit log paths](../../docs/audit-log.md) note: it is a
per-project `.jsonl`, not one global file).

A tail thread subscribes to the central tail and dispatches:

| Event | Action |
|---|---|
| `task.created` / `task.status_changed` / `task.deleted` | `invalidate(event["project"])` |
| `marker.created` / `marker.released` | `invalidate(event["project"])` (live worker change) |
| `marker.create_failed` / `marker.leaked` | `invalidate(event["project"])` |
| `work_db.opened` | ignore (informational) |
| Workspace-scoped events with empty `project` | `invalidate(None)` (global) |

Why audit-log over alternatives:

- The audit log is **already** the canonical "something interesting changed"
  stream — every write path emits by design.
- `src/pollypm/web_api/sse.py` and `src/pollypm/audit/watchdog.py` already
  tail the central log; the cache becomes a third consumer with the same
  shape.
- Events carry `project` natively, so per-project invalidation is free.
- We do not introduce a new contract — Move A piggybacks on an invariant
  that's already enforced by review.

### 4.2 Secondary — `state_epoch.mtime()` tiebreaker

`src/pollypm/state_epoch.py` exposes `bump()` and `mtime()`. Every state
mutation already calls `bump()`. The cache poller checks `mtime()` once per
tick as a "did anything happen?" coarse signal. If `mtime()` advanced but
no audit event arrived within a small grace window (e.g. 500ms), force a
full refresh on the next tick. This catches the gap the perf review called
out: a worker writing to sqlite directly and bypassing audit emit.

### 4.3 Tertiary — per-DB file mtime as a self-heal

Per-DB `stat()`, only consulted when (a) the audit log has been silent and
(b) a project's entry is older than `MAX_STALE_AGE_S` (default 30s).
Cheap (one stat per entry past TTL) and self-healing if the audit log is
partitioned or the cache process missed reconnection.

### 4.4 Anti-pattern — explicit `cache.invalidate()` from write paths

**Not recommended as a primary path.** Asking every write path to call
`cache.invalidate(project_key)` reintroduces exactly the
discipline-bug the audit-log tail avoids. It is acceptable as an explicit
nudge from a known-untrusted write path (a third-tier fallback), but should
never be the only way a write becomes visible.

### 4.5 Epoch / version model

Two `int` counters:

- `entry.version` — monotonic per project. Bumps every time the entry is
  recomputed.
- `cache.global_version` — monotonic across the cache. Bumps on any
  per-project `entry.version` bump.

Correctness rule: an entry's `version` is bumped **after** its payload is
written. Readers that see version N are guaranteed the payload reflects all
events processed through that version. Writes are RLock-protected; reads
are lock-free dict gets. A stale-by-one-version read is acceptable because
the reader re-checks `global_version` on its next tick.

---

## 5. Code surface — call sites that change

The §2.1 hot-path table, but with the new behavior column:

| # | File:line | Today | New |
|---|---|---|---|
| 1 | `src/pollypm/cockpit_inbox.py:130-265` `pm_inbox_awaits_user_list` | opens `create_work_service` + work store per project | `cache.snapshot()` → concat `entry.awaits_user_items` |
| 2 | `src/pollypm/cockpit_inbox.py:268-295` `_count_inbox_tasks_for_label` | wraps #1 | `sum(e.awaits_user_count for e in cache.snapshot().values())` |
| 3 | `src/pollypm/cockpit_inbox.py:298-…` `pm_inbox_filtered_list` | same fan-out as #1 with `kind_filter` | optional — see Open Question §9.2 |
| 4 | `src/pollypm/dashboard/operator_view.py:189-201` `load_operator_view[_from_config]` | `_parallel_scan_rows` opens 1-2 svc per project | iterates `cache.snapshot()` |
| 5 | `src/pollypm/dashboard/operator_view.py:359-400` `project_state_map_from_config` | parallel fan-out, every rail tick | `{key: e.state for key, e in cache.snapshot().items()}` |
| 6 | `src/pollypm/cockpit_rail.py:1806-1855` `_project_categorizations` | calls #5 behind a 2s TTL | direct `cache.snapshot()`; **TTL cache removed** |
| 7 | `src/pollypm/cockpit_rail.py:1857-1880` `_project_state_rollups` | calls #8 per project | iterates `cache.snapshot()`, reads `rail_state` / `approvals_pending` |
| 8 | `src/pollypm/cockpit_rail.py:1882-2049` `_project_tasks_for_rollup` | up to 3 per-project queries | becomes an internal helper of the cache refresher; no longer called from hot path |
| 9 | `src/pollypm/cockpit_rail.py:1474-1490, 2200-2220` `latest_heartbeat()` per project | one per-project query × N | `entry.latest_heartbeat_by_session[session]` |

Specifically, this collapses the following query fan-outs into ONE per
project per refresh:

- `pm_inbox_awaits_user_list`: 2 opens × N → 0 (reads cache)
- `_collect_project_scans` → `_open_work_service` → `list_tasks` × N
  candidates: up to 2 × N → 0
- `_project_tasks_for_rollup`: up to 3 × N → 0 (folded into refresher,
  runs once per cache cycle)
- `_attach_session_metadata` → `latest_heartbeat()`: 1 × N → 0
- `what_working()`: 1× `list_worker_sessions` + 1× `get(task_id)` per
  WORKING project → 0
- `project_state_map_from_config`: full fan-out × every rail tick → 0
  (the existing TTL goes away)

The rail's existing 2s TTL cache on `_project_categorizations`
(`src/pollypm/cockpit_rail.py:1806`) becomes redundant and is removed in
PR 3.

---

## 6. Rollout plan — four PRs, each independently revertable

Sized to land over ~2.5 days of focused work (see §9 for the
4-hour-increment breakdown). Each PR keeps the system shippable with
`POLLYPM_STATE_CACHE=0`.

### 6.1 PR 1 — Cache infra + tests, wired but unused

Effort: ~0.5d.

- Add `src/pollypm/state_cache/` (entry, cache, refresher).
- Add `POLLYPM_STATE_CACHE=1` env flag. Default **off**.
- With the flag off, `get_cache()` returns a shim whose `snapshot()` is
  `{}` and whose `get()` returns `None`. Call sites that opt in have a
  built-in fall-through to the direct-DB path.
- With the flag on, the audit-log tail and refresh worker start at import.
- No call sites change yet. CI green proves the cache loads and tails the
  audit log without side effects.
- Tests: unit tests for `get` / `snapshot` / `invalidate` / `version`;
  refresher tests against a fake config + mock work-service.

### 6.2 PR 2 — Route the two hottest call sites

Effort: ~0.5d.

- `pm_inbox_awaits_user_list` (#1) and `project_state_map_from_config`
  (#5). These are the two the perf review called out specifically.
- Wrap each in
  `if (entry := cache.get(project_key)) is not None: use entry else: fall through`.
  Flag off → unchanged behavior. Flag on → cache path skips the DB.
- **Divergence-detection logging**: a sampled (1-in-N, default N=50) call
  runs both paths, compares results, and emits a `WARN` on mismatch. Pin
  the comparison in a new test (`tests/test_project_state_cache_parity.py`).

### 6.3 PR 3 — Route remaining call sites; remove the TTL

Effort: ~0.5d.

- Call sites #2, #4, #6, #7, #9 from the §5 table. #3 is optional
  (Open Q §9.2).
- Remove the `_project_categorizations` TTL cache from `cockpit_rail.py`
  once the new cache is authoritative.
- Routes #7 and #9 are mechanical lookups; #4 (`load_operator_view`) is
  the largest change because it iterates the snapshot and rebuilds rows
  in-process.

### 6.4 PR 4 — Flip the default and remove the shim

Effort: ~0.5d.

- `POLLYPM_STATE_CACHE` default flips to **on**.
- Leave the env var as a kill-switch for one release.
- After two weeks of green production telemetry, remove the shim path and
  the direct-DB fallbacks in the routed call sites.

### 6.5 Rollback

- During PRs 1-3: `POLLYPM_STATE_CACHE=0` + cockpit restart returns the
  system to pre-change behavior.
- After PR 4: rollback is a revert of PR 4 only. PRs 1-3 stay landed and
  dormant.

---

## 7. Risks

| Risk | Detection | Mitigation |
|---|---|---|
| Cached data goes stale (audit-log event dropped, write path bypasses emit) | PR 2's divergence sampler logs `WARN`; `state_epoch.mtime()` advancing without a matching audit event triggers a forced refresh on the next tick | `MAX_STALE_AGE_S=30s` ceiling enforced by §4.3 mtime tiebreaker |
| Audit log tailing falls behind under burst (1000s of events in <1s) | New metric `cache.lag_events` (queue depth); alert if sustained >100 | Tail thread coalesces events per project — N events for one project collapse to one invalidation; bounded queue |
| Cache mismatch on dual-DB legacy/canonical split (#1542 split-brain) | Parity sampler runs both paths through `_open_work_service`'s canonical→legacy walk and confirms the same DB wins | Cache MUST mirror `_open_work_service`'s "first non-empty wins" walk exactly — refresher calls that helper, does not re-implement it |
| Memory growth from holding tuples of inbox items | Per-entry size ~10-50KB; 12 projects → ~600KB ceiling; track via `sys.getsizeof` in tests | If memory becomes real (it should not), TTL out paused-project `awaits_user_items` after 5min idle |
| Thread-safety bug in entry write / read | RLock around all writes; frozen dataclass entries; reads are atomic dict gets | Hypothesis test in `tests/test_project_state_cache_concurrency.py`: N invalidators + N readers, assert no exceptions and no torn reads |
| Test-suite breakage (~15 files monkeypatch `pm_inbox_awaits_user_list` etc.) | CI | Expected breakage; tests get a `seed_project_state_cache(cache, {...})` fixture instead of monkeypatching the helper. Per [`feedback_format_string_test_grep`](../../docs/conventions.md), grep for the legacy helper names before changing |
| Multi-process consistency (cockpit + rail_daemon + heartbeat each hold their own cache) | n/a in v1 — Move A is single-process | §9.6 Open Question: defer to Move B if needed |
| Refresher thread starves the UI under burst | Refresher runs in a dedicated worker thread with bounded work-per-tick; UI reads are dict gets and never block | Profile the refresher tick; cap at e.g. 4 project refreshes per 100ms |
| Cache lookup returns `None` when caller expected a hit (cold project) | Every call site has a fall-through to the existing direct-DB path during PRs 1-3 | Remove fall-throughs only in PR 4, after parity sampler has been silent for 14 days |

### 7.1 Staleness windows

Worst case: an audit event is emitted but the tail thread is paused for
500ms (GC pause, OS scheduler hiccup). The cache returns last-known state
for that window. This is **better** than the current 2s TTL on
`_project_categorizations`, which can return up to 2s of stale data with
no mechanism to refresh sooner.

### 7.2 Multi-process consistency

Out of scope for Move A. Each process (cockpit, rail_daemon, heartbeat)
holds its own cache. Each tails the audit log independently. Drift between
caches is bounded by the audit-log tail latency on each process. See
Open Q §9.6.

---

## 8. Acceptance criteria

Pass conditions for "Move A is done":

1. **Profiling.** Cockpit dashboard cold mount on Sam's 12-project workspace
   drops from 1-2s quiet (perf review §1) to <300ms median over 10 cold
   mounts. Under load (operator + workers + heartbeat active), wall-clock
   falls from ~12s to <2s.
2. **Sqlite-open count.** `pm_inbox_awaits_user_list` opens **1** sqlite
   connection per project per refresh (the cache refresher), not 2
   (`create_work_service` + `SQLAlchemyStore`). Measured via an
   `audit/log` instrumented count or a `sqlite3` trace hook.
3. **Rail rebuild.** `_project_tasks_for_rollup` is no longer called from
   `build_items`. The rail-refresh worker reads only from `cache.snapshot()`
   for project-state data.
4. **Parity.** 24 hours of production telemetry with PR 2's divergence
   sampler shows **0** WARN lines (cached and uncached paths agree on
   every sampled call).
5. **Test coverage.** New tests pass — `test_project_state_cache_parity.py`,
   `test_project_state_cache_invalidation.py`,
   `test_project_state_cache_concurrency.py`.
6. **No regression.** Existing rail/dashboard tests pass, including
   `tests/test_inbox_default_lens.py` (the three-surfaces-one-predicate
   invariant in `src/pollypm/cockpit_inbox.py:268-295`).
7. **Kill-switch.** `POLLYPM_STATE_CACHE=0` + restart reproduces
   pre-Move-A behavior verbatim (used as the regression A/B).

---

## 9. Open questions for the implementor

These are decisions deferred from the design ticket to the implementation
PRs. Recommended answers in italics; confirm with Sam before PR 1.

### 9.1 Audit-log tail position on cockpit start

Tail from start-of-file (slow boot, complete history) or `seek to end`
(fast boot, may miss events between last shutdown and start)?

*Recommendation: `seek to end` + force one full refresh on cockpit boot.
The full refresh closes the gap; tailing from start adds boot latency we
cannot amortize.*

### 9.2 `pm_inbox_filtered_list` (call site #3) — in or out?

It's used by the archive lenses (completion-fyi, activity-events from
#1573) — not the rail hot path, but cache-shaped.

*Recommendation: out of scope for Move A. Land as a follow-up. The lens
selectors are easy to extend the entry with later (`completion_fyi_items`,
etc.) without restructuring the cache.*

### 9.3 Per-project DB candidate set — frozen at boot or rediscovered per refresh?

`_open_work_service` does a `Path.exists()` check on each candidate. If a
new project DB appears mid-session (rare; `pm new-project`), the cache
must pick it up.

*Recommendation: rediscover the candidate set on every full refresh;
cache the resolved winner per entry. The stat cost is negligible compared
to the open.*

### 9.4 Cache lifetime on cockpit exit

Pure in-process. Killing the cockpit drops the cache. No disk persistence.

*Recommendation: no persistence. The crash-recovery / fast-restart win is
not worth the complexity for Move A. Boot cost after PR 4 is one full
fan-out (the same as today's first refresh).*

### 9.5 Remove `_project_categorizations` 2s TTL outright in PR 3, or keep as a second-level guard?

*Recommendation: remove. Two cache layers is worse than one. The
`global_version` short-circuit gives us the "skip work if nothing changed"
win the TTL was approximating.*

### 9.6 Where does the refresher run — cockpit process or `rail_daemon`?

*Recommendation: cockpit process for Move A. `rail_daemon` has its own
state and a cross-process cache adds IPC complexity. Revisit in Move B
(Textual Screens) if multiple panes start needing a shared cache.*

---

## 10. Effort breakdown (4-hour increments)

The perf review's 2-day estimate is realistic but tight. Honest assessment:
**2.5-3 days**. Skipping the divergence sampler is what gets you to 2.

| Increment | Work | Cumulative |
|---|---|---|
| 4h | `src/pollypm/state_cache/` skeleton: entry, cache + RLock, env-flag shim, unit tests for `get`/`snapshot`/`invalidate`/`version` | 0.5d |
| 4h | Refresher worker: spawn thread, pull config + audit-log tail, implement single-project refresh using `_open_work_service` + `categorize_project` + `rollup_project_state` + `pm_inbox_awaits_user_list` logic; tests against fake config + mock work-service | 1.0d |
| 4h | Audit-log tail integration: subscribe to central tail, map `EVENT_TASK_*` / `EVENT_MARKER_*` → `invalidate(project)`; synthetic event-stream tests; `state_epoch.mtime` tiebreaker | 1.5d |
| 4h | PR 2 — route `pm_inbox_awaits_user_list` and `project_state_map_from_config`; divergence sampler; parity test | 2.0d |
| 4h | PR 3 — route remaining call sites (`_project_state_rollups`, `_project_tasks_for_rollup`, `latest_heartbeat`, `_count_inbox_tasks_for_label`, `load_operator_view_from_config`); remove rail's `_project_categorizations` TTL | 2.5d |
| 4h | PR 4 — flip default; production telemetry; benchmark before/after on a 12-project fixture (CI perf budget candidate) | 3.0d |

---

## 11. Out of scope

- **Move B** (collapse cockpit panes into Textual Screens) — separate v2
  rewrite.
- **Move C** (persistent tmux control mode) — independent of Move A; can
  land in parallel.
- **Cross-process cache sharing** (rail-daemon + cockpit + heartbeat).
  One process for now.
- **Eviction of paused projects' entries.** Sized at ~600KB ceiling for
  12 projects; revisit if a 100-project workspace appears.
- **Cache for cold supporting helpers** (`_build_project_pm_primer`,
  `_build_operator_primer`). Fire-once on PM-chat mount; not a hot loop.
  Route opportunistically in a follow-up if PR 4 telemetry shows them
  showing up in the profile.

---

## 12. References

- Issue: [#1664](https://github.com/samhotchkiss/pollypm/issues/1664)
  `[design] Move A: in-process project-state cache with epoch-driven invalidation`
- Parent: [#1634](https://github.com/samhotchkiss/pollypm/issues/1634) rail-perf meta
- Prior wedges: #1605, #1608, #1630
- Related split-brain risk: #1542
- Audit log invariants: `docs/audit-log.md`
- Work-service architecture: `docs/work-service-spec.md`
