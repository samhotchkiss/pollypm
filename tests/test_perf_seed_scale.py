from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path

import pytest


def _load_seed_scale():
    path = Path(__file__).resolve().parents[1] / "scripts" / "perf" / "seed_scale.py"
    spec = importlib.util.spec_from_file_location("seed_scale", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


seed_scale = _load_seed_scale()


def test_ambient_live_pg_dsn_is_refused_shape() -> None:
    assert seed_scale.is_ambient_live_pg_dsn("postgresql://localhost:5432/pollypm")
    assert seed_scale.is_ambient_live_pg_dsn("dbname=pollypm host=/tmp port=5432")
    assert not seed_scale.is_ambient_live_pg_dsn("postgresql://localhost:6543/pollypm")
    assert not seed_scale.is_ambient_live_pg_dsn("postgresql://localhost:5432/pollypm_test")


def test_seed_dry_run_writes_s_scale_fixture(tmp_path: Path) -> None:
    args = Namespace(
        scale="s",
        dsn=None,
        execute=False,
        schema="perf_test_schema",
        workspace=str(tmp_path / "perf"),
        force_clean=True,
        json_out=None,
    )

    manifest = seed_scale.seed(args)

    assert manifest["sessions"] == 4
    assert manifest["tasks"] == 25
    assert manifest["transcripts"]["messages"] == 200
    assert Path(manifest["config_path"]).exists()
    assert Path(manifest["sql_path"]).exists()
    assert Path(manifest["manifest_path"]).exists()
    assert "CREATE SCHEMA IF NOT EXISTS" in Path(manifest["sql_path"]).read_text()


def test_seed_config_pins_dsn_to_perf_schema(tmp_path: Path) -> None:
    args = Namespace(
        scale="s",
        dsn="postgresql://localhost:6543/pollypm_test?sslmode=disable",
        execute=False,
        schema="perf_test_schema",
        workspace=str(tmp_path / "perf"),
        force_clean=True,
        json_out=None,
    )

    manifest = seed_scale.seed(args)

    config_text = Path(manifest["config_path"]).read_text(encoding="utf-8")
    assert "sslmode=disable" in config_text
    assert "options=-csearch_path%3Dperf_test_schema%2Cpublic" in config_text


def test_dsn_with_search_path_preserves_existing_options() -> None:
    dsn = seed_scale.dsn_with_search_path(
        "postgresql://localhost:6543/pollypm_test?options=-cstatement_timeout%3D5000",
        "perf_test_schema",
    )

    assert "options=-cstatement_timeout%3D5000+-csearch_path%3Dperf_test_schema%2Cpublic" in dsn


def test_execute_requires_non_ambient_dsn(tmp_path: Path) -> None:
    args = Namespace(
        scale="s",
        dsn="postgresql://localhost:5432/pollypm",
        execute=True,
        schema="perf_test_schema",
        workspace=str(tmp_path / "perf"),
        force_clean=True,
        json_out=None,
    )

    with pytest.raises(SystemExit, match="ambient local pollypm"):
        seed_scale.seed(args)


def test_schema_names_are_restricted() -> None:
    assert seed_scale.safe_schema_name("perf_m_123") == "perf_m_123"
    with pytest.raises(SystemExit):
        seed_scale.safe_schema_name("bad-name;drop")
