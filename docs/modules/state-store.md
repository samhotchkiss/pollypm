**Last Verified:** 2026-04-22

## Summary

PollyPM persists durable state in a per-project SQLite database at `<project>/.pollypm/state.db`. Two coexisting layers write to that file:

1. **Legacy `StateStore`** (`src/pollypm/storage/state.py`) owns the domain tables that haven't been ported off yet: `sessions`, `account_usage`, `account_runtime`, `session_runtime`, `work_jobs`, `worktrees`, `memory_entries` + `memory_summaries` (FTS), `heartbeats`, `leases`, `checkpoints`, and the `token_*` ledger.
2. **Unified `Store`** (`src/pollypm/store/`) is the SQLAlchemy-Core-backed replacement. Issue #342 retired the event/alert/message-shaped surfaces from `StateStore` onto `SQLAlchemyStore`; the `messages` table is the authoritative surface for `pm notify`, alerts, and the activity feed.

The `work_*` tables belong to `SQLiteWorkService` (see [modules/work-service.md](work-service.md)) and share the same file; the service owns its own connection and migrations.

Touch this module when adding a new domain table, migrating one off `StateStore`, or extending the unified `messages` surface. Do not open `sqlite3.connect()` on the state DB outside the sanctioned modules — callers should go through `StateStore`, `Store`, or the work service.

## Core Contracts

Legacy `StateStore`:

```python
# src/pollypm/storage/state.py
class StateStore:
    path: Path

    def __init__(self, path: Path, *, readonly: bool = False) -> None: ...
    def __enter__(self) -> "StateStore": ...
    def __exit__(self, *exc) -> None: ...

    # Sessions / accounts / heartbeats / leases
    def upsert_session(self, ...) -> None: ...
    def list_sessions(self) -> list[SessionRecord]: ...
    def upsert_session_runtime(self, ...) -> None: ...
    def get_session_runtime(self, session_name: str) -> SessionRuntimeRecord | None: ...
    def upsert_account_runtime(self, ...) -> None: ...
    def get_account_runtime(self, account_name: str) -> AccountRuntimeRecord | None: ...
    def record_heartbeat(self, ...) -> None: ...
    def latest_heartbeat(self, session_name: str) -> HeartbeatRecord | None: ...
    def set_lease(self, session_name, owner, note="") -> None: ...
    def clear_lease(self, session_name) -> None: ...
    def get_lease(self, session_name) -> LeaseRecord | None: ...
    def list_leases(self) -> list[LeaseRecord]: ...

    # Checkpoints + account usage
    def record_checkpoint(self, ...) -> None: ...
    def latest_checkpoint(self, session_name: str) -> CheckpointRecord | None: ...
    def upsert_account_usage(self, ...) -> None: ...
    def get_account_usage(self, account_name: str) -> AccountUsageRecord | None: ...

    # Worktrees
    def upsert_worktree(self, ...) -> None: ...
    def update_worktree_status(self, project_key, lane_kind, lane_key, status) -> None: ...

    # Retention / GC
    def prune_old_data(self, *, event_days: int = 7, heartbeat_hours: int = 24) -> dict[str, int]: ...
```

Dataclass records live in the same module: `AlertRecord`, `LeaseRecord`, `WorktreeRecord`, `AccountUsageRecord`, `AccountRuntimeRecord`, `SessionRuntimeRecord`.

Unified `Store`:

```python
# src/pollypm/store/__init__.py
from pollypm.store import Store, SQLAlchemyStore, get_store, make_engines, is_sqlite

# src/pollypm/store/protocol.py (structural)
class Store(Protocol):
    def transaction(self) -> ContextManager: ...
    def execute(self, stmt) -> Result: ...
    def enqueue_message(self, *, scope, type, recipient, sender, subject, body="",
                        payload=None, labels=(), tier="immediate", parent_id=None) -> int: ...
    def query_messages(self, *, recipient=None, state=None, type=None, tier=None,
                       scope=None, limit=None, offset=None) -> list[dict]: ...
    def append_event(self, *, scope, sender, subject, payload) -> int: ...
    # ... see protocol.py for full surface
```

The `messages` table is defined in `storage/state.py` (shared schema) so both the legacy and unified layers see it:

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    type TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'immediate',
    recipient TEXT NOT NULL,
    sender TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    parent_id INTEGER,
    subject TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    labels TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT
);
```

## File Structure

- `src/pollypm/storage/state.py` — `StateStore`, `SCHEMA`, schema migration, retention sweep.
- `src/pollypm/storage/fts_query.py` — FTS tokenizer helpers (`normalize_fts_query`) for memory-summary search.
- `src/pollypm/store/__init__.py` — re-exports for the unified `Store`.
- `src/pollypm/store/protocol.py` — `Store` protocol.
- `src/pollypm/store/engine.py` — `make_engines` (dual-pool: writer=1, reader=5) and `is_sqlite`.
- `src/pollypm/store/sqlalchemy_store.py` — `SQLAlchemyStore`.
- `src/pollypm/store/schema.py` — SQLAlchemy Core Table defs (`messages`, events).
- `src/pollypm/store/registry.py` — process-wide `get_store(state_db)` cache.
- `src/pollypm/store/migrations.py` — schema migrations for the unified store.
- `src/pollypm/store/event_buffer.py` — buffered event writer.
- `src/pollypm/store/classifier.py` — event-subject tier classifier (audit / operational / high-volume).
- `src/pollypm/store/title_contract.py` — `apply_title_contract` for canonical subjects.

## Implementation Details

- **Why two layers.** `SQLAlchemyStore` was introduced in #337 with a skeleton; #342 retired the legacy event/alert/message surfaces onto it. The *remaining* tables on `StateStore` (listed in `storage/state.py:1`) have not been ported yet — each table is a separate PR.
- **Dual pool.** `make_engines` returns `(writer, reader)` engines. Writer is `pool_size=1` so mutations serialize; reader is `pool_size=5` to exploit WAL concurrent reads. Callers should not share connections across threads outside this discipline.
- **Concurrent writes.** All persistent writers in PollyPM (work service, heartbeat job queue, state store) use a single writer connection per process — either theirs or the dual-pool writer. WAL mode is enabled on DB open.
- **Retention.** `core_recurring` registers `events.retention_sweep` (`37 * * * *`) which prunes `type='event'` rows in `messages` by tier (audit, operational, high-volume, default) according to `[events]` settings in `pollypm.toml`. `pinned` events are never pruned.
- **Migrations.** Legacy migrations live alongside the `StateStore` schema and run in the constructor (idempotent `CREATE TABLE IF NOT EXISTS`). Unified migrations live in `store/migrations.py` and are invoked by the rail daemon and cockpit at boot.
- **FTS memory search.** `storage/fts_query.normalize_fts_query` sanitizes user input before issuing a FTS match — unescaped punctuation in FTS5 expressions is a common crash source.

## Related Docs

- [modules/work-service.md](work-service.md) — `work_*` tables and `work_jobs` queue layout.
- [features/inbox-and-notify.md](../features/inbox-and-notify.md) — the `messages` table as the unified inbox.
- [plugins/activity-feed.md](../plugins/activity-feed.md) — consumer of events + messages.
- [modules/memory.md](memory.md) — consumer of `memory_entries` + `memory_summaries` FTS.
