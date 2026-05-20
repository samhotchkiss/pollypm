"""Backend-aware CLI coverage for ``pm inbox`` (re-adds #1790 surface).

History
-------

Slice K-tests part 5 (#1737, commit 04285152) deleted 15 CLI test
modules because the CLI hard-wired
``SQLAlchemyStore(f"sqlite:///{db_path}")`` — the test bodies seeded
state with the same construction so the writer / reader / store
singleton agreed. Re-routing the CLI to ``get_store(load_config())``
(#1790) closes the gap; this module re-adds a representative subset
of the deleted coverage against the new backend-aware seam.

Scope
-----

* ``pm inbox`` (listing) — empty, header singular/plural, JSON shape,
  channel filter (#754).
* ``pm inbox show msg:N`` — render single message, JSON roundtrip,
  missing / invalid id error contract.
* ``pm inbox archive`` — single ``msg:N``, ``--match`` glob, ``--dry-run``,
  empty-result no-op, ``--read`` bulk path (pinned exempt).

Backend
-------

The tests drive the CLI through the canonical ``--db <tmp>`` path so
the new :func:`pollypm.work.inbox_cli._resolve_messages_store` helper
takes the sqlite branch (matching the deleted suites' setup). Seeding
goes through :func:`pollypm.store.get_store_by_url` against the same
``state.db`` so the test writer + the CLI reader hit the same singleton
— that's the contract :func:`_resolve_messages_store` upholds when
``--db`` is non-default.

A separate ``test_pg_inbox_cli.py`` would exercise the pg branch via
:func:`get_store(load_config())`; the helper's design ensures the
sqlite tests below are sufficient to lock in the dispatch logic
(``--db`` non-default → sqlite, ``--db`` default → backend dispatch).
The post-port tripwire (``test_pg_cli_coverage_tripwire.py``) catches
any future regression that re-introduces a hard-coded sqlite URL.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.work.inbox_cli import inbox_app


runner = CliRunner()


# --------------------------------------------------------------------- #
# Helpers — seed messages through the same backend-aware helper the CLI
# uses, so the writer and reader share the cached Store singleton.
# --------------------------------------------------------------------- #


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Per-test sqlite ``state.db`` path the CLI's ``--db`` flag pins to.

    Using a non-default path forces
    :func:`pollypm.work.inbox_cli._resolve_messages_store` down its
    ``get_store_by_url`` branch, isolating the test from the operator's
    real workspace.
    """
    path = tmp_path / ".pollypm" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


@pytest.fixture(autouse=True)
def _reset_store_cache():
    """Reset the per-URL store singleton between tests.

    The registry caches by ``(backend, url)`` — different tests use
    different ``tmp_path`` ``state.db`` URLs so cache-bleed isn't a
    correctness risk, but ``dispose()``-ing the pool between tests
    keeps the open-file-handle count flat under the suite.
    """
    yield
    try:
        from pollypm.store.registry import reset_store_cache

        reset_store_cache()
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass


def _seed_message(db_path: str, **overrides) -> int:
    """Write a single notify message and return its id.

    Routes through :func:`get_store_by_url` so the seeded row lives in
    the same singleton the CLI's :func:`_resolve_messages_store` will
    return for the same ``--db <db_path>`` invocation.
    """
    from pollypm.store import get_store_by_url

    store = get_store_by_url(f"sqlite:///{db_path}")
    return store.enqueue_message(
        type=overrides.get("type", "notify"),
        tier=overrides.get("tier", "immediate"),
        recipient=overrides.get("recipient", "user"),
        sender=overrides.get("sender", "polly"),
        subject=overrides.get("subject", "A notify from Polly"),
        body=overrides.get("body", "Plan ready for review."),
        scope=overrides.get("scope", "demo"),
        labels=overrides.get("labels"),
        payload=overrides.get("payload"),
        state=overrides.get("state", "open"),
        kind=overrides.get("kind", "legacy"),
    )


# --------------------------------------------------------------------- #
# pm inbox (listing)
# --------------------------------------------------------------------- #


