"""Tests for the Postgres connection pool primitives (issue #1737).

The pool tests fan out across two surfaces:

* Pure-Python behaviour that doesn't need a live pg — DSN resolution,
  pool sizing, env-var override, the "pg DSN smells like sqlite" guard.
  These run unconditionally.
* Live-pool lifecycle (open + close + RW/RO separation + bad-DSN error).
  These need a live pg instance and are skipped automatically when
  Docker / pg isn't available. Local dev: ``brew services start
  postgresql@17 && createdb pollypm`` is enough. CI uses the
  testcontainers fixture in ``tests/test_pg_schema.py``.
"""

from __future__ import annotations

import importlib
import os

import pytest


# --------------------------------------------------------------------- #
# Pure-Python behaviour — no pg required.
# --------------------------------------------------------------------- #


def _reload_pool_module():
    """Re-import ``pg_pool`` so module-level singletons are fresh.

    Tests mutate env vars + call ``pg_pool_shutdown``; importlib.reload
    is the cheapest way to reset to a clean module state between cases.
    """
    import pollypm.storage.pg_pool as mod

    importlib.reload(mod)
    return mod


def test_resolve_dsn_default_when_no_env_no_config(monkeypatch):
    monkeypatch.delenv("POLLYPM_PG_DSN", raising=False)
    mod = _reload_pool_module()
    assert mod.resolve_dsn(None) == mod.DEFAULT_DSN


def test_resolve_dsn_env_wins(monkeypatch):
    monkeypatch.setenv("POLLYPM_PG_DSN", "postgresql://x:9999/y")
    mod = _reload_pool_module()
    assert mod.resolve_dsn(None) == "postgresql://x:9999/y"


def test_resolve_dsn_uses_config_url_when_postgres(monkeypatch):
    monkeypatch.delenv("POLLYPM_PG_DSN", raising=False)
    mod = _reload_pool_module()

    class _Storage:
        url = "postgresql://h:5432/db"

    class _Config:
        storage = _Storage()

    assert mod.resolve_dsn(_Config()) == "postgresql://h:5432/db"


def test_resolve_dsn_ignores_sqlite_config_url(monkeypatch):
    """A sqlite URL in ``[storage] url`` must not leak into pg DSN."""
    monkeypatch.delenv("POLLYPM_PG_DSN", raising=False)
    mod = _reload_pool_module()

    class _Storage:
        url = "sqlite:///tmp/state.db"

    class _Config:
        storage = _Storage()

    assert mod.resolve_dsn(_Config()) == mod.DEFAULT_DSN


def test_resolve_pool_sizing_defaults():
    mod = _reload_pool_module()
    assert mod.resolve_pool_sizing(None) == (
        mod.DEFAULT_POOL_MIN,
        mod.DEFAULT_POOL_MAX,
    )


def test_resolve_pool_sizing_honours_config():
    mod = _reload_pool_module()

    class _PgSection:
        pool_min = 3
        pool_max = 7

    class _Storage:
        pg = _PgSection()

    class _Config:
        storage = _Storage()

    assert mod.resolve_pool_sizing(_Config()) == (3, 7)


def test_resolve_pool_sizing_clamps_min_below_one():
    mod = _reload_pool_module()

    class _PgSection:
        pool_min = 0
        pool_max = 5

    class _Storage:
        pg = _PgSection()

    class _Config:
        storage = _Storage()

    # pool_min must be at least 1 — the pool would reject 0.
    assert mod.resolve_pool_sizing(_Config()) == (1, 5)


def test_resolve_pool_sizing_clamps_max_below_min():
    mod = _reload_pool_module()

    class _PgSection:
        pool_min = 4
        pool_max = 2  # nonsensical; clamped up

    class _Storage:
        pg = _PgSection()

    class _Config:
        storage = _Storage()

    min_size, max_size = mod.resolve_pool_sizing(_Config())
    assert min_size == 4
    assert max_size >= min_size


