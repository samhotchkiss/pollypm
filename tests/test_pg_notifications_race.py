"""Concurrency regression test for ``claim_notification_slot`` (#1821).

The pg port of the task-notification dedupe API
(:mod:`pollypm.storage.pg_notifications`) originally relied on
``SELECT ... FOR UPDATE`` to serialise the check + insert. That only
locks rows that *exist*; on the cold path (first ping for a task) two
concurrent callers both see an empty result and both insert ``pending``
rows, defeating the #952 dedupe contract.

The fix gates the check + insert with a transaction-scoped advisory
lock keyed on the dedupe tuple. These tests exercise the race by
firing two ``claim_notification_slot`` calls from sibling threads
against the same pg schema — the assertion is that exactly one of the
two callers receives a row id and the other receives ``None``.

The fixture uses the same per-test schema as the rest of the pg
parity suite (``pg_schema_pool`` from ``tests/conftest_pg.py``), but
because the schema is owned by ``search_path`` rather than a separate
DB, both threads must run against the same pool — the advisory lock
is keyed by ``(int4, int4)`` and is global to the pg cluster, so it
serialises regardless of which connection holds it.
"""

from __future__ import annotations

import threading


def _apply_initial_migrations(pg_schema_pool) -> None:
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)


def test_concurrent_claim_returns_one_winner(pg_schema_pool) -> None:
    """Two concurrent claims on the same empty slot — exactly one wins.

    Before the #1821 fix this test reliably saw both callers receive a
    non-None id (both inserted a ``pending`` row because the
    ``SELECT ... FOR UPDATE`` locked nothing). After the fix the
    advisory lock serialises them: the loser's SELECT then sees the
    winner's row and returns ``None``.
    """
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_notifications import claim_notification_slot

    barrier = threading.Barrier(2)
    results: list[object] = [None, None]
    errors: list[BaseException | None] = [None, None]

    def _worker(slot: int) -> None:
        try:
            # Both threads line up on the barrier so the race window
            # is real — without the barrier the first call wins the
            # CPU and the second one trivially sees the row.
            barrier.wait(timeout=10)
            results[slot] = claim_notification_slot(
                session_name="session-racey",
                task_id="racey:1",
                window_seconds=1800,
                execution_version=0,
                project="racey",
                message="kickoff",
                pool=pg_schema_pool,
            )
        except BaseException as exc:  # noqa: BLE001 — propagate to assertions
            errors[slot] = exc

    t1 = threading.Thread(target=_worker, args=(0,))
    t2 = threading.Thread(target=_worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert errors == [None, None], f"workers raised: {errors!r}"

    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1, (
        f"expected exactly one claim to succeed, got results={results!r}. "
        f"Two non-None ids means both callers inserted a pending row — "
        f"the #1821 TOCTOU race."
    )
    assert len(losers) == 1
    assert isinstance(winners[0], int) and winners[0] > 0


def test_concurrent_distinct_tasks_both_succeed(pg_schema_pool) -> None:
    """The advisory lock must not block claims for distinct tuples.

    Two threads claim slots for different ``task_id`` values; both
    should win because their advisory-lock keys differ. This guards
    against an over-broad lock that would serialise unrelated
    notifications and create a cross-task throughput cliff.
    """
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_notifications import claim_notification_slot

    barrier = threading.Barrier(2)
    results: list[object] = [None, None]
    errors: list[BaseException | None] = [None, None]

    def _worker(slot: int, task_id: str) -> None:
        try:
            barrier.wait(timeout=10)
            results[slot] = claim_notification_slot(
                session_name="session-distinct",
                task_id=task_id,
                window_seconds=1800,
                execution_version=0,
                project="distinct",
                message="kickoff",
                pool=pg_schema_pool,
            )
        except BaseException as exc:  # noqa: BLE001
            errors[slot] = exc

    t1 = threading.Thread(target=_worker, args=(0, "distinct:1"))
    t2 = threading.Thread(target=_worker, args=(1, "distinct:2"))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert errors == [None, None], f"workers raised: {errors!r}"
    assert all(isinstance(r, int) and r > 0 for r in results), (
        f"both distinct-task claims must succeed; got {results!r}"
    )
    assert results[0] != results[1]
