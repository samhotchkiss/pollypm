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
    monkeypatch.delenv("POLLYPM_HOLD_UNUSABLE_DATABASE_SCREEN", raising=False)


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


def _write_corrupt_db(path: Path) -> Path:
    path.write_text("garbage", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# --check / --apply
# ---------------------------------------------------------------------------


def test_format_pending_summary_pluralisation(tmp_path: Path) -> None:
    """Cycle 80: ``format_pending_summary`` pluralises per count.

    The summary header read ``N pending migration(s):`` — at one
    pending migration (the typical case after a single new migration
    lands in a release) the parenthetical reads as a copy bug.
    """
    one = mig_mod.MigrationStatus(
        db_path=tmp_path / "x.db",
        pending=[mig_mod.PendingMigration("state", 1, "first")],
    )
    rendered_one = mig_mod.format_pending_summary(one)
    assert rendered_one.startswith("1 pending migration:")
    assert "migration(s)" not in rendered_one

    many = mig_mod.MigrationStatus(
        db_path=tmp_path / "x.db",
        pending=[
            mig_mod.PendingMigration("state", 1, "first"),
            mig_mod.PendingMigration("state", 2, "second"),
            mig_mod.PendingMigration("work", 1, "wfirst"),
        ],
    )
    rendered_many = mig_mod.format_pending_summary(many)
    assert rendered_many.startswith("3 pending migrations:")


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


def test_inspect_rejects_corrupt_state_db(tmp_path: Path) -> None:
    db_path = _write_corrupt_db(tmp_path / "state.db")

    with pytest.raises(mig_mod.UnusableDatabaseError) as exc:
        mig_mod.inspect(db_path)

    assert exc.value.db_path == db_path
    assert "not a database" in exc.value.detail


# ---------------------------------------------------------------------------
# Refuse-start gate
# ---------------------------------------------------------------------------


def test_refuse_start_message_offers_three_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#760: the refuse-start message must surface the three recovery
    paths (apply, dry-run, bypass) as an ``Options:`` block so the
    user can pick their comfort level instead of being nudged toward a
    single action."""
    db_path = tmp_path / "state.db"
    from pollypm.work.sqlite_service import SQLiteWorkService

    # Bootstrap a fresh DB so inspect() has something real to read.
    with SQLiteWorkService(db_path):
        pass
    synthetic = list(StateStore._MIGRATIONS) + [
        (9010, "refuse-start options block", []),
    ]
    monkeypatch.setattr(StateStore, "_MIGRATIONS", synthetic)

    status = mig_mod.inspect(db_path)
    rendered = mig_mod._format_refuse_start_message(status)

    assert "Cannot start" in rendered
    # Each of the three options is named with its command.
    assert "Apply (recommended)" in rendered
    assert "pm migrate --apply" in rendered
    assert "pm is the PollyPM CLI installed alongside pollypm" in rendered
    assert "Dry-run first" in rendered
    assert "pm migrate --check" in rendered
    assert "Bypass for this shell only (risky)" in rendered
    assert "POLLYPM_SKIP_MIGRATION_GATE=1" in rendered
    # The options block follows Next:.
    assert rendered.index("Next:") < rendered.index("Options:")


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


def test_refuse_start_reports_corrupt_db_without_migration_advice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _write_corrupt_db(tmp_path / "state.db")

    with pytest.raises(SystemExit) as exc:
        mig_mod.require_no_pending_or_exit(db_path)

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert "Cannot use state.db" in captured.err
    assert "not a valid SQLite database" in captured.err
    assert "pm doctor" in captured.err
    assert "pm restore" in captured.err
    assert "Cannot start" not in captured.err
    assert "Apply (recommended)" not in captured.err


def test_unusable_db_hold_mode_parks_after_rendering_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _write_corrupt_db(tmp_path / "state.db")
    error = mig_mod.UnusableDatabaseError(db_path, "file is not a database")
    held: list[int] = []

    def fake_hold(*, code: int) -> None:
        held.append(code)
        raise SystemExit(99)

    monkeypatch.setenv("POLLYPM_HOLD_UNUSABLE_DATABASE_SCREEN", "1")
    monkeypatch.setattr(mig_mod, "_hold_unusable_database_screen", fake_hold)

    with pytest.raises(SystemExit) as exc:
        mig_mod.exit_unusable_database(error, code=2)

    captured = capsys.readouterr()
    assert exc.value.code == 99
    assert held == [2]
    assert "Cannot use state.db" in captured.err
    assert "pm restore" in captured.err


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


def test_refuse_start_gate_bootstraps_missing_db_fully(tmp_path: Path) -> None:
    """Fresh startup must not leave only work migrations pending (#1142).

    ``pm init`` writes config but no DB. The first bare ``pm`` reaches
    the migration gate before Supervisor/StateStore startup. If the gate
    simply returns, Supervisor can create a state-migrated DB without
    opening the work service; the second ``pm`` then refuses to start on
    pending ``[work]`` migrations.
    """
    db_path = tmp_path / "state.db"

    mig_mod.require_no_pending_or_exit(db_path)

    status = mig_mod.inspect(db_path)
    assert status.up_to_date
    assert (
        status.applied[mig_mod.NAMESPACE_STATE]
        == status.latest[mig_mod.NAMESPACE_STATE]
    )
    assert (
        status.applied[mig_mod.NAMESPACE_WORK]
        == status.latest[mig_mod.NAMESPACE_WORK]
    )


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


def test_cli_migrate_check_reports_corrupt_db_without_traceback(tmp_path: Path) -> None:
    config_path = _write_minimal_config(tmp_path)
    _write_corrupt_db(tmp_path / "state.db")

    app = _build_cli_app()
    result = runner.invoke(app, ["--check", "--config", str(config_path)])

    combined = result.stdout + "\n" + (result.stderr or "")
    assert result.exit_code == 2, result.output
    assert "Cannot use state.db" in combined
    assert "not a valid SQLite database" in combined
    assert "pm doctor" in combined
    assert "Traceback" not in combined


def test_cli_migrate_apply_reports_corrupt_db_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pollypm.cli_features import migrate as _migrate_mod

    monkeypatch.setattr(_migrate_mod, "_live_pollypm_processes", lambda: [])
    config_path = _write_minimal_config(tmp_path)
    _write_corrupt_db(tmp_path / "state.db")

    app = _build_cli_app()
    result = runner.invoke(app, ["--apply", "--config", str(config_path)])

    combined = result.stdout + "\n" + (result.stderr or "")
    assert result.exit_code == 2, result.output
    assert "Cannot use state.db" in combined
    assert "pm migrate --apply" not in combined
    assert "Traceback" not in combined


def test_cli_status_reports_corrupt_db_without_traceback(tmp_path: Path) -> None:
    from pollypm import cli as _cli

    config_path = _write_minimal_config(tmp_path)
    _write_corrupt_db(tmp_path / "state.db")

    result = runner.invoke(_cli.app, ["status", "--config", str(config_path)])

    combined = result.stdout + "\n" + (result.stderr or "")
    assert result.exit_code == 2, result.output
    assert "Cannot use state.db" in combined
    assert "pm doctor" in combined
    assert "Traceback" not in combined


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
        lambda status: "1 pending migration:\n  [state] v99: synthetic pending",
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
    from pollypm.cli_features import migrate as _migrate_mod

    monkeypatch.setattr(_migrate_mod, "_live_pollypm_processes", lambda: [])
    # No fresh DB this time — let --apply bootstrap it so the CLI prints
    # a non-empty "applied" list.
    config_path = _write_minimal_config(tmp_path)
    app = _build_cli_app()
    result = runner.invoke(app, ["--apply", "--force", "--config", str(config_path)])
    assert result.exit_code == 0
    combined = result.stdout + "\n" + (result.stderr or "")
    # Either "Applied N migration(s)..." on the bootstrap path or the
    # "All migrations up to date." fallback if some future refactor
    # makes the DB self-bootstrap earlier. Both are acceptable; failure
    # would be a non-zero exit.
    assert "Applied" in combined or "up to date" in combined


# ---------------------------------------------------------------------------
# #1006: --apply refuses while live processes hold DB connections
# ---------------------------------------------------------------------------


def test_pm_migrate_apply_refuses_when_rail_daemon_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1006: ``--apply`` must refuse when ``rail_daemon`` is alive.

    Running migrations underneath a live JobWorkerPool closes per-
    project DB handles the pool is still using. Pre-fix the cockpit
    cascaded ``Cannot operate on a closed database`` errors and
    zombied. The CLI now bails with structured guidance and a
    non-zero exit so the user stops the daemon first.
    """
    import os
    from pollypm.cli_features import migrate as _migrate_mod

    # Point ``DEFAULT_CONFIG_PATH.parent`` at a tmp home so we don't
    # poke the developer's real ``~/.pollypm/`` PID file.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(
        _migrate_mod, "DEFAULT_CONFIG_PATH", fake_home / "config.toml",
    )

    pidfile = fake_home / "rail_daemon.pid"
    # Use our own PID so ``os.kill(pid, 0)`` returns truthfully alive.
    pidfile.write_text(str(os.getpid()))

    config_path = _write_minimal_config(tmp_path)
    db_path = _fresh_state_db(tmp_path / "state.db")
    config_path.write_text(
        "[project]\n"
        f'base_dir = "{tmp_path}"\n'
        f'state_db = "{db_path}"\n'
        f'workspace_root = "{tmp_path}"\n'
    )

    app = _build_cli_app()
    result = runner.invoke(app, ["--apply", "--config", str(config_path)])
    assert result.exit_code == _migrate_mod._EXIT_LIVE_PROCESS, result.output
    combined = result.stdout + "\n" + (result.stderr or "")
    assert "Refusing to apply migrations" in combined
    assert "rail_daemon" in combined
    assert "--force" in combined


def test_pm_migrate_apply_force_overrides_live_process_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--force`` skips the rail_daemon guard for emergencies."""
    import os
    from pollypm.cli_features import migrate as _migrate_mod

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(
        _migrate_mod, "DEFAULT_CONFIG_PATH", fake_home / "config.toml",
    )
    pidfile = fake_home / "rail_daemon.pid"
    pidfile.write_text(str(os.getpid()))

    config_path = _write_minimal_config(tmp_path)
    db_path = _fresh_state_db(tmp_path / "state.db")
    config_path.write_text(
        "[project]\n"
        f'base_dir = "{tmp_path}"\n'
        f'state_db = "{db_path}"\n'
        f'workspace_root = "{tmp_path}"\n'
    )

    app = _build_cli_app()
    result = runner.invoke(
        app, ["--apply", "--force", "--config", str(config_path)]
    )
    assert result.exit_code == 0, result.output


def test_pm_migrate_apply_ignores_stale_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pidfile pointing at a dead PID does not block ``--apply``."""
    from pollypm.cli_features import migrate as _migrate_mod

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(
        _migrate_mod, "DEFAULT_CONFIG_PATH", fake_home / "config.toml",
    )
    # PID 0 is invalid + the helper rejects pid<=0; using a high pid
    # that almost certainly does not exist also exercises
    # ProcessLookupError — pick PID 0 since the helper short-circuits.
    pidfile = fake_home / "rail_daemon.pid"
    pidfile.write_text("0")

    config_path = _write_minimal_config(tmp_path)
    db_path = _fresh_state_db(tmp_path / "state.db")
    config_path.write_text(
        "[project]\n"
        f'base_dir = "{tmp_path}"\n'
        f'state_db = "{db_path}"\n'
        f'workspace_root = "{tmp_path}"\n'
    )

    app = _build_cli_app()
    result = runner.invoke(app, ["--apply", "--config", str(config_path)])
    assert result.exit_code == 0, result.output


def test_live_pollypm_processes_returns_empty_when_no_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_live_pollypm_processes`` is empty on a clean machine."""
    from pollypm.cli_features import migrate as _migrate_mod

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(
        _migrate_mod, "DEFAULT_CONFIG_PATH", fake_home / "config.toml",
    )
    assert _migrate_mod._live_pollypm_processes() == []
