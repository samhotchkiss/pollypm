"""Tests for er02 — core_rail_items built-in plugin + registry-driven rail.

See docs/extensible-rail-spec.md §5 and issue #222.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.cockpit import CockpitRouter
from pollypm.models import KnownProject, ProjectKind
from pollypm.plugin_api.v1 import (
    PanelSpec,
    PollyPMPlugin,
    RailContext,
    RailRegistry,
)
from pollypm.plugin_host import ExtensionHost


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "[project]\n"
        "name = \"PollyPM\"\n"
        f"root_dir = \"{tmp_path}\"\n"
        "tmux_session = \"pollypm\"\n"
        f"base_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    return config_path


class _FakeConfig:
    def __init__(self, tmp_path: Path) -> None:
        class Project:
            root_dir = tmp_path
            base_dir = tmp_path / ".pollypm"
            tmux_session = "pollypm"

        self.project = Project()
        self.projects = {
            "pollypm": KnownProject(
                key="pollypm", path=tmp_path, name="PollyPM",
                persona_name="Pete", kind=ProjectKind.GIT,
            ),
            "demo": KnownProject(
                key="demo", path=tmp_path / "demo", name="Demo",
                persona_name="Dora", kind=ProjectKind.GIT,
            ),
        }


class _FakeLaunch:
    def __init__(self, name: str, role: str, project: str, window_name: str) -> None:
        self.window_name = window_name
        self.session = type(
            "Session", (),
            {
                "name": name, "role": role, "project": project,
                "provider": type("P", (), {"value": "claude"})(),
            },
        )()


class _FakeWindow:
    def __init__(self, name: str, pane_dead: bool = False) -> None:
        self.name = name
        self.pane_dead = pane_dead
        self.pane_id = f"%{name}"


def _fake_supervisor(tmp_path: Path):
    config = _FakeConfig(tmp_path)

    class FakeSupervisor:
        def __init__(self) -> None:
            self.config = config

        def status(self):
            launches = [
                _FakeLaunch("operator", "operator-pm", "pollypm", "pm-operator"),
                _FakeLaunch("worker_demo", "worker", "demo", "worker-demo"),
            ]
            windows = [_FakeWindow("pm-operator"), _FakeWindow("worker-demo")]
            return launches, windows, [], [], []

    return FakeSupervisor()


def test_core_rail_items_plugin_loads_from_builtin_dir() -> None:
    """The core_rail_items plugin must be discoverable via the default
    builtin search path (no project-local manifest required)."""
    host = ExtensionHost(Path("/tmp"))
    plugins = host.plugins()
    assert "core_rail_items" in plugins


def test_build_items_identical_to_legacy_shape(monkeypatch, tmp_path: Path) -> None:
    """Visual-parity test — rail items match the legacy hardcoded shape."""
    monkeypatch.setattr(
        "pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 1,
    )
    _write_config(tmp_path)
    router = CockpitRouter(tmp_path / "pollypm.toml")
    monkeypatch.setattr(router, "_load_supervisor", lambda: _fake_supervisor(tmp_path))

    items = router.build_items(spinner_index=2)
    keys = [item.key for item in items]

    # Core rail entries all present.
    assert "polly" in keys
    assert "russell" in keys
    assert "inbox" in keys
    assert "project:pollypm" in keys
    assert "project:demo" in keys
    assert "settings" in keys

    # Order: top section (polly/russell/inbox) first, then projects, settings last.
    assert items[0].key == "polly"
    assert items[1].key == "russell"
    assert items[2].key == "inbox"
    assert items[2].label == "Inbox (1)"
    assert items[-1].key == "settings"


def test_removing_core_rail_items_yields_empty_rail(monkeypatch, tmp_path: Path) -> None:
    """Acceptance: if `core_rail_items` is removed, the rail is empty.

    The activity_feed plugin also registers a rail entry (lf03) — we
    disable it here too so the assertion still isolates the effect of
    removing the core items.
    """
    # Simulate the plugins being disabled by config.
    host = ExtensionHost(tmp_path, disabled=("core_rail_items", "activity_feed"))
    assert "core_rail_items" not in host.plugins()

    monkeypatch.setattr(
        "pollypm.plugin_host.extension_host_for_root", lambda root: host,
    )
    monkeypatch.setattr(
        "pollypm.cockpit._count_inbox_tasks_for_label", lambda config: 0,
    )
    _write_config(tmp_path)
    router = CockpitRouter(tmp_path / "pollypm.toml")
    monkeypatch.setattr(router, "_load_supervisor", lambda: _fake_supervisor(tmp_path))

    items = router.build_items(spinner_index=0)
    # Rail should be completely empty.
    assert items == []


def test_third_party_plugin_registers_below_core(monkeypatch, tmp_path: Path) -> None:
    """A third-party plugin registering at workflows:150 appears after core items."""
    # Create a plugin that adds a workflows:150 item.
    plugin_dir = tmp_path / "plugins_dir" / "thirdparty"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "pollypm-plugin.toml").write_text(
        """
