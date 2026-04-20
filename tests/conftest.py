"""Project-wide pytest config.

Test-hygiene defaults that should apply to every test in this repo.
Module-specific fixtures live beside their tests.
"""

from __future__ import annotations

import os


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
