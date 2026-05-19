"""Tests for :class:`pollypm.store.backends.pg_store.PgStore` (#1737, Slice I).

These tests target the real Postgres ``Store`` implementation registered
in :mod:`pyproject.toml`'s ``pollypm.store_backend`` entry-point group.
They use the schema-isolated ``pg_schema_pool`` fixture so each case
runs against its own ``test_<uuid>`` schema with the search_path
pre-patched — see ``tests/conftest_pg.py``.

Coverage
--------

* ``record_event`` / ``append_event`` round-trip into ``messages``.
* ``enqueue_message`` then ``query_messages`` returns the inserted row.
* ``query_messages`` with IN-list and unknown-filter rejection.
* ``upsert_message`` insert-then-update semantics.
* ``upsert_alert`` bumps ``payload['occurrences']``; ``clear_alert``
  closes the row and emits an ``alert.cleared`` event.
* ``transaction()`` commits on clean exit, rolls back on exception.
* ``execute()`` raises ``NotImplementedError`` — documented divergence.
* ``prune_messages`` deletes by type + older_than.
* Entry-point registration: ``importlib.metadata.entry_points`` exposes
  the ``postgres`` backend pointing at ``PgStore``.

Skipped when neither Docker nor a local pg with the ``vector`` extension
is reachable — see ``conftest_pg._pg_container`` for the fallback chain.
"""

from __future__ import annotations

import importlib.metadata
from datetime import datetime, timedelta, timezone

import pytest


def _new_store(pg_schema_pool):
    """Construct a :class:`PgStore` against the per-test schema pool.

    The fixture has already patched ``pg_pool._build_pool`` so every
    connection sets ``search_path`` to the test schema. ``PgStore``
    pulls the same module-level pool via :func:`get_rw_pool`, so the
    DSN passed here is informational only.
    """
    from pollypm.store.backends.pg_store import PgStore

    return PgStore(url="postgresql://test/ignored")


def test_entry_point_registers_postgres_backend():
    """The ``pollypm.store_backend`` group must expose ``postgres -> PgStore``."""
    matches = [
        ep
        for ep in importlib.metadata.entry_points(group="pollypm.store_backend")
        if ep.name == "postgres"
    ]
    assert matches, (
        "no 'postgres' entry point under 'pollypm.store_backend'; did "
        "pyproject.toml register it and was the editable install refreshed?"
    )
    target = matches[0].load()
    from pollypm.store.backends.pg_store import PgStore

    assert target is PgStore


def test_append_event_then_record_event_roundtrip(pg_schema_pool):
    """``append_event`` writes a row visible to ``query_messages``."""
    store = _new_store(pg_schema_pool)
    store.append_event(
        scope="proj-a",
        sender="worker",
        subject="job-finished",
        payload={"job_id": "j42"},
    )
    rows = store.query_messages(type="event", scope="proj-a")
    assert len(rows) == 1
    row = rows[0]
    assert row["sender"] == "worker"
    assert row["subject"] == "job-finished"
    assert row["payload"] == {"job_id": "j42"}
    assert row["kind"] == "activity_event"


