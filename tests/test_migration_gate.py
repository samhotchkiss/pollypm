"""Tests for the schema migration gate (#717).

Covers ``pm migrate --check``, ``pm migrate --apply``, refuse-start
behaviour, and the synthetic-failure safety net.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from pollypm.store import migrations as mig_mod
from pollypm.storage.state import StateStore


runner = CliRunner()


@pytest.fixture(autouse=True)
def _clear_gate_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the refuse-start gate to be active during these tests.

    ``tests/conftest.py`` sets ``POLLYPM_SKIP_MIGRATION_GATE`` so unrelated
    CLI plumbing tests don't collide with the developer's real state.db.
    The migration-gate suite exercises the gate itself, so we clear it.
    """
    monkeypatch.delenv("POLLYPM_SKIP_MIGRATION_GATE", raising=False)


def _fresh_state_db(path: Path) -> Path:
    """Bring ``path`` fully current by running both migration sets once.

    ``StateStore.__init__`` replays state migrations; the work-service
    migrations only run when ``SQLiteWorkService`` is instantiated.
    A "fresh" DB for our tests means both have been applied.
    """
    from pollypm.work.sqlite_service import SQLiteWorkService

    with StateStore(path):
        pass
    with SQLiteWorkService(path):
        pass
    return path


def _build_cli_app():
    """Build a fresh Typer app with just the migrate command attached.

    We avoid importing the root ``pollypm.cli`` module here because it
    eagerly registers every CLI surface in the project — far more than
    we need, and some carry heavy side-effectful imports.
    """
    import typer

    from pollypm.cli_features.migrate import register_migrate_commands

    app = typer.Typer()
    register_migrate_commands(app)
    return app


def _write_minimal_config(tmp_path: Path) -> Path:
    """Write a PollyPM config that points state_db at ``tmp_path/state.db``."""
    config_path = tmp_path / "pollypm.toml"
    state_db = tmp_path / "state.db"
    config_path.write_text(
        "[project]\n"
        f'base_dir = "{tmp_path}"\n'
        f'state_db = "{state_db}"\n'
        f'workspace_root = "{tmp_path}"\n'
    )
    return config_path


# ---------------------------------------------------------------------------
# --check / --apply
# ---------------------------------------------------------------------------


def test_pm_migrate_check_on_clean_db(tmp_path: Path) -> None:
    db_path = _fresh_state_db(tmp_path / "state.db")
    status = mig_mod.inspect(db_path)
    assert status.up_to_date
    outcome = mig_mod.check_against_clone(db_path)
    assert outcome.ok
    assert outcome.applied == []


def test_pm_migrate_apply_idempotent(tmp_path: Path) -> None:
    db_path = _fresh_state_db(tmp_path / "state.db")
    first = mig_mod.apply(db_path)
    assert first.already_up_to_date

    # Second call is still a no-op.
    second = mig_mod.apply(db_path)
    assert second.already_up_to_date

    # And the unified audit table now mirrors every migration.
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT namespace, version FROM schema_migrations"
        ).fetchall()
    seen = {(ns, ver) for ns, ver in rows}
    assert (mig_mod.NAMESPACE_STATE, 1) in seen
    assert (mig_mod.NAMESPACE_WORK, 1) in seen


def test_pm_migrate_check_detects_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _fresh_state_db(tmp_path / "state.db")

    # Synthetic pending migration — registered only for the duration of
    # this test. The runner replays whatever is declared on the class,
    # so a single extra row is enough to drive the "pending" branch
    # without mutating the production list on disk.
    synthetic = list(StateStore._MIGRATIONS) + [
        (
            9001,
            "Synthetic test-only migration — adds marker_table",
            ["CREATE TABLE IF NOT EXISTS migration_gate_marker (id INTEGER)"],
        ),
    ]
    monkeypatch.setattr(StateStore, "_MIGRATIONS", synthetic)

    status = mig_mod.inspect(db_path)
    assert not status.up_to_date
    pending = [p for p in status.pending if p.version == 9001]
    assert len(pending) == 1
    assert pending[0].namespace == mig_mod.NAMESPACE_STATE
    assert "marker_table" in pending[0].description

    outcome = mig_mod.check_against_clone(db_path)
    assert outcome.ok
    assert any(change == "+migration_gate_marker" for change in outcome.tables_changed)

    # Live DB must still be on the pre-synthetic schema.
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='migration_gate_marker'"
        ).fetchone()
    assert row is None


def test_synthetic_failing_migration_caught_by_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _fresh_state_db(tmp_path / "state.db")

    # Synthetic migration that intentionally fails — drops a table that
    # doesn't exist without IF EXISTS. The real DB must stay untouched.
    synthetic = list(StateStore._MIGRATIONS) + [
        (
            9002,
            "Synthetic failing migration — drops a non-existent table",
            ["DROP TABLE no_such_table_gate_test"],
        ),
    ]
    monkeypatch.setattr(StateStore, "_MIGRATIONS", synthetic)

    outcome = mig_mod.check_against_clone(db_path)
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.clone_path is None  # clone was cleaned up on failure

    # The real DB still has the pre-synthetic schema — no stray tables
    # or dropped rows from the failed check.
    with sqlite3.connect(str(db_path)) as conn:
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
    assert version is not None
    # MAX declared in the real list (not our synthetic fail).
    expected = max(v for v, _, _ in StateStore._MIGRATIONS if v < 9000)
    assert version == expected


