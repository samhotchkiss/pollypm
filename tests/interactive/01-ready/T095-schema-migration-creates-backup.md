# T095: Schema Migration Creates Backup

**Spec:** v1/15-migration-and-stability
**Area:** Migration
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that when a schema migration is performed on the SQLite database, the system automatically creates a backup of the database before applying changes.

## Prerequisites
- A Polly installation with an existing state database
- A pending schema migration (or ability to simulate one)

## Steps
1. Locate the current state database: `ls -la .pollypm/state.db`.
2. Record the file size and modification time: `stat .pollypm/state.db`.
3. Check for any existing backups: `ls .pollypm/state.db.bak*` or `ls .pollypm/backups/`.
4. Trigger a schema migration. This may happen automatically during a version update, or you can use `pm migrate` if such a command exists.
5. After the migration, check for a new backup file:
   - Look for `.pollypm/state.db.bak` or `.pollypm/state.db.<timestamp>.bak`
   - Or check `.pollypm/backups/state.db.<version>` or similar
6. Verify the backup file exists and is a valid SQLite database: `sqlite3 <backup-file> ".tables"` should list the pre-migration tables.
7. Verify the backup file size matches the pre-migration database size (approximately).
8. Verify the backup's data matches the pre-migration data: `sqlite3 <backup-file> "SELECT COUNT(*) FROM session_events;"` should match the pre-migration count.
9. Verify the main database has the new schema (post-migration): `sqlite3 .pollypm/state.db ".schema"` should show new columns/tables.
10. If the migration fails (test with a deliberate failure if possible), verify the backup can be restored: `cp <backup-file> .pollypm/state.db` and run `pm status` to verify the old database works.

## Expected Results
- A backup is automatically created before schema migration
- Backup is a valid SQLite database with pre-migration schema
- Backup data matches the pre-migration state
- The main database has the new schema after migration
- The backup can be used to restore the old state if needed
- Backup files are named with timestamps or version numbers for identification

## Log