def test_record_event_returns_inserted_id(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    rid = store.record_event(
        scope="proj-a",
        sender="worker",
        subject="hi",
        payload={"k": "v"},
    )
    assert isinstance(rid, int) and rid > 0


def test_enqueue_and_query_messages(pg_schema_pool):
    """``enqueue_message`` + ``query_messages`` round-trip the row + decode JSON."""
    store = _new_store(pg_schema_pool)
    row_id = store.enqueue_message(
        type="notify",
        tier="immediate",
        recipient="user",
        sender="supervisor",
        subject="deploy finished",
        body="done",
        scope="proj-x",
        labels=["deploy", "ci"],
        payload={"sha": "abc123"},
    )
    assert row_id > 0
    rows = store.query_messages(recipient="user", type="notify")
    assert len(rows) == 1
    row = rows[0]
    # apply_title_contract stamps the bracket tag.
    assert row["subject"].startswith("[Action]")
    assert "deploy finished" in row["subject"]
    assert row["labels"] == ["deploy", "ci"]
    assert row["payload"] == {"sha": "abc123"}
    assert row["state"] == "open"


def test_query_messages_in_list_and_unknown_filter(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    store.enqueue_message(
        type="notify", tier="immediate", recipient="user",
        sender="s", subject="a", body="", scope="p",
    )
    store.enqueue_message(
        type="alert", tier="immediate", recipient="user",
        sender="s", subject="b", body="", scope="p",
    )
    rows = store.query_messages(type=["notify", "alert"], scope="p")
    assert {row["type"] for row in rows} == {"notify", "alert"}

    with pytest.raises(ValueError):
        store.query_messages(no_such_column="x")


def test_query_messages_empty_in_list_short_circuits(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    # Sanity: even after writing a row, an empty IN-list returns [].
    store.enqueue_message(
        type="notify", tier="immediate", recipient="user",
        sender="s", subject="a", body="", scope="p",
    )
    assert store.query_messages(type=[]) == []


def test_upsert_message_insert_then_update(pg_schema_pool):
    """Second call with the same dedupe tuple updates the same row."""
    store = _new_store(pg_schema_pool)
    first = store.upsert_message(
        type="notify",
        tier="immediate",
        recipient="user",
        sender="supervisor",
        subject="first",
        body="body-1",
        scope="proj-x",
    )
    second = store.upsert_message(
        type="notify",
        tier="immediate",
        recipient="user",
        sender="supervisor",
        subject="second",
        body="body-2",
        scope="proj-x",
    )
    assert first == second
    rows = store.query_messages(recipient="user", type="notify")
    assert len(rows) == 1
    assert rows[0]["body"] == "body-2"


def test_upsert_message_rejects_unknown_dedupe_field(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    with pytest.raises(ValueError):
        store.upsert_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="s",
            subject="x",
            body="",
            scope="p",
            dedupe_key=("scope", "subject"),  # subject isn't dedupable
        )


def test_upsert_alert_bumps_occurrences(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    store.upsert_alert(
        session_name="sess-1",
        alert_type="quota_low",
        severity="warning",
        message="quota at 90%",
    )
    store.upsert_alert(
        session_name="sess-1",
        alert_type="quota_low",
        severity="warning",
        message="quota at 92%",
    )
    rows = store.query_messages(type="alert", scope="sess-1")
    assert len(rows) == 1
    assert rows[0]["payload"]["occurrences"] == 2
    assert rows[0]["payload"]["severity"] == "warning"


def test_clear_alert_closes_row_and_emits_event(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    store.upsert_alert(
        session_name="sess-2",
        alert_type="pane_dead",
        severity="critical",
        message="pane dead",
    )
    store.clear_alert("sess-2", "pane_dead", who_cleared="manual:cockpit-y-key")

    alerts = store.query_messages(type="alert", scope="sess-2", state="open")
    assert alerts == []
    closed = store.query_messages(type="alert", scope="sess-2", state="closed")
    assert len(closed) == 1

    events = store.query_messages(type="event", scope="sess-2")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["event_type"] == "alert.cleared"
    assert payload["who_cleared"] == "manual:cockpit-y-key"


def test_clear_alert_noop_when_no_open_row(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    # No matching alert ever opened — clear must be a silent no-op.
    store.clear_alert("sess-x", "pane_dead")
    assert store.query_messages(type="event", scope="sess-x") == []


def test_update_message_patches_fields(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    row_id = store.enqueue_message(
        type="notify",
        tier="immediate",
        recipient="user",
        sender="s",
        subject="x",
        body="",
        scope="p",
    )
    store.update_message(row_id, body="patched", labels=["x"])
    row = store.query_messages(recipient="user")[0]
    assert row["body"] == "patched"
    assert row["labels"] == ["x"]


def test_update_message_rejects_unknown_field(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    row_id = store.enqueue_message(
        type="notify", tier="immediate", recipient="user",
        sender="s", subject="x", body="", scope="p",
    )
    with pytest.raises(ValueError):
        store.update_message(row_id, no_such_column="x")


def test_close_message_marks_closed(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    row_id = store.enqueue_message(
        type="notify", tier="immediate", recipient="user",
        sender="s", subject="x", body="", scope="p",
    )
    store.close_message(row_id)
    rows = store.query_messages(recipient="user")
    assert len(rows) == 1
    assert rows[0]["state"] == "closed"


def test_transaction_commits_on_clean_exit(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    with store.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (scope, type, tier, recipient, sender, "
                "subject) VALUES ('p', 'notify', 'immediate', 'user', 's', 'tx')"
            )
    rows = store.query_messages(recipient="user")
    assert len(rows) == 1


def test_transaction_rolls_back_on_exception(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO messages (scope, type, tier, recipient, "
                    "sender, subject) VALUES ('p', 'notify', 'immediate', "
                    "'user', 's', 'tx-rollback')"
                )
            raise RuntimeError("force rollback")
    assert store.query_messages(recipient="user") == []


def test_prune_messages_requires_a_filter(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    with pytest.raises(ValueError):
        store.prune_messages()


def test_prune_messages_by_type_and_age(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    store.enqueue_message(
        type="event", tier="immediate", recipient="*",
        sender="s", subject="old", body="", scope="p",
    )
    store.enqueue_message(
        type="notify", tier="immediate", recipient="user",
        sender="s", subject="keep", body="", scope="p",
    )
    # Backdate the event row so the older_than cutoff catches it.
    with store.transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE messages SET created_at = now() - interval '7 days' "
            "WHERE type = 'event'"
        )
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    removed = store.prune_messages(type="event", older_than=cutoff)
    assert removed == 1
    remaining = store.query_messages()
    assert {row["type"] for row in remaining} == {"notify"}


def test_execute_raises_not_implemented(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    with pytest.raises(NotImplementedError):
        store.execute("SELECT 1")


def test_delete_all_messages_for_tests(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    store.enqueue_message(
        type="notify", tier="immediate", recipient="user",
        sender="s", subject="x", body="", scope="p",
    )
    store._delete_all_messages_for_tests()
    assert store.query_messages() == []


def test_dispose_and_close_are_safe(pg_schema_pool):
    store = _new_store(pg_schema_pool)
    store.dispose()
    store.close()
    store.close()  # idempotent
