"""Project-wide pytest config.

Test-hygiene defaults that should apply to every test in this repo.
Module-specific fixtures live beside their tests.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


def pytest_configure(config):  # noqa: ARG001
    """Opt every test out of side-effectful daemon spawns.

    ``pm up`` normally spawns a detached ``pollypm.rail_daemon``
    process so auto-recovery runs without the cockpit. Tests that
    invoke the ``pm up`` codepath (``tests/integration/test_config_split_integration.py``
    among others) would each leak a detached daemon pointing at
    their pytest-tmp config path. Setting the env var here blocks
    the spawn across the whole test run; real integration tests that
    want to exercise the daemon can clear the var in their own fixture.
    """
    os.environ.setdefault("POLLYPM_SKIP_RAIL_DAEMON", "1")
    os.environ.setdefault("POLLYPM_DISABLE_ERROR_NOTIFICATIONS", "1")
    os.environ.setdefault("POLLYPM_DISABLE_AGENTIC_REVIEW_SUMMARIES", "1")
    os.environ.setdefault("POLLYPM_DISABLE_WORK_DB_OPENED_AUDIT", "1")
    os.environ.setdefault(
        "POLLYPM_ERROR_LOG_PATH",
        str(
            Path(tempfile.gettempdir())
            / f"pollypm-pytest-{os.getpid()}"
            / "errors.log"
        ),
    )
    # ``pollypm.audit.log`` writes JSONL tails under ``~/.pollypm/audit/``
    # by default. Without this redirect every test that triggers a task
    # lifecycle event (worker register, marker reap, work-service hooks)
    # leaks audit rows into the dev machine's real audit dir. Mirror the
    # error-log pattern above and point at a pytest-tmp dir so the user's
    # real audit history stays clean. Tests that exercise the audit-log
    # itself (see ``tests/test_audit_log.py``) override via monkeypatch.
    os.environ.setdefault(
        "POLLYPM_AUDIT_HOME",
        str(
            Path(tempfile.gettempdir())
            / f"pollypm-pytest-{os.getpid()}"
            / "audit"
        ),
    )
    # Tests build their config in pytest tmp dirs but ``state_db``
    # defaults to ``~/.pollypm/state.db`` on the dev machine — which
    # may legitimately have pending migrations. Skip the refuse-start
    # gate globally so CLI plumbing tests don't pick up the real DB's
    # migration state. Tests that exercise the gate itself clear the
    # env var in their own monkeypatch fixture (see
    # ``tests/test_migration_gate.py``).
    os.environ.setdefault("POLLYPM_SKIP_MIGRATION_GATE", "1")


@pytest.fixture(autouse=True)
def _reset_store_cache_between_tests():
    """Drain the process-wide store cache before + after every test.

    ``pollypm.store.registry.get_store`` caches backend instances by
    ``(backend, db_path)`` so every caller in a process shares the
    same engine pool (prevents the FD exhaustion that bit us on
    2026-04-20). Tests build config against ``tmp_path``, so without
    this fixture an earlier test's cached engine would point at a
    now-deleted path and the next test would reuse it. Drain before
    + after so state from one test never leaks into another.
    """
    try:
        from pollypm.store.registry import reset_store_cache
    except ImportError:
        reset_store_cache = None  # type: ignore[assignment]
    if reset_store_cache is not None:
        reset_store_cache()
    yield
    if reset_store_cache is not None:
        reset_store_cache()


# Pg-backed test fixtures (issue #1737, Slice A). Lives in a sibling
# module so the heavy testcontainers / psycopg imports stay lazy
# (``pytest_plugins`` is registered up-front but the fixtures within
# only do their import work when actually invoked by a test).
pytest_plugins = ["tests.conftest_pg"]


# ----------------------------------------------------------------------
# Work-service backend dispatch (issue #1737, Slice F)
# ----------------------------------------------------------------------
#
# The ``work_service`` fixture below is the single entry point tests
# should reach for when they want a work-service instance and don't care
# which backend is providing it. Behaviour is controlled by an opt-in
# ``@pytest.mark.backend(...)`` marker:
#
# * ``@pytest.mark.backend("sqlite")`` — force the sqlite path.
# * ``@pytest.mark.backend("postgres")`` — force the pg path (requires
#   the ``pg_schema_pool`` machinery in ``conftest_pg.py``; skipped if
#   Docker / a local pg is unavailable).
# * ``@pytest.mark.backend("both")`` — parameterise the test against
#   both backends; the test runs twice and the active backend is
#   identifiable via ``request.node.callspec.id``.
# * No marker — sqlite. Slice K (the ripout) flips this default to pg
#   and deletes the sqlite branch.
#
# Tests that need the concrete service class (i.e. that today instantiate
# ``SQLiteWorkService(...)`` directly) should migrate to the dispatch
# fixture incrementally as their owners port them. The fixture is
# intentionally not auto-applied — Slice F is plumbing, not a rewrite.


def _build_sqlite_work_service(tmp_path):
    """Build a fresh ``SQLiteWorkService`` against a tmp-path DB."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    return SQLiteWorkService(db_path=tmp_path / "work.db")


