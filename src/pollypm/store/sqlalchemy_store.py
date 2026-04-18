"""Skeleton SQLAlchemy-backed :class:`Store` — real method bodies land later.

Issue #337 installs this scaffolding so downstream issues can implement one
method at a time without churning imports:

* :class:`SQLAlchemyStore.__init__` builds the writer + reader engines via
  :func:`pollypm.store.engine.make_engines` and holds a shared
  :class:`sqlalchemy.MetaData` placeholder that #338 will populate with
  table definitions.
* :meth:`SQLAlchemyStore.transaction` is fully functional — it yields a
  SQLAlchemy :class:`~sqlalchemy.engine.Connection` from the write engine,
  commits on clean exit, and rolls back on exception. Callers can start
  using it immediately for ad-hoc writes.
* Every other method raises :class:`NotImplementedError` with a message
  pointing to the issue that implements it (#338 for schema/events,
  #340 for callers). Those messages follow the three-question rule:
  *what happened* / *why it matters* / *how to fix it* (→ wait for / look
  at the issue).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import MetaData
from sqlalchemy.engine import Connection, Engine

from pollypm.store.engine import make_engines


_NOT_YET_IMPLEMENTED = (
    "SQLAlchemyStore.{method}() is not implemented yet. "
    "The storage rewrite lands in phases — method bodies arrive in issue-B "
    "(#338, schema + event log) and issue-D (#340, caller migration). "
    "Fix: wait for those issues to merge, or track progress in the #337 epic."
)


class SQLAlchemyStore:
    """Skeleton implementation of the :class:`pollypm.store.Store` protocol.

    Construction wires up the dual-pool engines so downstream PRs can test
    against a live DB without re-plumbing the factory. The ``transaction()``
    context manager is the only fully working method in this PR.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._write_engine, self._read_engine = make_engines(url)
        # Placeholder — #338 populates this with the full schema. Callers
        # in this PR should treat it as opaque.
        self._metadata = MetaData()

    # ------------------------------------------------------------------
    # Accessors (useful for tests + future issues; not part of ``Store``).
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        """Original SQLAlchemy URL passed to ``__init__``."""
        return self._url

    @property
    def write_engine(self) -> Engine:
        """Writer engine — single-connection pool, serialized writes."""
        return self._write_engine

    @property
    def read_engine(self) -> Engine:
        """Reader engine — 5-connection pool, WAL-concurrent reads."""
        return self._read_engine

    @property
    def metadata(self) -> MetaData:
        """Shared :class:`~sqlalchemy.MetaData` — populated by #338."""
        return self._metadata

    def dispose(self) -> None:
        """Dispose both pooled engines. Safe to call multiple times."""
        self._write_engine.dispose()
        self._read_engine.dispose()

    # ------------------------------------------------------------------
    # Transaction scope — the only functional method in this PR.
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Yield a write-scoped :class:`~sqlalchemy.engine.Connection`.

        Commits on clean exit, rolls back on exception. Uses the writer
        engine so all writes serialize through its single pooled
        connection — no "database is locked" races.
        """
        conn = self._write_engine.connect()
        try:
            trans = conn.begin()
            try:
                yield conn
            except BaseException:
                trans.rollback()
                raise
            else:
                trans.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Store protocol stubs — bodies land in #338 / #340.
    # ------------------------------------------------------------------

    def append_event(self, *, kind: str, payload: dict[str, Any]) -> None:
        raise NotImplementedError(
            _NOT_YET_IMPLEMENTED.format(method="append_event")
        )

    def record_event(
        self, session_name: str, event_type: str, message: str
    ) -> None:
        raise NotImplementedError(
            _NOT_YET_IMPLEMENTED.format(method="record_event")
        )

    def enqueue_message(self, message: dict[str, Any]) -> int:
        raise NotImplementedError(
            _NOT_YET_IMPLEMENTED.format(method="enqueue_message")
        )

    def update_message(self, message_id: int, **fields: Any) -> None:
        raise NotImplementedError(
            _NOT_YET_IMPLEMENTED.format(method="update_message")
        )

    def close_message(self, message_id: int, *, reason: str | None = None) -> None:
        raise NotImplementedError(
            _NOT_YET_IMPLEMENTED.format(method="close_message")
        )

    def query_messages(self, **filters: Any) -> list[dict[str, Any]]:
        raise NotImplementedError(
            _NOT_YET_IMPLEMENTED.format(method="query_messages")
        )

    def upsert_alert(
        self,
        session_name: str,
        alert_type: str,
        severity: str,
        message: str,
    ) -> None:
        raise NotImplementedError(
            _NOT_YET_IMPLEMENTED.format(method="upsert_alert")
        )

    def clear_alert(self, session_name: str, alert_type: str) -> None:
        raise NotImplementedError(
            _NOT_YET_IMPLEMENTED.format(method="clear_alert")
        )
