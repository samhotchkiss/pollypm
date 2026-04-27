"""Tests for the rail update-available pill (#715).

Covers the three states the pill can be in:
* hidden when no upgrade is available or the check is offline
* visible when ``release_check.check_latest`` reports an upgrade
* hidden for the session after the user presses ``x``

Plus the keybindings: ``u`` triggers the upgrade action (stubbed
pending #719), ``x`` dismisses for the session.

Uses Textual's async test harness (``run_test()``) to drive the app.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm import release_check


@pytest.fixture
def fake_config(tmp_path: Path) -> Path:
    """Minimal config file the cockpit app can load without crashing."""
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "[project]\n"
        f'name = "PollyPM"\n'
        f'tmux_session = "pollypm-test"\n'
        f'root_dir = "{tmp_path}"\n'
        f'workspace_root = "{tmp_path}"\n'
        f'base_dir = "{tmp_path}/.pollypm"\n'
        f'logs_dir = "{tmp_path}/.pollypm/logs"\n'
        f'snapshots_dir = "{tmp_path}/.pollypm/snapshots"\n'
        f'state_db = "{tmp_path}/.pollypm/state.db"\n'
        "\n"
        f'[projects.demo]\n'
        f'key = "demo"\n'
        f'name = "Demo"\n'
        f'path = "{tmp_path}"\n'
    )
    (tmp_path / ".pollypm").mkdir(exist_ok=True)
    return config_path


@pytest.fixture(autouse=True)
def isolated_release_check_cache(tmp_path, monkeypatch):
    """Route the release-check cache into tmp_path so tests can seed
    it without touching the user's real ``~/.pollypm`` state."""
    monkeypatch.setattr(
        release_check, "_cache_path", lambda: tmp_path / "release-check.json",
    )


# --------------------------------------------------------------------------- #
# _update_pill_refresh — unit-level checks that don't need the full app
# --------------------------------------------------------------------------- #

def test_pill_refresh_hides_when_no_upgrade(fake_config, monkeypatch):
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(fake_config)
    monkeypatch.setattr(
        "pollypm.release_check.check_latest",
        lambda *a, **kw: release_check.ReleaseCheck(
            current="0.2.0", latest="0.2.0", channel="stable",
            upgrade_available=False, cached_at=0.0,
        ),
    )
    app._update_pill_refresh()
    assert app.update_pill.display is False


def test_pill_refresh_shows_when_upgrade_available(fake_config, monkeypatch):
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(fake_config)
    monkeypatch.setattr(
        "pollypm.release_check.check_latest",
        lambda *a, **kw: release_check.ReleaseCheck(
            current="0.1.0", latest="0.2.0", channel="stable",
            upgrade_available=True, cached_at=0.0,
        ),
    )
    app._update_pill_refresh()
    assert app.update_pill.display is True
    rendered = str(app.update_pill.render())
    assert "0.2.0" in rendered
    assert "u:" in rendered  # keybind hint
    assert "x:" in rendered


def test_pill_refresh_labels_beta_channel(fake_config, monkeypatch):
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(fake_config)
    monkeypatch.setattr(
        "pollypm.release_check.check_latest",
        lambda *a, **kw: release_check.ReleaseCheck(
            current="0.1.0", latest="0.3.0-beta.1", channel="beta",
            upgrade_available=True, cached_at=0.0,
        ),
    )
    app._update_pill_refresh()
    rendered = str(app.update_pill.render())
    assert "beta" in rendered


def test_pill_refresh_offline_stays_hidden(fake_config, monkeypatch):
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(fake_config)
    monkeypatch.setattr(
        "pollypm.release_check.check_latest",
        lambda *a, **kw: None,
    )
    app._update_pill_refresh()
    assert app.update_pill.display is False


def test_pill_refresh_swallows_exceptions(fake_config, monkeypatch):
    """A raised exception inside release_check must not crash the
    cockpit — the pill just stays hidden."""
    from pollypm.cockpit_ui import PollyCockpitApp

    def boom(*_a, **_kw):
        raise RuntimeError("synthetic network failure")

    app = PollyCockpitApp(fake_config)
    monkeypatch.setattr("pollypm.release_check.check_latest", boom)
    app._update_pill_refresh()
    assert app.update_pill.display is False


