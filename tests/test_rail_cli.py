"""Tests for er04 — `pm rail list/hide/show` CLI + `[rail]` config.

See docs/extensible-rail-spec.md §6 and issue #224.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from pollypm.config import load_config
from pollypm.models import RailSettings
from pollypm import rail_cli


runner = CliRunner()


# ---------------------------------------------------------------------------
# Config parser — [rail] section is honoured.
# ---------------------------------------------------------------------------


def test_rail_config_parsed_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "[project]\n"
        "name = \"PollyPM\"\n"
        f"root_dir = \"{tmp_path}\"\n"
        "tmux_session = \"pollypm\"\n"
        f"base_dir = \"{tmp_path / '.pollypm'}\"\n"
        "\n"
        "[rail]\n"
        'hidden_items = ["tools.activity", "workflows.queue"]\n'
        'collapsed_sections = ["system"]\n'
    )
    config = load_config(config_path)
    assert config.rail.hidden_items == ("tools.activity", "workflows.queue")
    assert config.rail.collapsed_sections == ("system",)


def test_rail_config_defaults_when_section_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "[project]\n"
        "name = \"PollyPM\"\n"
        f"root_dir = \"{tmp_path}\"\n"
        "tmux_session = \"pollypm\"\n"
        f"base_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    config = load_config(config_path)
    assert config.rail == RailSettings()


def test_rail_config_tolerates_bad_types(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "[project]\n"
        "name = \"PollyPM\"\n"
        f"root_dir = \"{tmp_path}\"\n"
        "tmux_session = \"pollypm\"\n"
        f"base_dir = \"{tmp_path / '.pollypm'}\"\n"
        "\n"
        "[rail]\n"
        'hidden_items = "not-a-list"\n'
    )
    config = load_config(config_path)
    assert config.rail.hidden_items == ()


# ---------------------------------------------------------------------------
# Renderer respects hidden_items and collapsed_sections.
# ---------------------------------------------------------------------------


def test_build_items_skips_hidden_items(monkeypatch, tmp_path: Path) -> None:
    from pollypm.cockpit import CockpitRouter
    from pollypm.models import KnownProject, ProjectKind

    class _FakeConfig:
        def __init__(self, tmp_path: Path) -> None:
            class Project:
                root_dir = tmp_path
                base_dir = tmp_path / ".pollypm"
                tmux_session = "pollypm"

            self.project = Project()
            self.projects = {
                "demo": KnownProject(key="demo", path=tmp_path / "demo", name="Demo", kind=ProjectKind.GIT),
            }
            # Attach a RailSettings that hides the inbox row.
            self.rail = RailSettings(hidden_items=("top.Inbox",))

    class FakeSupervisor:
        def __init__(self, cfg) -> None:
            self.config = cfg

        def status(self):
            return [], [], [], [], []

    monkeypatch.setattr(
        "pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 0,
    )
    monkeypatch.setattr(
        "pollypm.cockpit.load_config", lambda path: _FakeConfig(tmp_path),
    )
    cfg = _FakeConfig(tmp_path)
    cfg_path = tmp_path / "pollypm.toml"
    cfg_path.write_text(f"[project]\nname = \"P\"\nbase_dir = \"{tmp_path}\"\n")
    router = CockpitRouter(cfg_path)
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor(cfg))

    items = router.build_items(spinner_index=0)
    keys = [i.key for i in items]
    assert "inbox" not in keys


def test_build_items_collapses_sections(monkeypatch, tmp_path: Path) -> None:
    from pollypm.cockpit import CockpitRouter
    from pollypm.models import KnownProject, ProjectKind

    class _FakeConfig:
        def __init__(self, tmp_path: Path) -> None:
            class Project:
                root_dir = tmp_path
                base_dir = tmp_path / ".pollypm"
                tmux_session = "pollypm"

            self.project = Project()
            self.projects = {
                "demo": KnownProject(key="demo", path=tmp_path / "demo", name="Demo", kind=ProjectKind.GIT),
            }
            self.rail = RailSettings(collapsed_sections=("system",))

    class FakeSupervisor:
        def __init__(self, cfg) -> None:
            self.config = cfg

        def status(self):
            return [], [], [], [], []

    monkeypatch.setattr(
        "pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 0,
    )
    monkeypatch.setattr(
        "pollypm.cockpit.load_config", lambda path: _FakeConfig(tmp_path),
    )
    cfg = _FakeConfig(tmp_path)
    cfg_path = tmp_path / "pollypm.toml"
    cfg_path.write_text(f"[project]\nname = \"P\"\nbase_dir = \"{tmp_path}\"\n")
    router = CockpitRouter(cfg_path)
    monkeypatch.setattr(router, "_load_supervisor", lambda: FakeSupervisor(cfg))

    items = router.build_items(spinner_index=0)
    # Settings section should be rendered as a collapsed marker row.
    keys = [i.key for i in items]
    assert "settings" not in keys
    assert any(k.startswith("_section:system") for k in keys)


# ---------------------------------------------------------------------------
# CLI — list / hide / show
# ---------------------------------------------------------------------------


def _patch_user_config(monkeypatch, tmp_path: Path) -> Path:
    user_cfg = tmp_path / "home" / ".pollypm" / "pollypm.toml"
    user_cfg.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rail_cli, "USER_CONFIG_PATH", user_cfg)
    return user_cfg


def _app():
    app = typer.Typer()
    app.add_typer(rail_cli.rail_app, name="rail")
    return app


def test_pm_rail_list_human(monkeypatch, tmp_path: Path) -> None:
    _patch_user_config(monkeypatch, tmp_path)
    result = runner.invoke(_app(), ["rail", "list"])
    assert result.exit_code == 0, result.stdout
    # core_rail_items registrations surface in stdout.
    assert "polly" in result.stdout.lower() or "POLLY" in result.stdout.upper() or "Polly" in result.stdout


def test_pm_rail_list_json(monkeypatch, tmp_path: Path) -> None:
    _patch_user_config(monkeypatch, tmp_path)
    result = runner.invoke(_app(), ["rail", "list", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) > 0
    item_keys = [p["item_key"] for p in payload]
    # Confirm the core items are present.
    assert "top.Polly" in item_keys
    assert "top.Inbox" in item_keys
    assert "system.Settings" in item_keys
    # Confirm the JSON entries have the expected fields.
    sample = payload[0]
    for field in ("section", "index", "label", "plugin", "item_key", "visibility", "hidden"):
        assert field in sample


def test_pm_rail_hide_appends_to_config(monkeypatch, tmp_path: Path) -> None:
    user_cfg = _patch_user_config(monkeypatch, tmp_path)
    result = runner.invoke(_app(), ["rail", "hide", "tools.activity"])
    assert result.exit_code == 0, result.stdout
    assert "Hid rail item" in result.stdout

    contents = user_cfg.read_text()
    assert "[rail]" in contents
    assert 'hidden_items = ["tools.activity"]' in contents

    # Double-hide is a no-op.
    result2 = runner.invoke(_app(), ["rail", "hide", "tools.activity"])
    assert result2.exit_code == 0
    assert "already hidden" in result2.stdout


def test_pm_rail_show_removes_from_config(monkeypatch, tmp_path: Path) -> None:
    user_cfg = _patch_user_config(monkeypatch, tmp_path)
    # Pre-populate a hidden entry.
    user_cfg.write_text(
        '[rail]\nhidden_items = ["tools.activity", "workflows.queue"]\ncollapsed_sections = []\n'
    )
    result = runner.invoke(_app(), ["rail", "show", "tools.activity"])
    assert result.exit_code == 0, result.stdout

    contents = user_cfg.read_text()
    assert "workflows.queue" in contents
    assert "tools.activity" not in contents

    # Show on an un-hidden item is a no-op.
    result2 = runner.invoke(_app(), ["rail", "show", "tools.activity"])
    assert result2.exit_code == 0
    assert "not hidden" in result2.stdout


def test_pm_rail_hide_validates_key_format(monkeypatch, tmp_path: Path) -> None:
    _patch_user_config(monkeypatch, tmp_path)
    result = runner.invoke(_app(), ["rail", "hide", "nodotkey"])
    assert result.exit_code != 0
    assert "section.label" in result.stderr + result.stdout


def test_pm_rail_list_marks_hidden_items(monkeypatch, tmp_path: Path) -> None:
    user_cfg = _patch_user_config(monkeypatch, tmp_path)
    user_cfg.write_text('[rail]\nhidden_items = ["top.Inbox"]\ncollapsed_sections = []\n')
    result = runner.invoke(_app(), ["rail", "list", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    inbox_entry = next(p for p in payload if p["item_key"] == "top.Inbox")
    assert inbox_entry["hidden"] is True