def _build_pg_work_service(request):
    """Pull the per-test ``pg_work_service`` fixture via ``request``.

    The pg fixture chain may ``pytest.skip`` if Docker / a local pg with
    pgvector isn't reachable; we let that propagate so tests pinned to
    the pg backend skip cleanly on dev machines without Docker.
    """
    return request.getfixturevalue("pg_work_service")


def _resolve_backend_marker(request) -> str:
    """Read the ``@pytest.mark.backend(...)`` marker, default ``sqlite``.

    Returns the marker argument as a lowercase string. Unknown values
    fall back to sqlite so a typo doesn't silently route to the wrong
    backend — the dispatch path logs a warning when this happens.
    """
    marker = request.node.get_closest_marker("backend")
    if marker is None:
        return "sqlite"
    if not marker.args:
        return "sqlite"
    raw = str(marker.args[0]).strip().lower()
    if raw not in {"sqlite", "postgres", "both"}:
        import warnings

        warnings.warn(
            f"Unknown @pytest.mark.backend({marker.args[0]!r}); "
            "defaulting to 'sqlite'. Valid values: 'sqlite', 'postgres', 'both'.",
            stacklevel=2,
        )
        return "sqlite"
    return raw


@pytest.fixture
def work_service(request, tmp_path):
    """Dispatch fixture returning a work-service backed by the marker.

    See the comment block above for marker semantics. Tests that need
    a backend-specific service (e.g. they assert on pg row counts or
    sqlite pragmas) should reach for the concrete fixtures
    (``pg_work_service`` or build a ``SQLiteWorkService`` directly)
    instead.
    """
    backend = _resolve_backend_marker(request)
    if backend == "both":
        # ``both`` is implemented via the ``_work_service_backend``
        # parametrize hook below — when this fixture is invoked under a
        # ``both`` marker, the parametrize layer has already picked one
        # of ``sqlite`` / ``postgres`` and stashed it on the request.
        chosen = getattr(request, "param", "sqlite")
        backend = chosen
    if backend == "postgres":
        return _build_pg_work_service(request)
    return _build_sqlite_work_service(tmp_path)


def pytest_generate_tests(metafunc):
    """Parameterise ``work_service`` against both backends when marked.

    Implements the ``@pytest.mark.backend('both')`` half of the dispatch
    contract: when the marker is present AND the test asks for the
    ``work_service`` fixture, expand into two test items — one per
    backend. The ``work_service`` fixture above reads ``request.param``
    to pick the right side.
    """
    if "work_service" not in metafunc.fixturenames:
        return
    backend_marker = metafunc.definition.get_closest_marker("backend")
    if backend_marker is None or not backend_marker.args:
        return
    if str(backend_marker.args[0]).strip().lower() != "both":
        return
    metafunc.parametrize(
        "work_service",
        ["sqlite", "postgres"],
        indirect=True,
        ids=["sqlite", "postgres"],
    )
