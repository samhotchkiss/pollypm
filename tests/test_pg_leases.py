"""pg-parity tests for the ``pg_leases`` facade (#1737, Slice K-state-port phase 2d).

Covers cluster D: ``leases`` table.
"""

from __future__ import annotations


def _apply_initial_migrations(pg_schema_pool) -> None:
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)


def test_pg_leases_set_get_clear(pg_schema_pool):
    """set_lease writes, get_lease reads, clear_lease removes."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_leases import (
        clear_lease,
        get_lease,
        set_lease,
    )

    assert get_lease("worker-alpha", pool=pg_schema_pool) is None

    set_lease("worker-alpha", "polly", "owns dispatch", pool=pg_schema_pool)

    row = get_lease("worker-alpha", pool=pg_schema_pool)
    assert row is not None
    assert row.session_name == "worker-alpha"
    assert row.owner == "polly"
    assert row.note == "owns dispatch"

    clear_lease("worker-alpha", pool=pg_schema_pool)
    assert get_lease("worker-alpha", pool=pg_schema_pool) is None
    # Idempotent — clearing again is a no-op.
    clear_lease("worker-alpha", pool=pg_schema_pool)


def test_pg_leases_set_lease_overwrites(pg_schema_pool):
    """Re-setting a lease overwrites owner / note in place."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_leases import get_lease, list_leases, set_lease

    set_lease("worker-alpha", "polly", "first", pool=pg_schema_pool)
    set_lease("worker-alpha", "architect", "second", pool=pg_schema_pool)

    leases = list_leases(pool=pg_schema_pool)
    assert len(leases) == 1
    row = get_lease("worker-alpha", pool=pg_schema_pool)
    assert row is not None
    assert row.owner == "architect"
    assert row.note == "second"


def test_pg_leases_list_sorted_by_session_name(pg_schema_pool):
    """list_leases returns rows sorted ascending by session_name."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_leases import list_leases, set_lease

    set_lease("worker-charlie", "polly", "", pool=pg_schema_pool)
    set_lease("worker-alpha", "polly", "", pool=pg_schema_pool)
    set_lease("worker-bravo", "polly", "", pool=pg_schema_pool)

    rows = list_leases(pool=pg_schema_pool)
    assert [r.session_name for r in rows] == [
        "worker-alpha",
        "worker-bravo",
        "worker-charlie",
    ]
