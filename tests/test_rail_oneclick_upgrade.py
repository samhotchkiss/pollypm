"""Tests for the one-click rail upgrade flow (#719).

Covers:
* ``action_trigger_upgrade`` spawns ``pm upgrade`` in a new tmux window
  (via the existing ``TmuxClient.create_window`` primitive).
* The pill swaps to "Upgrading…" while the window is active.
* ``_check_post_upgrade_flag`` reads ``~/.pollypm/post-upgrade.flag``
  and swaps the pill to a "restart to pick up new code" nudge.
* Failure modes degrade gracefully — no tmux, no window creation, no
  flag — without crashing the cockpit.

The actual ``pm upgrade`` subprocess never runs; we fake the tmux
client and assert on the ``create_window`` args.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_config(tmp_path: Path) -> Path:
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
def isolated_home(tmp_path, monkeypatch):
    """Route ``Path.home()`` into the sandbox so the post-upgrade flag
    read/write doesn't touch the developer's real ``~/.pollypm``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".pollypm").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))


# --------------------------------------------------------------------------- #
# action_trigger_upgrade — spawns the window
# --------------------------------------------------------------------------- #

def test_trigger_upgrade_spawns_pm_upgrade_window(fake_config, monkeypatch):
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(fake_config)
    fake_tmux = MagicMock()
    app.router.tmux = fake_tmux
    monkeypatch.setattr(
        app.router, "_load_supervisor",
        lambda fresh=False: SimpleNamespace(
            config=SimpleNamespace(
                project=SimpleNamespace(tmux_session="pollypm-test"),
            ),
        ),
    )

    app.action_trigger_upgrade()

    fake_tmux.create_window.assert_called_once()
    args, kwargs = fake_tmux.create_window.call_args
    assert args[0] == "pollypm-test"
    assert args[1] == "pm-upgrade"
    assert "pm upgrade" in args[2]
    assert kwargs.get("detached") is False


def test_trigger_upgrade_updates_pill_to_upgrading_state(fake_config):
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(fake_config)
    app.router.tmux = MagicMock()

    app.action_trigger_upgrade()

    rendered = str(app.update_pill.render())
    assert "Upgrading" in rendered
    assert "pm-upgrade" in rendered
    assert app.update_pill.display is True


def test_trigger_upgrade_handles_tmux_failure(fake_config, monkeypatch):
    from pollypm.cockpit_ui import PollyCockpitApp

    class _BrokenTmux:
        def create_window(self, *_args, **_kwargs):
            raise RuntimeError("tmux unavailable")

    app = PollyCockpitApp(fake_config)
    app.router.tmux = _BrokenTmux()
    notices: list[str] = []
    monkeypatch.setattr(
        app, "notify", lambda msg, *, timeout=None: notices.append(msg),
    )

    app.action_trigger_upgrade()

    assert len(notices) == 1
    assert "pm upgrade" in notices[0]
    assert "terminal" in notices[0]


def test_trigger_upgrade_falls_back_when_supervisor_missing(fake_config, monkeypatch):
    """No supervisor (e.g. not yet booted) → use the default tmux
    session name rather than crashing."""
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(fake_config)
    fake_tmux = MagicMock()
    app.router.tmux = fake_tmux
    monkeypatch.setattr(
        app.router, "_load_supervisor",
        lambda fresh=False: (_ for _ in ()).throw(RuntimeError("no supervisor")),
    )

    app.action_trigger_upgrade()

    fake_tmux.create_window.assert_called_once()
    args, _kwargs = fake_tmux.create_window.call_args
    # Default fallback tmux session name.
    assert args[0] == "pollypm"


# --------------------------------------------------------------------------- #
# _check_post_upgrade_flag — picks up the sentinel
# --------------------------------------------------------------------------- #

def test_check_flag_switches_pill_to_restart_nudge(fake_config):
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(fake_config)
    flag = Path.home() / ".pollypm" / "post-upgrade.flag"
    flag.write_text(json.dumps({"from": "0.1.0", "to": "0.2.0", "at": 1.0}))

    app._check_post_upgrade_flag()

    rendered = str(app.update_pill.render())
    assert "Upgraded" in rendered
    assert "v0.2.0" in rendered
    assert "restart" in rendered.lower()
    assert app.update_pill.display is True


def test_check_flag_no_op_when_absent(fake_config):
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(fake_config)
    # update_pill starts hidden — this must stay that way.
    app._check_post_upgrade_flag()
    # Not asserting display here because it's whatever Textual inits to;
    # we're checking the method doesn't crash and doesn't render a label.
    rendered = str(app.update_pill.render())
    assert "Upgraded" not in rendered


def test_check_flag_handles_malformed_json(fake_config):
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(fake_config)
    flag = Path.home() / ".pollypm" / "post-upgrade.flag"
    flag.write_text("not-json")

    # Should not raise.
    app._check_post_upgrade_flag()


def test_check_flag_respects_dismiss(fake_config):
    """A dismissed pill must not render "Upgraded" content even when
    the flag appears — the user explicitly opted out of the nudge for
    this session."""
    from pollypm.cockpit_ui import PollyCockpitApp

    app = PollyCockpitApp(fake_config)
    app._update_pill_dismissed = True
    flag = Path.home() / ".pollypm" / "post-upgrade.flag"
    flag.write_text(json.dumps({"from": "0.1.0", "to": "0.2.0", "at": 1.0}))

    app._check_post_upgrade_flag()
    rendered = str(app.update_pill.render())
    assert "Upgraded" not in rendered
    assert "restart" not in rendered.lower()
