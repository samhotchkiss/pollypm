"""Persistent state for the morning briefing plugin.

State lives at ``<project.base_dir>/briefing-state.json`` — small JSON
payload tracking the last briefing date (so we fire at most once per
local day) and the last quiet-mode weekly fire (so vacation briefings
arrive weekly instead of daily). See mb01 / mb04.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

from pollypm.atomic_io import atomic_write_text


STATE_FILENAME = "briefing-state.json"


@dataclass(slots=True)
class BriefingState:
    """Persisted briefing state.

    ``last_briefing_date`` — ISO ``YYYY-MM-DD`` of the most recent
    successful fire (in the configured local timezone). Empty string
    means "never fired". Used as the dedupe gate.

    ``last_quiet_weekly_date`` — for quiet mode (mb04). ISO date of the
    most recent fire while in quiet mode. Ignored outside quiet mode.

    ``last_fire_at`` — optional ISO-8601 UTC timestamp of the last fire,
    for diagnostics.
    """

    last_briefing_date: str = ""
    last_quiet_weekly_date: str = ""
    last_fire_at: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "last_briefing_date": self.last_briefing_date,
            "last_quiet_weekly_date": self.last_quiet_weekly_date,
            "last_fire_at": self.last_fire_at,
        }
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "BriefingState":
        known = {"last_briefing_date", "last_quiet_weekly_date", "last_fire_at"}
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(
            last_briefing_date=str(data.get("last_briefing_date") or ""),
            last_quiet_weekly_date=str(data.get("last_quiet_weekly_date") or ""),
            last_fire_at=str(data.get("last_fire_at") or ""),
            extra=extra,
        )


def state_path(base_dir: Path) -> Path:
    """Return the on-disk path for the briefing state file."""
    return Path(base_dir) / STATE_FILENAME


def load_state(base_dir: Path) -> BriefingState:
    """Load briefing state. Missing/corrupt → fresh empty state."""
    path = state_path(base_dir)
    if not path.exists():
        return BriefingState()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return BriefingState()
    if not isinstance(raw, dict):
        return BriefingState()
    return BriefingState.from_dict(raw)


def save_state(base_dir: Path, state: BriefingState) -> None:
    """Persist briefing state atomically. Creates parent directory if missing."""
    path = state_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n")


def iso_date(d: _date) -> str:
    """Canonical ISO date formatting (``YYYY-MM-DD``)."""
    return d.isoformat()
