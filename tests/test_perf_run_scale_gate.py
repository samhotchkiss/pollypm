from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


def _load_run_scale_gate():
    path = Path(__file__).resolve().parents[1] / "scripts" / "perf" / "run_scale_gate.py"
    spec = importlib.util.spec_from_file_location("run_scale_gate", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


run_scale_gate = _load_run_scale_gate()


def test_default_perf_dsn_uses_non_live_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLLYPM_PERF_PG_DSN", raising=False)
    monkeypatch.delenv("POLLYPM_PERF_DB", raising=False)

    dsn = run_scale_gate.default_perf_dsn()

    assert dsn == "postgresql://localhost:5432/pollypm_perf"
    assert not run_scale_gate.seed_scale.is_ambient_live_pg_dsn(dsn)


def test_ambient_perf_dsn_is_refused() -> None:
    with pytest.raises(SystemExit, match="ambient local pollypm"):
        run_scale_gate.ensure_not_ambient("postgresql://localhost:5432/pollypm")


def test_admin_dsn_uses_postgres_database_and_drops_options() -> None:
    admin_dsn = run_scale_gate.admin_dsn_for(
        "postgresql://user:pass@localhost:5432/pollypm_perf"
        "?sslmode=require&options=-csearch_path%3Dperf%2Cpublic"
    )

    parsed = urlsplit(admin_dsn)
    assert parsed.path == "/postgres"
    assert parse_qs(parsed.query) == {"sslmode": ["require"]}


def test_choose_port_refuses_live_port() -> None:
    with pytest.raises(SystemExit, match="live port 8765"):
        run_scale_gate.choose_port(8765)


def test_child_env_pins_isolated_dsn_and_error_log(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    workspace = tmp_path / "workspace"
    dsn = "postgresql://localhost:5432/pollypm_perf?options=-csearch_path%3Dperf%2Cpublic"

    env = run_scale_gate.child_env(config_path, dsn, workspace)

    assert env["POLLYPM_CONFIG"] == str(config_path)
    assert env["POLLYPM_PG_DSN"] == dsn
    assert env["POLLYPM_PERF_PG_DSN"] == dsn
    assert env["POLLYPM_ERROR_LOG_PATH"] == str(workspace / ".pollypm" / "errors.log")
    assert env["POLLYPM_DISABLE_ERROR_NOTIFICATIONS"] == "1"
    assert str(run_scale_gate.SRC_DIR) in env["PYTHONPATH"].split(os.pathsep)
