from pathlib import Path

from typer.testing import CliRunner

import pollypm.cli as cli


def test_up_surfaces_postgres_bootstrap_hint_on_pg_launch_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\nname = \"pollypm\"\n")

    class OperationalError(Exception):
        pass

    def raise_pg_error(_path: Path):
        raise OperationalError("connection refused while opening postgresql://localhost:5432/pollypm")

    monkeypatch.setattr(cli, "_load_supervisor", raise_pg_error)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["up", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "PollyPM needs Postgres. Run: pm bootstrap-pg --yes" in result.output
    assert "connection refused" in result.output
