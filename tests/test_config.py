from pathlib import Path

from promptmaster.config import load_config, render_example_config


def test_load_example_config(tmp_path: Path) -> None:
    config_path = tmp_path / "promptmaster.toml"
    config_path.write_text(render_example_config())

    config = load_config(config_path)

    assert config.project.name == "PollyPM"
    assert config.project.tmux_session == "promptmaster"
    assert config.project.workspace_root == Path.home() / "dev"
    assert config.promptmaster.controller_account == "codex_primary"
    assert config.promptmaster.open_permissions_by_default is True
    assert config.promptmaster.failover_enabled is True
    assert config.promptmaster.failover_accounts == ["claude_primary"]
    assert config.memory.backend == "file"
    assert set(config.accounts) == {"codex_primary", "claude_primary"}
    assert set(config.sessions) == {"heartbeat", "operator", "worker_demo"}
    assert set(config.projects) == {"promptmaster"}
    assert config.sessions["operator"].provider.value == "codex"
    assert config.sessions["heartbeat"].provider.value == "codex"
