"""Tests for the rail registration API (er01).

See docs/extensible-rail-spec.md §3 and issue #221.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.plugin_api.v1 import (
    PanelSpec,
    PluginAPI,
    RAIL_SECTIONS,
    RESERVED_RAIL_SECTIONS,
    RailAPI,
    RailContext,
    RailItemRegistration,
    RailRegistry,
)
from pollypm.plugin_host import ExtensionHost, PluginManifest


def _noop_handler(ctx: RailContext) -> PanelSpec:
    return PanelSpec(widget="noop")


def test_rail_api_register_item_appends_registration() -> None:
    registry = RailRegistry()
    api = RailAPI(plugin_name="demo", registry=registry)

    reg = api.register_item(
        section="workflows",
        index=30,
        label="Activity",
        handler=_noop_handler,
    )

    assert isinstance(reg, RailItemRegistration)
    assert reg.plugin_name == "demo"
    assert reg.section == "workflows"
    assert reg.index == 30
    assert reg.label == "Activity"
    assert reg.visibility == "always"
    items = registry.items()
    assert len(items) == 1
    assert items[0].item_key == "workflows.Activity"


def test_rail_api_register_item_unknown_section_raises() -> None:
    registry = RailRegistry()
    api = RailAPI(plugin_name="demo", registry=registry)

    with pytest.raises(ValueError, match="Unknown rail section"):
        api.register_item(
            section="nonsense",
            index=10,
            label="Bad",
            handler=_noop_handler,
        )


def test_rail_api_register_item_non_callable_handler_raises() -> None:
    registry = RailRegistry()
    api = RailAPI(plugin_name="demo", registry=registry)

    with pytest.raises(TypeError, match="handler must be callable"):
        api.register_item(
            section="workflows",
            index=10,
            label="Bad",
            handler="not-a-callable",  # type: ignore[arg-type]
        )


def test_rail_api_register_item_bad_visibility_raises() -> None:
    registry = RailRegistry()
    api = RailAPI(plugin_name="demo", registry=registry)

    with pytest.raises(TypeError, match="visibility must be"):
        api.register_item(
            section="workflows",
            index=10,
            label="Bad",
            handler=_noop_handler,
            visibility="sometimes",  # type: ignore[arg-type]
        )


def test_rail_registry_items_sort_by_section_then_index_then_plugin() -> None:
    registry = RailRegistry()
    RailAPI(plugin_name="z_plugin", registry=registry).register_item(
        section="workflows", index=30, label="Z-Work", handler=_noop_handler,
    )
    RailAPI(plugin_name="a_plugin", registry=registry).register_item(
        section="workflows", index=30, label="A-Work", handler=_noop_handler,
    )
    RailAPI(plugin_name="mid", registry=registry).register_item(
        section="tools", index=0, label="Tool", handler=_noop_handler,
    )
    RailAPI(plugin_name="any", registry=registry, reserved_allowed=True).register_item(
        section="top", index=5, label="Home", handler=_noop_handler,
    )

    items = registry.items()
    order = [(r.section, r.plugin_name, r.label) for r in items]
    # top comes first; workflows: a_plugin wins tie on index 30.
    assert order == [
        ("top", "any", "Home"),
        ("workflows", "a_plugin", "A-Work"),
        ("workflows", "z_plugin", "Z-Work"),
        ("tools", "mid", "Tool"),
    ]


def test_rail_registry_dedupe_same_plugin_section_label() -> None:
    registry = RailRegistry()
    api = RailAPI(plugin_name="dup", registry=registry)
    api.register_item(
        section="tools", index=100, label="Same", handler=_noop_handler,
    )
    api.register_item(
        section="tools", index=150, label="Same", handler=_noop_handler,
    )
    items = registry.items()
    assert len(items) == 1
    assert items[0].index == 150  # last write wins


def test_rail_api_reserved_section_warns_without_flag(caplog) -> None:
    import logging

    registry = RailRegistry()
    api = RailAPI(plugin_name="untrusted", registry=registry, reserved_allowed=False)

    with caplog.at_level(logging.WARNING, logger="pollypm.plugin_api.v1"):
        api.register_item(
            section="system",
            index=99,
            label="Extras",
            handler=_noop_handler,
        )

    assert any("reserved rail section" in rec.message for rec in caplog.records)


def test_rail_sections_frozen_set_matches_spec() -> None:
    assert RAIL_SECTIONS == ("top", "projects", "workflows", "tools", "system")
    assert RESERVED_RAIL_SECTIONS == frozenset({"top", "projects", "system"})


def test_plugin_api_rail_property_exposes_rail_api() -> None:
    registry = RailRegistry()
    rail_api = RailAPI(plugin_name="demo", registry=registry)
    api = PluginAPI(
        plugin_name="demo", roster_api=None, jobs_api=None, rail_api=rail_api,
    )
    assert api.rail is rail_api


def test_plugin_api_rail_without_api_raises() -> None:
    api = PluginAPI(plugin_name="demo", roster_api=None, jobs_api=None)
    with pytest.raises(RuntimeError, match="RailAPI not available"):
        _ = api.rail


# ---------------------------------------------------------------------------
# Integration: ExtensionHost initialize_plugins wires RailAPI into plugins
# and the registry collects registrations.
# ---------------------------------------------------------------------------


def test_extension_host_initialize_plugins_populates_rail_registry(tmp_path: Path) -> None:
    # Create a tiny plugin that registers one rail item in its initialize() hook.
    plugin_dir = tmp_path / "plugins_builtin" / "testrail"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "pollypm-plugin.toml").write_text(
        """
