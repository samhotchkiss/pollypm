"""Shared fixtures for the pg-backed tests (issue #1737, Slice A).

Imported from ``tests/conftest.py`` via ``pytest_plugins`` so every
``test_pg_*.py`` module in this directory has access to the
``_pg_container`` + ``pg_schema_pool`` fixtures without re-importing.
"""

from __future__ import annotations

import importlib
import os
import uuid
from typing import Iterator

import pytest


def _has_docker() -> bool:
    """Probe for a working Docker daemon without raising."""
    try:
        import docker  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        client = docker.from_env()
        client.ping()
    except Exception:  # noqa: BLE001
        return False
    return True


def _local_pg_with_vector() -> str | None:
    """Return a DSN to a local pg with pgvector installed, or None.

    Used as the fallback when Docker isn't available. Tries the env
    DSN first, then the conventional dev DBs (``pollypm_test``,
    ``pollypm``); returns the first one that's reachable and has the
    ``vector`` extension available. Returns None on missing driver or
    no reachable candidate.
    """
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        return None
    candidates: list[str] = []
    env = os.environ.get("POLLYPM_PG_DSN", "").strip()
    if env:
        candidates.append(env)
    candidates.extend(
        [
            "postgresql://localhost:5432/pollypm_test",
            "postgresql://localhost:5432/pollypm",
        ]
    )
    for dsn in candidates:
        try:
            conn = psycopg.connect(dsn, connect_timeout=2)
        except Exception:  # noqa: BLE001
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
                )
                if cur.fetchone() is None:
                    continue
        finally:
            conn.close()
        return dsn
    return None


@pytest.fixture(scope="session")
def _pg_container() -> Iterator[str]:
    """Yield a DSN to a pg instance with pgvector installed.

    Prefers testcontainers when Docker is available; falls back to a
    local DSN. Skips the consuming test when neither is reachable.
    """
    if _has_docker():
        try:
            from testcontainers.postgres import (  # type: ignore[import-not-found]
                PostgresContainer,
            )
        except ImportError:
            container = None
        else:
            container = (
                PostgresContainer("pgvector/pgvector:pg16")
                .with_env("POSTGRES_DB", "pollypm")
                .with_env("POSTGRES_USER", "pollypm")
                .with_env("POSTGRES_PASSWORD", "pollypm")
            )
            container.start()
            try:
                dsn = container.get_connection_url().replace(
                    "postgresql+psycopg2://", "postgresql://"
                )
                yield dsn
                return
            finally:
                container.stop()

    dsn = _local_pg_with_vector()
    if dsn is None:
        pytest.skip(
            "no pg container / local pg with vector ext; install Docker "
            "or set POLLYPM_PG_DSN to a pg with pgvector installed"
        )
    yield dsn


@pytest.fixture()
def pg_schema_pool(_pg_container, monkeypatch) -> Iterator[object]:
    """Per-test pool against a fresh schema namespace.

    Creates ``CREATE SCHEMA test_<uuid>`` and patches the pool factory
    so every connection sets ``search_path`` to the test schema. Drops
    the schema on teardown.
    """
    monkeypatch.setenv("POLLYPM_PG_DSN", _pg_container)

    from pollypm.storage import pg_pool

    importlib.reload(pg_pool)

    schema = f"test_{uuid.uuid4().hex[:12]}"

    bootstrap = pg_pool.get_rw_pool(None)
    try:
        with bootstrap.connection() as conn, conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        pg_pool.pg_pool_shutdown()

    importlib.reload(pg_pool)

    def _build_with_search_path(*args, **kwargs):
        from psycopg_pool import ConnectionPool

        read_only = kwargs.get("read_only", False)
        application_name = kwargs.get("application_name", "pollypm/test")
        safe_app = "".join(
            c for c in application_name
            if c.isalnum() or c in "._/-"
        ) or "pollypm-test"

        def _configure(conn) -> None:
            with conn.cursor() as cur:
                cur.execute(f"SET application_name = '{safe_app}'")
                # ``public`` stays on the path so the ``vector`` type
                # (installed into ``public`` by ``CREATE EXTENSION``)
                # resolves from inside the per-test schema. Without
                # public on the path the embeddings DDL can't see the
                # vector type.
                cur.execute(f'SET search_path = "{schema}", public')
                if read_only:
                    cur.execute(
                        "SET default_transaction_read_only = on"
                    )
            conn.commit()

        return ConnectionPool(
            conninfo=args[0],
            min_size=kwargs["min_size"],
            max_size=kwargs["max_size"],
            configure=_configure,
            open=True,
            name=f"pollypm-test-{'ro' if read_only else 'rw'}",
        )

    monkeypatch.setattr(pg_pool, "_build_pool", _build_with_search_path)
    pool = pg_pool.get_rw_pool(None)
    try:
        yield pool
    finally:
        pg_pool.pg_pool_shutdown()
        import psycopg  # type: ignore[import-not-found]

        with psycopg.connect(_pg_container) as conn:
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
