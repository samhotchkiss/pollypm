"""LaunchPlanner — pluggable session launch-plan producer.

The LaunchPlanner owns the policy that turns a ``PollyPMConfig`` into a
list of concrete ``SessionLaunchSpec`` entries. Supervisor delegates to
a planner at runtime; the default implementation matches the historical
Supervisor behavior verbatim.

Use :func:`get_launch_planner` to resolve a planner by name via the
plugin host.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.launch_planner.base import LaunchPlanner

__all__ = [
    "LaunchPlanner",
    "get_launch_planner",
]


def get_launch_planner(
    name: str,
    *,
    root_dir: Path | None = None,
    **kwargs: object,
) -> LaunchPlanner:
    """Resolve a launch planner implementation by name via the plugin host."""
    from pollypm.plugin_host import extension_host_for_root

    root = str((root_dir or Path.cwd()).resolve())
    return extension_host_for_root(root).get_launch_planner(name, **kwargs)
