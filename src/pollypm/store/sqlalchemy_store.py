"""SQLAlchemy-backed :class:`Store` — schema bootstrap + message/event methods.

Issue #337 landed the skeleton (engine factory + ``transaction()``). Issue
#338 filled in the surface that writes to the unified ``messages`` table;
#342 retired the legacy ``events`` / ``alerts`` bridge now that supervisor
and heartbeat writers land straight on ``messages``.

* Schema bootstrap in ``__init__`` — ``metadata.create_all(write_engine)``
  plus the FTS5 shadow DDL, all inside a single writer transaction so a
  partial failure rolls back cleanly.
* :meth:`append_event` — fire-and-forget; lazily provisions the private
  :class:`EventBuffer` on first call so test doubles that never emit
  events don't spin up a drainer thread.
* :meth:`record_event`, :meth:`enqueue_message`, :meth:`update_message`,
  :meth:`close_message`, :meth:`query_messages` — synchronous inserts /
  updates / reads against the ``messages`` table.
* :meth:`prune_messages` — tier-aware retention, folded in from the
  deleted :mod:`pollypm.storage.events_retention` module.
* :meth:`upsert_alert` / :meth:`clear_alert` — thin wrappers that map
  the legacy ``(session_name, alert_type)`` shape onto ``messages``.
* :meth:`close` — idempotent teardown that flushes the event buffer and
  disposes both engine pools.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import Executable, MetaData, and_, delete, insert, select, text, update
from sqlalchemy.engine import Connection, CursorResult, Engine
from sqlalchemy.exc import IntegrityError

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

        #1044 — Before installing the partial unique index that enforces
        ``one open alert per (scope, sender)``, run a one-shot backfill
        that closes any duplicate open alert rows the pre-fix race left
        behind. Without this, ``CREATE UNIQUE INDEX`` would fail on any
        workspace whose ``pm alerts`` output already shows duplicate
        rows (the exact symptom the issue was filed for). The backfill
        keeps the *oldest* row id per ``(type='alert', scope, sender)``
        group and marks the rest as ``closed`` — the operator-facing
        view de-duplicates in the same heartbeat tick the upgrade lands.

        #1565 — ``create_all`` is a no-op on an already-existing
        ``messages`` table, so a legacy DB opened directly through
        ``SQLAlchemyStore`` (without passing through the StateStore v18
        migration) won't gain the ``kind`` column. Backfill it here
        with the documented default so every consumer's SELECT path
        sees the column.
        """
        metadata.create_all(self._write_engine)
        with self.transaction() as conn:
            self._ensure_messages_kind_column(conn)
            # Run the backfill first so the partial unique index DDL
            # below doesn't trip over pre-existing duplicates.
            self._collapse_duplicate_open_alerts(conn)
            for stmt in FTS_DDL_STATEMENTS:
                conn.execute(text(stmt))

    @staticmethod
    def _ensure_messages_kind_column(conn: Connection) -> None:
        """Backfill ``messages.kind`` on legacy DBs (#1565).

        Mirrors the ``_safe_add_column`` pattern from StateStore. SQLite
        lacks IF NOT EXISTS on ADD COLUMN, so check PRAGMA first. The
        default ``'legacy'`` matches both the SCHEMA constant and the
        StateStore migration v18 so a DB opened through either entry
        point ends up with the same shape.
        """
        cols = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(messages)")
        }
        if "kind" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE messages "
                "ADD COLUMN kind TEXT NOT NULL DEFAULT 'legacy'"
            )

    @staticmethod
    def _collapse_duplicate_open_alerts(conn: Connection) -> int:
        """Close duplicate open alert rows, keeping the oldest id per group.

        #1044 — backfill helper. Returns the number of rows closed (0 on
        a clean DB). Called from :meth:`_bootstrap_schema` immediately
        before the partial unique index is created so the ``CREATE
        UNIQUE INDEX`` statement doesn't fail on existing dupes.

        Group key is ``(type='alert', scope, sender)`` — same tuple the
        unique index constrains. Within each group the smallest ``id``
        survives ('older first', matching the issue's "keep older" ask)
        and every other open row is flipped to ``state='closed'`` with a
        ``closed_at`` timestamp so downstream surfaces (cockpit,
        ``pm alerts``) see exactly one open row per project / alert
        type.
        """
        now = datetime.now(timezone.utc)
        result = conn.execute(
            text(
                """
                UPDATE messages
                SET state = 'closed', closed_at = :now, updated_at = :now
                WHERE type = 'alert'
                  AND state = 'open'
                  AND id NOT IN (
                      SELECT MIN(id)
                      FROM messages
                      WHERE type = 'alert' AND state = 'open'
                      GROUP BY scope, sender, recipient
                  )
                """
            ),
            {"now": now},
        )
        return int(result.rowcount or 0)

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
            # Events are firehose entries, not user-facing rows. They
            # carry ``activity_event`` so any future inbox/dashboard
            # filter that queries the unified messages table treats
            # them as informational. The kind here mirrors the
            # ``InboxItemKind.ACTIVITY_EVENT`` value verbatim.
            "kind": "activity_event",
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
        kind: str = "legacy",
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
            "kind": kind,
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
        kind: str = "legacy",
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
            field_word = "field" if len(unknown) == 1 else "fields"
            raise ValueError(
                f"upsert_message received unsupported dedupe_key {field_word} "
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

        def _select_existing(conn: Connection) -> int | None:
            conditions = [messages.c.state == "open"]
            for field in dedupe_key:
                conditions.append(messages.c[field] == local_vars[field])
            row = conn.execute(
                select(messages.c.id)
                .where(and_(*conditions))
                .order_by(messages.c.id.desc())
                .limit(1)
            ).fetchone()
            return int(row[0]) if row is not None else None

        def _apply_update(conn: Connection, row_id: int) -> None:
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
                    kind=kind,
                    updated_at=now,
                )
            )

        # First attempt: classic select-then-(update|insert) inside a
        # single writer transaction. Single-process this is race-free
        # because the writer pool is size=1; cross-process the SELECT
        # may see a stale snapshot and the INSERT may collide with the
        # partial unique index (#1044). On IntegrityError we retry
        # exactly once — a second SELECT now sees the row the other
        # writer just committed, and we land on the UPDATE branch
        # instead of inserting a second open row.
        try:
            with self.transaction() as conn:
                existing_id = _select_existing(conn)
                if existing_id is not None:
                    _apply_update(conn, existing_id)
                    return existing_id
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
                        "kind": kind,
                    },
                )
                inserted = result.inserted_primary_key
            return int(inserted[0]) if inserted else 0
        except IntegrityError:
            # #1044 — another writer raced us to the INSERT. The unique
            # index rejected the duplicate, which is the correct
            # outcome. Re-enter the transaction, re-SELECT, and update
            # the row that won the race. If for some reason no row is
            # visible on retry (the other writer rolled back), retry
            # the INSERT once more — this is the only path that can
            # legitimately race twice in a row.
            with self.transaction() as conn:
                existing_id = _select_existing(conn)
                if existing_id is not None:
                    _apply_update(conn, existing_id)
                    return existing_id
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
                        "kind": kind,
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
            filter_word = "filter" if len(unknown) == 1 else "filters"
            raise ValueError(
                f"query_messages received unsupported {filter_word} "
                f"{sorted(unknown)!r}. "
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

        # Decode JSON text columns for caller convenience. Producers
        # always write dict payloads / list labels (see ``upsert_message``
        # / ``append_event``), but a hand-edited or legacy DB could
        # surface a non-dict payload (list/null/string) — downstream
        # code does ``payload.get(...)``, which would AttributeError
        # on those shapes. Coerce parsed-but-wrong-type values back to
        # their expected empty form so a corrupt row degrades gracefully
        # instead of crashing the cockpit inbox.
        for row in rows:
            payload_json = row.get("payload_json")
            if isinstance(payload_json, str):
                try:
                    parsed = json.loads(payload_json)
                except json.JSONDecodeError:
                    parsed = {}
                row["payload"] = parsed if isinstance(parsed, dict) else {}
            labels_json = row.get("labels")
            if isinstance(labels_json, str):
                try:
                    parsed_labels = json.loads(labels_json)
                except json.JSONDecodeError:
                    parsed_labels = []
                row["labels"] = (
                    parsed_labels if isinstance(parsed_labels, list) else []
                )
        return rows

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def prune_messages(
        self,
        *,
        type: str | list[str] | tuple[str, ...] | set[str] | None = None,
        older_than: datetime | None = None,
        subject: str | list[str] | tuple[str, ...] | set[str] | None = None,
        subject_not_in: list[str] | tuple[str, ...] | set[str] | None = None,
        state: str | list[str] | tuple[str, ...] | set[str] | None = None,
        exclude_pinned: bool = False,
    ) -> int:
        """Delete messages matching the given filters.

        Replaces the legacy tiered ``events`` retention sweep (folded in
        from :mod:`pollypm.storage.events_retention` when #342 retired
        the legacy tables). Also serves as the typed entry point for
        ``pm reset``'s alert wipe (#1820) so callers don't reach into
        SQLAlchemy ``delete()`` through :meth:`execute`.

        At least one filter is required. Returns the number of rows
        deleted. ``0`` is a valid (and common) result.

        Raises
        ------
        ValueError
            If no filter is supplied. We refuse to delete the whole
            table accidentally; pass at least one filter.
        """
        if (
            type is None
            and older_than is None
            and subject is None
            and subject_not_in is None
            and state is None
        ):
            raise ValueError(
                "prune_messages requires at least one filter. "
                "An unfiltered delete would truncate the ``messages`` "
                "table, which is never the intent. "
                "Fix: pass one of ``type=``, ``older_than=``, "
                "``subject=``, ``subject_not_in=``, or ``state=``."
            )
        conditions = []
        if type is not None:
            if isinstance(type, (list, tuple, set, frozenset)):
                conditions.append(messages.c.type.in_(list(type)))
            else:
                conditions.append(messages.c.type == type)
        if older_than is not None:
            conditions.append(messages.c.created_at < older_than)
        if subject is not None:
            if isinstance(subject, (list, tuple, set, frozenset)):
                conditions.append(messages.c.subject.in_(list(subject)))
            else:
                conditions.append(messages.c.subject == subject)
        if subject_not_in is not None:
            vals = list(subject_not_in)
            if vals:
                conditions.append(messages.c.subject.notin_(vals))
        if state is not None:
            if isinstance(state, (list, tuple, set, frozenset)):
                conditions.append(messages.c.state.in_(list(state)))
            else:
                conditions.append(messages.c.state == state)
        if exclude_pinned:
            from sqlalchemy import func as _func

            conditions.append(
                _func.coalesce(
                    _func.json_extract(messages.c.payload_json, "$.pinned"),
                    0,
                )
                != 1
            )
        stmt = delete(messages).where(and_(*conditions))
        with self.transaction() as conn:
            result = conn.execute(stmt)
        return int(result.rowcount or 0)

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

        #1044 — increments a ``payload['occurrences']`` counter on every
        re-emit so operators can tell a chronic alert from a one-shot.
        The counter starts at 1 on first emit; each idempotent re-emit
        bumps it. The bump is observed inside the same writer
        transaction that ``upsert_message`` opens, so cross-process
        races round-trip through the partial unique index instead of
        producing two rows with ``occurrences=1`` each.
        """
        # Look up any existing open row to compute the next occurrences
        # value. This second read is cheap (the dedupe index covers it)
        # and it lets us hand a fully-materialised payload to
        # ``upsert_message`` without teaching that helper the alert-
        # specific counter semantics.
        with self._read_engine.connect() as conn:
            existing_row = conn.execute(
                select(messages.c.payload_json)
                .where(
                    and_(
                        messages.c.type == "alert",
                        messages.c.scope == session_name,
                        messages.c.sender == alert_type,
                        messages.c.state == "open",
                    )
                )
                .order_by(messages.c.id.desc())
                .limit(1)
            ).fetchone()
        prior_occurrences = 0
        if existing_row is not None:
            try:
                prior_payload = json.loads(existing_row[0] or "{}")
            except json.JSONDecodeError:
                prior_payload = {}
            if isinstance(prior_payload, dict):
                raw = prior_payload.get("occurrences", 0)
                if isinstance(raw, int) and raw > 0:
                    prior_occurrences = raw
        self.upsert_message(
            type="alert",
            tier="immediate",
            recipient="user",
            sender=alert_type,
            subject=message,
            body="",
            scope=session_name,
            payload={
                "severity": severity,
                "session_name": session_name,
                "occurrences": prior_occurrences + 1,
            },
        )

    def clear_alert(
        self,
        session_name: str,
        alert_type: str,
        *,
        who_cleared: str = "system",
    ) -> None:
        """Close any open alert matching ``(session_name, alert_type)``.

        Mirrors the legacy :meth:`StateStore.clear_alert` — if no open
        row exists, the call is a no-op (we don't raise on "already
        cleared" because the heartbeat drives this on every sweep).

        When at least one open row is closed, an ``alert.cleared`` event
        is recorded into the activity stream so the feed shows a paired
        clear for every create (#1033). The ``who_cleared`` kwarg lets
        callers attribute the close (``auto:supervisor-recovery``,
        ``manual:pm-alert-clear``, ``manual:cockpit-y-key``); the default
        of ``"system"`` keeps existing call sites working without
        changes.
        """
        now = datetime.now(timezone.utc)
        with self.transaction() as conn:
            existing = conn.execute(
                select(
                    messages.c.id,
                    messages.c.subject,
                    messages.c.payload_json,
                    messages.c.created_at,
                ).where(
                    and_(
                        messages.c.type == "alert",
                        messages.c.scope == session_name,
                        messages.c.sender == alert_type,
                        messages.c.state == "open",
                    )
                )
            ).fetchall()
            if not existing:
                return
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

        # Emit one ``alert.cleared`` event per row that we just closed.
        # Done after the UPDATE transaction so reads of the event will
        # always see ``state='closed'`` on the underlying alert row.
        for row in existing:
            try:
                row_payload = json.loads(row[2]) if row[2] else {}
            except (json.JSONDecodeError, ValueError):
                row_payload = {}
            severity = ""
            if isinstance(row_payload, dict):
                severity = str(row_payload.get("severity") or "")
            subject_text = row[1] or ""
            if subject_text.startswith("[Alert] "):
                subject_text = subject_text[len("[Alert] "):]
            summary_text = (
                f"Cleared {alert_type} on {session_name}"
                f" ({who_cleared})"
            )
            self.record_event(
                scope=session_name,
                sender=alert_type,
                subject="alert.cleared",
                payload={
                    "event_type": "alert.cleared",
                    "alert_id": int(row[0]),
                    "alert_type": alert_type,
                    "session_name": session_name,
                    "severity": severity,
                    "who_cleared": who_cleared,
                    "summary": summary_text,
                    "message": subject_text,
                    "opened_at": (
                        row[3].isoformat()
                        if hasattr(row[3], "isoformat")
                        else str(row[3] or "")
                    ),
                },
            )

    # ------------------------------------------------------------------
    # Test-only conveniences — do NOT call from production code paths.
    # ------------------------------------------------------------------

    def _delete_all_messages_for_tests(self) -> None:
        """Wipe the ``messages`` table. Tests only — prod callers must not use."""
        with self.transaction() as conn:
            conn.execute(delete(messages))
