from pathlib import Path

from pollypm.config_patches import remove_project_override
from pollypm.config import write_config
from pollypm.models import AccountConfig, KnownProject, ProjectKind, ProjectSettings, PollyPMConfig, PollyPMSettings, ProviderKind, SessionConfig
from pollypm.service_api import PollyPMService


def test_agent_driven_rule_override_is_project_local_and_takes_effect_next_session(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
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
    service = PollyPMService(config_path)

    proposal = service.detect_preference_override("demo", "I don't want to run unit tests before every commit")
    assert proposal is not None
    assert proposal["kind"] == "rule"
    assert proposal["path"].endswith(".pollypm/rules/build.md")
    assert "project-local override" in proposal["offer_text"]

    built_in_path = Path(__file__).resolve().parents[2] / "src" / "pollypm" / "defaults" / "rules" / "build.md"
    built_in_before = built_in_path.read_text()
    applied = service.apply_preference_override("demo", "I don't want to run unit tests before every commit")
    override_path = Path(applied["path"])
    assert override_path.exists()
    assert built_in_path.read_text() == built_in_before
    assert "Do not require unit tests before every commit unless the change specifically needs them." in override_path.read_text()

    overrides = service.list_overrides("demo")
    assert str(override_path) in overrides

    supervisor = service.load_supervisor()
    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}
    prompt = launches["worker_demo"].session.prompt or ""
    assert ".pollypm/rules/build.md" in prompt

    remove_project_override(project_root, "rule", "build")
    assert not override_path.exists()

    supervisor = service.load_supervisor()
    launches = {launch.session.name: launch for launch in supervisor.plan_launches()}
    prompt = launches["worker_demo"].session.prompt or ""
    assert "pollypm/defaults/rules/build.md" in prompt
