"""Built-in plugin registration for the default launch planner."""

from __future__ import annotations

from pollypm.plugin_api.v1 import PollyPMPlugin


def _factory(**kwargs):
    # Local import so loading this plugin's manifest doesn't drag the
    # planner module (and all its transitive imports) into processes that
    # never actually resolve a planner.
    from pollypm.plugins_builtin.default_launch_planner.planner import DefaultLaunchPlanner

    return DefaultLaunchPlanner(**kwargs)


plugin = PollyPMPlugin(
    name="default_launch_planner",
    capabilities=("launch_planner",),
    launch_planners={"default": _factory},
)
