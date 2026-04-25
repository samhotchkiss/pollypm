"""Lightweight state-change signaling via file mtime.

Producers call bump() after any state mutation (heartbeat sweep,
pm send, recovery, alert changes). The cockpit checks changed()
on every tick — if nothing changed, it skips all expensive queries.

Cost: one stat() call per tick. No subprocess. No FD leak.
"""
from __future__ import annotations


from pollypm.config import GLOBAL_CONFIG_DIR

_EPOCH_PATH = GLOBAL_CONFIG_DIR / "state_epoch"


def bump() -> None:
    """Signal that system state has changed. Cheap — just touches a file."""
    try:
        _EPOCH_PATH.parent.mkdir(parents=True, exist_ok=True)
        _EPOCH_PATH.touch()
    except OSError:
        pass


def mtime() -> float:
    """Return the mtime of the epoch file, or 0 if it doesn't exist."""
    try:
        return _EPOCH_PATH.stat().st_mtime
    except (FileNotFoundError, OSError):
        return 0.0
