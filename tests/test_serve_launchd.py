"""Tests for :mod:`pollypm.serve_launchd`.

The launchctl boundary is always faked here; these tests never load or
unload a real launchd service.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import typer
from typer.testing import CliRunner

from pollypm.cli_features.web_api import register_web_api_commands
from pollypm.serve_launchd import (
    DEFAULT_LABEL,
    EVENT_DAEMON_SERVE_RESPAWN,
    LaunchdActionResult,
    install_launch_agent,
    plist_path_for_label,
    quiesced_marker_path,
    record_serve_startup,
    render_plist,
    serve_pid_path,
    start_launch_agent,
    stop_launch_agent,
    uninstall_launch_agent,
)


class LaunchctlSpy:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(
            args=argv,
            returncode=self.returncode,
            stdout="",
            stderr=self.stderr,
        )


def test_rendered_plist_matches_approved_launchd_shape(tmp_path: Path) -> None:
    home = tmp_path / "home"
    pm_binary = tmp_path / "bin" / "pm"
    rendered = render_plist(home=home, pm_binary=pm_binary)
    parsed = plistlib.loads(rendered.encode("utf-8"))

    assert parsed["Label"] == DEFAULT_LABEL
    assert parsed["ProgramArguments"] == [str(pm_binary), "serve"]
    assert parsed["KeepAlive"] is True
    assert parsed["RunAtLoad"] is True
    assert parsed["ThrottleInterval"] == 10
    assert parsed["StandardOutPath"] == str(home / ".pollypm" / "logs" / "serve.log")
    assert parsed["StandardErrorPath"] == parsed["StandardOutPath"]


def test_install_writes_launchagent_and_calls_launchctl_load(tmp_path: Path) -> None:
    spy = LaunchctlSpy()
    home = tmp_path / "home"
    plist_dir = tmp_path / "LaunchAgents"
    pm_binary = tmp_path / "pm"

    result = install_launch_agent(
        home=home,
        plist_dir=plist_dir,
        pm_binary=pm_binary,
        launchctl_runner=spy,
    )

    assert result.plist_path == plist_dir / f"{DEFAULT_LABEL}.plist"
    assert result.plist_path.exists()
    assert (home / ".pollypm" / "logs").is_dir()
    assert spy.calls == [["launchctl", "load", str(result.plist_path)]]


def test_uninstall_unloads_then_removes_existing_plist(tmp_path: Path) -> None:
    spy = LaunchctlSpy()
    home = tmp_path / "home"
    plist_dir = tmp_path / "LaunchAgents"
    pm_binary = tmp_path / "pm"
    installed = install_launch_agent(
        home=home,
        plist_dir=plist_dir,
        pm_binary=pm_binary,
        launchctl_runner=spy,
    )
    spy.calls.clear()

    result = uninstall_launch_agent(
        home=home,
        plist_dir=plist_dir,
        launchctl_runner=spy,
    )

    assert result.removed is True
    assert result.plist_path == installed.plist_path
    assert not installed.plist_path.exists()
    assert spy.calls == [["launchctl", "unload", str(installed.plist_path)]]


def test_stop_writes_quiesced_marker_and_unloads(tmp_path: Path) -> None:
    spy = LaunchctlSpy()
    home = tmp_path / "home"
    plist_dir = tmp_path / "LaunchAgents"
    expected_plist = plist_path_for_label(plist_dir=plist_dir)

    result = stop_launch_agent(
        home=home,
        plist_dir=plist_dir,
        launchctl_runner=spy,
    )

    assert quiesced_marker_path(home=home).read_text(encoding="utf-8") == "quiesced\n"
    assert result.plist_path == expected_plist
    assert spy.calls == [["launchctl", "unload", str(expected_plist)]]


def test_start_removes_quiesced_marker_writes_plist_and_loads(tmp_path: Path) -> None:
    spy = LaunchctlSpy()
    home = tmp_path / "home"
    plist_dir = tmp_path / "LaunchAgents"
    pm_binary = tmp_path / "pm"
    marker = quiesced_marker_path(home=home)
    marker.parent.mkdir(parents=True)
    marker.write_text("quiesced\n", encoding="utf-8")

    result = start_launch_agent(
        home=home,
        plist_dir=plist_dir,
        pm_binary=pm_binary,
        launchctl_runner=spy,
    )

    assert not marker.exists()
    assert result.plist_path.exists()
    assert spy.calls == [["launchctl", "load", str(result.plist_path)]]


def test_record_serve_startup_emits_respawn_when_pid_changes(tmp_path: Path) -> None:
    base_dir = tmp_path / ".pollypm"
    base_dir.mkdir()
    serve_pid_path(base_dir).write_text("111\n", encoding="utf-8")
    emitted: list[dict[str, Any]] = []

    record_serve_startup(
        base_dir=base_dir,
        current_pid=222,
        audit_emit=lambda **kwargs: emitted.append(kwargs),
    )

    assert serve_pid_path(base_dir).read_text(encoding="utf-8") == "222\n"
    assert emitted == [
        {
            "event": EVENT_DAEMON_SERVE_RESPAWN,
            "project": "_workspace",
            "subject": "serve/222",
            "actor": "system",
            "status": "warn",
            "metadata": {
                "role": "serve",
                "previous_pid": 111,
                "pid": 222,
                "pid_file": str(serve_pid_path(base_dir)),
            },
        }
    ]


def test_record_serve_startup_skips_audit_when_pid_is_same(tmp_path: Path) -> None:
    base_dir = tmp_path / ".pollypm"
    base_dir.mkdir()
    serve_pid_path(base_dir).write_text("333\n", encoding="utf-8")
    emitted: list[dict[str, Any]] = []

    record_serve_startup(
        base_dir=base_dir,
        current_pid=333,
        audit_emit=lambda **kwargs: emitted.append(kwargs),
    )

    assert emitted == []
    assert serve_pid_path(base_dir).read_text(encoding="utf-8") == "333\n"


def test_pm_serve_install_subcommand_does_not_start_server(monkeypatch) -> None:
    root = typer.Typer()
    register_web_api_commands(root)

    def _explode_load_config(_path):
        raise AssertionError("serve callback should not run for serve install")

    def _fake_install() -> LaunchdActionResult:
        return LaunchdActionResult(
            plist_path=Path("/tmp/com.pollypm.serve.plist"),
            launchctl_result=subprocess.CompletedProcess(
                args=["launchctl", "load", "/tmp/com.pollypm.serve.plist"],
                returncode=0,
                stdout="",
                stderr="",
            ),
        )

    monkeypatch.setattr("pollypm.cli_features.web_api.load_config", _explode_load_config)
    monkeypatch.setattr("pollypm.serve_launchd.install_launch_agent", _fake_install)

    result = CliRunner().invoke(root, ["serve", "install"])

    assert result.exit_code == 0, result.output
    assert "Installed /tmp/com.pollypm.serve.plist" in result.output


def test_pm_serve_startup_records_pid_from_config_base_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = typer.Typer()
    register_web_api_commands(root)
    base_dir = tmp_path / ".pollypm"
    config = SimpleNamespace(project=SimpleNamespace(base_dir=base_dir))
    recorded: list[Path] = []

    monkeypatch.setattr("pollypm.cli_features.web_api.load_config", lambda _path: config)
    monkeypatch.setattr(
        "pollypm.cli_features.web_api.detect_tailscale_ip",
        lambda: None,
    )
    monkeypatch.setattr(
        "pollypm.serve_launchd.record_serve_startup",
        lambda *, base_dir: recorded.append(base_dir),
    )
    monkeypatch.setattr(
        "pollypm.web_api.ensure_token",
        lambda _path: ("test-token", False),
    )
    monkeypatch.setattr("pollypm.web_api.create_app", lambda **_kwargs: object())

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(
        root,
        ["serve", "--token-path", str(tmp_path / "api-token")],
    )

    assert result.exit_code == 0, result.output
    assert recorded == [base_dir]
