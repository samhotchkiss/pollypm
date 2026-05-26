"""Regression tests for #2194 — pm migrate gate loop and --check NameError.

Wave 1B finding: a returning user runs ``git pull && pm up`` and the
migration gate fires. ``pm migrate --apply`` reports success but the
gate still blocks the cockpit, and ``pm migrate --check`` crashes with
``NameError: GLOBAL_CONFIG_DIR``. The user's only escape is the
undiscoverable ``POLLYPM_SKIP_MIGRATION_GATE=1`` env var.

Root causes:

1. ``_default_clone_path`` referenced ``GLOBAL_CONFIG_DIR`` without
   importing it, so ``check_against_clone`` raised ``NameError`` the
   moment it needed the default clone path.

2. ``_apply_all`` delegated work-domain migrations to
   ``create_work_service(db_path=...)``, but post-sqlite-ripout (#1971)
   that factory returns a PG-backed service that ignores ``db_path``.
   The sqlite ``work_schema_version`` table never advanced, so
   ``inspect()`` kept reporting v11 pending and the refuse-start gate
   kept firing.

These tests pin the user-facing contracts:

* ``check_against_clone`` against a stale-work-schema DB succeeds
  without raising ``NameError``.
* ``apply`` followed by ``inspect`` reports the DB up-to-date — the
  same probe ``require_no_pending_or_exit`` uses, so the cockpit
  startup gate no longer fires.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pollypm.store import migrations as _migrations


def _seed_state_db_at_work_v10(db_path: Path) -> None:
    """Produce a full state.db whose ``work_schema_version`` stops at v10.

    Steps:

    1. Open a full ``StateStore`` + ``create_work_tables`` against
       ``db_path`` so every state-domain and work-domain table exists.
    2. Roll the ``work_schema_version`` row set back to v10 and drop
       the v11 column from ``work_tasks``. SQLite supports DROP COLUMN
       since 3.35 (May 2021), which is well below our floor.

    This recreates the exact on-disk state an operator hits after
    ``git pull`` on the #2145 commit: every prior migration applied,
    only v11 outstanding.
    """
    # Late imports — they pull pollypm.config et al., which want a real
    # workspace; we only want the sqlite DDL bits here.
    from pollypm.storage.state import StateStore
    from pollypm.work.schema import create_work_tables

    with StateStore(db_path) as _store:
        pass

    conn = sqlite3.connect(str(db_path))
    try:
        create_work_tables(conn)
        # Trim work_schema_version back to v10 and drop the v11 column
        # so ``inspect()`` reports work v11 as pending.
        conn.execute("DELETE FROM work_schema_version WHERE version > 10")
        conn.execute("ALTER TABLE work_tasks DROP COLUMN claimed_by_session")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def stale_state_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    _seed_state_db_at_work_v10(db)
    return db


def test_check_against_clone_does_not_raise_name_error(
    stale_state_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pm migrate --check`` must not crash with ``NameError`` (#2194).

    The bug was in ``_default_clone_path``, which referenced
    ``GLOBAL_CONFIG_DIR`` without importing it. ``check_against_clone``
    invokes ``_default_clone_path`` whenever the caller does not supply
    an explicit clone path, so any real CLI invocation hit the
    ``NameError`` before SQLite was touched.

    Pin both the helper directly (catches the import regression
    surface-level) and the public ``check_against_clone`` call against
    a stale-schema DB (catches the integration path the CLI uses).
    """
    # Point POLLYPM_HOME at a writable tmp dir so the dry-run clone
    # doesn't pollute ~/.pollypm during testing.
    monkeypatch.setenv("POLLYPM_HOME", str(tmp_path))
    monkeypatch.setenv("POLLYPM_SKIP_MIGRATION_GATE", "1")

    # Direct surface: the bug raised here.
    clone_path = _migrations._default_clone_path()
    assert clone_path.parent == tmp_path
    assert clone_path.name == "migration-check.db"

    # Integration: the CLI hits this with clone_path=None.
    outcome = _migrations.check_against_clone(stale_state_db)
    assert outcome.ok, f"dry-run failed: {outcome.error!r}"
    assert any(
        item.namespace == "work" and item.version == 11
        for item in outcome.applied
    ), "expected work v11 in the dry-run applied set"


def test_apply_then_inspect_reports_up_to_date(
    stale_state_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pm migrate --apply`` must actually clear the gate (#2194).

    The bug: ``_apply_all`` called ``create_work_service(db_path=...)``
    but post-sqlite-ripout that factory returns a PG service that
    ignores ``db_path``. The sqlite ``work_schema_version`` table never
    advanced, so ``inspect()`` kept reporting v11 pending — which is
    exactly the probe ``require_no_pending_or_exit`` (the cockpit
    refuse-start gate) uses. Net effect: ``pm migrate --apply &&
    pm cockpit`` loops the user back to the gate forever.

    Pin the user-facing contract: after ``apply()`` returns success,
    ``inspect()`` must report ``up_to_date=True`` and the gate must
    silently succeed.
    """
    monkeypatch.setenv("POLLYPM_SKIP_MIGRATION_GATE", "1")

    status_before = _migrations.inspect(stale_state_db)
    assert not status_before.up_to_date
    pending_versions = {
        (p.namespace, p.version) for p in status_before.pending
    }
    assert ("work", 11) in pending_versions, (
        "test setup expected work v11 pending; got "
        f"{pending_versions!r}"
    )

    outcome = _migrations.apply(stale_state_db)
    assert not outcome.already_up_to_date
    applied_versions = {(p.namespace, p.version) for p in outcome.applied}
    assert ("work", 11) in applied_versions

    # The probe ``require_no_pending_or_exit`` runs — must not raise.
    status_after = _migrations.inspect(stale_state_db)
    assert status_after.up_to_date, (
        f"gate would still fire after apply: pending="
        f"{[(p.namespace, p.version) for p in status_after.pending]}"
    )
    _migrations.require_no_pending_or_exit(stale_state_db)

    # And the v11 column actually landed on the live DB.
    conn = sqlite3.connect(str(stale_state_db))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(work_tasks)")}
    finally:
        conn.close()
    assert "claimed_by_session" in cols, (
        "v11 migration claims success but claimed_by_session column "
        "was never added to work_tasks"
    )


def test_rail_daemon_startup_auto_applies_pending_work_migration(
    stale_state_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct rail-daemon restart after upgrade must not crash-loop on v11."""
    from pollypm.rail_daemon import _apply_pending_migrations_for_startup

    monkeypatch.delenv("POLLYPM_SKIP_MIGRATION_GATE", raising=False)
    applied = _apply_pending_migrations_for_startup(stale_state_db)

    assert applied >= 1
    status_after = _migrations.inspect(stale_state_db)
    assert status_after.up_to_date
    _migrations.require_no_pending_or_exit(stale_state_db)
