from pathlib import Path

from typer.testing import CliRunner

import promptmaster.cli as cli


def test_root_command_defaults_to_up(monkeypatch, tmp_path: Path) -> None:
    called: dict[str, Path] = {}
    config_path = tmp_path / "promptmaster.toml"
    config_path.write_text("[project]\nname = \"promptmaster\"\n")

    def fake_up(config_path: Path) -> None:
        called["config_path"] = config_path

    monkeypatch.setattr(cli, "up", fake_up)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["--config", str(config_path)])

    assert result.exit_code == 0
    assert called["config_path"] == config_path


def test_root_command_runs_onboarding_when_config_missing(monkeypatch, tmp_path: Path) -> None:
    called: dict[str, Path] = {}

    def fake_first_run(config_path: Path) -> None:
        called["config_path"] = config_path

    def fail_up(config_path: Path) -> None:
        raise AssertionError("up should not run when config is missing")

    monkeypatch.setattr(cli, "_first_run_setup_and_launch", fake_first_run)
    monkeypatch.setattr(cli, "up", fail_up)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["--config", str(tmp_path / "promptmaster.toml")])

    assert result.exit_code == 0
    assert called["config_path"] == tmp_path / "promptmaster.toml"


def test_discover_config_path_walks_up_parents(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    config_path = root / "promptmaster.toml"
    config_path.write_text("[project]\nname = \"PollyPM\"\n")

    monkeypatch.chdir(nested)

    resolved = cli._discover_config_path(Path("promptmaster.toml"))

    assert resolved == config_path


def test_root_command_attaches_existing_session_when_default_config_missing(monkeypatch, tmp_path: Path) -> None:
    called: dict[str, object] = {}

    class FakeTmux:
        def current_session_name(self):
            return None

        def has_session(self, name: str) -> bool:
            return name == "pollypm"

        def attach_session(self, name: str) -> int:
            called["attached"] = name
            return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "TmuxClient", lambda: FakeTmux())
    monkeypatch.setattr(cli, "_first_run_setup_and_launch", lambda config_path: (_ for _ in ()).throw(AssertionError("should not onboard")))

    runner = CliRunner()
    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0
    assert called["attached"] == "pollypm"


def test_up_surfaces_bootstrap_failure_cleanly(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "promptmaster.toml"
    config_path.write_text("[project]\nname = \"promptmaster\"\n")

    class FakeTmux:
        def has_session(self, name: str) -> bool:
            return False

        def current_session_name(self):
            return None

    class FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = FakeTmux()
            self.config = type("Config", (), {"project": type("Project", (), {"tmux_session": "pollypm"})()})()

        def ensure_layout(self) -> None:
            return None

        def bootstrap_tmux(self) -> str:
            raise RuntimeError("PollyPM could not launch any controller account: claude_demo: probe failed")

    monkeypatch.setattr(cli, "_load_supervisor", lambda path: FakeSupervisor())

    runner = CliRunner()
    result = runner.invoke(cli.app, ["up", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "PollyPM could not launch any controller account" in result.output
