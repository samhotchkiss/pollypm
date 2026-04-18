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

    def append_event(
        self,
        scope: str,
        sender: str,
        subject: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Fire-and-forget event append. No return value; see #338."""
        ...

    def record_event(
        self,
        scope: str,
        sender: str,
        subject: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Synchronous event insert — returns the new row id."""
        ...

    # ------------------------------------------------------------------
    # Inbox messages
    # ------------------------------------------------------------------

    def enqueue_message(
        self,
        type: str,
        tier: str,
        recipient: str,
        sender: str,
        subject: str,
        body: str,
        scope: str,
        **extra: Any,
    ) -> int:
        """Insert a new message row. Returns the assigned row id."""
        ...

    def upsert_message(
        self,
        type: str,
        tier: str,
        recipient: str,
        sender: str,
        subject: str,
        body: str,
        scope: str,
        **extra: Any,
    ) -> int:
        """Insert-if-no-open-match-for-dedupe-key-else-update. Returns row id."""
        ...

    def update_message(self, id: int, **fields: Any) -> None:
        """Patch fields on an existing message row."""
        ...

    def close_message(self, id: int) -> None:
        """Mark a message as closed/resolved."""
        ...

    def query_messages(self, **filters: Any) -> list[dict[str, Any]]:
        """Return messages matching ``filters`` (ordered by recency)."""
        ...

    def query_messages_with_legacy_bridge(
        self, **filters: Any
    ) -> list[dict[str, Any]]:
        """:meth:`query_messages` UNIONed with legacy ``events`` / ``alerts``.

        BRIDGE(#349): temporary during the rollout window between #341
        (this reader-migration) and #349 (supervisor/heartbeat writer
        migration). Remove when the legacy tables drain.
        """
        ...

    # ------------------------------------------------------------------
    # Alerts — thin wrappers over upsert_message / close_message.
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
    # Transaction scope + escape hatch
    # ------------------------------------------------------------------

    def transaction(self) -> AbstractContextManager[Any]:
        """Context manager yielding a write-scoped connection.

        Commits on clean exit, rolls back on exception. Implementations
        return a SQLAlchemy :class:`~sqlalchemy.engine.Connection`; test
        doubles may yield a lighter-weight handle as long as it supports
        ``execute(...)``.
        """
        ...

    def execute(self, stmt: Any) -> Any:
        """Execute a SQLAlchemy Core statement inside a write transaction.

        Escape hatch for callers writing to tables the Store does not
        own (e.g. ``work_tasks``). Returns a cursor-shaped result with
        ``rowcount`` / ``inserted_primary_key``.
        """
        ...
