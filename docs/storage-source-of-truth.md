# Storage Source-Of-Truth Read APIs

Source: implements GitHub issue #887.

This document specifies the canonical read API per cockpit
storage concept and the migration-completion guards. Code home:
`src/pollypm/storage_contracts.py`. Test home:
`tests/test_storage_contracts.py`.

## Why a contract

The pre-launch audit (`docs/launch-issue-audit-2026-04-27.md`
§6) cites the recurring shape: data exists, but a reader queries
the wrong scope, project DB, legacy table, namespace, provider
home, or ambient environment. Counts diverge because Home, Rail,
Inbox, Activity, and CLI paths each maintain their own reader.

* `#820` — Home counted only tracked projects while Rail counted
  registered projects.
* `#259` / `#377` — workspace DB vs per-project DB confusion
  killed task pickup.
* `#271` — notifications written to a namespace the cockpit did
  not surface.
* `#809` / `#812` / `#813` / `#814` — transcript / token
  accounting via the wrong provider root.
* `#704` — `notification_staging` compatibility seam still
  reachable.

The contract is the structural fix. One canonical reader per
concept; a registry that names it; a test that resolves every
entry at import time.

## Canonical reader registry

| Concept             | Canonical reader                                         |
| ------------------- | -------------------------------------------------------- |
| `PROJECT`           | `pollypm.config:load_config` (`.projects`)               |
| `SESSION`           | `pollypm.storage.state:StateStore.list_sessions`         |
| `INBOX_ITEM`        | `pollypm.signal_routing:shared_inbox_count`              |
| `ACTIVITY_EVENT`    | `pollypm.store.backends.pg_store:PgStore.query_messages` |
| `ALERT`             | `pollypm.signal_routing:shared_alert_count`              |
| `TASK`              | `pollypm.work.pg_service:PgWorkService.list_tasks` |
| `EXECUTION`         | `pollypm.work.pg_service:PgWorkService.get_execution` |
| `TRANSCRIPT`        | `pollypm.transcript_ingest:TranscriptIngestor`           |
| `TOKEN_USAGE`       | `pollypm.storage.state:StateStore.get_token_sample`      |
| `PROVIDER_ACCOUNT`  | `pollypm.config:load_config` (`.accounts`)               |

Tests in `test_storage_contracts.py` resolve every
`module:function` pair at import time. A renamed or removed
function fails the test — turning the audit's #377 shape (a
documented reader pointing at a function that no longer exists)
into an immediate test failure.

## Tracked vs registered (#820)

The audit cites #820 as the canonical user-visible symptom.
The fix is at the `INBOX_ITEM` reader: every reader (Rail,
Home, Inbox panel, recovery prompt) goes through
`shared_inbox_count` so the tracked-vs-registered filter
applies consistently. The recovery prompt and morning briefing
keep their tracked-only filter as an explicit caller decision —
a documented policy, not a parallel reader.

## Legacy writers

`LEGACY_WRITERS` enumerates compatibility seams that must be
retired or isolated. Each entry declares:

* `name` — short identifier for log lines.
* `concept` — the concept it shadows.
* `migration_plan` — the work to retire it.
* `removal_condition` — when the audit can drop it.
* `is_isolated` — `True` once the writer is gated behind a
  documented compatibility-only path that cannot reach user
  surfaces.

`audit_legacy_writers()` returns one human-readable line per
non-isolated entry. The release gate (#889) blocks v1 while any
line is present.

Currently active:

* **`notification_staging` table** — shadows `INBOX_ITEM`.
  Migration plan: port to `type='staging'` partition of the
  unified messages table. Removal condition: issue #704 closed.

Currently isolated (audit-passing):

* **per-task workspace DB writes** — shadows `TASK`. Already
  routed through `PgWorkService` against the single workspace
  Postgres database. Per-project tables are namespaced by
  `project_key` rather than separate DBs.

## Adding a new concept

1. Add the value to `StorageConcept`.
2. Register a `ReadAPI` in `STORAGE_CONTRACTS` pointing at the
   canonical reader.
3. Add a description and (optional) notes line.
4. If a legacy writer also writes the concept, add a
   `LegacyWriter` row.
5. Update this document with the new row.

The release gate's `all_concepts_have_canonical_reader()` check
asserts step 2 was completed.

## What this contract does NOT do

* It does not implement readers — it points at where they live.
* It does not enforce that *callers* import from the canonical
  module — the boundary tests in `#890` enforce that.
* It does not remove legacy writers — the migration plan does.

*Last updated: 2026-04-27.*
