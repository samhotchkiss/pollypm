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

from sqlalchemy import Executable, MetaData, and_, delete, insert, select, text, update
from sqlalchemy.engine import Connection, CursorResult, Engine

from pollypm.store.engine import make_engines
from pollypm.store.event_buffer import EventBuffer
from pollypm.store.schema import FTS_DDL_STATEMENTS, messages, metadata
from pollypm.store.title_contract import apply_title_contract


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
        state: str = "open",
    ) -> int:
        """Insert a single message row. Returns the new row id.

        Keyword arguments map directly onto columns; JSON fields
        (``labels`` / ``payload``) are serialized here so callers pass
        native Python values. ``subject`` is routed through
        :func:`apply_title_contract` so every stored row starts with a
        bracket tag (``[Action]`` / ``[FYI]`` / ``[Audit]`` / …) — see
        :mod:`pollypm.store.title_contract` for the full tag table.

        ``state`` defaults to ``'open'``; pass ``'staged'`` to insert a
        digest-tier row that shouldn't surface until a flush sweep
        promotes it.
        """
        stamped_subject = apply_title_contract(subject, tier=tier, type=type)
        row = {
            "scope": scope,
            "type": type,
            "tier": tier,
            "recipient": recipient,
            "sender": sender,
            "state": state,
            "parent_id": parent_id,
            "subject": stamped_subject,
            "body": body,
            "payload_json": json.dumps(payload if payload is not None else {}),
            "labels": json.dumps(labels if labels is not None else []),
        }
        with self.transaction() as conn:
            result = conn.execute(insert(messages), row)
            inserted = result.inserted_primary_key
        return int(inserted[0]) if inserted else 0

    def upsert_message(
        self,
        type: str,
        tier: str,
        recipient: str,
        sender: str,
        subject: str,
        body: str,
        scope: str,
        dedupe_key: tuple[str, ...] = ("scope", "recipient", "type", "sender"),
        labels: list[str] | None = None,
        parent_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Insert-if-no-open-match-else-update. Returns the row id.

        The dedupe contract: at most one ``state='open'`` row exists for
        the tuple named by ``dedupe_key`` (default: ``scope`` + ``recipient``
        + ``type`` + ``sender``). If such a row exists, its ``body`` /
        ``subject`` / ``payload`` / ``labels`` / ``tier`` are refreshed
        and the existing row id is returned. Otherwise a fresh row is
        inserted via :meth:`enqueue_message`.

        Enforcement lives in application code — we query for a matching
        open row inside the same writer transaction that performs the
        update/insert, so a check-then-act race is impossible for a
        single-pool writer. Callers from multiple processes are
        serialized by SQLite's file lock + the ``busy_timeout`` in
        :mod:`pollypm.store.engine`.

        The default dedupe key matches the pre-migration alert semantics
        — one open alert per ``(session_name, alert_type)`` — by mapping
        ``scope=session_name`` and ``sender=alert_type``. Alert callers
        typically leave ``dedupe_key`` at its default.
        """
        stamped_subject = apply_title_contract(subject, tier=tier, type=type)
        supported = {"scope", "recipient", "type", "sender"}
        unknown = set(dedupe_key) - supported
        if unknown:
            raise ValueError(
                f"upsert_message received unsupported dedupe_key field(s) "
                f"{sorted(unknown)!r}. "
                f"Only {sorted(supported)} are valid because those are the "
                f"columns the indexed open-row lookup can match on. "
                f"Fix: remove the field or, if a new dedupe axis is "
                f"genuinely needed, extend the schema + widen this "
                f"allowlist in SQLAlchemyStore.upsert_message."
            )
        # Dedupe tuple — the values we match an existing open row against.
        local_vars = {
            "scope": scope,
            "recipient": recipient,
            "type": type,
            "sender": sender,
        }
        now = datetime.now(timezone.utc)
        payload_json = json.dumps(payload if payload is not None else {})
        labels_json = json.dumps(labels if labels is not None else [])
        with self.transaction() as conn:
            conditions = [messages.c.state == "open"]
            for field in dedupe_key:
                conditions.append(messages.c[field] == local_vars[field])
            existing = conn.execute(
                select(messages.c.id)
                .where(and_(*conditions))
                .order_by(messages.c.id.desc())
                .limit(1)
            ).fetchone()
            if existing is not None:
                row_id = int(existing[0])
                conn.execute(
                    update(messages)
                    .where(messages.c.id == row_id)
                    .values(
                        tier=tier,
                        subject=stamped_subject,
                        body=body,
                        payload_json=payload_json,
                        labels=labels_json,
                        parent_id=parent_id,
                        updated_at=now,
                    )
                )
                return row_id
            result = conn.execute(
                insert(messages),
                {
                    "scope": scope,
                    "type": type,
                    "tier": tier,
                    "recipient": recipient,
                    "sender": sender,
                    "state": "open",
                    "parent_id": parent_id,
                    "subject": stamped_subject,
                    "body": body,
                    "payload_json": payload_json,
                    "labels": labels_json,
                },
            )
            inserted = result.inserted_primary_key
        return int(inserted[0]) if inserted else 0

    def execute(self, stmt: Executable) -> CursorResult[Any]:
        """Execute an arbitrary SQLAlchemy Core statement in a write tx.

        Escape hatch for callers that need to write to tables the Store
        doesn't own — most notably ``work_tasks`` (flow-engine state)
        and its siblings. Tasks are not messages, so the Store should
        not grow a bespoke method for every table, but every writer
        still needs to share the single-connection writer pool or two
        processes will contend for SQLite's file lock.

        Returns the :class:`~sqlalchemy.engine.CursorResult` so callers
        can inspect ``rowcount`` / ``inserted_primary_key``. Commits on
        clean exit, rolls back on exception — the semantics are the
        same as :meth:`transaction`, this is just the single-statement
        convenience form.
        """
        with self.transaction() as conn:
            return conn.execute(stmt)

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
        ``limit`` (``int``). Any scalar filter may also be passed as a
        list / tuple / set; the generated SQL switches to ``IN (...)`` so
        the inbox reader (#341) can say
        ``type=['notify', 'inbox_task', 'alert']`` in one call instead of
        running three separate queries and merging in Python. Unknown
        filter keys raise ``ValueError`` — silent filter drops have
        burned us in the legacy inbox code.
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

        conditions = []
        for key, value in filters.items():
            column = messages.c[key]
            if isinstance(value, (list, tuple, set, frozenset)):
                conditions.append(column.in_(list(value)))
            else:
                conditions.append(column == value)
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
    # Legacy-bridge reader — temporary (remove when #349 lands).
    # ------------------------------------------------------------------

    def query_messages_with_legacy_bridge(
        self, **filters: Any
    ) -> list[dict[str, Any]]:
        """:meth:`query_messages` UNIONed with the old ``events`` / ``alerts``.

        Background
        ----------
        Issue #341 migrated the reader surface (``pm inbox``, cockpit
        inbox/activity/alerts, ``pm doctor``, digest flush) onto the
        unified ``messages`` table. The matching writer migration for
        supervisor + heartbeat (~199 call sites) lands in #349 — until
        then, every ``record_event`` / ``upsert_alert`` on the legacy
        :class:`StateStore` still lands in ``events`` / ``alerts``.

        This method bridges that window: on top of the normal
        ``messages`` query, it also scans the legacy tables for matching
        rows and reshapes them into the ``messages`` dict shape so the
        caller can treat the merged list uniformly. Readers that need
        the full cockpit view during the rollout call *this* method;
        readers that only care about the new writers call
        :meth:`query_messages` directly.

        BRIDGE(#349): remove when D-rest lands and the legacy tables are
        confirmed drained. Issue F (#342) then drops the tables.
        """
        type_filter = filters.get("type")
        types: set[str]
        if type_filter is None:
            types = set()
        elif isinstance(type_filter, (list, tuple, set, frozenset)):
            types = set(type_filter)
        else:
            types = {type_filter}

        rows = self.query_messages(**filters)

        want_alerts = not types or "alert" in types
        want_events = not types or "event" in types
        if want_alerts:
            rows.extend(self._legacy_alerts_as_messages(filters))
        if want_events:
            rows.extend(self._legacy_events_as_messages(filters))

        # Re-sort merged set newest-first so list behaves like a single
        # query would — callers already expect that ordering.
        rows.sort(
            key=lambda r: (
                str(r.get("created_at") or ""),
                int(r.get("id") or 0),
            ),
            reverse=True,
        )
        limit = filters.get("limit")
        if limit is not None:
            rows = rows[: int(limit)]
        return rows

    def _legacy_alerts_as_messages(
        self, filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Read legacy ``alerts WHERE status='open'`` and reshape as messages.

        BRIDGE(#349): remove when D-rest lands.
        """
        state = filters.get("state")
        # The legacy alerts table only knows 'open' / 'cleared'; skip the
        # scan entirely when the caller asked for a state the legacy row
        # could never satisfy.
        if state is not None:
            wanted = state if isinstance(state, (list, tuple, set, frozenset)) else {state}
            if "open" not in wanted:
                return []
        scope_filter = filters.get("scope")
        recipient_filter = filters.get("recipient")
        if recipient_filter is not None:
            wanted = (
                recipient_filter
                if isinstance(recipient_filter, (list, tuple, set, frozenset))
                else {recipient_filter}
            )
            # Legacy alerts are always recipient='user' by contract.
            if "user" not in wanted:
                return []

        rows: list[dict[str, Any]] = []
        try:
            with self._read_engine.connect() as conn:
                result = conn.execute(
                    text(
                        "SELECT id, session_name, alert_type, severity, "
                        "message, status, created_at, updated_at "
                        "FROM alerts WHERE status = 'open' "
                        "ORDER BY updated_at DESC"
                    )
                )
                raw = result.fetchall()
        except Exception:  # noqa: BLE001 — table may not exist yet.
            return []
        for r in raw:
            session_name = r[1] or ""
            if scope_filter is not None:
                wanted_scopes = (
                    scope_filter
                    if isinstance(scope_filter, (list, tuple, set, frozenset))
                    else {scope_filter}
                )
                if session_name not in wanted_scopes:
                    continue
            rows.append(
                {
                    # Negative ids keep legacy rows distinct from message
                    # ids so the "which table is this from?" check stays
                    # trivial (``id < 0``). The absolute value carries
                    # the legacy PK for debugging.
                    "id": -int(r[0]),
                    "scope": session_name,
                    "type": "alert",
                    "tier": "immediate",
                    "recipient": "user",
                    "sender": r[2] or "",
                    "state": "open" if (r[5] or "") == "open" else "closed",
                    "parent_id": None,
                    "subject": r[4] or "",
                    "body": "",
                    "payload_json": json.dumps(
                        {"severity": r[3] or "", "session_name": session_name}
                    ),
                    "labels": "[]",
                    "created_at": r[6],
                    "updated_at": r[7],
                    "closed_at": None,
                    "payload": {
                        "severity": r[3] or "",
                        "session_name": session_name,
                    },
                    "_source": "legacy_alerts",
                }
            )
        return rows

    def _legacy_events_as_messages(
        self, filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Read legacy ``events`` and reshape as messages.

        BRIDGE(#349): remove when D-rest lands.
        """
        state = filters.get("state")
        if state is not None:
            wanted = state if isinstance(state, (list, tuple, set, frozenset)) else {state}
            # Legacy events have no lifecycle — treat every row as
            # state='open'. Bail when the caller asked for anything else.
            if "open" not in wanted:
                return []

        scope_filter = filters.get("scope")
        since = filters.get("since")
        limit = filters.get("limit")

        where: list[str] = []
        params: dict[str, Any] = {}
        if since is not None:
            where.append("created_at >= :since")
            params["since"] = (
                since.isoformat() if hasattr(since, "isoformat") else str(since)
            )
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        limit_sql = ""
        if limit is not None:
            # Fetch a generous slice — merged result trims again below.
            limit_sql = f" LIMIT {int(limit) * 2}"
        sql = (
            "SELECT id, session_name, event_type, message, created_at "
            "FROM events"
            + where_sql
            + " ORDER BY id DESC"
            + limit_sql
        )
        try:
            with self._read_engine.connect() as conn:
                result = conn.execute(text(sql), params)
                raw = result.fetchall()
        except Exception:  # noqa: BLE001 — table may not exist yet.
            return []

        rows: list[dict[str, Any]] = []
        for r in raw:
            session_name = r[1] or ""
            if scope_filter is not None:
                wanted_scopes = (
                    scope_filter
                    if isinstance(scope_filter, (list, tuple, set, frozenset))
                    else {scope_filter}
                )
                if session_name not in wanted_scopes:
                    continue
            rows.append(
                {
                    "id": -int(r[0]),
                    "scope": session_name,
                    "type": "event",
                    "tier": "immediate",
                    "recipient": "*",
                    "sender": session_name,
                    "state": "open",
                    "parent_id": None,
                    "subject": r[2] or "",
                    "body": r[3] or "",
                    "payload_json": "{}",
                    "labels": "[]",
                    "created_at": r[4],
                    "updated_at": r[4],
                    "closed_at": None,
                    "payload": {"event_type": r[2] or ""},
                    "_source": "legacy_events",
                }
            )
        return rows

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
        """Create-or-refresh an alert row in the unified messages table.

        Maps the legacy ``(session_name, alert_type)`` dedupe key onto
        the ``(scope, sender)`` columns of ``messages`` and routes
        through :meth:`upsert_message`, so at most one open alert exists
        per ``(session_name, alert_type)`` at a time — matching the
        pre-migration contract from :class:`StateStore.upsert_alert`.

        The ``severity`` column used to be first-class; under the
        unified schema it rides along in ``payload['severity']`` so the
        cockpit/alert readers can still filter by severity without a
        dedicated column. The subject is auto-tagged ``[Alert]`` by
        :func:`apply_title_contract`.
        """
        self.upsert_message(
            type="alert",
            tier="immediate",
            recipient="user",
            sender=alert_type,
            subject=message,
            body="",
            scope=session_name,
            payload={"severity": severity, "session_name": session_name},
        )

    def clear_alert(self, session_name: str, alert_type: str) -> None:
        """Close any open alert matching ``(session_name, alert_type)``.

        Mirrors the legacy :meth:`StateStore.clear_alert` — if no open
        row exists, the call is a no-op (we don't raise on "already
        cleared" because the heartbeat drives this on every sweep).
        """
        now = datetime.now(timezone.utc)
        with self.transaction() as conn:
            conn.execute(
                update(messages)
                .where(
                    and_(
                        messages.c.type == "alert",
                        messages.c.scope == session_name,
                        messages.c.sender == alert_type,
                        messages.c.state == "open",
                    )
                )
                .values(state="closed", closed_at=now, updated_at=now)
            )

    # ------------------------------------------------------------------
    # Test-only conveniences — do NOT call from production code paths.
    # ------------------------------------------------------------------

    def _delete_all_messages_for_tests(self) -> None:
        """Wipe the ``messages`` table. Tests only — prod callers must not use."""
        with self.transaction() as conn:
            conn.execute(delete(messages))
