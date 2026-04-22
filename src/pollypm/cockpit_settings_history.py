"""Undo metadata for cockpit settings screens.

Contract:
- Inputs: a label and a zero-arg callable that restores prior state.
- Outputs: an in-memory undo record with a 24h expiry.
- Side effects: none.
- Invariants: view code can ask this helper whether the undo window is
  still open without owning expiry math itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable


@dataclass(slots=True)
class UndoAction:
    label: str
    expires_at: datetime
    apply: Callable[[], None]


def make_undo_action(
    label: str,
    apply: Callable[[], None],
    *,
    hours: int = 24,
) -> UndoAction:
    return UndoAction(
        label=label,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=hours),
        apply=apply,
    )


def undo_expired(action: UndoAction | None) -> bool:
    return action is None or datetime.now(timezone.utc) > action.expires_at


def undo_expires_text(action: UndoAction) -> str:
    return action.expires_at.astimezone(timezone.utc).strftime("%H:%M UTC")
