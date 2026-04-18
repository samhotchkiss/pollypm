from pathlib import Path

from pollypm.config import write_config
from pollypm.models import AccountConfig, KnownProject, ProjectKind, ProjectSettings, PollyPMConfig, PollyPMSettings, ProviderKind, SessionConfig
from pollypm.service_api import PollyPMService


def test_project_rule_appears_in_worker_prompt_manifest(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".pollypm" / "rules").mkdir(parents=True)
    (project_root / ".pollypm" / "rules" / "deploy.md").write_text(
        "Description: Project deploy process\nTrigger: when deploying this project\n"
    )

    config = PollyPMConfig(
        project=ProjectSettings(
            name="pollypm",
            root_dir=project_root,
            base_dir=project_root / ".pollypm",
            logs_dir=project_root / ".pollypm/logs",
            snapshots_dir=project_root / ".pollypm/snapshots",
            state_db=project_root / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm/homes/claude_main",
            )
        },
        sessions={
            "worker_demo": SessionConfig(
                name="worker_demo",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
                project="demo",
                agent_profile="worker",
            )
        },
        projects={
            "demo": KnownProject(
                key="demo",
                path=project_root,
                name="Demo",
                kind=ProjectKind.GIT,
            )
        },
    )
    config_path = project_root / "pollypm.toml"
    write_config(config, config_path, force=True)

    supervisor = PollyPMService(config_path).load_supervisor()
    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}
    prompt = launches["worker_demo"].session.prompt or ""

    assert "## Available Rules" in prompt
    assert ".pollypm/rules/deploy.md" in prompt
    assert "Project deploy process" in prompt
    assert "pollypm/defaults/rules/bugfix.md" in prompt