api_version = "1"
name = "thirdparty"
kind = "rail"
version = "0.1.0"
entrypoint = "plugin.py:plugin"
description = "third-party rail registration"
"""
    )
    (plugin_dir / "plugin.py").write_text(
        """
from pollypm.plugin_api.v1 import PollyPMPlugin, PanelSpec


def _handler(ctx):
    return PanelSpec(widget=None)


def _init(api):
    api.rail.register_item(
        section="workflows",
        index=150,
        label="ThirdParty",
        handler=_handler,
        key="tp",
    )


plugin = PollyPMPlugin(name="thirdparty", initialize=_init)
"""
    )

    # Build host that includes both builtin (to get core_rail_items) and the
    # third-party directory.
    host = ExtensionHost(tmp_path)
    builtin_path = Path(__file__).resolve().parents[1] / "src" / "pollypm" / "plugins_builtin"
    host._plugin_search_paths = lambda: [  # type: ignore[assignment]
        ("builtin", builtin_path),
        ("project", tmp_path / "plugins_dir"),
    ]
    host.initialize_plugins(config=_FakeConfig(tmp_path))

    items = host.rail_registry().items()
    # Extract (section, plugin_name, label) in render order.
    order = [(r.section, r.plugin_name, r.label) for r in items]
    # Third-party appears in workflows section (the only workflows entry).
    assert ("workflows", "thirdparty", "ThirdParty") in order

    # Within the top section, core items have low indexes (0, 10, 20) so
    # they render before any plugin-contributed items at index 100+.
    for idx, (section, plugin, _label) in enumerate(order):
        if section == "workflows" and plugin == "thirdparty":
            # Every earlier item is either core (index < 100) or from a
            # different section appearing earlier in RAIL_SECTIONS order.
            for earlier in order[:idx]:
                e_section, e_plugin, _e_label = earlier
                if e_section == "workflows":
                    # Any earlier workflow item must be a core
                    # registration (e.g. activity_feed at index 30) or
                    # the third-party item itself. Plugin-contributed
                    # items at 100+ must always sort after thirdparty
                    # (150) only if their index is higher.
                    assert e_plugin in {
                        "core_rail_items",
                        "activity_feed",
                        "thirdparty",
                    }


def test_rail_registry_items_honour_index_and_section_order() -> None:
    """Independently verify RailRegistry ordering semantics."""
    registry = RailRegistry()
    from pollypm.plugin_api.v1 import RailAPI

    def _h(ctx):
        return PanelSpec(widget=None)

    RailAPI(plugin_name="z", registry=registry).register_item(
        section="workflows", index=50, label="Z50", handler=_h,
    )
    RailAPI(plugin_name="a", registry=registry).register_item(
        section="workflows", index=50, label="A50", handler=_h,
    )
    RailAPI(plugin_name="any", registry=registry, reserved_allowed=True).register_item(
        section="top", index=0, label="Top0", handler=_h,
    )
    RailAPI(plugin_name="t", registry=registry).register_item(
        section="tools", index=0, label="Tool0", handler=_h,
    )
    order = [(r.section, r.label) for r in registry.items()]
    assert order == [
        ("top", "Top0"),
        ("workflows", "A50"),
        ("workflows", "Z50"),
        ("tools", "Tool0"),
    ]