# ---------------------------------------------------------------------------
# Refuse-start gate
# ---------------------------------------------------------------------------


def test_cockpit_refuses_start_on_pending_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _fresh_state_db(tmp_path / "state.db")

    # Declare a pending migration *after* the DB was brought current,
    # so the inspector sees it as unapplied.
    synthetic = list(StateStore._MIGRATIONS) + [
        (9003, "pending — refuse-start gate test", []),
    ]
    monkeypatch.setattr(StateStore, "_MIGRATIONS", synthetic)
    # Make sure the bypass env is not leaking in from the CLI entry.
    monkeypatch.delenv(mig_mod._BYPASS_ENV, raising=False)

    with pytest.raises(SystemExit) as exc:
        mig_mod.require_no_pending_or_exit(db_path)
    assert exc.value.code != 0


def test_migration_gate_skipped_when_bypass_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bypass env var lets ``pm migrate`` itself open the store."""
    db_path = _fresh_state_db(tmp_path / "state.db")
    synthetic = list(StateStore._MIGRATIONS) + [
        (9004, "pending — bypass test", []),
    ]
    monkeypatch.setattr(StateStore, "_MIGRATIONS", synthetic)
    monkeypatch.setenv(mig_mod._BYPASS_ENV, "1")

    # No exception — the gate short-circuits out.
    mig_mod.require_no_pending_or_exit(db_path)


def test_up_enforces_migration_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pm up`` bails with a non-zero exit when migrations are pending."""
    from pollypm import cli as _cli

    # Make the bypass check return False and the gate itself raise so we
    # can assert the entrypoint propagates the exit without needing a
    # full config surface.
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("# dummy\n")

    def _boom(_db_path):
        raise SystemExit(2)

    monkeypatch.setattr(_cli, "_discover_config_path", lambda p: config_path)
    monkeypatch.setattr(
        "pollypm.store.migrations.bypass_env_is_set", lambda: False
    )
    monkeypatch.setattr(
        "pollypm.store.migrations.require_no_pending_or_exit", _boom
    )
    # load_config must return an object with .project.state_db for the
    # CLI helper to extract the path before calling the gate.
    fake_cfg = mock.Mock()
    fake_cfg.project.state_db = tmp_path / "state.db"
    monkeypatch.setattr("pollypm.config.load_config", lambda _p: fake_cfg)

    with pytest.raises(SystemExit) as exc:
        _cli.up(config_path=config_path)
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_migrate_check_outputs_up_to_date(tmp_path: Path) -> None:
    db_path = _fresh_state_db(tmp_path / "state.db")
    config_path = _write_minimal_config(tmp_path)
    # Point state_db to the DB we just fresh-built.
    config_path.write_text(
        "[project]\n"
        f'base_dir = "{tmp_path}"\n'
        f'state_db = "{db_path}"\n'
        f'workspace_root = "{tmp_path}"\n'
    )
    app = _build_cli_app()
    result = runner.invoke(app, ["--check", "--config", str(config_path)])
    assert result.exit_code == 0
    assert "up to date" in result.stdout.lower()


def test_cli_migrate_check_renders_failure_in_structured_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#760 adopter: when the dry-run fails, the CLI renders the
    error in the four-field StructuredUserMessage shape — summary,
    why, next, details — not a bare free-form line."""
    db_path = _fresh_state_db(tmp_path / "state.db")
    config_path = _write_minimal_config(tmp_path)
    config_path.write_text(
        "[project]\n"
        f'base_dir = "{tmp_path}"\n'
        f'state_db = "{db_path}"\n'
        f'workspace_root = "{tmp_path}"\n'
    )

    # Force the clone check to report failure.
    from pollypm.store import migrations as _mig
    class _FakeOutcome:
        ok = False
        error = "simulated replay error for test"
    monkeypatch.setattr(
        _mig, "check_against_clone",
        lambda _db_path: _FakeOutcome(),
    )
    # Ensure inspect() reports pending so _run_check reaches the
    # check_against_clone path.
    class _FakeStatus:
        up_to_date = False
        pending = [_mig.PendingMigration("state", 99, "synthetic pending")]
    monkeypatch.setattr(_mig, "inspect", lambda _db_path: _FakeStatus())
    monkeypatch.setattr(
        _mig, "format_pending_summary",
        lambda status: "1 pending migration(s):\n  [state] v99: synthetic pending",
    )

    app = _build_cli_app()
    result = runner.invoke(app, ["--check", "--config", str(config_path)])
    assert result.exit_code == 3, result.output
    combined = result.stdout + "\n" + (result.stderr or "")
    # Structured-shape markers:
    assert "✗ Migration check FAILED" in combined
    assert "Next:" in combined
    assert "simulated replay error" in combined


def test_cli_migrate_apply_reports_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No fresh DB this time — let --apply bootstrap it so the CLI prints
    # a non-empty "applied" list.
    config_path = _write_minimal_config(tmp_path)
    app = _build_cli_app()
    result = runner.invoke(app, ["--apply", "--config", str(config_path)])
    assert result.exit_code == 0
    combined = result.stdout + "\n" + (result.stderr or "")
    # Either "Applied N migration(s)..." on the bootstrap path or the
    # "All migrations up to date." fallback if some future refactor
    # makes the DB self-bootstrap earlier. Both are acceptable; failure
    # would be a non-zero exit.
    assert "Applied" in combined or "up to date" in combined
