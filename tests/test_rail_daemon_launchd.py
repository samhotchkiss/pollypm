"""Tests for :mod:`pollypm.rail_daemon_launchd` (layer-3 supervision).

These tests never write to the user's real ``~/Library/LaunchAgents/``
or actually invoke ``launchctl``. Both are stubbed via the
``plist_dir`` and ``launchctl_runner`` injection points.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from pollypm.rail_daemon_launchd import (
    DEFAULT_LABEL,
    DEFAULT_THROTTLE_INTERVAL_SECONDS,
    build_plist_dict,
    install_launchd_keepalive,
    plist_path_for_label,
    uninstall_launchd_keepalive,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _LaunchctlSpy:
    """Captures every ``launchctl`` invocation a test triggers."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        return subprocess.CompletedProcess(
            args=argv, returncode=self.returncode, stdout="", stderr=self.stderr,
        )


# ---------------------------------------------------------------------------
# build_plist_dict
# ---------------------------------------------------------------------------


def test_plist_has_keep_alive_and_run_at_load():
    plist = build_plist_dict(
        label=DEFAULT_LABEL,
        config_path=Path("/Users/sam/.pollypm/pollypm.toml"),
    )
    assert plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True


def test_plist_program_args_include_config_path():
    plist = build_plist_dict(
        label=DEFAULT_LABEL,
        config_path=Path("/tmp/test/pollypm.toml"),
        python_executable="/usr/bin/python3",
    )
    args = plist["ProgramArguments"]
    assert args[0] == "/usr/bin/python3"
    assert args[1] == "-m"
    assert args[2] == "pollypm.rail_daemon"
    assert "--config" in args
    assert "/tmp/test/pollypm.toml" in args


def test_plist_label_is_propagated():
    plist = build_plist_dict(
        label="com.example.test",
        config_path=Path("/tmp/cfg.toml"),
    )
    assert plist["Label"] == "com.example.test"


def test_plist_throttle_interval_default():
    plist = build_plist_dict(
        label=DEFAULT_LABEL, config_path=Path("/tmp/cfg.toml"),
    )
    assert plist["ThrottleInterval"] == DEFAULT_THROTTLE_INTERVAL_SECONDS


def test_plist_log_path_default(tmp_path: Path):
    plist = build_plist_dict(
        label=DEFAULT_LABEL, config_path=Path("/tmp/cfg.toml"),
    )
    # The default log path is the same one ``cli._spawn_rail_daemon``
    # writes to — operators don't have to track two log files.
    assert plist["StandardOutPath"].endswith(".pollypm/rail_daemon.log")
    assert plist["StandardErrorPath"] == plist["StandardOutPath"]


def test_plist_serializes_with_plistlib(tmp_path: Path):
    """The shape we build must round-trip through ``plistlib.dumps``."""
    plist = build_plist_dict(
        label=DEFAULT_LABEL, config_path=tmp_path / "pollypm.toml",
    )
    raw = plistlib.dumps(plist)
    parsed = plistlib.loads(raw)
    assert parsed["Label"] == DEFAULT_LABEL
    assert parsed["KeepAlive"] is True


# ---------------------------------------------------------------------------
# install_launchd_keepalive
# ---------------------------------------------------------------------------


def test_install_writes_plist_file(tmp_path: Path):
    spy = _LaunchctlSpy()
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("")
    plist_dir = tmp_path / "LaunchAgents"

    plist_path = install_launchd_keepalive(
        config_path=config_path,
        plist_dir=plist_dir,
        launchctl_runner=spy,
    )

    assert plist_path.exists()
    assert plist_path == plist_dir / f"{DEFAULT_LABEL}.plist"
    parsed = plistlib.loads(plist_path.read_bytes())
    assert parsed["Label"] == DEFAULT_LABEL


def test_install_calls_launchctl_bootout_then_bootstrap(tmp_path: Path):
    """We bootout first to handle re-installs cleanly."""
    spy = _LaunchctlSpy()
    config_path = tmp_path / "pollypm.toml"
    plist_dir = tmp_path / "LaunchAgents"

    install_launchd_keepalive(
        config_path=config_path,
        plist_dir=plist_dir,
        launchctl_runner=spy,
    )

    assert len(spy.calls) == 2
    assert spy.calls[0][1] == "bootout"
    assert spy.calls[1][1] == "bootstrap"


def test_install_with_load_false_skips_launchctl(tmp_path: Path):
    spy = _LaunchctlSpy()
    config_path = tmp_path / "pollypm.toml"
    plist_dir = tmp_path / "LaunchAgents"

    install_launchd_keepalive(
        config_path=config_path,
        plist_dir=plist_dir,
        launchctl_runner=spy,
        load=False,
    )

    assert spy.calls == []


def test_install_creates_parent_directory(tmp_path: Path):
    """Tolerate a fresh user account without ``~/Library/LaunchAgents``."""
    spy = _LaunchctlSpy()
    config_path = tmp_path / "pollypm.toml"
    plist_dir = tmp_path / "deeply" / "nested" / "LaunchAgents"

    plist_path = install_launchd_keepalive(
        config_path=config_path,
        plist_dir=plist_dir,
        launchctl_runner=spy,
    )

    assert plist_path.exists()
    assert plist_dir.exists()


def test_install_returncode_nonzero_does_not_raise(tmp_path: Path):
    spy = _LaunchctlSpy(returncode=5, stderr="already loaded")
    config_path = tmp_path / "pollypm.toml"
    plist_dir = tmp_path / "LaunchAgents"

    # Should not raise — bootstrap returncode is informational, not fatal.
    plist_path = install_launchd_keepalive(
        config_path=config_path,
        plist_dir=plist_dir,
        launchctl_runner=spy,
    )
    assert plist_path.exists()


# ---------------------------------------------------------------------------
# uninstall_launchd_keepalive
# ---------------------------------------------------------------------------


def test_uninstall_removes_plist_and_calls_bootout(tmp_path: Path):
    spy = _LaunchctlSpy()
    config_path = tmp_path / "pollypm.toml"
    plist_dir = tmp_path / "LaunchAgents"
    install_launchd_keepalive(
        config_path=config_path, plist_dir=plist_dir, launchctl_runner=spy,
    )
    assert (plist_dir / f"{DEFAULT_LABEL}.plist").exists()

    spy.calls.clear()
    removed = uninstall_launchd_keepalive(
        plist_dir=plist_dir, launchctl_runner=spy,
    )
    assert removed is True
    assert not (plist_dir / f"{DEFAULT_LABEL}.plist").exists()
    assert any(c[1] == "bootout" for c in spy.calls)


def test_uninstall_when_no_plist_returns_false(tmp_path: Path):
    spy = _LaunchctlSpy()
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()

    removed = uninstall_launchd_keepalive(
        plist_dir=plist_dir, launchctl_runner=spy,
    )
    assert removed is False
    assert spy.calls == []


def test_plist_path_for_label_default():
    path = plist_path_for_label("com.example.thing")
    assert path.name == "com.example.thing.plist"
    assert "LaunchAgents" in str(path)
