"""Shared fixtures for the pg-backed tests (issue #1737, Slices A + F).

Imported from ``tests/conftest.py`` via ``pytest_plugins`` so every
``test_pg_*.py`` module in this directory has access to the pg fixtures
without re-importing.

Layered surface
---------------

* ``_pg_container`` (session-scoped) — one pg+pgvector container per
  pytest run. The container start cost (~5 s on a warm Docker host) is
  paid once instead of per test. Slice A shipped this.
* ``pg_schema_pool`` (function-scoped) — fresh ``test_<uuid>`` schema
  with ``search_path`` patched into a per-test pool factory. Lets tests
  parallelize safely within one container. Slice A shipped this.
* ``pg_work_service`` (function-scoped, Slice F) — ready-to-use
  :class:`pollypm.work.pg_service.PgWorkService` backed by
  ``pg_schema_pool``. Replaces the per-test boilerplate of building one
  by hand.
* ``pg_state_store`` (function-scoped, Slice F) — placeholder shim for
  the future pg port of :class:`pollypm.storage.state.StateStore`. Until
  the state.py tables port (#342-followup), this fixture returns a
  ``tmp_path``-backed sqlite StateStore so test authors writing
  pg-style state tests have a hook today. Swap the implementation once
  ``PgStateStore`` lands.
* ``seeded_pg_workspace`` (function-scoped, Slice F) — a
  ``pg_work_service`` with a small canned project + handful of tasks
  pre-created. The common case "I want to test queries / lists against
  a non-empty workspace" without repeating the create boilerplate.
"""

from __future__ import annotations

import importlib
import os
import uuid
from pathlib import Path
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


# --------------------------------------------------------------------- #
# Slice F additions: ergonomic fixtures on top of ``pg_schema_pool``.
# --------------------------------------------------------------------- #


@pytest.fixture()
def pg_work_service(pg_schema_pool):
    """Pre-built :class:`PgWorkService` against the per-test schema.

    Most pg-backed work-service tests don't care about the pool itself —
    they want a ready service. This fixture wraps the boilerplate from
    ``tests/test_pg_work_service.py``'s local ``pg_service`` fixture so
    it can be shared across the parity suite and any future pg-backed
    tests.

    The service runs the schema migration applier on construction (see
    :meth:`PgWorkService.__init__`), so the per-test schema gets the
    full ``work_tasks`` / ``work_transitions`` / ... DDL applied.
    """
    from pollypm.work.pg_service import PgWorkService

    return PgWorkService(pool=pg_schema_pool, ro_pool=None)


@pytest.fixture()
def pg_state_store(tmp_path: Path):
    """Placeholder for the future pg port of :class:`StateStore`.

    :mod:`pollypm.storage.state` is sqlite-only today (see the module's
    own ``TODO(#342-followup)``). Until that port lands, this fixture
    returns a tmp-path-backed sqlite :class:`StateStore` so test authors
    writing "state.py reads" tests have a stable fixture name they can
    rebind to a real ``PgStateStore`` once the port ships, without
    touching the test bodies.

    The fixture deliberately does NOT use the pg pool — there is no pg
    schema for these tables yet. Slice F's job is to make the seam
    available; the actual implementation flips when the state-table
    migration slice merges.
    """
    from pollypm.storage.state import StateStore
    db_path = tmp_path / "state.db"
    store = StateStore(db_path)
    try:
        yield store
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


@pytest.fixture()
def pg_job_queue(pg_schema_pool):
    """Pre-built :class:`JobQueue` against the per-test schema (#1737 Slice K-jobs).

    Applies the schema migrations on first call so the ``work_jobs``
    table is present, then constructs a queue bound to the per-test
    pool. The retry policy is configured for near-zero delay so
    failure / retry assertions don't pay the production backoff ladder.

    Tests that want a custom retry policy build their own ``JobQueue``
    against ``pg_schema_pool`` directly.
    """
    from pollypm.jobs import JobQueue, exponential_backoff
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    queue = JobQueue(
        pool=pg_schema_pool,
        retry_policy=exponential_backoff(
            base_seconds=0.01, factor=1.0, max_seconds=0.01, jitter=0,
        ),
    )
    try:
        yield queue
    finally:
        queue.close()


@pytest.fixture()
def seeded_pg_workspace(pg_work_service):
    """A ``PgWorkService`` pre-seeded with one project + a few tasks.

    Returns a ``(service, tasks)`` tuple where ``tasks`` is a list of
    the three created :class:`Task` objects in creation order. The
    project key is ``"demo"``. The seed is intentionally small — tests
    that need different shapes should build on top via the underlying
    ``service``.

    Why a fixture at all: the "I just want a non-empty workspace to
    query against" case is the single most repeated pattern in the
    sqlite work-service tests (``svc`` + three ``_create_standard_task``
    calls at the top of half the test bodies). One fixture is cheaper
    than rewriting that incantation in every parity test.
    """
    svc = pg_work_service
    tasks = [
        svc.create(
            title=f"seed-{i}",
            description=f"seeded task {i}",
            type="task",
            project="demo",
            flow_template="default",
            roles={"worker": "alice"},
            priority="normal",
            created_by="seed",
        )
        for i in range(3)
    ]
    return svc, tasks
