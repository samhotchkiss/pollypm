"""CLI entry points for the project_planning plugin.

Exports ``project_app`` — mounted in ``pollypm.cli`` as ``pm project ...``.
"""
from pollypm.plugins_builtin.project_planning.cli.project import project_app

__all__ = ["project_app"]
