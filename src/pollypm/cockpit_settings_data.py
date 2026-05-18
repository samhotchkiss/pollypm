"""Settings-screen data snapshot used by the cockpit settings pane.

Contract:
- Inputs: nine keyword arguments — ``accounts``, ``projects``, ``roles``,
  ``heartbeat``, ``plugins``, ``planner``, ``inbox``, ``about``, and
  ``errors`` — each a list shaped to match what the settings sections
  render.
- Outputs: a ``SettingsData`` instance with the same nine attributes,
  declared via ``__slots__`` for memory + typo discipline.
- Side effects: none. Pure container.
- Invariants: this module owns one data class and nothing else; it has
  no dependency on Textual, cockpit state, or services. Keep it
  data-only so tests can construct an instance without mounting a
  screen.
- Allowed dependencies: stdlib only.
- Public: ``SettingsData`` is re-exported via ``cockpit_ui`` for
  back-compat (see #1354).

Wedge of the cockpit_ui.py god-module split tracked by #1354.
"""

from __future__ import annotations


class SettingsData:
    """Snapshot of everything the settings screen renders — gathered once."""

    __slots__ = (
        "accounts",
        "projects",
        "roles",
        "heartbeat",
        "plugins",
        "planner",
        "inbox",
        "about",
        "errors",
    )

    def __init__(
        self,
        *,
        accounts: list[dict],
        projects: list[dict],
        roles: list[dict],
        heartbeat: list[tuple[str, str]],
        plugins: list[dict],
        planner: list[tuple[str, str]],
        inbox: list[tuple[str, str]],
        about: list[tuple[str, str]],
        errors: list[str],
    ) -> None:
        self.accounts = accounts
        self.projects = projects
        self.roles = roles
        self.heartbeat = heartbeat
        self.plugins = plugins
        self.planner = planner
        self.inbox = inbox
        self.about = about
        self.errors = errors


__all__ = ["SettingsData"]
