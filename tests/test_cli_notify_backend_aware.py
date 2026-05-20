"""Backend-aware CLI coverage for ``pm notify`` (re-adds #1790 surface).

History
-------

Slice K-tests part 5 (#1737, commit 04285152) deleted
``tests/test_cli_notify.py`` because the test bodies seeded /
inspected state through ``SQLAlchemyStore(f"sqlite:///{db}")`` and the
CLI's notify write site did the same — every step was sqlite-tied.

This module re-adds the load-bearing assertions through the
post-#1790 backend-aware seam: the test seeds + inspects messages via
:func:`pollypm.store.get_store_by_url` (the canonical sqlite singleton
helper) and the CLI's write goes through
:func:`pollypm.cli_features.session_runtime._resolve_notify_store`,
which on a ``--db <tmp>`` non-default invocation routes to the same
``get_store_by_url`` cache. Writer + reader therefore share the
process-wide singleton — without that, every test that re-opened the
store mid-flight would race the in-memory sqlite WAL.

Scope
-----

* Tier classification — immediate / digest / silent each write the
  expected ``messages`` row shape.
* Actor flag — recorded as ``sender`` + payload ``actor``.
* Stdin body — ``--body -`` reads from stdin.
* Validation — empty subject exits non-zero.
* Immediate-priority fan-out — creates the work-service inbox task
  and cross-links the message payload to it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.cli import app as root_app


runner = CliRunner()


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Per-test sqlite ``state.db`` path the CLI's ``--db`` flag pins to.

    Non-default path forces the new backend-aware helpers down their
    sqlite branch, mirroring :func:`pollypm.work.cli._svc` dispatch.
    """
    path = tmp_path / ".pollypm" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


@pytest.fixture(autouse=True)
def _reset_store_cache():
    """Drop the ``(backend, url)`` store cache between tests."""
    yield
    try:
        from pollypm.store.registry import reset_store_cache

        reset_store_cache()
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass


def _invoke_notify(
    db_path: str,
    *args: str,
    input_text: str | None = None,
):
    return runner.invoke(
        root_app,
        ["notify", *args, "--db", db_path],
        input=input_text,
    )


def _fetch_messages(db_path: str) -> list[dict]:
    """Return every message row, ordered by id ascending.

    Uses :func:`get_store_by_url` so the read hits the same cached
    sqlite singleton the CLI wrote into via
    :func:`_resolve_notify_store`.
    """
    from pollypm.store import get_store_by_url

    store = get_store_by_url(f"sqlite:///{db_path}")
    rows = store.query_messages(recipient="user")
    rows.extend(store.query_messages(recipient="polly"))
    rows.sort(key=lambda r: r.get("id", 0))
    return rows


