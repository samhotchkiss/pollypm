from pathlib import Path

from pollypm.agent_profiles.builtin import heartbeat_prompt, polly_prompt
from pollypm.config import load_config, project_config_path, render_example_config, write_config
from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)


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
    assert set(config.sessions) == {"heartbeat", "operator"}
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


def test_control_sessions_use_workspace_root_for_dot_cwd(tmp_path: Path) -> None:
    """Control sessions with cwd='.' should resolve to workspace_root, not base_dir."""
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "pollypm"
tmux_session = "pollypm"
workspace_root = "/Users/test/dev"

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

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.worker]
role = "worker"
provider = "claude"
account = "claude_primary"
cwd = "."
"""
    )
    config = load_config(config_path)
    # Control sessions should use workspace_root
    assert config.sessions["heartbeat"].cwd == Path("/Users/test/dev")
    assert config.sessions["operator"].cwd == Path("/Users/test/dev")
    # Workers should use base_dir (config parent)
    assert config.sessions["worker"].cwd == tmp_path


def test_load_config_merges_project_local_worker_sessions(tmp_path: Path) -> None:
    project_root = tmp_path / "wire"
    project_root.mkdir()
    (project_root / ".pollypm" / "config").mkdir(parents=True)
    (project_root / ".pollypm" / "config" / "project.toml").write_text(
        """
[project]
display_name = "Wire"
persona_name = "Wren"

[sessions.worker_wire]
role = "worker"
provider = "claude"
account = "claude_primary"
cwd = "."
prompt = "Implement issue #1."
"""
    )
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "PollyPM"
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

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[projects.wire]
path = "wire"
"""
    )

    config = load_config(config_path)

    assert config.projects["wire"].name == "Wire"
    assert config.projects["wire"].persona_name == "Wren"
    assert config.sessions["worker_wire"].project == "wire"
    assert config.sessions["worker_wire"].cwd == project_root


def test_write_config_splits_worker_sessions_into_project_local_files(tmp_path: Path) -> None:
    project_root = tmp_path / "wire"
    project_root.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm-state",
            logs_dir=tmp_path / ".pollypm-state/logs",
            snapshots_dir=tmp_path / ".pollypm-state/snapshots",
            state_db=tmp_path / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_primary"),
        accounts={
            "claude_primary": AccountConfig(
                name="claude_primary",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm-state" / "homes" / "claude_primary",
            )
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
                cwd=tmp_path,
            ),
            "worker_wire": SessionConfig(
                name="worker_wire",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
                cwd=project_root,
                project="wire",
                prompt="Implement issue #1.",
            ),
        },
        projects={
            "wire": KnownProject(
                key="wire",
                path=project_root,
                name="Wire",
                persona_name="Wren",
            )
        },
    )

    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

    global_text = config_path.read_text()
    local_text = project_config_path(project_root).read_text()

    assert "[sessions.worker_wire]" not in global_text
    assert '[projects.wire]' in global_text
    assert 'persona_name = "Wren"' in global_text
    assert '[project]' in local_text
    assert 'persona_name = "Wren"' in local_text
    assert '[sessions.worker_wire]' in local_text

    loaded = load_config(config_path)
    assert "worker_wire" in loaded.sessions
    assert loaded.sessions["worker_wire"].project == "wire"
    assert loaded.projects["wire"].persona_name == "Wren"
