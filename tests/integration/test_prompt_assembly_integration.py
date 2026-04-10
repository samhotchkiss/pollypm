from pathlib import Path

from pollypm.config import write_config
from pollypm.models import AccountConfig, KnownProject, ProjectKind, ProjectSettings, PollyPMConfig, PollyPMSettings, ProviderKind, SessionConfig
from pollypm.service_api import PollyPMService
from pollypm.storage.state import StateStore


def test_worker_prompt_assembles_overview_manifest_issue_and_checkpoint(tmp_path: Path) -> None:
    control_root = tmp_path / "control"
    control_root.mkdir()
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "docs").mkdir()
    (project_root / "docs" / "project-overview.md").write_text("# Project Overview\n\nCurrent architecture summary.\n")
    (project_root / ".pollypm" / "rules").mkdir(parents=True)
    (project_root / ".pollypm" / "rules" / "deploy.md").write_text(
        "Description: Project deploy process\nTrigger: when deploying this project\n"
    )
    (project_root / ".pollypm" / "magic").mkdir(parents=True)
    (project_root / ".pollypm" / "magic" / "ship.md").write_text(
        "Description: Release helper\nTrigger: when shipping a release\n"
    )
    (project_root / "issues" / "02-in-progress").mkdir(parents=True)
    (project_root / "issues" / "02-in-progress" / "0007-fix-deploy.md").write_text(
        "# 0007 Fix deploy\n\nShip the deployment fix.\n"
    )
    checkpoint_path = project_root / ".pollypm" / "artifacts" / "checkpoints" / "worker_demo" / "latest.md"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text("# Checkpoint `worker_demo`\n\nRecent handoff notes.\n")

    config = PollyPMConfig(
        project=ProjectSettings(
            name="pollypm",
            root_dir=control_root,
            base_dir=control_root / ".pollypm-state",
            logs_dir=control_root / ".pollypm-state/logs",
            snapshots_dir=control_root / ".pollypm-state/snapshots",
            state_db=control_root / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=control_root / ".pollypm-state" / "homes" / "claude_main",
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
    config_path = control_root / "pollypm.toml"
    write_config(config, config_path, force=True)
    store = StateStore(config.project.state_db)
    store.upsert_session_runtime(
        session_name="worker_demo",
        status="healthy",
        last_checkpoint_path=str(checkpoint_path),
    )

    supervisor = PollyPMService(config_path).load_supervisor()
    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}
    prompt = launches["worker_demo"].session.prompt or ""

    overview_index = prompt.index("## Project Overview")
    rules_index = prompt.index("## Available Rules")
    magic_index = prompt.index("## Available Magic")
    issue_index = prompt.index("## Active Issue")
    checkpoint_index = prompt.index("## Latest Checkpoint")

    assert overview_index < rules_index < magic_index < issue_index < checkpoint_index
    assert "Current architecture summary." in prompt
    assert ".pollypm/rules/deploy.md" in prompt
    assert ".pollypm/magic/ship.md" in prompt
    assert "Source: `issues/02-in-progress/0007-fix-deploy.md`" in prompt
    assert "Ship the deployment fix." in prompt
    assert "Recent handoff notes." in prompt