class TestNotifyTierClassification:
    def test_immediate_message_lands_in_messages_table(
        self, db_path: str,
    ) -> None:
        result = _invoke_notify(
            db_path,
            "Deploy blocked",
            "Needs verification email click.",
        )
        assert result.exit_code == 0, result.output
        # The last output line is the assigned task id (immediate
        # priority spawns a chat-flow inbox task — `inbox/N`).
        out_line = result.output.strip().splitlines()[-1]
        assert out_line.startswith("inbox/")

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["type"] == "notify"
        assert row["tier"] == "immediate"
        # Title contract auto-stamps [Action] for immediate notify.
        assert (row.get("subject") or "").startswith("[Action]")
        assert "Deploy blocked" in (row.get("subject") or "")
        assert row.get("body") == "Needs verification email click."
        assert row.get("state") == "closed"
        assert row.get("recipient") == "user"

    def test_digest_tier_lands_staged(self, db_path: str) -> None:
        # ``done`` + ``merged`` → classifier picks digest.
        result = _invoke_notify(
            db_path,
            "Task done",
            "PR merged cleanly.",
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip().startswith("digest:")

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        assert rows[0].get("tier") == "digest"
        assert rows[0].get("state") == "staged"
        assert (rows[0].get("subject") or "").startswith("[FYI]")

    def test_silent_tier_lands_closed(self, db_path: str) -> None:
        result = _invoke_notify(
            db_path,
            "Audit trace",
            "Recorded for the log.",
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "silent"

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        assert rows[0].get("tier") == "silent"
        assert rows[0].get("state") == "closed"
        assert (rows[0].get("subject") or "").startswith("[Audit]")


class TestNotifyMetadata:
    def test_actor_flag_is_recorded_on_message(self, db_path: str) -> None:
        result = _invoke_notify(
            db_path,
            "Heads up",
            "Something happened.",
            "--actor", "morning-briefing",
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        assert rows[0].get("sender") == "morning-briefing"
        payload_raw = rows[0].get("payload") or rows[0].get("payload_json") or {}
        payload = (
            payload_raw
            if isinstance(payload_raw, dict)
            else json.loads(payload_raw)
        )
        assert payload.get("actor") == "morning-briefing"

    def test_body_from_stdin_via_dash(self, db_path: str) -> None:
        result = _invoke_notify(
            db_path,
            "Long body",
            "-",
            input_text="line 1\nline 2\nline 3\n",
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        body = rows[0].get("body") or ""
        assert "line 1" in body
        assert "line 3" in body


class TestNotifyValidation:
    def test_empty_subject_exits_nonzero(self, db_path: str) -> None:
        result = _invoke_notify(db_path, "", "non-empty body")
        assert result.exit_code != 0, result.output


class TestNotifyImmediatePriorityFanout:
    def test_immediate_message_creates_inbox_task_cross_linked_to_message(
        self, db_path: str,
    ) -> None:
        """Immediate-priority notify creates a chat-flow inbox task and
        threads its ``task_id`` back into the originating message's
        payload so the dashboard can cross-link them.

        Locks in the contract that #1790 was filed to preserve: the
        backend-aware Store singleton that
        :func:`_resolve_notify_store` returns is shared with the
        :func:`_create_notify_inbox_task` helper, so the
        ``update_message`` write lands on the same row the original
        enqueue produced (not the empty sqlite shadow under pg).
        """
        result = _invoke_notify(
            db_path,
            "Deploy blocked",
            "Needs verification email click.",
        )
        assert result.exit_code == 0, result.output
        inbox_task_id = result.output.strip().splitlines()[-1]
        assert inbox_task_id.startswith("inbox/")

        rows = _fetch_messages(db_path)
        assert len(rows) == 1
        row = rows[0]
        payload_raw = row.get("payload") or row.get("payload_json") or {}
        payload = (
            payload_raw
            if isinstance(payload_raw, dict)
            else json.loads(payload_raw)
        )
        # The fan-out wrote the task_id back into the originating
        # message payload (not a second row — same id, mutated payload).
        assert payload.get("task_id") == inbox_task_id


# --------------------------------------------------------------------- #
# Backend-aware helper smoke test — locks in the dispatch contract that
# the post-#1790 tripwire (test_pg_cli_coverage_tripwire.py) grep-guards.
# --------------------------------------------------------------------- #


def test_resolve_notify_store_routes_sqlite_for_non_default_db(
    db_path: str,
) -> None:
    """``--db`` non-default → :func:`get_store_by_url` (sqlite-pinned).

    Mirrors :func:`pollypm.work.inbox_cli._resolve_messages_store` so
    ``pm notify`` and ``pm inbox`` agree on which singleton serves a
    given ``--db`` value. Without that agreement the inbox reader
    would query an empty shadow while ``pm notify`` writes lived on
    a different file / pool.
    """
    from pollypm.cli_features.session_runtime import _resolve_notify_store
    from pollypm.store import get_store_by_url

    store = _resolve_notify_store(db_path)
    same = get_store_by_url(f"sqlite:///{db_path}")
    assert store is same


def test_resolve_notify_store_uses_get_store_for_default_db(
    monkeypatch, tmp_path: Path,
) -> None:
    """``--db`` default → :func:`get_store(load_config())`.

    Locks in that the helper threads through ``load_config()`` so a
    pg-configured operator's ``pm notify`` write lands on the pg pool
    (not the empty sqlite shadow — the #1755 / #1811 failure mode).
    """
    from pollypm.cli_features.session_runtime import _resolve_notify_store
    from pollypm.work.db_resolver import WORKSPACE_DEFAULT_DB_PATH

    sentinel_store = object()
    sentinel_config = object()
    seen: dict[str, object] = {}

    def _fake_load_config():
        seen["load_config"] = True
        return sentinel_config

    def _fake_get_store(config):
        seen["get_store_config"] = config
        return sentinel_store

    monkeypatch.setattr(
        "pollypm.config.load_config", _fake_load_config,
    )
    monkeypatch.setattr(
        "pollypm.store.get_store", _fake_get_store,
    )

    result = _resolve_notify_store(WORKSPACE_DEFAULT_DB_PATH)
    assert seen.get("load_config") is True
    assert seen.get("get_store_config") is sentinel_config
    assert result is sentinel_store
