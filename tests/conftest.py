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
