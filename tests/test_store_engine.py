"""Tests for :mod:`pollypm.store.engine` — the dual-pool engine factory.

Run with an isolated HOME so the suite never leaks into ``~/.pollypm/``:

    HOME=/tmp/pytest-store-foundation uv run pytest tests/test_store_engine.py -x

Coverage (per issue #337 acceptance):

1. All four SQLite pragmas (``journal_mode=WAL``, ``busy_timeout=30000``,
   ``synchronous=NORMAL``, ``foreign_keys=ON``) take effect on every
   connection the pool hands out.
2. Writer pool serializes under thread contention — 10 threads each
   holding a write transaction for ~0.05s produce wall-clock ≥ 0.5s with
   no ``database is locked`` errors.
3. Reader pool allows concurrent reads — 5 threads each sleeping ~0.1s
   inside a read complete in well under the serial baseline.
4. :func:`is_sqlite` correctly classifies SQLite vs non-SQLite URLs.
5. :class:`SQLAlchemyStore.transaction` commits on success + rolls back
   on exception, using the writer engine.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import text

from pollypm.store import SQLAlchemyStore, Store, is_sqlite, make_engines


# --------------------------------------------------------------------------
# is_sqlite — pure URL classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("sqlite:///foo.db", True),
        ("sqlite:///:memory:", True),
        ("sqlite+pysqlite:///bar.db", True),
        ("postgresql+psycopg://host/db", False),
        ("postgresql://user:pw@host:5432/db", False),
        ("mysql+pymysql://user@host/db", False),
    ],
)
def test_is_sqlite_classification(url: str, expected: bool) -> None:
    assert is_sqlite(url) is expected


# --------------------------------------------------------------------------
# Pragmas
# --------------------------------------------------------------------------


def _db_url(tmp_path: Path, name: str = "store.db") -> str:
    return f"sqlite:///{tmp_path / name}"


def test_sqlite_pragmas_applied_on_writer(tmp_path: Path) -> None:
    write_engine, _ = make_engines(_db_url(tmp_path))
    with write_engine.connect() as conn:
        assert (
            conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
        )
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000
        # synchronous: NORMAL == 1
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1
        # foreign_keys: ON == 1
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_sqlite_pragmas_applied_on_reader(tmp_path: Path) -> None:
    # Writer has to create the file + flip WAL first so the reader sees
    # the same journal_mode on its fresh connection.
    write_engine, read_engine = make_engines(_db_url(tmp_path))
    with write_engine.connect() as conn:
        conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
        conn.commit()

    with read_engine.connect() as conn:
        assert (
            conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
        )
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


# --------------------------------------------------------------------------
# Writer pool serialization
# --------------------------------------------------------------------------


def test_writer_pool_serializes_writes(tmp_path: Path) -> None:
    """10 threads, each holding a write txn for 0.05s, wall time ≥ 0.5s."""
    write_engine, _ = make_engines(_db_url(tmp_path))
    with write_engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, worker INTEGER)"
        )

    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    hold = 0.05
    n_workers = 10

    def worker(i: int) -> None:
        try:
            with write_engine.begin() as conn:
                time.sleep(hold)
                conn.execute(text("INSERT INTO t (worker) VALUES (:w)"), {"w": i})
        except BaseException as exc:  # pragma: no cover - failure capture
            with errors_lock:
                errors.append(exc)

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(worker, i) for i in range(n_workers)]
        for fut in futures:
            fut.result()
    elapsed = time.monotonic() - start

    assert not errors, f"writer threads raised: {errors!r}"
    # With pool_size=1, the 10 transactions must run back-to-back.
    assert elapsed >= hold * n_workers, (
        f"writer pool did not serialize: {elapsed:.3f}s < "
        f"{hold * n_workers:.3f}s expected floor"
    )

    # Sanity: all 10 inserts landed.
    with write_engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM t")).scalar()
    assert count == n_workers


# --------------------------------------------------------------------------
# Reader pool concurrency
# --------------------------------------------------------------------------


def test_reader_pool_allows_concurrent_reads(tmp_path: Path) -> None:
    """5 threads each sleep 0.1s inside a read; wall time < serial total."""
    write_engine, read_engine = make_engines(_db_url(tmp_path))
    with write_engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        conn.exec_driver_sql("INSERT INTO t (id, v) VALUES (1, 42)")

    hold = 0.1
    n_readers = 5
    serial_total = hold * n_readers

    def reader() -> int:
        with read_engine.connect() as conn:
            row = conn.execute(text("SELECT v FROM t WHERE id=1")).scalar()
            time.sleep(hold)
            return int(row)

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=n_readers) as pool:
        results = [f.result() for f in [pool.submit(reader) for _ in range(n_readers)]]
    elapsed = time.monotonic() - start

    assert results == [42] * n_readers
    # Concurrent reads should finish well below the serial baseline.
    # Allow generous headroom (0.7 * serial) to stay stable on busy CI.
    assert elapsed < serial_total * 0.7, (
        f"reader pool did not parallelize: {elapsed:.3f}s >= "
        f"{serial_total * 0.7:.3f}s (serial total {serial_total:.3f}s)"
    )


# --------------------------------------------------------------------------
# SQLAlchemyStore skeleton
# --------------------------------------------------------------------------


def test_store_protocol_is_satisfied_by_sqlalchemy_store(tmp_path: Path) -> None:
    store = SQLAlchemyStore(_db_url(tmp_path))
    # Runtime-checkable Protocol: the isinstance check must pass.
    assert isinstance(store, Store)
    store.dispose()


def test_store_transaction_commits_on_success(tmp_path: Path) -> None:
    store = SQLAlchemyStore(_db_url(tmp_path))
    try:
        with store.transaction() as conn:
            conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
            conn.execute(text("INSERT INTO t (id, v) VALUES (1, 7)"))

        with store.read_engine.connect() as conn:
            assert conn.execute(text("SELECT v FROM t WHERE id=1")).scalar() == 7
    finally:
        store.dispose()


def test_store_transaction_rolls_back_on_exception(tmp_path: Path) -> None:
    store = SQLAlchemyStore(_db_url(tmp_path))
    try:
        with store.transaction() as conn:
            conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")

        class Boom(RuntimeError):
            pass

        with pytest.raises(Boom):
            with store.transaction() as conn:
                conn.execute(text("INSERT INTO t (id, v) VALUES (1, 99)"))
                raise Boom("forced rollback")

        with store.read_engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM t")).scalar() == 0
    finally:
        store.dispose()


def test_alert_methods_wired_to_messages_table(tmp_path: Path) -> None:
    """Issue #340 wired ``upsert_alert`` / ``clear_alert`` onto the unified
    ``messages`` table. The previous version of this test asserted these
    were still stubbed; now the same methods round-trip through the real
    writer surface.
    """
    store = SQLAlchemyStore(_db_url(tmp_path))
    try:
        store.upsert_alert(
            session_name="s", alert_type="a", severity="warn", message="m",
        )
        rows = store.query_messages(type="alert", scope="s")
        assert len(rows) == 1
        assert rows[0]["state"] == "open"
        store.clear_alert("s", "a")
        rows = store.query_messages(type="alert", scope="s")
        assert rows[0]["state"] == "closed"
    finally:
        store.close()