class TestInboxList:
    def test_empty_inbox_lists_zero_items(self, db_path: str) -> None:
        result = runner.invoke(inbox_app, ["--db", db_path])
        assert result.exit_code == 0, result.output
        assert "Inbox: 0 items" in result.output
        assert "No messages waiting for you." in result.output

    def test_json_output_when_empty_has_messages_and_tasks_keys(
        self, db_path: str,
    ) -> None:
        result = runner.invoke(inbox_app, ["--db", db_path, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # #341 — the JSON shape includes a ``messages`` key alongside
        # ``tasks`` so the union surface is discoverable.
        assert payload == {
            "assigned_count": 0,
            "messages": [],
            "tasks": [],
        }

    def test_seeded_notify_appears_in_json(self, db_path: str) -> None:
        msg_id = _seed_message(db_path, subject="real action required")
        result = runner.invoke(inbox_app, ["--db", db_path, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        ids = [m["id"] for m in payload["messages"]]
        assert f"msg:{msg_id}" in ids


# --------------------------------------------------------------------- #
# pm inbox --channel (channel separation, #754)
# --------------------------------------------------------------------- #


class TestInboxChannelFilter:
    def test_default_channel_hides_dev_traffic(self, db_path: str) -> None:
        _seed_message(db_path, subject="real action required")
        _seed_message(
            db_path, subject="test-noise-1", labels=["channel:dev"],
        )

        result = runner.invoke(inbox_app, ["--db", db_path, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        subjects = " ".join(m["title"] for m in payload["messages"])
        assert "real action required" in subjects
        assert "test-noise-1" not in subjects

    def test_channel_dev_shows_only_dev_traffic(self, db_path: str) -> None:
        _seed_message(db_path, subject="real action required")
        _seed_message(
            db_path, subject="test-noise-1", labels=["channel:dev"],
        )

        result = runner.invoke(
            inbox_app, ["--db", db_path, "--json", "--channel", "dev"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        subjects = " ".join(m["title"] for m in payload["messages"])
        assert "real action required" not in subjects
        assert "test-noise-1" in subjects

    def test_channel_all_shows_everything(self, db_path: str) -> None:
        _seed_message(db_path, subject="real action required")
        _seed_message(
            db_path, subject="test-noise-1", labels=["channel:dev"],
        )

        result = runner.invoke(
            inbox_app, ["--db", db_path, "--json", "--channel", "all"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        subjects = " ".join(m["title"] for m in payload["messages"])
        assert "real action required" in subjects
        assert "test-noise-1" in subjects

    def test_channel_rejects_unknown_value(self, db_path: str) -> None:
        result = runner.invoke(
            inbox_app, ["--db", db_path, "--channel", "bogus"],
        )
        assert result.exit_code == 1, result.output
        assert "--channel" in result.output


# --------------------------------------------------------------------- #
# pm inbox show msg:N (#760)
# --------------------------------------------------------------------- #


class TestInboxShowMessage:
    def test_show_accepts_msg_id_form(self, db_path: str) -> None:
        msg_id = _seed_message(db_path, subject="Plan ready for review")

        result = runner.invoke(
            inbox_app, ["show", f"msg:{msg_id}", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert f"msg:{msg_id}" in result.output
        assert "Plan ready for review" in result.output
        assert "sender:" in result.output
        assert "body:" in result.output

    def test_show_msg_json_roundtrip(self, db_path: str) -> None:
        msg_id = _seed_message(db_path, subject="JSON subject")

        result = runner.invoke(
            inbox_app,
            ["show", f"msg:{msg_id}", "--db", db_path, "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["id"] == msg_id
        # ``enqueue_message`` applies the title contract — the raw
        # subject is preserved verbatim somewhere in the field.
        assert "JSON subject" in payload["subject"]

    def test_show_msg_missing_id_exits_nonzero(self, db_path: str) -> None:
        _seed_message(db_path)  # ensure store isn't empty

        result = runner.invoke(
            inbox_app, ["show", "msg:9999999", "--db", db_path],
        )
        assert result.exit_code != 0
        assert "no message" in (result.output + (result.stderr or "")).lower()

    def test_show_msg_invalid_form_exits_nonzero(self, db_path: str) -> None:
        _seed_message(db_path)

        result = runner.invoke(
            inbox_app, ["show", "msg:not-a-number", "--db", db_path],
        )
        assert result.exit_code != 0
        assert (
            "invalid message id"
            in (result.output + (result.stderr or "")).lower()
        )


# --------------------------------------------------------------------- #
# pm inbox archive msg:N + bulk modes
# --------------------------------------------------------------------- #


class TestInboxArchive:
    def test_archive_single_msg_id(self, db_path: str) -> None:
        msg_id = _seed_message(db_path, subject="archive me directly")

        result = runner.invoke(
            inbox_app, ["archive", f"msg:{msg_id}", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert f"msg:{msg_id} → archived" in result.output

        from pollypm.store import get_store_by_url

        store = get_store_by_url(f"sqlite:///{db_path}")
        open_rows = store.query_messages(recipient="user", state="open")
        assert all(row.get("id") != msg_id for row in open_rows)

    def test_archive_match_bulk_archives_by_glob(self, db_path: str) -> None:
        _seed_message(db_path, subject="loop-test-111")
        _seed_message(db_path, subject="loop-test-222")
        _seed_message(db_path, subject="real-action-please-review")

        result = runner.invoke(
            inbox_app,
            ["archive", "--match", "*loop-test-*", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert "Archived 2 messages" in result.output

        from pollypm.store import get_store_by_url

        store = get_store_by_url(f"sqlite:///{db_path}")
        rows = store.query_messages(recipient="user", state="open")
        remaining = [r.get("subject") or "" for r in rows]
        assert any("real-action" in s for s in remaining)
        assert not any("loop-test" in s for s in remaining)

    def test_archive_match_dry_run_does_not_change_state(
        self, db_path: str,
    ) -> None:
        _seed_message(db_path, subject="loop-test-aaa")
        _seed_message(db_path, subject="loop-test-bbb")

        result = runner.invoke(
            inbox_app,
            [
                "archive", "--match", "*loop-test-*",
                "--dry-run", "--db", db_path,
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Would archive 2 messages" in result.output

        from pollypm.store import get_store_by_url

        store = get_store_by_url(f"sqlite:///{db_path}")
        rows = store.query_messages(recipient="user", state="open")
        assert len(rows) == 2

    def test_archive_match_empty_result_is_a_clean_no_op(
        self, db_path: str,
    ) -> None:
        _seed_message(db_path, subject="unrelated")

        result = runner.invoke(
            inbox_app,
            ["archive", "--match", "*nothing-matches*", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert "No open messages matched" in result.output

    def test_archive_without_arg_and_without_match_errors(
        self, db_path: str,
    ) -> None:
        result = runner.invoke(inbox_app, ["archive", "--db", db_path])
        assert result.exit_code == 2, result.output
        assert "--match" in result.output

    def test_archive_read_bulk_archives_open_notifies(
        self, db_path: str,
    ) -> None:
        _seed_message(db_path, subject="notify-a")
        _seed_message(db_path, subject="notify-b")
        # Pinned notifies are exempt (#1013).
        _seed_message(
            db_path, subject="pinned-notify", labels=["pinned"],
        )

        result = runner.invoke(
            inbox_app, ["archive", "--read", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert "Archived 2 open" in result.output

        from pollypm.store import get_store_by_url

        store = get_store_by_url(f"sqlite:///{db_path}")
        rows = store.query_messages(recipient="user", state="open")
        remaining = [r.get("subject") or "" for r in rows]
        assert any("pinned" in s for s in remaining)
        assert not any("notify-a" in s for s in remaining)
        assert not any("notify-b" in s for s in remaining)

    def test_archive_multiple_bulk_modes_rejected(
        self, db_path: str,
    ) -> None:
        result = runner.invoke(
            inbox_app,
            [
                "archive",
                "--match", "*x*",
                "--read",
                "--db", db_path,
            ],
        )
        assert result.exit_code == 2, result.output
        assert "only one bulk archive mode" in (
            result.output + (result.stderr or "")
        )


# --------------------------------------------------------------------- #
# Backend-aware helper smoke test — locks in the dispatch contract that
# the tripwire (test_pg_cli_coverage_tripwire.py) grep-guards.
# --------------------------------------------------------------------- #


def test_resolve_messages_store_routes_sqlite_via_url_for_non_default_db(
    db_path: str,
) -> None:
    """``--db`` non-default → :func:`get_store_by_url` (sqlite-pinned).

    Mirrors the ``_svc`` dispatch in :mod:`pollypm.work.cli`. Without
    this branch, an operator whose ``pollypm.toml`` selects postgres
    would have their ``--db /tmp/state.db`` request silently routed to
    pg, breaking the test / CI escape hatch.
    """
    from pollypm.store import get_store_by_url
    from pollypm.work.inbox_cli import _resolve_messages_store

    store = _resolve_messages_store(db_path)
    # Same URL → same cached singleton.
    same = get_store_by_url(f"sqlite:///{db_path}")
    assert store is same


def test_resolve_messages_store_uses_get_store_for_default_db(
    monkeypatch, tmp_path: Path,
) -> None:
    """``--db`` default → :func:`get_store(load_config())`.

    Pin the backend to sqlite via a hand-rolled config so the assertion
    doesn't need a pg container. The point of this test is to lock in
    that the default path threads through ``load_config()`` (the seam
    the pg branch needs), NOT which backend it ultimately picks.
    """
    from pollypm.work.db_resolver import WORKSPACE_DEFAULT_DB_PATH
    from pollypm.work.inbox_cli import _resolve_messages_store

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

    result = _resolve_messages_store(WORKSPACE_DEFAULT_DB_PATH)
    assert seen.get("load_config") is True
    assert seen.get("get_store_config") is sentinel_config
    assert result is sentinel_store
