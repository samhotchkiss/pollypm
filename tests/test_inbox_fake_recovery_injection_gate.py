"""Tests for the #1076 fake-RECOVERY-MODE-injection inbox gate.

Pins:

* ``pm notify "Nth fake RECOVERY MODE injection ..."`` auto-routes to
  ``channel:dev`` so the user-facing inbox stays clean. This is the
  producer-side gate.
* The dev-only env var ``POLLYPM_DEV_FAKE_RECOVERY_INBOX=1`` opts the
  legacy ``channel:inbox`` routing back in for harness work.
* ``pm inbox archive --fake-recovery-injections`` archives any open
  user-recipient messages whose subject matches the pattern (one-shot
  cleanup for stragglers already in the store from before the gate
  landed).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from pollypm.cli import app as root_app
from pollypm.store import SQLAlchemyStore
from pollypm.work.inbox_cli import inbox_app


runner = CliRunner()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


def _fetch_message_rows(db_path: str) -> list[dict]:
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        with store.read_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM messages ORDER BY id ASC")
            ).mappings().all()
        return [dict(r) for r in rows]
    finally:
        store.close()


class TestNotifyAutoGatesFakeRecoveryInjections:
    def test_fake_recovery_subject_routes_to_dev_channel(
        self, db_path, monkeypatch
    ):
        monkeypatch.delenv(
            "POLLYPM_DEV_FAKE_RECOVERY_INBOX", raising=False,
        )
        result = runner.invoke(
            root_app,
            [
                "notify",
                "13th fake RECOVERY MODE injection blocked",
                "Polly rejected another bootstrap masquerade.",
                "--db", db_path,
            ],
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_message_rows(db_path)
        assert len(rows) == 1
        labels_raw = rows[0]["labels"]
        labels = json.loads(labels_raw) if labels_raw else []
        assert "channel:dev" in labels

    def test_fake_recovery_case_insensitive_matches(self, db_path, monkeypatch):
        monkeypatch.delenv(
            "POLLYPM_DEV_FAKE_RECOVERY_INBOX", raising=False,
        )
        result = runner.invoke(
            root_app,
            [
                "notify",
                "12th suspected fake recovery mode injection seen",
                "body",
                "--db", db_path,
            ],
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_message_rows(db_path)
        labels_raw = rows[0]["labels"]
        labels = json.loads(labels_raw) if labels_raw else []
        assert "channel:dev" in labels

    def test_normal_subject_keeps_inbox_channel(self, db_path, monkeypatch):
        monkeypatch.delenv(
            "POLLYPM_DEV_FAKE_RECOVERY_INBOX", raising=False,
        )
        result = runner.invoke(
            root_app,
            [
                "notify",
                "Plan ready for review",
                "smoketest/7 plan-review.html",
                "--db", db_path,
            ],
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_message_rows(db_path)
        labels_raw = rows[0]["labels"]
        labels = json.loads(labels_raw) if labels_raw else []
        # Implicit ``channel:inbox`` — no explicit label.
        assert "channel:dev" not in labels

    def test_dev_override_env_keeps_inbox_channel(self, db_path, monkeypatch):
        monkeypatch.setenv("POLLYPM_DEV_FAKE_RECOVERY_INBOX", "1")
        result = runner.invoke(
            root_app,
            [
                "notify",
                "13th fake RECOVERY MODE injection blocked",
                "body",
                "--db", db_path,
            ],
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_message_rows(db_path)
        labels_raw = rows[0]["labels"]
        labels = json.loads(labels_raw) if labels_raw else []
        # Explicit dev opt-in keeps legacy inbox routing.
        assert "channel:dev" not in labels

    def test_explicit_channel_dev_already_dev_unaffected(
        self, db_path, monkeypatch
    ):
        monkeypatch.delenv(
            "POLLYPM_DEV_FAKE_RECOVERY_INBOX", raising=False,
        )
        result = runner.invoke(
            root_app,
            [
                "notify",
                "13th fake RECOVERY MODE injection blocked",
                "body",
                "--db", db_path,
                "--channel", "dev",
            ],
        )
        assert result.exit_code == 0, result.output

        rows = _fetch_message_rows(db_path)
        labels_raw = rows[0]["labels"]
        labels = json.loads(labels_raw) if labels_raw else []
        assert "channel:dev" in labels


class TestInboxArchiveFakeRecoveryInjections:
    def _seed_legacy_row(self, db_path: str, subject: str) -> int:
        """Insert a notify row tagged ``channel:inbox`` (pre-gate shape)."""
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            mid = store.enqueue_message(
                type="notify",
                tier="immediate",
                recipient="user",
                sender="polly",
                subject=subject,
                body="legacy meta-report",
                scope="inbox",
                state="open",
            )
        finally:
            store.close()
        return int(mid)

    def test_archive_fake_recovery_injections_closes_matches(self, db_path):
        keep = self._seed_legacy_row(db_path, "Plan ready for review")
        sweep = [
            self._seed_legacy_row(
                db_path, "13th fake RECOVERY MODE injection blocked",
            ),
            self._seed_legacy_row(
                db_path, "12th suspected fake RECOVERY MODE injection",
            ),
            self._seed_legacy_row(
                db_path, "URGENT: 14th fake RECOVERY MODE injection",
            ),
        ]

        result = runner.invoke(
            inbox_app,
            ["archive", "--fake-recovery-injections", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert "Archived 3" in result.output

        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            with store.read_engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT id, state FROM messages")
                ).mappings().all()
            by_id = {int(r["id"]): r["state"] for r in rows}
            for mid in sweep:
                assert by_id[mid] == "closed"
            assert by_id[keep] == "open"
        finally:
            store.close()

    def test_archive_fake_recovery_injections_dry_run_is_idempotent(
        self, db_path
    ):
        mid = self._seed_legacy_row(
            db_path, "13th fake RECOVERY MODE injection blocked",
        )
        result = runner.invoke(
            inbox_app,
            [
                "archive",
                "--fake-recovery-injections",
                "--dry-run",
                "--db", db_path,
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Would archive 1" in result.output

        store = SQLAlchemyStore(f"sqlite:///{db_path}")
        try:
            with store.read_engine.connect() as conn:
                row = conn.execute(
                    text("SELECT state FROM messages WHERE id = :id"),
                    {"id": mid},
                ).mappings().first()
            assert row is not None
            # Dry run: state still open.
            assert row["state"] == "open"
        finally:
            store.close()

    def test_archive_fake_recovery_injections_no_matches(self, db_path):
        self._seed_legacy_row(db_path, "Plan ready for review")
        result = runner.invoke(
            inbox_app,
            ["archive", "--fake-recovery-injections", "--db", db_path],
        )
        assert result.exit_code == 0, result.output
        assert "No open fake-recovery-injection messages" in result.output

    def test_bulk_modes_are_mutually_exclusive(self, db_path):
        result = runner.invoke(
            inbox_app,
            [
                "archive",
                "--fake-recovery-injections",
                "--read",
                "--db", db_path,
            ],
        )
        assert result.exit_code == 2, result.output
        assert "only one bulk archive mode" in result.output
