"""SQLAlchemy-backed :class:`Store` — schema bootstrap + message/event methods.

Issue #337 landed the skeleton (engine factory + ``transaction()``). Issue
#338 fills in the surface that writes to the unified ``messages`` table:

* Schema bootstrap in ``__init__`` — ``metadata.create_all(write_engine)``
  plus the FTS5 shadow DDL, all inside a single writer transaction so a
  partial failure rolls back cleanly.
* :meth:`append_event` — fire-and-forget; lazily provisions the private
  :class:`EventBuffer` on first call so test doubles that never emit
  events don't spin up a drainer thread.
* :meth:`record_event`, :meth:`enqueue_message`, :meth:`update_message`,
  :meth:`close_message`, :meth:`query_messages` — synchronous inserts /
  updates / reads against the ``messages`` table.
* :meth:`close` — idempotent teardown that flushes the event buffer and
  disposes both engine pools.

Alerts (``upsert_alert`` / ``clear_alert``) stay stubbed until their own
issue lands; the unified alerts table is still under design. All other
``NotImplementedError`` paths from #337 are gone.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import MetaData, and_, delete, insert, select, text, update
from sqlalchemy.engine import Connection, Engine

from pollypm.store.engine import make_engines
from pollypm.store.event_buffer import EventBuffer
from pollypm.store.schema import FTS_DDL_STATEMENTS, messages, metadata


_NOT_YET_IMPLEMENTED = (
    "SQLAlchemyStore.{method}() is not implemented yet. "
    "The storage rewrite lands in phases — alert methods arrive in a "
    "follow-up issue to #338. "
    "Fix: wait for the unified-alerts issue to merge, or track progress "
    "in the #337 epic."
)


class SQLAlchemyStore:
    """SQLAlchemy-backed implementation of :class:`pollypm.store.Store`.

    Construction wires up the dual-pool engines (writer ``pool_size=1``,
    reader ``pool_size=5``), creates the unified ``messages`` schema if
    it does not yet exist, installs the FTS5 shadow + triggers, and
    leaves the event buffer uninitialized until first :meth:`append_event`.

    Parameters
    ----------
    url:
        SQLAlchemy URL. Typically ``sqlite:///<path>``. ``:memory:`` is
        supported for tests, with the caveat that the reader engine will
        not see writes from the writer engine (separate connections,
        separate in-memory DBs) — use a file on tmp_path in tests that
        need cross-engine visibility.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._write_engine, self._read_engine = make_engines(url)
        self._metadata: MetaData = metadata

        self._event_buffer: EventBuffer | None = None
        self._event_buffer_lock = threading.Lock()

        self._closed = False
        self._close_lock = threading.Lock()

        self._bootstrap_schema()

    # ------------------------------------------------------------------
    # Accessors (tests + future issues; not part of ``Store`` protocol).
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
        """Shared :class:`~sqlalchemy.MetaData` with the ``messages`` table."""
        return self._metadata

    def dispose(self) -> None:
        """Dispose both pooled engines without stopping the event buffer.

        Callers that want a full teardown (flush events, then dispose
        pools) should use :meth:`close` instead. ``dispose`` exists as
        a narrow escape hatch for tests that need to drop a pool but
        keep running.
        """
        self._write_engine.dispose()
        self._read_engine.dispose()

    def close(self) -> None:
        """Idempotent shutdown: flush events, dispose pools.

        Safe to call multiple times. Safe to call even if the event
        buffer was never provisioned.
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

        if self._event_buffer is not None:
            self._event_buffer.close()
        self.dispose()

    # ------------------------------------------------------------------
    # Transaction scope
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Yield a write-scoped :class:`~sqlalchemy.engine.Connection`.

        Commits on clean exit, rolls back on exception. All writes share
        the writer engine's single pooled connection.
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
    # Schema bootstrap
    # ------------------------------------------------------------------

    def _bootstrap_schema(self) -> None:
        """Create ``messages`` + FTS5 shadow + triggers in one transaction.

        ``metadata.create_all`` is itself idempotent thanks to SQLAlchemy's
        ``checkfirst=True`` default, and the FTS DDL uses ``IF NOT EXISTS``
        guards, so re-running on an already-bootstrapped DB is a no-op.
        """
        metadata.create_all(self._write_engine)
        with self.transaction() as conn:
            for stmt in FTS_DDL_STATEMENTS:
                conn.execute(text(stmt))

    # ------------------------------------------------------------------
    # Event log — firehose ('event' type) entries.
    # ------------------------------------------------------------------

    def _get_or_create_event_buffer(self) -> EventBuffer:
        """Lazily construct the private :class:`EventBuffer`.

        Constructed on first :meth:`append_event` so long-lived but
        quiet stores (e.g. integration-test fixtures) don't spin up a
        background thread they'll never use.
        """
        if self._event_buffer is not None:
            return self._event_buffer
        with self._event_buffer_lock:
            if self._event_buffer is None:
                self._event_buffer = EventBuffer(self)
            return self._event_buffer

    def append_event(
        self,
        scope: str,
        sender: str,
        subject: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Fire-and-forget: enqueue an event for the background drain.

        Routes through the private :class:`EventBuffer`, so the call
        returns immediately even when the writer pool is saturated. Use
        :meth:`record_event` when the caller needs the row to be durable
        by the time the call returns.
        """
        buffer = self._get_or_create_event_buffer()
        buffer.append(scope=scope, sender=sender, subject=subject, payload=payload)

    def record_event(
        self,
        scope: str,
        sender: str,
        subject: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Synchronously insert an event row. Returns the new row id.

        Blocks on the writer pool until the insert commits. Reserve for
        audit-trail writes that must be visible on the next read.
        """
        row = {
            "scope": scope,
            "type": "event",
            "tier": "immediate",
            "recipient": "*",
            "sender": sender,
            "state": "open",
            "subject": subject,
            "body": "",
            "payload_json": json.dumps(payload if payload is not None else {}),
            "labels": "[]",
        }
        with self.transaction() as conn:
            result = conn.execute(insert(messages), row)
            inserted = result.inserted_primary_key
        return int(inserted[0]) if inserted else 0

    # ------------------------------------------------------------------
    # Message surface — notify / alert / inbox_task / audit rows.
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
        labels: list[str] | None = None,
        parent_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Insert a single message row. Returns the new row id.

        Keyword arguments map directly onto columns; JSON fields
        (``labels`` / ``payload``) are serialized here so callers pass
        native Python values.
        """
        row = {
            "scope": scope,
            "type": type,
            "tier": tier,
            "recipient": recipient,
            "sender": sender,
            "state": "open",
            "parent_id": parent_id,
            "subject": subject,
            "body": body,
            "payload_json": json.dumps(payload if payload is not None else {}),
            "labels": json.dumps(labels if labels is not None else []),
        }
        with self.transaction() as conn:
            result = conn.execute(insert(messages), row)
            inserted = result.inserted_primary_key
        return int(inserted[0]) if inserted else 0

    def update_message(self, id: int, **fields: Any) -> None:
        """Patch ``fields`` onto the message with the given ``id``.

        ``labels`` and ``payload`` are JSON-encoded if passed as native
        Python values. Unknown columns raise a ``ValueError`` so typos
        don't silently no-op.
        """
        if not fields:
            return

        allowed = {col.name for col in messages.columns}
        translated: dict[str, Any] = {}
        for key, value in fields.items():
            column = key
            translated_value = value
            if key == "payload":
                column = "payload_json"
                translated_value = (
                    json.dumps(value) if not isinstance(value, str) else value
                )
            elif key == "labels" and not isinstance(value, str):
                translated_value = json.dumps(value)
            if column not in allowed:
                raise ValueError(
                    f"update_message received unknown field {key!r}. "
                    f"No column by that name exists on ``messages`` so the "
                    f"update would be silently dropped. "
                    f"Fix: pass one of {sorted(allowed)} or extend the "
                    f"schema in pollypm/store/schema.py."
                )
            translated[column] = translated_value

        translated["updated_at"] = datetime.now(timezone.utc)

        with self.transaction() as conn:
            conn.execute(
                update(messages).where(messages.c.id == id).values(**translated)
            )

    def close_message(self, id: int) -> None:
        """Mark a message as closed and stamp ``closed_at`` / ``updated_at``."""
        now = datetime.now(timezone.utc)
        with self.transaction() as conn:
            conn.execute(
                update(messages)
                .where(messages.c.id == id)
                .values(state="closed", closed_at=now, updated_at=now)
            )

    def query_messages(self, **filters: Any) -> list[dict[str, Any]]:
        """Return rows matching ``filters``, newest first.

        Supported filters: ``type``, ``tier``, ``recipient``, ``state``,
        ``scope``, ``sender``, ``parent_id``, ``since`` (``datetime``),
        ``limit`` (``int``). Unknown filter keys raise ``ValueError`` —
        silent filter drops have burned us in the legacy inbox code.
        """
        limit = filters.pop("limit", None)
        since = filters.pop("since", None)

        supported = {
            "type",
            "tier",
            "recipient",
            "state",
            "scope",
            "sender",
            "parent_id",
        }
        unknown = set(filters) - supported
        if unknown:
            raise ValueError(
                f"query_messages received unsupported filter(s) {sorted(unknown)!r}. "
                f"Silent filter drops mask bugs in the inbox aggregation path. "
                f"Fix: remove the key, or widen the supported set in "
                f"SQLAlchemyStore.query_messages."
            )

        conditions = [messages.c[key] == value for key, value in filters.items()]
        if since is not None:
            conditions.append(messages.c.created_at >= since)

        stmt = select(messages)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(messages.c.created_at.desc(), messages.c.id.desc())
        if limit is not None:
            stmt = stmt.limit(int(limit))

        with self._read_engine.connect() as conn:
            result = conn.execute(stmt)
            rows = [dict(row._mapping) for row in result]

        # Decode JSON text columns for caller convenience.
        for row in rows:
            payload_json = row.get("payload_json")
            if isinstance(payload_json, str):
                try:
                    row["payload"] = json.loads(payload_json)
                except json.JSONDecodeError:
                    row["payload"] = {}
            labels_json = row.get("labels")
            if isinstance(labels_json, str):
                try:
                    row["labels"] = json.loads(labels_json)
                except json.JSONDecodeError:
                    row["labels"] = []
        return rows

    # ------------------------------------------------------------------
    # Alerts — still stubbed; follow-up issue to #338.
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Test-only conveniences — do NOT call from production code paths.
    # ------------------------------------------------------------------

    def _delete_all_messages_for_tests(self) -> None:
        """Wipe the ``messages`` table. Tests only — prod callers must not use."""
        with self.transaction() as conn:
            conn.execute(delete(messages))
