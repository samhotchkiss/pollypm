"""Structural :class:`Store` protocol — the contract every backend implements.

This is a deliberately narrow surface: only the methods current PollyPM
callers actually use today. It is defined as a :class:`typing.Protocol`
so existing duck-typed callers (and the ``MockWorkService``-style test
doubles sprinkled around the repo) satisfy it without inheritance.

Method bodies live in :mod:`pollypm.store.sqlalchemy_store` (skeleton in
#337, real impls in #338 / #340). Signatures here are the minimal shapes
needed for Issue A — individual follow-ups may widen them (e.g. adding
``*, actor`` kwargs) without breaking the protocol check because
``Protocol`` methods are matched structurally by name + basic shape.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Store(Protocol):
    """Structural contract for PollyPM's persistent-state backend.

    Implementations:

    * :class:`pollypm.store.sqlalchemy_store.SQLAlchemyStore` — the real
      backend (skeleton in #337, impls in #338 + #340).
    * Test doubles in ``tests/`` — duck-typed, not inheritance-based.

    The protocol is intentionally small. If a caller needs a method that
    is not listed here, add it in the PR that introduces the caller, not
    speculatively.
    """

    # ------------------------------------------------------------------
    # Event log (append-only journal)
    # ------------------------------------------------------------------

    def append_event(self, *, kind: str, payload: dict[str, Any]) -> None:
        """Append an event to the journal. Fire-and-forget; no return value."""
        ...

    def record_event(
        self, session_name: str, event_type: str, message: str
    ) -> None:
        """Record a session-scoped event (heartbeat / supervisor audit trail)."""
        ...

    # ------------------------------------------------------------------
    # Inbox messages
    # ------------------------------------------------------------------

    def enqueue_message(self, message: dict[str, Any]) -> int:
        """Insert a new inbox message. Returns the assigned row id."""
        ...

    def update_message(self, message_id: int, **fields: Any) -> None:
        """Patch fields on an existing inbox message."""
        ...

    def close_message(self, message_id: int, *, reason: str | None = None) -> None:
        """Mark an inbox message as closed/resolved."""
        ...

    def query_messages(self, **filters: Any) -> list[dict[str, Any]]:
        """Return inbox messages matching ``filters`` (ordered by recency)."""
        ...

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def upsert_alert(
        self,
        session_name: str,
        alert_type: str,
        severity: str,
        message: str,
    ) -> None:
        """Create or refresh an alert for ``(session_name, alert_type)``."""
        ...

    def clear_alert(self, session_name: str, alert_type: str) -> None:
        """Clear any alert matching ``(session_name, alert_type)``."""
        ...

    # ------------------------------------------------------------------
    # Transaction scope
    # ------------------------------------------------------------------

    def transaction(self) -> AbstractContextManager[Any]:
        """Context manager yielding a write-scoped connection.

        Commits on clean exit, rolls back on exception. Implementations
        return a SQLAlchemy :class:`~sqlalchemy.engine.Connection`; test
        doubles may yield a lighter-weight handle as long as it supports
        ``execute(...)``.
        """
        ...
