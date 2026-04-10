# T092: SQLite State Survives Schema Migration

**Spec:** v1/15-migration-and-stability
**Area:** Migration
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that the SQLite state database survives schema migrations: existing data is preserved, new columns/tables are added cleanly, and the system functions correctly after migration.

## Prerequisites
- A Polly installation with an existing state database containing data (events, checkpoints, alerts, usage)
- Access to the database for direct inspection

## Steps
1. Locate the state database: `ls -la .pollypm/state.db` (or equivalent path).
2. Record the current schema: `sqlite3 .pollypm/state.db ".schema" > /tmp/schema-before.txt`.
3. Record data counts:
   ```
   sqlite3 .pollypm/state.db "SELECT 'events', COUNT(*) FROM session_events UNION SELECT 'checkpoints', COUNT(*) FROM checkpoints UNION SELECT 'alerts', COUNT(*) FROM alerts;"
   ```
4. Back up the database: `cp .pollypm/state.db /tmp/state-backup-T092.db`.
5. Simulate or perform a version update that includes schema changes.
6. Run `pm up` or `pm migrate` (or equivalent) to trigger the migration.
7. Record the new schema: `sqlite3 .pollypm/state.db ".schema" > /tmp/schema-after.txt`.
8. Compare schemas: `diff /tmp/schema-before.txt /tmp/schema-after.txt`. New columns/tables may be added, but existing columns should not be removed.
9. Re-run the data counts query from step 3. All counts should be >= the previous values (data not lost).
10. Run `pm status` and verify the system works correctly with the migrated database.
11. Query some existing data to verify it is intact: `sqlite3 .pollypm/state.db "SELECT * FROM session_events LIMIT 5;"`.
12. Clean up: `rm /tmp/schema-before.txt /tmp/schema-after.txt /tmp/state-backup-T092.db`.

## Expected Results
- Schema migration runs without errors
- Existing tables and columns are preserved
- New columns/tables are added cleanly
- Data counts are preserved (no data loss)
- Existing data is still queryable and correct
- System functions normally after migration

## Log
