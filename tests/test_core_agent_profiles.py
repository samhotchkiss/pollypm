"""Regression tests for core agent profile prompt assembly."""

from __future__ import annotations

from pathlib import Path

from pollypm.agent_profiles.base import AgentProfileContext
from pollypm.config import write_config
from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.plugins_builtin.core_agent_profiles import plugin as core_profiles


def _make_worker_context(tmp_path: Path) -> tuple[AgentProfileContext, Path]:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_primary"),
        accounts={
            "claude_primary": AccountConfig(
                name="claude_primary",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm/homes/claude_primary",
            )
        },
        sessions={
            "worker_demo": SessionConfig(
                name="worker_demo",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
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
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    context = AgentProfileContext(
        config=config,
        session=config.sessions["worker_demo"],
        account=config.accounts["claude_primary"],
    )
    return context, project_root


def test_worker_profile_explains_optional_overrides_and_missing_files(tmp_path: Path) -> None:
    context, project_root = _make_worker_context(tmp_path)
    profile = core_profiles.plugin.agent_profiles["worker"]()

    missing_prompt = profile.build_prompt(context)
    assert missing_prompt is not None
    assert ".pollypm/INSTRUCT.md" in missing_prompt
    assert ".pollypm/docs/SYSTEM.md" in missing_prompt
    assert "optional project-level overrides written by the PM" in missing_prompt
    assert "override the built-in defaults" in missing_prompt
    assert "defaults apply — continue without blocking" in missing_prompt

    system_path = project_root / ".pollypm" / "docs" / "SYSTEM.md"
    system_path.parent.mkdir(parents=True, exist_ok=True)
    system_path.write_text("system override body\n", encoding="utf-8")
    instruct_path = project_root / ".pollypm" / "INSTRUCT.md"
    instruct_path.write_text("project override body\n", encoding="utf-8")

    present_prompt = profile.build_prompt(context)
    assert present_prompt is not None
    assert "system override body" in present_prompt
    assert "project override body" in present_prompt
