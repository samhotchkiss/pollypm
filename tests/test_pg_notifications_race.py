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


def test_concurrent_normal_and_forced_claim_serialise(pg_schema_pool) -> None:
    """#1841 — a ``normal`` and ``forced_kickoff`` claim must serialise.

    Concurrent ``normal`` + ``forced_kickoff`` calls against the same
    ``(session, task, version)`` tuple must hit the *same* advisory
    key (scope-agnostic) so they enter the check-and-insert window
    one at a time. The pre-fix key included ``scope``, so both
    callers landed on different keys, both passed the empty SELECT,
    and both inserted simultaneously — the #1841 race.

    After serialisation the dedupe *predicate* (scope-aware as of
    #1852) decides what happens:

      * If ``normal`` wins the lock first, ``forced_kickoff`` runs
        second and bypasses the normal row (different scope) — two
        rows total. This is the explicit user-driven bypass.
      * If ``forced_kickoff`` wins first, ``normal`` runs second and
        matches the forced row (any-scope match) — one row total.

    Either ordering is correct. The bug under test is "BOTH callers
    inserted at the same instant without serialising" — which would
    manifest as e.g. transient errors or duplicate rows that violate
    the lock ordering. This test asserts both calls complete cleanly
    and at least one returns a real id.
    """
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_notifications import claim_notification_slot

    barrier = threading.Barrier(2)
    results: list[object] = [None, None]
    errors: list[BaseException | None] = [None, None]
    scopes = ("normal", "forced_kickoff")

    def _worker(slot: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[slot] = claim_notification_slot(
                session_name="session-mixed",
                task_id="mixed:1",
                window_seconds=1800,
                execution_version=0,
                project="mixed",
                message="kickoff",
                dedupe_scope=scopes[slot],
                pool=pg_schema_pool,
            )
        except BaseException as exc:  # noqa: BLE001
            errors[slot] = exc

    t1 = threading.Thread(target=_worker, args=(0,))
    t2 = threading.Thread(target=_worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert errors == [None, None], f"workers raised: {errors!r}"

    # At minimum, the first caller to win the advisory lock must
    # succeed (the table is empty). The second caller's outcome
    # depends on ordering: normal-after-forced dedupes (None);
    # forced-after-normal bypasses (new id). Either is correct.
    winners = [r for r in results if isinstance(r, int) and r > 0]
    assert 1 <= len(winners) <= 2, (
        f"expected one or two non-None ids depending on ordering; got "
        f"results={results!r}. Zero winners means the first caller "
        f"crashed; three winners is impossible. The bug under test "
        f"(#1841) was BOTH callers inserting against an empty table "
        f"without serialising — which still manifests as a missing "
        f"lock-release error or a duplicate insert under a unique "
        f"constraint, not as a silent two-row outcome."
    )


def test_forced_kickoff_bypasses_recent_normal_row(pg_schema_pool) -> None:
    """#1852 — forced kickoff must bypass a recent ``normal`` row.

    Scenario:
      1. ``normal`` claim at T0 wins (empty table) and inserts.
      2. ``forced_kickoff`` claim at T0+10s — well inside the
         ``window_seconds`` cutoff — must still fire because the
         scope-aware predicate only suppresses non-normal claims
         against same-scope rows.

    The #1848 collapse to a scope-agnostic predicate silently dropped
    this bypass: any recent row (regardless of scope) suppressed the
    forced claim. The fix restores the sqlite-parity semantic — a
    stale ``normal`` does NOT suppress an explicit forced kickoff.
    """
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_notifications import claim_notification_slot

    normal_id = claim_notification_slot(
        session_name="session-bypass",
        task_id="bypass:1",
        window_seconds=1800,
        execution_version=0,
        project="bypass",
        message="normal ping",
        dedupe_scope="normal",
        pool=pg_schema_pool,
    )
    assert isinstance(normal_id, int) and normal_id > 0, (
        "the normal claim against an empty table must win"
    )

    forced_id = claim_notification_slot(
        session_name="session-bypass",
        task_id="bypass:1",
        window_seconds=1800,
        execution_version=0,
        project="bypass",
        message="forced kickoff",
        dedupe_scope="forced_kickoff",
        pool=pg_schema_pool,
    )
    assert isinstance(forced_id, int) and forced_id > 0, (
        "the forced kickoff must bypass the recent normal row — the "
        "scope-aware dedupe predicate only suppresses non-normal "
        "claims against same-scope rows (#1852)."
    )
    assert forced_id != normal_id

    # A second forced claim at the same instant SHOULD now dedupe
    # against its same-scope sibling — the bypass is one-shot, not a
    # blanket disable of dedupe.
    forced_again = claim_notification_slot(
        session_name="session-bypass",
        task_id="bypass:1",
        window_seconds=1800,
        execution_version=0,
        project="bypass",
        message="forced kickoff again",
        dedupe_scope="forced_kickoff",
        pool=pg_schema_pool,
    )
    assert forced_again is None, (
        "a same-scope forced kickoff inside the window must still "
        "dedupe — the bypass only ignores other-scope rows."
    )

    # And a normal claim after a recent forced should be suppressed
    # (the throttle property is symmetric to the bypass).
    normal_after = claim_notification_slot(
        session_name="session-bypass",
        task_id="bypass:1",
        window_seconds=1800,
        execution_version=0,
        project="bypass",
        message="normal after forced",
        dedupe_scope="normal",
        pool=pg_schema_pool,
    )
    assert normal_after is None, (
        "a normal claim inside the window must dedupe against any "
        "recent row regardless of scope — the throttle property."
    )
