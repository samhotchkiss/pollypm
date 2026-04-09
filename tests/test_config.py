from pathlib import Path

from promptmaster.agent_profiles.builtin import heartbeat_prompt, polly_prompt
from promptmaster.config import load_config, render_example_config


def test_load_example_config(tmp_path: Path) -> None:
    config_path = tmp_path / "promptmaster.toml"
    config_path.write_text(render_example_config())

    config = load_config(config_path)

    assert config.project.name == "PollyPM"
    assert config.project.tmux_session == "pollypm"
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


def test_load_config_normalizes_legacy_branding_and_control_prompts(tmp_path: Path) -> None:
    config_path = tmp_path / "promptmaster.toml"
    config_path.write_text(
        """
[project]
name = "promptmaster"
tmux_session = "promptmaster"

[promptmaster]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".promptmaster/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."
prompt = "You are Prompt Master session 0. Remain as a true interactive CLI session."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."
prompt = "You are Prompt Master session 1. Remain as a true interactive CLI session."

[sessions.worker_promptmaster]
role = "worker"
provider = "claude"
account = "claude_primary"
cwd = "."
prompt = "Read the Prompt Master issue queue, start with the highest-leverage open issue."

[projects.promptmaster]
path = "."
name = "promptmaster"
"""
    )

    config = load_config(config_path)

    assert config.project.name == "PollyPM"
    assert config.project.tmux_session == "pollypm"
    assert config.projects["promptmaster"].name == "PollyPM"
    assert config.sessions["heartbeat"].prompt == heartbeat_prompt()
    assert config.sessions["operator"].prompt == polly_prompt()
    assert "PollyPM issue queue" in (config.sessions["worker_promptmaster"].prompt or "")
