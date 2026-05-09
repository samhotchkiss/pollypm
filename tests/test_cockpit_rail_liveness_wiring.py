"""Smoke test for the cockpit's rail-daemon liveness watchdog wiring.

The full PollyCockpitApp can't be exercised without a Textual harness +
the supervisor stack, so this test focuses on the small surface that
``_tick_rail_liveness`` and its helpers expose:

* The periodic-cadence env-var override actually wins.
* ``_cockpit_uses_external_rail_daemon`` only returns True when the
  PID file exists — i.e. it skips the in-process rail mode (where
  respawning a daemon would race the cockpit's own ticker thread).
* ``_resolve_rail_liveness_interval`` falls back cleanly on garbage.

We don't unit-test ``_tick_rail_liveness`` itself — it delegates to
:func:`rail_daemon_supervisor.revive_if_needed`, which has its own
:mod:`tests.test_rail_daemon_supervisor` coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.cockpit_ui import PollyCockpitApp


class _StubRouter:
    """Minimal CockpitRouter stub so the App ctor doesn't blow up."""

    def __init__(self) -> None:
        self.tmux = object()

    def selected_key(self) -> str:
        return "dashboard"


@pytest.fixture
def isolated_app(monkeypatch, tmp_path: Path) -> PollyCockpitApp:
    """Construct a PollyCockpitApp without running ``on_mount``.

    Patches every collaborator the constructor expects so we get a
    bare object we can poke at — the actual UI never mounts.
    """
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("")

    monkeypatch.setattr("pollypm.cockpit_ui.CockpitRouter", lambda *a, **k: _StubRouter())
    monkeypatch.setattr("pollypm.cockpit_ui.CockpitPresence", lambda *a, **k: object())

    class _StubService:
        def __init__(self, *a, **k) -> None:
            pass

    monkeypatch.setattr("pollypm.service_api.PollyPMService", _StubService)

    app = PollyCockpitApp(config_path)
    return app


def test_resolve_interval_uses_class_default(isolated_app, monkeypatch):
    monkeypatch.delenv("POLLYPM_RAIL_LIVENESS_INTERVAL_SECONDS", raising=False)
    assert isolated_app._resolve_rail_liveness_interval() == float(
        PollyCockpitApp.RAIL_LIVENESS_CHECK_INTERVAL_SECONDS
    )


def test_resolve_interval_honours_env(isolated_app, monkeypatch):
    monkeypatch.setenv("POLLYPM_RAIL_LIVENESS_INTERVAL_SECONDS", "5")
    assert isolated_app._resolve_rail_liveness_interval() == 5.0


def test_resolve_interval_garbage_falls_back(isolated_app, monkeypatch):
    monkeypatch.setenv("POLLYPM_RAIL_LIVENESS_INTERVAL_SECONDS", "not-a-number")
    assert isolated_app._resolve_rail_liveness_interval() == float(
        PollyCockpitApp.RAIL_LIVENESS_CHECK_INTERVAL_SECONDS
    )


def test_resolve_interval_negative_falls_back(isolated_app, monkeypatch):
    monkeypatch.setenv("POLLYPM_RAIL_LIVENESS_INTERVAL_SECONDS", "-10")
    assert isolated_app._resolve_rail_liveness_interval() == float(
        PollyCockpitApp.RAIL_LIVENESS_CHECK_INTERVAL_SECONDS
    )


def test_uses_external_rail_daemon_returns_boot_capture_true(isolated_app):
    """Boot-time captured ``True`` means the cockpit deferred to a daemon."""
    isolated_app._rail_daemon_was_external = True
    assert isolated_app._cockpit_uses_external_rail_daemon() is True


def test_uses_external_rail_daemon_default_is_false(isolated_app):
    """Without a boot-time capture (cockpit-hosted mode) the gate is False."""
    assert isolated_app._rail_daemon_was_external is False
    assert isolated_app._cockpit_uses_external_rail_daemon() is False


def test_tick_rail_liveness_skips_when_cockpit_hosted(
    isolated_app, monkeypatch,
):
    """When the cockpit is hosting the rail the tick MUST NOT call ``revive_if_needed``.

    Running the watcher in cockpit-hosted mode would race the
    cockpit's own ticker thread (the contention failure described in
    ``_start_core_rail``).
    """
    isolated_app._rail_daemon_was_external = False  # cockpit-hosted
    called: list[bool] = []

    def _fake_revive(**kwargs):
        called.append(True)
        raise AssertionError("revive_if_needed must not run in cockpit-hosted mode")

    monkeypatch.setattr(
        "pollypm.rail_daemon_supervisor.revive_if_needed", _fake_revive,
    )

    # Method should swallow without raising and without calling the helper.
    isolated_app._tick_rail_liveness()
    assert called == []


def test_tick_rail_liveness_invokes_revive_in_external_mode(
    isolated_app, monkeypatch, tmp_path: Path,
):
    """When a daemon was alive at boot, the tick DOES call ``revive_if_needed``."""
    isolated_app._rail_daemon_was_external = True

    captured: dict[str, object] = {}

    def _fake_revive(**kwargs):
        captured.update(kwargs)
        # Return a stub RevivalResult that signals "alive" so the
        # tick treats this as a no-op revival.
        from pollypm.rail_daemon_supervisor import RevivalDecision, RevivalResult
        return RevivalResult(
            decision=RevivalDecision(
                state="alive",
                pid=12345,
                last_tick_age_seconds=10.0,
                reason="pid alive",
            ),
            revived=False,
            spawn_error=None,
            killed_pid=None,
            kill_signal=None,
        )

    monkeypatch.setattr(
        "pollypm.rail_daemon_supervisor.revive_if_needed", _fake_revive,
    )

    isolated_app._tick_rail_liveness()
    assert "config_path" in captured
    assert "pid_path" in captured