api_version = "1"
name = "testrail"
kind = "test"
version = "0.1.0"
entrypoint = "plugin.py:plugin"
description = "rail test plugin"
"""
    )
    (plugin_dir / "plugin.py").write_text(
        """
from pollypm.plugin_api.v1 import PollyPMPlugin, PanelSpec


def _handler(ctx):
    return PanelSpec(widget="ok")


def _init(api):
    api.rail.register_item(
        section="workflows",
        index=30,
        label="Activity",
        handler=_handler,
    )


plugin = PollyPMPlugin(
    name="testrail",
    initialize=_init,
)
"""
    )

    host = ExtensionHost(tmp_path)
    # Monkey-patch the search paths to only include our tmp dir.
    host._plugin_search_paths = lambda: [  # type: ignore[assignment]
        ("builtin", tmp_path / "plugins_builtin"),
    ]

    host.initialize_plugins()

    items = host.rail_registry().items()
    assert any(r.section == "workflows" and r.index == 30 and r.label == "Activity" for r in items)


def test_reserved_section_flag_gates_reserved_allowed(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins_builtin" / "reservedrail"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "pollypm-plugin.toml").write_text(
        """
api_version = "1"
name = "reservedrail"
kind = "test"
version = "0.1.0"
entrypoint = "plugin.py:plugin"
description = "reserved rail plugin"
contributes_to_reserved_section = true
"""
    )
    (plugin_dir / "plugin.py").write_text(
        """
from pollypm.plugin_api.v1 import PollyPMPlugin


def _handler(ctx):
    return None


def _init(api):
    api.rail.register_item(
        section="system",
        index=99,
        label="Admin",
        handler=_handler,
    )


plugin = PollyPMPlugin(
    name="reservedrail",
    initialize=_init,
)
"""
    )

    host = ExtensionHost(tmp_path)
    host._plugin_search_paths = lambda: [  # type: ignore[assignment]
        ("builtin", tmp_path / "plugins_builtin"),
    ]

    # Force plugins to load (so manifest flag is picked up).
    host.plugins()
    assert "reservedrail" in host._reserved_rail_allowed

    host.initialize_plugins()
    items = host.rail_registry().items()
    assert any(r.section == "system" and r.label == "Admin" for r in items)
