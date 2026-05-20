"""Postgres-backed :class:`Store` — Slice I of issue #1737.

This is the real Postgres implementation of the structural
:class:`pollypm.store.protocol.Store` contract. It replaces the
NotImplementedError stub in :mod:`pollypm.store.backends.postgres_stub`
that previously kept the protocol dialect-neutral but blocked the
``[storage] backend = "postgres"`` cutover with a ``StoreBackendNotFound``
regression.

Design
------

* **Reuses the shared pg pools.** The class never opens its own
  connections; reads route through :func:`pollypm.storage.pg_pool.get_ro_pool`
  and writes through :func:`~pollypm.storage.pg_pool.get_rw_pool` so all
  pg-backed subsystems share one pair of pools.
* **Schema bootstrap delegates to the migration applier.** On first
  construction we run :func:`pollypm.storage.pg_migrations.apply_migrations`
  so the per-process schema-on-open contract the SQLite path provides
  carries over verbatim.
* **SQL semantics ported, not the SQL.** The wire-level shape (column
  names, defaults, JSON encoding) matches :mod:`pollypm.store.sqlalchemy_store`
  but the queries are hand-written psycopg statements — the SQLAlchemy
  Executable surface does NOT travel cleanly across the dialect boundary
  for our schema (no ``RETURNING`` round-trip on sqlite, ``jsonb`` vs
  ``TEXT`` for payload columns, etc.).
* **``execute()`` is wired through the pool (#1820).** The legacy
  ``execute()`` escape hatch on :class:`SQLAlchemyStore` accepts a
  SQLAlchemy ``Executable``. Five production call sites still select
  ``execute()`` via ``hasattr(store, "execute")`` and broad ``except
  Exception``, so a raising stub silently broke the cockpit
  no-session metric and skipped event retention deletes on pg. The
  pg implementation compiles the Executable to the postgresql
  dialect and runs it on the rw pool, returning a small adapter
  exposing ``rowcount`` / ``fetchall()``. New callers should prefer
  the typed methods (:meth:`prune_messages`, :meth:`query_messages`,
  ...) so the protocol stays portable.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator

from pollypm.store.title_contract import apply_title_contract

if TYPE_CHECKING:
    from psycopg import Connection
    from psycopg_pool import ConnectionPool


logger = logging.getLogger(__name__)


# Supported dedupe-key fields for :meth:`PgStore.upsert_message`. Mirrors
# the SQLite path; new dedupe axes must be added to both stores so the
# protocol stays portable.
_SUPPORTED_DEDUPE_FIELDS = frozenset({"scope", "recipient", "type", "sender"})


# Supported query_messages filters. Mirrors :class:`SQLAlchemyStore`.
_SUPPORTED_QUERY_FILTERS = frozenset(
    {"type", "tier", "recipient", "state", "scope", "sender", "parent_id"}
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _ExecuteResult:
    """Cursor-shaped adapter returned by :meth:`PgStore.execute`.

    Mirrors the bits of SQLAlchemy's ``CursorResult`` that the existing
    callers actually read: ``rowcount`` on writes and ``fetchall()`` on
    reads. No fancier methods are added on purpose — if a caller wants
    more, it should move to a typed method on :class:`PgStore`.
    """

    __slots__ = ("rowcount", "_rows")

    def __init__(self, *, rowcount: int | None, rows: list[Any]) -> None:
        self.rowcount = int(rowcount) if rowcount is not None else 0
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


def _rewrite_qmark_to_percent_s(sql: str) -> str:
    """Rewrite sqlite ``?`` placeholders to psycopg ``%s``.

    Skips ``?`` inside single-quoted string literals so a SQL fragment
    like ``WHERE label = 'a?b'`` isn't mangled. Doubled quotes (escaped
    quotes inside strings) are handled by toggling the in-string state
    only on un-escaped quotes.

    This is deliberately a narrow rewrite — pg-native callers should
    use ``%s`` directly. The rewrite is just a backstop for the legacy
    ``DELETE FROM leases``-style call sites that pre-date the cutover.
    """
    out: list[str] = []
    in_string = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            # Doubled '' inside a string stays in-string.
            if in_string and i + 1 < len(sql) and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_string = not in_string
            out.append(ch)
        elif ch == "?" and not in_string:
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


class PgStore:
    """Postgres-backed implementation of the :class:`Store` protocol.

    Parameters
    ----------
    url:
        The pg DSN. The entry-point registry passes the resolved
        ``config.storage.url`` here. The store builds a thin config
        shim from ``url`` (when it looks like a pg DSN) and passes it
        to :func:`pollypm.storage.pg_pool.get_rw_pool` so the
        process-wide pool opens against the operator-configured DSN
        rather than silently falling back to localhost (#1819). When
        the pool is already open against a different DSN, that pool
        wins — the call site that opens the pool first sets the DSN
        for the lifetime of the process.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._closed = False
        self._close_lock = threading.Lock()
        self._bootstrap_lock = threading.Lock()
        self._schema_ready = False
        # Lazy schema bootstrap — every public method that opens a
        # connection routes through :meth:`_ensure_schema` first, so the
        # constructor stays light (cheap to instantiate in tests that
        # never touch the DB) and the first real call self-heals a fresh
        # database. Slice A's migration applier is idempotent on a
        # fully-applied schema, so the cost amortizes to one pg round-trip.
        self._ensure_schema()

    def _pool_config(self):
        """Build a minimal config-shaped object carrying ``self._url``.

        The pg pool's :func:`resolve_dsn` reads
        ``config.storage.pg.dsn`` and ``config.storage.url`` — passing
        the store's constructor URL through both fields covers the
        case where the registry handed us either the legacy
        ``[storage] url`` or the canonical ``[storage.pg] dsn`` (the
        registry resolves them into one ``url`` string before
        construction, and we can't tell which knob it came from).
        """
        url = (self._url or "").strip()
        if not url:
            return None
        # Only proxy a pg-shaped URL; sqlite URLs would be filtered
        # out by ``_looks_like_pg_dsn`` anyway, but skipping the build
        # avoids a useless shim object on sqlite-backed installs.
        from pollypm.storage.pg_pool import _looks_like_pg_dsn

        if not _looks_like_pg_dsn(url):
            return None

        class _PgSection:
            __slots__ = ("dsn",)

            def __init__(self, dsn: str) -> None:
                self.dsn = dsn

        class _Storage:
            __slots__ = ("url", "pg")

            def __init__(self, url: str) -> None:
                self.url = url
                self.pg = _PgSection(url)

        class _Config:
            __slots__ = ("storage",)

            def __init__(self, url: str) -> None:
                self.storage = _Storage(url)

        return _Config(url)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        """The DSN the store was constructed with. Not used to connect."""
        return self._url

    def dispose(self) -> None:
        """No-op — the pg pool lifetime is owned by ``pg_pool``.

        Provided for :class:`SQLAlchemyStore` API parity. Callers that
        want to tear down the pool should use
        :func:`pollypm.storage.pg_pool.pg_pool_shutdown` directly.
        """
        return None

    def close(self) -> None:
        """Idempotent teardown. No-op on pg — the pool is process-wide."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

    # ------------------------------------------------------------------
    # Pool helpers
    # ------------------------------------------------------------------

    def _rw_pool(self) -> "ConnectionPool":
        from pollypm.storage.pg_pool import get_rw_pool

        return get_rw_pool(self._pool_config())

    def _ro_pool(self) -> "ConnectionPool":
        # Read-side queries can run on the rw pool if the ro pool is
        # unavailable; the dispatch helpers do the same in Slice C. The
        # ro pool enforces ``default_transaction_read_only`` so a stray
        # write fails fast, which is the only reason to prefer it.
        from pollypm.storage.pg_pool import get_ro_pool, get_rw_pool

        cfg = self._pool_config()
        try:
            return get_ro_pool(cfg)
        except Exception:  # noqa: BLE001 - degrade to rw on ro errors
            return get_rw_pool(cfg)

    def _ensure_schema(self) -> None:
        """Apply the canonical pg migration pack on first use.

        Lazy + locked so concurrent first-callers don't double-apply.
        The migration applier is itself idempotent, so a double-apply
        would be safe — the lock just keeps the log noise down.
        """
        if self._schema_ready:
            return
        with self._bootstrap_lock:
            if self._schema_ready:
                return
            from pollypm.storage.pg_migrations import apply_migrations

            apply_migrations(self._rw_pool())
            self._schema_ready = True

    # ------------------------------------------------------------------
    # Transaction scope
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator["Connection"]:
        """Yield a write-scoped psycopg connection.

        Commits on clean exit, rolls back on exception. The yielded
        object is a psycopg :class:`Connection` (NOT a SQLAlchemy one).
        Callers that previously assumed the SQLAlchemy interface need a
        pg branch — every in-tree caller of ``store.transaction()`` is
        either already pg-aware (Slice C facades) or reached through
        typed methods on this class.
        """
        self._ensure_schema()
        with self._rw_pool().connection() as conn:
            # psycopg3 uses an implicit transaction; commit on clean exit.
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------

    def append_event(
        self,
        scope: str,
        sender: str,
        subject: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append an event row.

        The SQLite backend routes this through a background
        :class:`EventBuffer` so callers don't block on the writer
        pool. On pg the rw pool is already deep enough (default 10
        connections) that synchronous inserts are not the bottleneck;
        we call :meth:`record_event` directly and discard the row id.
        The Protocol does not promise async — only fire-and-forget at
        the call site — so the semantic is preserved.
        """
        try:
            self.record_event(scope, sender, subject, payload)
        except Exception:  # noqa: BLE001 - fire-and-forget swallows errors
            logger.exception(
                "pg_store: append_event failed; row dropped scope=%s sender=%s",
                scope,
                sender,
            )

    def record_event(
        self,
        scope: str,
        sender: str,
        subject: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Synchronously insert an event row. Returns the new row id."""
        self._ensure_schema()
        payload_obj = payload if payload is not None else {}
        sql = (
            "INSERT INTO messages ("
            "scope, type, tier, recipient, sender, state, subject, body, "
            "payload_json, labels, kind"
            ") VALUES ("
            "%s, 'event', 'immediate', '*', %s, 'open', %s, '', "
            "%s::jsonb, '[]'::jsonb, 'activity_event'"
            ") RETURNING id"
        )
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (scope, sender, subject, json.dumps(payload_obj)),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Message surface
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
        """Insert a single message row and return the new id."""
        self._ensure_schema()
        stamped_subject = apply_title_contract(subject, tier=tier, type=type)
        labels_obj = labels if labels is not None else []
        payload_obj = payload if payload is not None else {}
        sql = (
            "INSERT INTO messages ("
            "scope, type, tier, recipient, sender, state, parent_id, "
            "subject, body, payload_json, labels, kind"
            ") VALUES ("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s"
            ") RETURNING id"
        )
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    scope,
                    type,
                    tier,
                    recipient,
                    sender,
                    state,
                    parent_id,
                    stamped_subject,
                    body,
                    json.dumps(payload_obj),
                    json.dumps(labels_obj),
                    kind,
                ),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

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
        """Insert-or-update the open row matching ``dedupe_key``.

        Matches :meth:`SQLAlchemyStore.upsert_message` semantics:
        SELECT-then-(UPDATE|INSERT) inside a single writer transaction.
        Single-process this is race-free; cross-process the partial
        unique index in migration 0002 turns a racing INSERT into an
        ``IntegrityError`` that we catch and retry as an UPDATE.
        """
        self._ensure_schema()
        unknown = set(dedupe_key) - _SUPPORTED_DEDUPE_FIELDS
        if unknown:
            field_word = "field" if len(unknown) == 1 else "fields"
            raise ValueError(
                f"upsert_message received unsupported dedupe_key {field_word} "
                f"{sorted(unknown)!r}. "
                f"Only {sorted(_SUPPORTED_DEDUPE_FIELDS)} are valid because those "
                f"are the columns the indexed open-row lookup can match on. "
                f"Fix: remove the field or, if a new dedupe axis is genuinely "
                f"needed, extend the schema + widen this allowlist in PgStore."
            )

        stamped_subject = apply_title_contract(subject, tier=tier, type=type)
        labels_obj = labels if labels is not None else []
        payload_obj = payload if payload is not None else {}
        local_vars = {
            "scope": scope,
            "recipient": recipient,
            "type": type,
            "sender": sender,
        }
        now = _now()

        # Build the WHERE clause for the open-row lookup.
        where_parts = ["state = 'open'"]
        where_params: list[Any] = []
        for field in dedupe_key:
            where_parts.append(f"{field} = %s")
            where_params.append(local_vars[field])
        where_sql = " AND ".join(where_parts)

        select_sql = (
            f"SELECT id FROM messages WHERE {where_sql} "
            "ORDER BY id DESC LIMIT 1"
        )
        update_sql = (
            "UPDATE messages "
            "SET tier = %s, subject = %s, body = %s, "
            "    payload_json = %s::jsonb, labels = %s::jsonb, "
            "    parent_id = %s, kind = %s, updated_at = %s "
            "WHERE id = %s"
        )
        insert_sql = (
            "INSERT INTO messages ("
            "scope, type, tier, recipient, sender, state, parent_id, "
            "subject, body, payload_json, labels, kind"
            ") VALUES ("
            "%s, %s, %s, %s, %s, 'open', %s, %s, %s, %s::jsonb, %s::jsonb, %s"
            ") RETURNING id"
        )
        insert_params: tuple[Any, ...] = (
            scope,
            type,
            tier,
            recipient,
            sender,
            parent_id,
            stamped_subject,
            body,
            json.dumps(payload_obj),
            json.dumps(labels_obj),
            kind,
        )

        # First attempt — classic select-then-(update|insert).
        try:
            return self._upsert_round_trip(
                select_sql,
                where_params,
                update_sql,
                insert_sql,
                insert_params,
                stamped_subject=stamped_subject,
                tier=tier,
                body=body,
                payload_obj=payload_obj,
                labels_obj=labels_obj,
                parent_id=parent_id,
                kind=kind,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - branch on IntegrityError
            # psycopg raises ``psycopg.errors.UniqueViolation`` which
            # subclasses ``IntegrityError``; importing the symbol at
            # module scope would force psycopg into the import graph
            # for sqlite installs. Match by class name to keep the
            # dependency local without losing the discrimination.
            cls_name = exc.__class__.__name__
            if cls_name not in ("UniqueViolation", "IntegrityError"):
                raise
            # Retry: another writer raced us; re-SELECT to find the row
            # they committed and apply the UPDATE instead.
            return self._upsert_round_trip(
                select_sql,
                where_params,
                update_sql,
                insert_sql,
                insert_params,
                stamped_subject=stamped_subject,
                tier=tier,
                body=body,
                payload_obj=payload_obj,
                labels_obj=labels_obj,
                parent_id=parent_id,
                kind=kind,
                now=now,
            )

    def _upsert_round_trip(
        self,
        select_sql: str,
        where_params: list[Any],
        update_sql: str,
        insert_sql: str,
        insert_params: tuple[Any, ...],
        *,
        stamped_subject: str,
        tier: str,
        body: str,
        payload_obj: dict[str, Any],
        labels_obj: list[Any],
        parent_id: int | None,
        kind: str,
        now: datetime,
    ) -> int:
        """One SELECT-then-(UPDATE|INSERT) round-trip; see :meth:`upsert_message`."""
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute(select_sql, tuple(where_params))
            existing = cur.fetchone()
            if existing is not None:
                row_id = int(existing[0])
                cur.execute(
                    update_sql,
                    (
                        tier,
                        stamped_subject,
                        body,
                        json.dumps(payload_obj),
                        json.dumps(labels_obj),
                        parent_id,
                        kind,
                        now,
                        row_id,
                    ),
                )
                return row_id
            cur.execute(insert_sql, insert_params)
            new_row = cur.fetchone()
            return int(new_row[0]) if new_row else 0

    def update_message(self, id: int, **fields: Any) -> None:
        """Patch ``fields`` onto the message with id ``id``.

        ``payload`` / ``labels`` are JSON-encoded if passed as native
        Python objects. Unknown columns raise ``ValueError`` — silent
        no-op on typos has burned us in the legacy inbox code.
        """
        if not fields:
            return
        self._ensure_schema()

        # Column allowlist mirrors the sqlite schema. Kept in sync with
        # :data:`pollypm.store.schema.messages` and the pg DDL in
        # :data:`pollypm.storage.pg_schema._GROUP_MESSAGES`.
        allowed = {
            "scope",
            "type",
            "tier",
            "recipient",
            "sender",
            "state",
            "parent_id",
            "subject",
            "body",
            "payload_json",
            "labels",
            "kind",
            "created_at",
            "updated_at",
            "closed_at",
        }
        translated: dict[str, Any] = {}
        jsonb_cols: set[str] = set()
        for key, value in fields.items():
            column = key
            translated_value = value
            if key == "payload":
                column = "payload_json"
                translated_value = (
                    json.dumps(value) if not isinstance(value, str) else value
                )
                jsonb_cols.add(column)
            elif key == "labels":
                translated_value = (
                    json.dumps(value) if not isinstance(value, str) else value
                )
                jsonb_cols.add(column)
            if column not in allowed:
                raise ValueError(
                    f"update_message received unknown field {key!r}. "
                    f"No column by that name exists on ``messages`` so the "
                    f"update would be silently dropped. "
                    f"Fix: pass one of {sorted(allowed)} or extend the "
                    f"pg schema in pollypm/storage/pg_schema.py."
                )
            translated[column] = translated_value

        translated["updated_at"] = _now()

        set_parts: list[str] = []
        values: list[Any] = []
        for column, value in translated.items():
            if column in jsonb_cols:
                set_parts.append(f"{column} = %s::jsonb")
            else:
                set_parts.append(f"{column} = %s")
            values.append(value)
        values.append(id)
        sql = f"UPDATE messages SET {', '.join(set_parts)} WHERE id = %s"
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(values))

    def close_message(self, id: int) -> None:
        """Mark the message as closed; stamp ``closed_at`` / ``updated_at``."""
        self._ensure_schema()
        now = _now()
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE messages SET state = 'closed', closed_at = %s, "
                "updated_at = %s WHERE id = %s",
                (now, now, id),
            )

    def query_messages(self, **filters: Any) -> list[dict[str, Any]]:
        """Return rows matching ``filters``, newest first."""
        self._ensure_schema()
        limit = filters.pop("limit", None)
        since = filters.pop("since", None)

        unknown = set(filters) - _SUPPORTED_QUERY_FILTERS
        if unknown:
            filter_word = "filter" if len(unknown) == 1 else "filters"
            raise ValueError(
                f"query_messages received unsupported {filter_word} "
                f"{sorted(unknown)!r}. "
                f"Silent filter drops mask bugs in the inbox aggregation path. "
                f"Fix: remove the key, or widen the supported set in "
                f"PgStore.query_messages."
            )

        where: list[str] = []
        params: list[Any] = []
        for key, value in filters.items():
            if isinstance(value, (list, tuple, set, frozenset)):
                vals = list(value)
                if not vals:
                    # Empty IN-list — no rows can match; short-circuit
                    # so we don't synthesize ``IN ()`` (a pg syntax error).
                    return []
                placeholders = ",".join(["%s"] * len(vals))
                where.append(f"{key} IN ({placeholders})")
                params.extend(vals)
            else:
                where.append(f"{key} = %s")
                params.append(value)
        if since is not None:
            where.append("created_at >= %s")
            params.append(since)

        sql = (
            "SELECT id, scope, type, tier, recipient, sender, state, "
            "parent_id, subject, body, payload_json, labels, kind, "
            "created_at, updated_at, closed_at "
            "FROM messages"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        columns = (
            "id",
            "scope",
            "type",
            "tier",
            "recipient",
            "sender",
            "state",
            "parent_id",
            "subject",
            "body",
            "payload_json",
            "labels",
            "kind",
            "created_at",
            "updated_at",
            "closed_at",
        )
        rows: list[dict[str, Any]] = []
        with self._ro_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            for raw in cur.fetchall():
                row: dict[str, Any] = dict(zip(columns, raw))
                # Postgres ``jsonb`` columns come back already decoded;
                # mirror the sqlite path's coerce-to-empty fallback so
                # downstream callers can always ``payload.get(...)``.
                payload = row.get("payload_json")
                row["payload"] = payload if isinstance(payload, dict) else {}
                labels = row.get("labels")
                row["labels"] = labels if isinstance(labels, list) else []
                rows.append(row)
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

        Extended in #1820 to mirror :meth:`SQLAlchemyStore.prune_messages`
        — adds ``subject`` / ``subject_not_in`` / ``state`` filters so
        the event-retention sweep and ``pm reset`` alert wipe can stay
        on the typed protocol surface instead of falling through to
        :meth:`execute` (which previously raised on the pg backend).
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
                "An unfiltered delete would truncate the ``messages`` table, "
                "which is never the intent. "
                "Fix: pass one of ``type=``, ``older_than=``, "
                "``subject=``, ``subject_not_in=``, or ``state=``."
            )
        self._ensure_schema()
        where: list[str] = []
        params: list[Any] = []
        if type is not None:
            if isinstance(type, (list, tuple, set, frozenset)):
                vals = list(type)
                if not vals:
                    return 0
                placeholders = ",".join(["%s"] * len(vals))
                where.append(f"type IN ({placeholders})")
                params.extend(vals)
            else:
                where.append("type = %s")
                params.append(type)
        if older_than is not None:
            where.append("created_at < %s")
            params.append(older_than)
        if subject is not None:
            if isinstance(subject, (list, tuple, set, frozenset)):
                vals = list(subject)
                if not vals:
                    return 0
                placeholders = ",".join(["%s"] * len(vals))
                where.append(f"subject IN ({placeholders})")
                params.extend(vals)
            else:
                where.append("subject = %s")
                params.append(subject)
        if subject_not_in is not None:
            vals = list(subject_not_in)
            if vals:
                placeholders = ",".join(["%s"] * len(vals))
                where.append(f"subject NOT IN ({placeholders})")
                params.extend(vals)
        if state is not None:
            if isinstance(state, (list, tuple, set, frozenset)):
                vals = list(state)
                if not vals:
                    return 0
                placeholders = ",".join(["%s"] * len(vals))
                where.append(f"state IN ({placeholders})")
                params.extend(vals)
            else:
                where.append("state = %s")
                params.append(state)
        if exclude_pinned:
            # ``payload_json`` is ``jsonb`` on pg. ``->>`` extracts the
            # value as text, so JSON booleans surface as ``'true'`` /
            # ``'false'`` and JSON ints as ``'1'`` / ``'0'``. The
            # protocol (#1912) treats any truthy ``pinned`` payload as
            # "preserve" — sqlite's ``json_extract(...) != 1`` does so
            # implicitly because it coerces JSON ``true`` to ``1``. On
            # pg we have to enumerate the truthy text forms ourselves
            # (and let NULL fall through to "not pinned" via NOT IN).
            where.append(
                "COALESCE(payload_json->>'pinned', '') "
                "NOT IN ('1', 'true', 't', 'True')"
            )
        sql = f"DELETE FROM messages WHERE {' AND '.join(where)}"
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.rowcount or 0

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
        """Create-or-refresh an alert row; bumps ``occurrences`` counter.

        Mirrors :meth:`SQLAlchemyStore.upsert_alert` — at most one open
        alert per ``(session_name, alert_type)``, ``payload['occurrences']``
        increments on every re-emit. Maps to ``(scope, sender)`` on
        ``messages``.
        """
        self._ensure_schema()
        # Read prior occurrences so we hand a fully-materialised payload
        # to ``upsert_message`` (which doesn't know about alerts).
        prior_occurrences = 0
        with self._ro_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT payload_json FROM messages "
                "WHERE type = 'alert' AND scope = %s AND sender = %s "
                "  AND state = 'open' "
                "ORDER BY id DESC LIMIT 1",
                (session_name, alert_type),
            )
            row = cur.fetchone()
        if row is not None and isinstance(row[0], dict):
            raw = row[0].get("occurrences", 0)
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

        Emits one ``alert.cleared`` event per closed row so the activity
        feed surfaces both creates and clears (#1033). No-op if no open
        row exists — the heartbeat sweep calls this on every tick.
        """
        self._ensure_schema()
        now = _now()
        closed_rows: list[tuple[int, str, dict[str, Any], datetime | None]] = []
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, subject, payload_json, created_at FROM messages "
                "WHERE type = 'alert' AND scope = %s AND sender = %s "
                "  AND state = 'open'",
                (session_name, alert_type),
            )
            for row in cur.fetchall():
                payload = row[2] if isinstance(row[2], dict) else {}
                closed_rows.append((int(row[0]), row[1] or "", payload, row[3]))
            if not closed_rows:
                return
            cur.execute(
                "UPDATE messages SET state = 'closed', closed_at = %s, "
                "updated_at = %s "
                "WHERE type = 'alert' AND scope = %s AND sender = %s "
                "  AND state = 'open'",
                (now, now, session_name, alert_type),
            )

        # Emit ``alert.cleared`` events outside the closing transaction
        # so a reader of the event always sees ``state='closed'`` on
        # the underlying alert row.
        for row_id, subject_text, payload, opened_at in closed_rows:
            severity = ""
            if isinstance(payload, dict):
                severity = str(payload.get("severity") or "")
            cleaned_subject = subject_text
            if cleaned_subject.startswith("[Alert] "):
                cleaned_subject = cleaned_subject[len("[Alert] "):]
            summary_text = (
                f"Cleared {alert_type} on {session_name} ({who_cleared})"
            )
            opened_iso = ""
            if opened_at is not None:
                opened_iso = (
                    opened_at.isoformat()
                    if hasattr(opened_at, "isoformat")
                    else str(opened_at)
                )
            self.record_event(
                scope=session_name,
                sender=alert_type,
                subject="alert.cleared",
                payload={
                    "event_type": "alert.cleared",
                    "alert_id": row_id,
                    "alert_type": alert_type,
                    "session_name": session_name,
                    "severity": severity,
                    "who_cleared": who_cleared,
                    "summary": summary_text,
                    "message": cleaned_subject,
                    "opened_at": opened_iso,
                },
            )

    # ------------------------------------------------------------------
    # SQLAlchemy / raw-SQL escape hatch
    # ------------------------------------------------------------------

    def execute(self, stmt: Any, params: Any = None) -> Any:
        """Execute a SQLAlchemy Executable or a raw SQL string on the pg pool.

        #1820 — historically this raised ``NotImplementedError`` on pg,
        but five production callers select it via ``hasattr(store,
        "execute")`` and broad ``except Exception``, so under
        ``[storage] backend = "postgres"`` the cockpit no-session
        metric reported zero and event retention silently skipped
        pruning. Wiring this through to the pool restores parity with
        :meth:`SQLAlchemyStore.execute` for the existing callers.

        Two accepted call shapes:

        * **SQLAlchemy Core ``Executable``** — compiled with the
          ``postgresql`` dialect (no literal-binds) and executed through
          a psycopg cursor. Returns a small adapter exposing
          ``rowcount`` and ``fetchall()`` so existing callers that read
          ``result.rowcount`` keep working.

        * **Raw SQL string (+ optional params tuple)** — passed to
          ``cur.execute()`` after a sqlite ``?`` → pg ``%s`` rewrite
          so the few raw-SQL call sites that drifted in don't have to
          branch on backend.

        The implementation deliberately stays narrow: callers wanting
        full SQLAlchemy ORM semantics should still use the typed methods
        (:meth:`prune_messages`, :meth:`query_messages`, ...). This is
        the escape hatch the Store protocol promises (#1820), not a
        full SQLAlchemy execution surface.
        """
        self._ensure_schema()

        # Branch 1: raw SQL string. Rewrite sqlite ``?`` placeholders
        # to psycopg ``%s`` so the legacy ``DELETE FROM leases``-style
        # call sites stay backend-agnostic. The rewrite is a literal
        # substitution that skips ``?`` inside single-quoted strings.
        if isinstance(stmt, str):
            sql = _rewrite_qmark_to_percent_s(stmt)
            args = tuple(params) if params else ()
            with self.transaction() as conn, conn.cursor() as cur:
                cur.execute(sql, args)
                rowcount = cur.rowcount
                rows: list[Any] = []
                try:
                    if cur.description is not None:
                        rows = list(cur.fetchall())
                except Exception:  # noqa: BLE001 — non-SELECTs have no rows
                    rows = []
            return _ExecuteResult(rowcount=rowcount, rows=rows)

        # Branch 2: SQLAlchemy Executable. Compile to a pg dialect
        # string + bind-parameter dict, then run through psycopg.
        try:
            from sqlalchemy.dialects import postgresql as _pg_dialect
        except ImportError as exc:  # pragma: no cover — sqlalchemy is a hard dep
            raise NotImplementedError(
                "PgStore.execute received a SQLAlchemy Executable but "
                "SQLAlchemy isn't importable in this environment."
            ) from exc

        try:
            compiled = stmt.compile(
                dialect=_pg_dialect.dialect(),
                compile_kwargs={"render_postcompile": True},
            )
        except AttributeError as exc:
            raise TypeError(
                "PgStore.execute expects a SQLAlchemy Executable or a "
                f"SQL string; got {type(stmt).__name__}."
            ) from exc

        sql_text = str(compiled)
        bind_params = dict(getattr(compiled, "params", {}) or {})

        with self.transaction() as conn, conn.cursor() as cur:
            # psycopg accepts named placeholders via a dict mapping.
            cur.execute(sql_text, bind_params)
            rowcount = cur.rowcount
            rows = []
            try:
                if cur.description is not None:
                    rows = list(cur.fetchall())
            except Exception:  # noqa: BLE001 — non-SELECTs have no rows
                rows = []
        return _ExecuteResult(rowcount=rowcount, rows=rows)

    # ------------------------------------------------------------------
    # Test-only conveniences — do NOT call from production paths.
    # ------------------------------------------------------------------

    def _delete_all_messages_for_tests(self) -> None:
        """Wipe the ``messages`` table within the active schema.

        Used by tests against the per-test schema fixture, where the
        search_path is pinned to ``test_<uuid>``. A DELETE here only
        affects the test schema's table — no cross-test leakage.
        """
        self._ensure_schema()
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM messages")


__all__ = ["PgStore"]
