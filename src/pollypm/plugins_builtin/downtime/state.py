"""Persistent state for the downtime plugin.

Lives at ``<project.base_dir>/downtime-state.json`` — tiny JSON payload
with a pause marker (so ``pm downtime pause`` survives restart) and the
most recent selection history (used by ``pick_candidate`` to avoid
picking the same kind twice in a row).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

from pollypm.atomic_io import atomic_write_text


STATE_FILENAME = "downtime-state.json"
RECENT_TITLE_LIMIT = 20


@dataclass(slots=True)
class DowntimeState:
    """Persisted downtime state.

    ``pause_until`` — ISO-8601 date or datetime string. When set and in
    the future, the tick handler skips with reason ``"paused"``. Empty
    string means no pause. Written by ``pm downtime pause``.

    ``last_kind`` / ``last_source`` — the category kind and source of the
    most recently scheduled downtime task. Used by the candidate selector
    to nudge variety.

    ``recent_titles`` — titles of the N most recent scheduled downtime
    tasks, bounded to a small ring. Used to dedupe candidate sourcing so
    we don't re-explore the same idea hour after hour.
    """

    pause_until: str = ""
    last_kind: str = ""
    last_source: str = ""
    recent_titles: list[str] = field(default_factory=list)
    extra: dict[str, object] = field(default_factory=dict)

    def note_scheduled(self, *, kind: str, source: str, title: str) -> None:
        """Record a newly-scheduled downtime task.

        Mutates in place — the caller is expected to immediately persist
        via :func:`save_state`.
        """
        self.last_kind = kind
        self.last_source = source
        if title and title not in self.recent_titles:
            self.recent_titles.append(title)
        # Bound the ring so the state file stays tiny.
        if len(self.recent_titles) > RECENT_TITLE_LIMIT:
            self.recent_titles = self.recent_titles[-RECENT_TITLE_LIMIT:]

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "pause_until": self.pause_until,
            "last_kind": self.last_kind,
            "last_source": self.last_source,
            "recent_titles": list(self.recent_titles),
        }
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "DowntimeState":
        known = {"pause_until", "last_kind", "last_source", "recent_titles"}
        extra = {k: v for k, v in data.items() if k not in known}
        recent = data.get("recent_titles")
        if not isinstance(recent, list):
            recent_list: list[str] = []
        else:
            recent_list = [str(x) for x in recent if isinstance(x, str)]
        return cls(
            pause_until=str(data.get("pause_until") or ""),
            last_kind=str(data.get("last_kind") or ""),
            last_source=str(data.get("last_source") or ""),
            recent_titles=recent_list,
            extra=extra,
        )


def state_path(base_dir: Path) -> Path:
    """Return the on-disk path for the downtime state file."""
    return Path(base_dir) / STATE_FILENAME


def load_state(base_dir: Path) -> DowntimeState:
    """Load downtime state. Missing/corrupt → fresh empty state."""
    path = state_path(base_dir)
    if not path.exists():
        return DowntimeState()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return DowntimeState()
    if not isinstance(raw, dict):
        return DowntimeState()
    return DowntimeState.from_dict(raw)


def save_state(base_dir: Path, state: DowntimeState) -> None:
    """Persist downtime state atomically. Creates parent directory if missing."""
    path = state_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n")


def iso_date(d: _date) -> str:
    """Canonical ISO date formatting (``YYYY-MM-DD``)."""
    return d.isoformat()
