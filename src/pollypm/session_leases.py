"""Shared session-lease control errors."""

from __future__ import annotations


class SessionLeaseConflictError(RuntimeError):
    """Raised when an operation is blocked by another lease owner."""

    def __init__(self, *, session_name: str, owner: str, action: str) -> None:
        self.session_name = session_name
        self.owner = owner
        self.action = action
        super().__init__(
            f"Cannot {action} {session_name}: session is currently leased "
            f"to {owner}; use --force to bypass"
        )
