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


def test_worker_prompt_routes_blocking_questions_to_polly_with_label() -> None:
    """Workers blocking on credentials, ambiguous specs, or environment
    issues used to ``pm notify`` straight to Sam's inbox by default —
    raw worker context (commit refs, file paths, test stack traces)
    arriving as a user-facing alert. Route to Polly with the
    ``blocking_question`` label so her operator playbook can
    translate into the user_prompt contract before anything
    user-facing surfaces."""
    from pollypm.plugins_builtin.core_agent_profiles.profiles import (
        worker_prompt,
    )

    text = worker_prompt()
    assert "--requester polly" in text
    assert "--label blocking_question" in text
    # Voice rule: stop after escalating, don't keep retrying — Polly
    # owns the response path via ``pm send``.
    assert "pm send" in text


def test_reviewer_prompt_routes_escalations_to_polly_not_user() -> None:
    """Russell's escalations are reviewer-jargon ('security concern',
    'architectural drift') — they belong in Polly's inbox, where she
    rewrites them into the user_prompt contract before anything
    surfaces to Sam. Routing them to the user directly bypasses
    that translation and floods the user inbox with low-signal
    operations metadata.

    The reviewer prompt must teach Russell to pass
    ``--requester polly`` on the escalation and plan-review
    misroute notify calls."""
    from pollypm.plugins_builtin.core_agent_profiles.profiles import (
        reviewer_prompt,
    )

    text = reviewer_prompt()
    assert "--requester polly" in text
    # The escalation block describes why we route to Polly so future
    # editors can preserve the intent if they refactor the section.
    assert "Polly" in text and "operator" in text


def test_operator_guide_documents_user_prompt_json_contract() -> None:
    """The polly-operator-guide must teach Polly to send escalations
    with the structured ``--user-prompt-json`` contract — that is the
    payload the dashboard's Action Needed card and the inbox detail
    pane render. A bare ``pm notify "subject" "body"`` produces no
    user-facing summary and forces the operator to parse worker
    jargon."""
    from pollypm.plugins_builtin.core_agent_profiles.profiles import (
        _POLLY_OPERATOR_GUIDE_PATH,
    )

    text = _POLLY_OPERATOR_GUIDE_PATH.read_text(encoding="utf-8")

    # The escalation section must explicitly reference the JSON
    # contract flag and explain its required keys.
    assert "--user-prompt-json" in text
    for required_key in ("summary", "steps", "question", "actions"):
        assert f"`{required_key}`" in text, (
            f"operator guide should describe the user_prompt key "
            f"`{required_key}` so Polly knows the contract shape"
        )
    # And the dashboard contract is named so future readers can cross
    # reference the architect / dashboard rendering.
    assert "user_prompt" in text


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
