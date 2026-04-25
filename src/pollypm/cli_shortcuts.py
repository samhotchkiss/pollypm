"""Shared curated CLI shortcuts for PollyPM surfaces.

Contract:
- Inputs: none; this module exposes a stable curated shortcut list.
- Outputs: plain-text and row-oriented render helpers for CLI and UI use.
- Side effects: none.
- Invariants: shortcut content is defined once here so CLI and onboarding
  surfaces do not drift.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ShortcutGroup:
    name: str
    commands: tuple[str, ...]


_SHORTCUT_GROUPS: tuple[ShortcutGroup, ...] = (
    ShortcutGroup("Setup", ("pm onboard", "pm projects", "pm doctor")),
    ShortcutGroup("Create", ("pm task create", "pm issue create")),
    ShortcutGroup("Monitor", ("pm activity --follow", "pm cockpit")),
    ShortcutGroup("Review", ("pm inbox", "pm task approve")),
    ShortcutGroup("Advanced", ("pm advisor", "pm briefing")),
)


def shortcut_groups() -> tuple[ShortcutGroup, ...]:
    return _SHORTCUT_GROUPS


def shortcut_rows() -> tuple[tuple[str, str], ...]:
    return tuple((group.name, " | ".join(group.commands)) for group in _SHORTCUT_GROUPS)


def render_shortcuts_text() -> str:
    width = max(len(group.name) for group in _SHORTCUT_GROUPS)
    lines = ["PollyPM shortcuts", ""]
    for group in _SHORTCUT_GROUPS:
        lines.append(f"{group.name.ljust(width)}: {' | '.join(group.commands)}")
    return "\n".join(lines)
