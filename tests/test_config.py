from pathlib import Path

from pollypm.agent_profiles.builtin import heartbeat_prompt, polly_prompt
from pollypm.config import load_config, render_example_config


def test_load_example_config(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(render_example_config())

    config = load_config(config_path)

    assert config.project.name == "PollyPM"
    assert config.project.tmux_session == "pollypm"
    assert config.project.workspace_root == Path.home() / "dev"
    assert config.pollypm.controller_account == "codex_primary"
    assert config.pollypm.open_permissions_by_default is True
    assert config.pollypm.failover_enabled is True
    assert config.pollypm.failover_accounts == ["claude_primary"]
    assert config.memory.backend == "file"
    assert set(config.accounts) == {"codex_primary", "claude_primary"}
    assert set(config.sessions) == {"heartbeat", "operator", "worker_demo"}
    assert set(config.projects) == {"pollypm"}
    assert config.sessions["operator"].provider.value == "codex"
    assert config.sessions["heartbeat"].provider.value == "codex"


def test_load_config_normalizes_control_prompts(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "pollypm"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm-state/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."
prompt = "You are PollyPM session 0, remain as a true interactive CLI session."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."
prompt = "You are Polly, the PollyPM project manager, in session 1."

[sessions.worker_pollypm]
role = "worker"
provider = "claude"
account = "claude_primary"
cwd = "."
prompt = "Read the PollyPM issue queue, start with the highest-leverage open issue."

[projects.pollypm]
path = "."
name = "pollypm"
"""
    )

    config = load_config(config_path)

    assert config.project.name == "PollyPM"
    assert config.project.tmux_session == "pollypm"
    assert config.projects["pollypm"].name == "PollyPM"
    assert config.sessions["heartbeat"].prompt == heartbeat_prompt()
    assert config.sessions["operator"].prompt == polly_prompt()
    assert config.sessions["worker_pollypm"].prompt == "Read the PollyPM issue queue, start with the highest-leverage open issue."
