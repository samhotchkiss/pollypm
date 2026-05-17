"""Unified ``messages`` table — one surface for every "something happened" row.

PollyPM used to spread this concept across five separate tables (``events``,
``inbox_messages``, ``work_tasks-chat``, ``notifications_staged``, …), each
with its own schema + writers + readers. Issue #338 collapses them into a
single ``messages`` table with ``type`` / ``tier`` / ``recipient`` / ``state``
as first-class filter columns.

The module exports:

* :data:`metadata` — the :class:`sqlalchemy.MetaData` that :meth:`SQLAlchemyStore.__init__`
  hands to ``metadata.create_all`` to materialize the schema.
* :data:`messages` — the :class:`sqlalchemy.Table` definition. Importable so
  callers can build ``select(messages).where(...)`` expressions without
  resurrecting raw-SQL string concatenation.
* :data:`FTS_DDL_STATEMENTS` — the list of raw DDL strings that create the
  FTS5 shadow table + its sync triggers. :class:`SQLAlchemyStore` executes
  these after ``create_all`` because SQLAlchemy Core has no portable
  representation for FTS5 virtual tables or SQLite triggers.

Anything beyond the messages surface belongs in a sibling schema module —
please don't bolt additional tables onto this one just because they fit.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
)


# --------------------------------------------------------------------------
# Core metadata + table
# --------------------------------------------------------------------------

metadata = MetaData()
"""Shared :class:`~sqlalchemy.MetaData` for the store package.

:class:`pollypm.store.sqlalchemy_store.SQLAlchemyStore` reuses this exact
instance so ``metadata.create_all(engine)`` materializes the full schema
in one pass. Additional tables (alerts, worktree state, …) should attach
to this same metadata so the store bootstrap stays single-source.
"""


messages = Table(
    "messages",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # scope partitions rows by blast radius. 'root' is workspace-wide; a
    # concrete project key scopes to that project's view.
    Column("scope", String, nullable=False),
    # type distinguishes firehose entries ('event') from human-visible
    # rows ('notify' / 'alert' / 'inbox_task' / 'audit').
    Column("type", String, nullable=False),
    # tier drives "show immediately" vs "batch into digest" vs "log-only".
    Column("tier", String, nullable=False, default="immediate"),
    # recipient is who should see this: 'user' / 'polly' / session name / '*'.
    Column("recipient", String, nullable=False),
    Column("sender", String, nullable=False),
    # state tracks the open/closed lifecycle. 'archived' is a terminal state
    # callers can use to hide without deleting.
    Column("state", String, nullable=False, default="open"),
    # parent_id lets rollup rows reference their rollup children without a
    # separate join table.
    Column(
        "parent_id",
        Integer,
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("subject", String, nullable=False),
    Column("body", Text, nullable=False, default=""),
    # payload_json / labels are stored as TEXT-JSON for portability; callers
    # decode via ``json.loads`` at the store boundary.
    Column("payload_json", Text, nullable=False, default="{}"),
    Column("labels", Text, nullable=False, default="[]"),
    # #1565 — structured inbox-item discriminator. See
    # ``pollypm.inbox.kind.InboxItemKind`` for the value set. The
    # server_default ensures the on-disk DDL DEFAULT matches the
    # StateStore schema so pre-migration rows and
    # SQLAlchemy-bootstrapped rows agree on the sentinel.
    Column("kind", String, nullable=False, server_default="legacy", default="legacy"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    # Hot-path indexes: inbox list (recipient+state), firehose filter
    # (type+tier), and per-scope recency scans.
    Index("idx_messages_recipient_state", "recipient", "state"),
    Index("idx_messages_type_tier", "type", "tier"),
    Index("idx_messages_scope_created", "scope", "created_at"),
)


# --------------------------------------------------------------------------
# FTS5 shadow table + triggers
# --------------------------------------------------------------------------
#
# SQLAlchemy Core can't express SQLite FTS5 virtual tables or triggers
# portably, so we ship raw DDL. ``SQLAlchemyStore.__init__`` executes these
# statements in a single transaction immediately after ``create_all`` so
# the FTS shadow is always in sync with the main table.
#
# Shape:
#   * ``messages_fts`` — FTS5 virtual table indexing (subject, body, labels)
#     with content='messages' + content_rowid='id' so the virtual table is
#     "contentless" and only stores the tokenized tail.
#   * Three triggers (``_ai``, ``_ad``, ``_au``) keep the FTS shadow
#     synchronized on every INSERT / DELETE / UPDATE to ``messages``.
#
# The ``IF NOT EXISTS`` guards make the bootstrap idempotent, which matters
# because the store __init__ path runs on every process start.

FTS_DDL_STATEMENTS: list[str] = [
    # Virtual table — FTS5 indexes subject/body/labels, backed by the real
    # ``messages`` table via content='messages' / content_rowid='id'.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        subject,
        body,
        labels,
        content='messages',
        content_rowid='id'
    )
    """,
    # #1044 — Partial unique index that pins down the alert-emit dedupe
    # contract. The legacy upsert_message check-then-act path was racy
    # across processes (heartbeat sweep + per-project sweep both writing
    # to the same DB through separate writer pools), letting two open
    # rows with the same (scope, sender, type='alert', recipient='user')
    # coexist. This partial index converts that race into an
    # IntegrityError that the upsert path catches and re-runs as an
    # update. SQLite supports partial indexes with WHERE clauses; the
    # index is keyed on the alert-dedupe tuple plus state='open' so it
    # only constrains live alerts (closed rows are duplicate-allowed
    # because their lifecycle has ended). The IF NOT EXISTS guard keeps
    # bootstrap idempotent on already-migrated DBs.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS messages_open_alert_uniq
    ON messages(scope, sender, recipient, type)
    WHERE state = 'open' AND type = 'alert'
    """,
    # After-insert: project new row into the FTS shadow.
    """
    CREATE TRIGGER IF NOT EXISTS messages_ai
    AFTER INSERT ON messages
    BEGIN
        INSERT INTO messages_fts (rowid, subject, body, labels)
        VALUES (new.id, new.subject, new.body, new.labels);
    END
    """,
    # After-delete: tombstone the row from FTS. The 'delete' command with
    # matching payload is the FTS5 external-content cleanup idiom.
    """
    CREATE TRIGGER IF NOT EXISTS messages_ad
    AFTER DELETE ON messages
    BEGIN
        INSERT INTO messages_fts (messages_fts, rowid, subject, body, labels)
        VALUES ('delete', old.id, old.subject, old.body, old.labels);
    END
    """,
    # After-update: delete old then insert new so the FTS index stays in
    # lockstep with edits to subject/body/labels.
    """
    CREATE TRIGGER IF NOT EXISTS messages_au
    AFTER UPDATE ON messages
    BEGIN
        INSERT INTO messages_fts (messages_fts, rowid, subject, body, labels)
        VALUES ('delete', old.id, old.subject, old.body, old.labels);
        INSERT INTO messages_fts (rowid, subject, body, labels)
        VALUES (new.id, new.subject, new.body, new.labels);
    END
    """,
]


__all__ = ["FTS_DDL_STATEMENTS", "messages", "metadata"]