def test_safe_dsn_redacts_password():
    mod = _reload_pool_module()
    masked = mod._safe_dsn("postgresql://alice:secretpw@host:5432/db")
    assert "secretpw" not in masked
    assert "alice" in masked


def test_safe_dsn_preserves_passwordless():
    mod = _reload_pool_module()
    assert mod._safe_dsn("postgresql://host/db") == "postgresql://host/db"


# --------------------------------------------------------------------- #
# Live-pool lifecycle — needs a real pg.
# --------------------------------------------------------------------- #


def _live_pg_dsn() -> str | None:
    """Return a working pg DSN for the live pool tests, or None.

    Honours ``POLLYPM_PG_DSN`` when it points at a reachable instance
    and falls back to the local-default DSN otherwise. Returns None
    (and the live tests skip) on any connection failure or missing
    driver.
    """
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        return None
    candidates: list[str] = []
    env = os.environ.get("POLLYPM_PG_DSN", "").strip()
    if env:
        candidates.append(env)
    candidates.append("postgresql://localhost:5432/pollypm_test")
    candidates.append("postgresql://localhost:5432/pollypm")
    for dsn in candidates:
        try:
            conn = psycopg.connect(dsn, connect_timeout=2)
        except Exception:  # noqa: BLE001
            continue
        conn.close()
        return dsn
    return None


_LIVE_DSN = _live_pg_dsn()

live_pg = pytest.mark.skipif(
    _LIVE_DSN is None,
    reason="local pg not reachable; set POLLYPM_PG_DSN or start postgres",
)


@live_pg
def test_get_rw_pool_returns_singleton(monkeypatch):
    monkeypatch.setenv("POLLYPM_PG_DSN", _LIVE_DSN)
    mod = _reload_pool_module()
    try:
        p1 = mod.get_rw_pool(None)
        p2 = mod.get_rw_pool(None)
        assert p1 is p2
        with p1.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1
    finally:
        mod.pg_pool_shutdown()


@live_pg
def test_ro_pool_blocks_writes(monkeypatch):
    monkeypatch.setenv("POLLYPM_PG_DSN", _LIVE_DSN)
    mod = _reload_pool_module()
    try:
        import psycopg  # type: ignore[import-not-found]

        rw = mod.get_rw_pool(None)
        with rw.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS _pg_pool_test (id int PRIMARY KEY)"
            )
        ro = mod.get_ro_pool(None)
        with ro.connection() as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                cur.execute("INSERT INTO _pg_pool_test VALUES (1)")
        # Cleanup — RW pool drops the helper table.
        with rw.connection() as conn, conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS _pg_pool_test")
    finally:
        mod.pg_pool_shutdown()


@live_pg
def test_pg_pool_shutdown_is_idempotent(monkeypatch):
    monkeypatch.setenv("POLLYPM_PG_DSN", _LIVE_DSN)
    mod = _reload_pool_module()
    mod.get_rw_pool(None)
    mod.pg_pool_shutdown()
    mod.pg_pool_shutdown()  # second call must not raise


def test_bad_dsn_raises_on_first_use(monkeypatch):
    """A DSN that points nowhere must fail loudly when used.

    psycopg_pool defers the actual connect until first ``.connection()``
    when ``open=True`` is set (the pool backfills in a background
    thread). The user-visible contract is: trying to USE a bad DSN
    raises within a reasonable time. We assert that contract directly.
    """
    try:
        import psycopg_pool  # noqa: F401  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("psycopg_pool not installed")
    monkeypatch.setenv(
        "POLLYPM_PG_DSN",
        "postgresql://does-not-exist.invalid:65535/nope?connect_timeout=1",
    )
    mod = _reload_pool_module()
    try:
        with pytest.raises(Exception):
            pool = mod.get_rw_pool(None)
            # Force-acquire so a deferred-connect pool surfaces the
            # bad-host error rather than hanging on the background
            # warm-up loop.
            with pool.connection(timeout=3) as conn:
                conn.execute("SELECT 1")
    finally:
        mod.pg_pool_shutdown()