def test_dismiss_hides_pill_for_session(fake_config, monkeypatch):
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(fake_config)
    monkeypatch.setattr(
        "pollypm.release_check.check_latest",
        lambda *a, **kw: release_check.ReleaseCheck(
            current="0.1.0", latest="0.2.0", channel="stable",
            upgrade_available=True, cached_at=0.0,
        ),
    )
    app._update_pill_refresh()
    assert app.update_pill.display is True

    # Dismiss — even if the next tick finds an upgrade, stays hidden.
    app.action_dismiss_update_pill()
    assert app.update_pill.display is False
    app._update_pill_refresh()
    assert app.update_pill.display is False


# --------------------------------------------------------------------------- #
# Key bindings are present in the binding list
# --------------------------------------------------------------------------- #

def test_upgrade_binding_registered() -> None:
    from pollypm.cockpit_ui import PollyCockpitApp

    bindings = {b.key: b.action for b in PollyCockpitApp.BINDINGS}
    assert bindings.get("u") == "trigger_upgrade"
    assert bindings.get("x") == "dismiss_update_pill"


# --------------------------------------------------------------------------- #
# action_trigger_upgrade — stub pending #719
# --------------------------------------------------------------------------- #

def test_trigger_upgrade_emits_notification(fake_config, monkeypatch):
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(fake_config)
    notices: list[str] = []

    def _capture(message: str, *, timeout: int | None = None) -> None:
        del timeout
        notices.append(message)

    monkeypatch.setattr(app, "notify", _capture)
    app.action_trigger_upgrade()
    assert len(notices) == 1
    assert "pm upgrade" in notices[0]


def test_check_post_upgrade_flag_handles_corrupt_non_dict_payload(
    fake_config, tmp_path, monkeypatch,
) -> None:
    """Cycle 97: a corrupted ``post-upgrade.flag`` (parses to list/null/
    string) must not crash the cockpit on startup. The pill should
    fall back to a short restart hint rather than raising
    ``AttributeError`` on the ``payload.get("to")`` call.
    """
    from pollypm.cockpit_ui import PollyCockpitApp

    flag_dir = tmp_path / "fake_home" / ".pollypm"
    flag_dir.mkdir(parents=True)
    flag = flag_dir / "post-upgrade.flag"
    flag.write_text("[1, 2, 3]")

    app = PollyCockpitApp(fake_config)
    monkeypatch.setattr(app, "_post_upgrade_flag_path", lambda: flag)
    # Should not raise.
    app._check_post_upgrade_flag()
    rendered = str(app.update_pill.render())
    assert "Upgraded" in rendered
    assert "ctrl+q restart" in rendered
    assert "v?" in app.update_pill.tooltip

    # Sanity check: a well-formed payload renders the actual version.
    flag.write_text('{"to": "1.2.3", "from": "1.2.2", "at": 0}')
    # Reset dismiss flag so the refresh fires.
    app._update_pill_dismissed = False
    app.update_pill.update("")
    app.update_pill.display = False
    app._check_post_upgrade_flag()
    rendered = str(app.update_pill.render())
    assert "ctrl+q restart" in rendered
    assert "v1.2.3" in app.update_pill.tooltip


def test_post_upgrade_pill_keeps_restart_hint_inside_rail_width(
    fake_config, tmp_path, monkeypatch,
) -> None:
    from pollypm.cockpit_ui import PollyCockpitApp

    flag_dir = tmp_path / "fake_home" / ".pollypm"
    flag_dir.mkdir(parents=True)
    flag = flag_dir / "post-upgrade.flag"
    flag.write_text(
        '{"from": "1.0.0rc2", "to": "999.0.0", "at": 0, '
        '"notified": 3, "pending_restart": 3, "recycled": 0}'
    )
    app = PollyCockpitApp(fake_config)
    monkeypatch.setattr(app, "_post_upgrade_flag_path", lambda: flag)

    app._check_post_upgrade_flag()

    rendered = str(app.update_pill.render())
    assert "ctrl+q" in rendered
    assert "3 pending" in rendered
    assert "999.0.0" not in rendered
    assert "999.0.0" in app.update_pill.tooltip
