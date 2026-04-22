"""Tests for state.db hygiene: auto_vacuum mode + TTL sweep for memory_entries.

These guard against the class of bugs that bloated Sam's live cockpit DB to
1.9 GB (1.6 GB dead space). The three checks, mirroring the remediation:

* A fresh StateStore must come up in ``auto_vacuum=INCREMENTAL`` mode so
  every future delete immediately adds pages to the freelist — ready for
  the daily ``PRAGMA incremental_vacuum``.
* ``incremental_vacuum()`` must actually reclaim disk space after a bulk
  insert+delete cycle. It's an easy regression to miss if someone adds a
  transaction wrapper around the pragma (which SQLite silently rejects).
* ``sweep_expired_memory_entries()`` must drop exactly the expired rows
  and leave non-expired + NULL-TTL rows untouched. Retention policy in
  the live cockpit is decided at write-time; this sweep only enforces
  what's already on the row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.storage.state import StateStore


def test_auto_vacuum_incremental_on_fresh_db(tmp_path: Path) -> None:
    """Fresh DB must be created with auto_vacuum=INCREMENTAL (mode 2)."""
    store = StateStore(tmp_path / "state.db")
    try:
        # PRAGMA auto_vacuum returns: 0=NONE, 1=FULL, 2=INCREMENTAL.
        row = store.execute("PRAGMA auto_vacuum").fetchone()
        assert row is not None
        assert int(row[0]) == 2, f"expected INCREMENTAL (2), got {row[0]}"
    finally:
        store.close()


def test_incremental_vacuum_reclaims_space_after_bulk_delete(tmp_path: Path) -> None:
    """After bulk insert+delete, incremental_vacuum should free pages."""
    store = StateStore(tmp_path / "state.db")
    try:
        # Insert enough dummy rows to guarantee multiple pages land on the
        # freelist after delete. ``messages`` is the durable event ledger
        # post-#411, and 2000 rows with a 1 KB body is ~2 MB, plenty to
        # exceed a single 4 KB page.
        big_message = "x" * 1024
        for i in range(2000):
            store.record_event(
                session_name=f"bulk-{i}",
                event_type="bulk.test",
                message=big_message,
            )

        # Checkpoint the WAL so pages land in the main DB file — without
        # this, the "reclaimed" measurement is dominated by WAL mechanics
        # rather than freelist behaviour.
        store.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.execute(
            "DELETE FROM messages "
            "WHERE type = 'event' AND subject = 'bulk.test' AND body = ?",
            (big_message,),
        )
        store.commit()
        store.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        # Freelist should now hold the freed pages — measurable before we
        # call incremental_vacuum.
        freelist_row = store.execute("PRAGMA freelist_count").fetchone()
        assert freelist_row is not None
        pages_before = int(freelist_row[0])
        assert pages_before > 0, (
            "bulk delete should leave pages on the freelist under "
            "auto_vacuum=INCREMENTAL; found 0"
        )

        reclaimed = store.incremental_vacuum()
        assert reclaimed > 0, "incremental_vacuum should reclaim non-zero bytes"

        # Freelist should be drained (or nearly so — SQLite may keep a
        # handful of pages for bookkeeping, but the bulk should be gone).
        after_row = store.execute("PRAGMA freelist_count").fetchone()
        pages_after = int(after_row[0]) if after_row else 0
        assert pages_after < pages_before, (
            f"freelist_count should drop after vacuum: before={pages_before} "
            f"after={pages_after}"
        )
    finally:
        store.close()


def test_memory_ttl_sweep_drops_expired_only(tmp_path: Path) -> None:
    """Sweep drops expired rows; leaves non-expired + NULL-ttl rows."""
    store = StateStore(tmp_path / "state.db")
    try:
        now = datetime.now(UTC)
        expired_iso = (now - timedelta(days=1)).isoformat()
        future_iso = (now + timedelta(days=7)).isoformat()

        expired = store.record_memory_entry(
            scope="proj",
            kind="note",
            title="expired entry",
            body="should be swept",
            tags=["ttl"],
            source="test",
            file_path="/dev/null",
            summary_path="/dev/null",
            ttl_at=expired_iso,
        )
        future = store.record_memory_entry(
            scope="proj",
            kind="note",
            title="future entry",
            body="should survive",
            tags=["ttl"],
            source="test",
            file_path="/dev/null",
            summary_path="/dev/null",
            ttl_at=future_iso,
        )
        null = store.record_memory_entry(
            scope="proj",
            kind="note",
            title="no-ttl entry",
            body="should survive (NULL ttl)",
            tags=[],
            source="test",
            file_path="/dev/null",
            summary_path="/dev/null",
            ttl_at=None,
        )

        deleted = store.sweep_expired_memory_entries()
        assert deleted == 1, f"expected 1 deletion, got {deleted}"

        assert store.get_memory_entry(expired.entry_id) is None
        # Non-expired + NULL TTL rows must survive untouched.
        survivor_future = store.get_memory_entry(future.entry_id)
        assert survivor_future is not None
        assert survivor_future.title == "future entry"
        survivor_null = store.get_memory_entry(null.entry_id)
        assert survivor_null is not None
        assert survivor_null.title == "no-ttl entry"
        assert survivor_null.ttl_at is None

        # Second sweep on a clean DB should be a no-op.
        assert store.sweep_expired_memory_entries() == 0
    finally:
        store.close()
