from __future__ import annotations

import os
from pathlib import Path

import typer
from typer.testing import CliRunner

from pollypm.cli_features.migrate import register_migrate_commands
from pollypm.storage.state import StateStore
from pollypm.store import migrations


def _state_db_with_only_state_schema(tmp_path: Path) -> Path:
    db_path = tmp_path / "state.db"
    with StateStore(db_path):
        pass
    assert any(
        item.namespace == migrations.NAMESPACE_WORK
        for item in migrations.inspect(db_path).pending
    )
    return db_path


def _write_config(tmp_path: Path, db_path: Path) -> Path:
    config_path = tmp_path / "pollypm.toml"
    base_dir = tmp_path / ".pollypm"
    config_path.write_text(
        "\n".join(
            [
                "[project]",
                'name = "PollyPM"',
                f'base_dir = "{base_dir}"',
                f'logs_dir = "{base_dir / "logs"}"',
                f'snapshots_dir = "{base_dir / "snapshots"}"',
                f'state_db = "{db_path}"',
                f'workspace_root = "{tmp_path}"',
                "",
                "[pollypm]",
                'controller_account = ""',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def test_apply_replays_sqlite_work_migrations_and_clears_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("POLLYPM_SKIP_MIGRATION_GATE", raising=False)
    db_path = _state_db_with_only_state_schema(tmp_path)

    outcome = migrations.apply(db_path)

    assert any(item.namespace == migrations.NAMESPACE_WORK for item in outcome.applied)
    assert migrations.inspect(db_path).pending == []
    migrations.require_no_pending_or_exit(db_path)


def test_check_against_clone_uses_default_clone_path_without_name_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from pollypm import config as config_mod

    db_path = _state_db_with_only_state_schema(tmp_path)
    fake_global = tmp_path / "global"
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_DIR", fake_global)
    monkeypatch.delenv("POLLYPM_HOME", raising=False)

    outcome = migrations.check_against_clone(db_path)

    assert outcome.ok is True
    assert outcome.clone_path == fake_global / "migration-check.db"
    assert outcome.clone_path.is_file()
    assert migrations.inspect(outcome.clone_path).pending == []
    assert any(
        item.namespace == migrations.NAMESPACE_WORK
        for item in migrations.inspect(db_path).pending
    )


def test_migrate_apply_force_clears_gate_and_restores_bypass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("POLLYPM_SKIP_MIGRATION_GATE", raising=False)
    db_path = _state_db_with_only_state_schema(tmp_path)
    config_path = _write_config(tmp_path, db_path)
    app = typer.Typer()
    register_migrate_commands(app)

    result = CliRunner().invoke(
        app,
        ["--apply", "--force", "--config", str(config_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Applied" in result.output
    assert migrations.inspect(db_path).pending == []
    assert "POLLYPM_SKIP_MIGRATION_GATE" not in os.environ
    migrations.require_no_pending_or_exit(db_path)
