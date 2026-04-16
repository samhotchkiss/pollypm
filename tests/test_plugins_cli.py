"""Tests for the `pm plugins` subcommand group (issue #171)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.plugin_cli import plugins_app


runner = CliRunner()


def _write_plugin_dir(root: Path, name: str, body: str = "", manifest_extras: str = "") -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "pollypm-plugin.toml").write_text(
        f'''api_version = "1"
name = "{name}"
version = "0.1.0"
entrypoint = "plugin.py:plugin"
{manifest_extras}
'''
    )
    (plugin_dir / "plugin.py").write_text(
        body
        or (
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            f"plugin = PollyPMPlugin(name='{name}')\n"
        )
    )
    return plugin_dir


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    # Point plugin_cli's module-level constants at the fake home too.
    from pollypm import plugin_cli

    monkeypatch.setattr(plugin_cli, "USER_PLUGINS_DIR", fake_home / ".pollypm" / "plugins")
    monkeypatch.setattr(plugin_cli, "USER_CONFIG_PATH", fake_home / ".pollypm" / "pollypm.toml")
    monkeypatch.chdir(tmp_path)
    yield fake_home


def test_plugins_list_json_default_builtins(_isolate_home) -> None:
    result = runner.invoke(plugins_app, ["list", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    names = {item["name"] for item in data}
    assert "claude" in names
    assert "codex" in names


def test_plugins_list_text_mode_shows_rows(_isolate_home) -> None:
    result = runner.invoke(plugins_app, ["list"])
    assert result.exit_code == 0, result.output
    assert "claude" in result.output
    assert "codex" in result.output
    assert "provider" in result.output


def test_plugins_show_existing_plugin_json(_isolate_home) -> None:
    result = runner.invoke(plugins_app, ["show", "claude", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["name"] == "claude"
    assert any(c["kind"] == "provider" for c in data["capabilities"])


def test_plugins_show_missing_plugin_errors(_isolate_home) -> None:
    result = runner.invoke(plugins_app, ["show", "nonexistent", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert "error" in data


def test_plugins_disable_writes_config_and_enable_removes(_isolate_home) -> None:
    from pollypm import plugin_cli

    # Disable
    result = runner.invoke(plugins_app, ["disable", "magic", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "disabled"
    assert plugin_cli.USER_CONFIG_PATH.exists()
    assert "magic" in plugin_cli.USER_CONFIG_PATH.read_text()

    # Disable again → idempotent
    result = runner.invoke(plugins_app, ["disable", "magic", "--json"])
    data = json.loads(result.output)
    assert data["status"] == "already_disabled"

    # Enable
    result = runner.invoke(plugins_app, ["enable", "magic", "--json"])
    data = json.loads(result.output)
    assert data["status"] == "enabled"
    assert "magic" not in plugin_cli.USER_CONFIG_PATH.read_text()

    # Enable again → idempotent
    result = runner.invoke(plugins_app, ["enable", "magic", "--json"])
    data = json.loads(result.output)
    assert data["status"] == "already_enabled"


def test_plugins_install_local_directory(_isolate_home, tmp_path: Path) -> None:
    from pollypm import plugin_cli

    src_dir = tmp_path / "source" / "my-plugin"
    src_dir.parent.mkdir()
    _write_plugin_dir(src_dir.parent, "my-plugin")

    result = runner.invoke(plugins_app, ["install", str(src_dir), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["method"] == "directory_copy"
    assert (plugin_cli.USER_PLUGINS_DIR / "my-plugin").exists()


def test_plugins_install_rejects_existing_target(_isolate_home, tmp_path: Path) -> None:
    from pollypm import plugin_cli

    src_dir = tmp_path / "source" / "dup"
    src_dir.parent.mkdir()
    _write_plugin_dir(src_dir.parent, "dup")
    # Pre-create the target
    (plugin_cli.USER_PLUGINS_DIR / "dup").mkdir(parents=True)

    result = runner.invoke(plugins_app, ["install", str(src_dir), "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert "already exists" in data["error"]


def test_plugins_uninstall_local_dir(_isolate_home) -> None:
    from pollypm import plugin_cli

    target = plugin_cli.USER_PLUGINS_DIR / "byebye"
    _write_plugin_dir(plugin_cli.USER_PLUGINS_DIR, "byebye")
    assert target.exists()

    result = runner.invoke(plugins_app, ["uninstall", "byebye", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["method"] == "directory_remove"
    assert not target.exists()


def test_plugins_doctor_json(_isolate_home) -> None:
    result = runner.invoke(plugins_app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "plugins_loaded" in data
    assert "validation" in data
    assert "all_passed" in data["validation"]


def test_plugins_list_shows_disabled(_isolate_home) -> None:
    # Disable magic first
    runner.invoke(plugins_app, ["disable", "magic"])

    result = runner.invoke(plugins_app, ["list", "--json"])
    data = json.loads(result.output)
    magic = next((p for p in data if p["name"] == "magic"), None)
    assert magic is not None
    assert magic["status"] == "disabled"
    assert magic["reason"] == "config"
