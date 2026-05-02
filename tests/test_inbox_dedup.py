"""Tests for inbox dedup-key collapsing (#1013, sub-bug C).

Pins:

* :func:`find_open_dedup_message` returns the matching open notify, or
  ``None`` when no open row carries the key.
* :func:`bump_dedup_message` increments ``count``, refreshes
  ``last_seen``, and updates ``subject``/``body``/``payload`` in place.
* :func:`initial_dedup_payload` seeds ``count=1`` and ``last_seen``.
* :func:`format_dedup_suffix` renders ``9x - last seen 2d ago`` style
  copy when ``count > 1``; empty string otherwise.
* The ``pm notify --dedup-key`` writer collapses repeated calls onto a
  single row instead of inserting fresh rows each time.
* ``pm inbox`` surfaces the count as a parenthetical suffix on the
  collapsed row.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.cli import app as root_app
from pollypm.inbox_dedup import (
    bump_dedup_message,
    find_open_dedup_message,
    format_dedup_suffix,
    initial_dedup_payload,
)
from pollypm.store import SQLAlchemyStore
from pollypm.work.inbox_cli import inbox_app


runner = CliRunner()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


@pytest.fixture
def store(db_path):
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    yield store
    store.close()


class TestFindOpenDedupMessage:
    def test_returns_match(self, store):
        mid = store.enqueue_message(
            type="notify", tier="digest", recipient="user",
            sender="polly", subject="alert", body=".", scope="proj",
            payload={"dedup_key": "my-key", "count": 1},
            state="open",
        )
        match = find_open_dedup_message(store, "my-key")
        assert match is not None
        assert match["id"] == mid

    def test_returns_none_for_unknown_key(self, store):
        store.enqueue_message(
            type="notify", tier="digest", recipient="user",
            sender="polly", subject="alert", body=".", scope="proj",
            payload={"dedup_key": "other-key"},
            state="open",
        )
        assert find_open_dedup_message(store, "missing") is None

    def test_returns_none_for_empty_key(self, store):
        # Empty key never matches — the legacy insert-as-new path
        # depends on the empty-key guard so existing callers don't
        # silently collapse onto each other.
        store.enqueue_message(
            type="notify", tier="digest", recipient="user",
            sender="polly", subject="alert", body=".", scope="proj",
            payload={"dedup_key": ""},
            state="open",
        )
        assert find_open_dedup_message(store, "") is None

    def test_skips_closed_messages(self, store):
        mid = store.enqueue_message(
            type="notify", tier="digest", recipient="user",
            sender="polly", subject="alert", body=".", scope="proj",
            payload={"dedup_key": "my-key"},
            state="open",
        )
        store.close_message(mid)
        # Closed → fresh insert path runs.
        assert find_open_dedup_message(store, "my-key") is None


class TestBumpDedupMessage:
    def test_increments_count_and_refreshes_payload(self, store):
        mid = store.enqueue_message(
            type="notify", tier="digest", recipient="user",
            sender="polly", subject="alert v1", body="first body",
            scope="proj",
            payload={"dedup_key": "k", "count": 1},
            state="open",
        )
        rows = store.query_messages(type="notify", recipient="user")
        bump_dedup_message(
            store, rows[0],
            subject="alert v2",
            body="updated body",
            payload={"dedup_key": "k", "actor": "polly"},
        )

        rows = store.query_messages(type="notify", recipient="user")
        match = next(r for r in rows if r["id"] == mid)
        # apply_title_contract may add a tag prefix; assert substring.
        assert "alert v2" in match["subject"]
        assert match["body"] == "updated body"
        payload = match["payload"]
        assert payload["count"] == 2
        assert payload["dedup_key"] == "k"
        assert "last_seen" in payload

    def test_preserves_dedup_key_when_caller_omits_it(self, store):
        mid = store.enqueue_message(
            type="notify", tier="digest", recipient="user",
            sender="polly", subject="alert", body=".", scope="proj",
            payload={"dedup_key": "k", "count": 1},
            state="open",
        )
        rows = store.query_messages(type="notify", recipient="user")
        # Caller passes payload without dedup_key — the bump must
        # preserve the existing key so future lookups still find it.
        bump_dedup_message(
            store, rows[0],
            subject="alert", body=".", payload={"actor": "polly"},
        )

        rows = store.query_messages(type="notify", recipient="user")
        match = next(r for r in rows if r["id"] == mid)
        assert match["payload"]["dedup_key"] == "k"


class TestInitialDedupPayload:
    def test_seeds_count_and_last_seen(self):
        out = initial_dedup_payload({"actor": "polly"}, "k")
        assert out["dedup_key"] == "k"
        assert out["count"] == 1
        assert "last_seen" in out
        # Caller's payload is preserved.
        assert out["actor"] == "polly"

    def test_empty_key_returns_payload_copy(self):
        original = {"actor": "polly"}
        out = initial_dedup_payload(original, "")
        # No annotation when key is empty — caller's payload is
        # returned as a copy.
        assert "dedup_key" not in out
        assert "count" not in out
        # And it's a copy, not the same dict.
        assert out is not original


class TestFormatDedupSuffix:
    def test_count_one_returns_empty(self):
        assert format_dedup_suffix({"count": 1, "last_seen": "x"}) == ""

    def test_count_zero_returns_empty(self):
        assert format_dedup_suffix({"count": 0}) == ""

    def test_no_count_returns_empty(self):
        assert format_dedup_suffix({}) == ""

    def test_count_and_recent_seen(self):
        now = datetime.now(timezone.utc)
        last_seen = (now - timedelta(seconds=30)).isoformat()
        out = format_dedup_suffix(
            {"count": 9, "last_seen": last_seen}, now=now,
        )
        assert "9x" in out
        assert "just now" in out

    def test_count_and_days_ago(self):
        now = datetime.now(timezone.utc)
        last_seen = (now - timedelta(days=2)).isoformat()
        out = format_dedup_suffix(
            {"count": 12, "last_seen": last_seen}, now=now,
        )
        assert "12x" in out
        assert "2d ago" in out

    def test_count_without_last_seen(self):
        # Missing last_seen — render just the count, no age phrase.
        out = format_dedup_suffix({"count": 5})
        assert out == "5x"


class TestPmNotifyDedupCli:
    def test_repeated_calls_with_dedup_key_collapse(self, db_path):
        # First call writes, second + third collapse.
        for _ in range(3):
            result = runner.invoke(
                root_app,
                [
                    "notify", "Recovery mode injection",
                    "polly rejected another",
                    "--db", db_path,
                    "--dedup-key", "polly:recovery-mode-injection",
                    "--priority", "digest",
                ],
            )
            assert result.exit_code == 0, result.output

        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            rows = store.query_messages(type="notify", recipient="user")
            # Exactly one row exists, count=3.
            assert len(rows) == 1
            assert rows[0]["payload"]["count"] == 3
            assert (
                rows[0]["payload"]["dedup_key"]
                == "polly:recovery-mode-injection"
            )
        finally:
            store.close()

    def test_distinct_dedup_keys_do_not_collapse(self, db_path):
        runner.invoke(
            root_app,
            [
                "notify", "Pattern A", "first",
                "--db", db_path,
                "--dedup-key", "key-a",
                "--priority", "digest",
            ],
        )
        runner.invoke(
            root_app,
            [
                "notify", "Pattern B", "second",
                "--db", db_path,
                "--dedup-key", "key-b",
                "--priority", "digest",
            ],
        )

        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            rows = store.query_messages(type="notify", recipient="user")
            assert len(rows) == 2
        finally:
            store.close()

    def test_no_dedup_key_keeps_legacy_insert_behavior(self, db_path):
        # Two notifies without --dedup-key should NOT collapse — the
        # legacy callers don't expect implicit collapsing.
        for subject in ("first", "second"):
            runner.invoke(
                root_app,
                [
                    "notify", subject, "body",
                    "--db", db_path, "--priority", "digest",
                ],
            )

        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            rows = store.query_messages(type="notify", recipient="user")
            assert len(rows) == 2
        finally:
            store.close()


class TestPmInboxRendersDedup:
    def test_inbox_surfaces_count_in_text_listing(self, db_path):
        # Three repeated dedup-collapsed notifies → one row with 3x.
        for _ in range(3):
            runner.invoke(
                root_app,
                [
                    "notify", "Repeating alert", "ongoing",
                    "--db", db_path,
                    "--dedup-key", "k",
                    "--priority", "digest",
                ],
            )
        # digest notifies stay in state='staged' until flushed, so
        # they don't surface in the default inbox view. Promote to
        # state='open' directly so the listing shows the row.
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            rows = store.query_messages(type="notify", recipient="user")
            store.update_message(rows[0]["id"], state="open")
        finally:
            store.close()

        # #1027 — pure ``notify`` rows are hidden by default; this dedup
        # test seeds a notify row, so opt back in with ``--all`` to keep
        # exercising the rendering branch.
        result = runner.invoke(inbox_app, ["--db", db_path, "--all"])
        assert result.exit_code == 0, result.output
        assert "Repeating alert" in result.output
        assert "3x" in result.output

    def test_inbox_json_carries_dedup_count(self, db_path):
        for _ in range(2):
            runner.invoke(
                root_app,
                [
                    "notify", "alert", "body",
                    "--db", db_path,
                    "--dedup-key", "k",
                    "--priority", "digest",
                ],
            )
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            rows = store.query_messages(type="notify", recipient="user")
            store.update_message(rows[0]["id"], state="open")
        finally:
            store.close()

        result = runner.invoke(inbox_app, ["--db", db_path, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert len(payload["messages"]) == 1
        assert payload["messages"][0]["dedup_count"] == 2
        assert "2x" in payload["messages"][0]["dedup_suffix"]
