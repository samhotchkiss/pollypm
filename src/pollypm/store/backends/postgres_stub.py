"""Postgres backend stub — proves the :class:`Store` protocol is dialect-agnostic.

Issue #343 lands this file as a **reference stub only**. It is
intentionally *not* registered in PollyPM's ``pyproject.toml``
entry-point group: the real Postgres backend will ship as a separate
installable package (``pollypm-store-postgres``) that registers its own
``pollypm.store_backend`` entry point at install time.

The value of keeping this class in-tree is structural: every method on
the :class:`pollypm.store.Store` protocol has a stub here, which forces
the protocol to stay dialect-neutral. If a future change introduces a
SQLite-only assumption into :class:`~pollypm.store.protocol.Store`, the
stub will stop being trivially implementable and reviewers will catch
the leak before it bakes in.

Every method raises :class:`NotImplementedError` with a three-question
message (what / why / fix — see issue #240).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any


_NOT_YET_IMPLEMENTED = (
    "Postgres backend is v1.1; install pollypm-store-postgres. "
    "The in-tree stub exists to prove the Store protocol stays "
    "dialect-agnostic — it deliberately does not connect to a "
    "database. Fix: `uv tool install pollypm-store-postgres` (once the "
    "package is published), then set ``[storage] backend = 'postgres'`` "
    "in ~/.pollypm/pollypm.toml."
)


class PostgresStore:
    """Structural stub — every method raises :class:`NotImplementedError`.

    Accepts ``url=`` so the resolver's construction contract
    (``cls(url=...)``) is identical to the SQLite backend. The URL is
    stored verbatim for debugging (e.g. ``pm doctor`` might print it)
    but never opened.
    """

    def __init__(self, url: str) -> None:
        self._url = url

    @property
    def url(self) -> str:
        """The URL the stub was constructed with. Never used to connect."""
        return self._url

    def dispose(self) -> None:
        """No-op — the stub holds no resources."""
        return None

    # ------------------------------------------------------------------
    # Store protocol — every method raises NotImplementedError.
    # ------------------------------------------------------------------

    def append_event(
        self,
        scope: str,
        sender: str,
        subject: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError(_NOT_YET_IMPLEMENTED)

    def record_event(
        self,
        scope: str,
        sender: str,
        subject: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        raise NotImplementedError(_NOT_YET_IMPLEMENTED)

    def enqueue_message(self, **kwargs: Any) -> int:
        raise NotImplementedError(_NOT_YET_IMPLEMENTED)

    def upsert_message(self, **kwargs: Any) -> int:
        raise NotImplementedError(_NOT_YET_IMPLEMENTED)

    def update_message(self, id: int, **fields: Any) -> None:
        raise NotImplementedError(_NOT_YET_IMPLEMENTED)

    def close_message(self, id: int) -> None:
        raise NotImplementedError(_NOT_YET_IMPLEMENTED)

    def query_messages(self, **filters: Any) -> list[dict[str, Any]]:
        raise NotImplementedError(_NOT_YET_IMPLEMENTED)

    def upsert_alert(
        self,
        session_name: str,
        alert_type: str,
        severity: str,
        message: str,
    ) -> None:
        raise NotImplementedError(_NOT_YET_IMPLEMENTED)

    def clear_alert(self, session_name: str, alert_type: str) -> None:
        raise NotImplementedError(_NOT_YET_IMPLEMENTED)

    def transaction(self) -> AbstractContextManager[Any]:
        raise NotImplementedError(_NOT_YET_IMPLEMENTED)

    def execute(self, stmt: Any) -> Any:
        raise NotImplementedError(_NOT_YET_IMPLEMENTED)


__all__ = ["PostgresStore"]
