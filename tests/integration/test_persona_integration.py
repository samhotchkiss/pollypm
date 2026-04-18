from pathlib import Path

from pollypm.cockpit import CockpitRouter
from pollypm.config import load_config, project_config_path, write_config
from pollypm.models import AccountConfig, PollyPMConfig, PollyPMSettings, ProjectSettings, ProviderKind, SessionConfig
from pollypm.projects import register_project
from pollypm.service_api import PollyPMService


def _base_config(tmp_path: Path) -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
            )
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=tmp_path,
                window_name="pm-heartbeat",
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=tmp_path,
                window_name="pm-operator",
            ),
        },
        projects={},
    )


def test_register_project_persists_persona_and_shows_it_in_left_rail(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    write_config(_base_config(tmp_path), config_path, force=True)
    project_root = tmp_path / "plain-project"
    project_root.mkdir()

    project = register_project(config_path, project_root, name="Plain")
    loaded = load_config(config_path)

    assert loaded.projects[project.key].persona_name == "Pete"
    assert 'persona_name = "Pete"' in project_config_path(project_root).read_text()

    class FakeSupervisor:
        config = loaded

        def status(self):
            return [], [], [], [], []

    router = CockpitRouter(config_path)
    monkeypatch.setattr(router, "_load_supervisor", lambda fresh=False: FakeSupervisor())
    items = router.build_items()

    labels = {item.key: item.label for item in items}
    # Project label in the rail shows just the name, not persona (persona is only in PM Chat label)
    assert labels[f"project:{project.key}"] == "Plain"


def test_worker_prompt_includes_persona_and_rename_instruction(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    project_root = tmp_path / "plain-project"
    project_root.mkdir()
    write_config(_base_config(tmp_path), config_path, force=True)
    register_project(config_path, project_root, name="Plain")
    project_config = project_config_path(project_root)
    project_config.write_text(
        """
[project]
display_name = "Plain"
persona_name = "Pete"

[sessions.worker_plain]
role = "worker"
provider = "claude"
account = "claude_main"
cwd = "."
window_name = "worker-plain"
agent_profile = "worker"
"""
    )

    supervisor = PollyPMService(config_path).load_supervisor()
    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}
    prompt = launches["worker_plain"].session.prompt or ""

    assert "Your name for this project is Pete." in prompt
    assert "update `.pollypm/config/project.toml`" in prompt
