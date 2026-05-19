from __future__ import annotations

import logging
from pathlib import Path

from pollypm.model_registry import Registry
from pollypm.models import (
    AccountConfig,
    KnownProject,
    ModelAssignment,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
)
from pollypm.role_routing import resolve_role_assignment


def _config(tmp_path: Path) -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(
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
        sessions={},
        projects={
            "demo": KnownProject(
                key="demo",
                path=tmp_path / "demo",
                kind=ProjectKind.FOLDER,
            )
        },
    )


def test_project_override_wins_over_global(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.pollypm.role_assignments["architect"] = ModelAssignment(alias="opus-4.7")
    config.projects["demo"].role_assignments["architect"] = ModelAssignment(
        alias="sonnet-4.6"
    )

    resolved = resolve_role_assignment("architect", "demo", config=config)

    assert resolved.alias == "sonnet-4.6"
    assert resolved.provider == "claude"
    assert resolved.source == "project"


def test_global_assignment_used_when_project_has_no_override(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.pollypm.role_assignments["worker"] = ModelAssignment(alias="codex-gpt-5.4")

    resolved = resolve_role_assignment("worker", "demo", config=config)

    assert resolved.alias == "codex-gpt-5.4"
    assert resolved.provider == "codex"
    assert resolved.source == "global"


def test_fallback_assignment_used_when_no_configured_assignment(tmp_path: Path) -> None:
    # #1737 — claude-only configs fall back to Opus across the board
    # (no Codex account means we can't route worker/architect to Codex).
    config = _config(tmp_path)

    resolved = resolve_role_assignment("reviewer", "demo", config=config)

    assert resolved.alias == "opus-4.7"
    assert resolved.provider == "claude"
    assert resolved.source == "fallback"


def test_dual_provider_fallback_table(tmp_path: Path) -> None:
    # #1737 — with both Claude and Codex accounts configured but NO
    # explicit role_assignments, each role falls back to the
    # dual-provider default table:
    #   operator_pm / reviewer  -> Opus (Claude)
    #   worker / architect       -> Codex
    #   advisor                  -> Codex
    config = _config(tmp_path)
    config.accounts["codex_main"] = AccountConfig(
        name="codex_main",
        provider=ProviderKind.CODEX,
        home=tmp_path / ".pollypm" / "homes" / "codex_main",
    )

    expectations = {
        "operator_pm": ("opus-4.7", "claude"),
        "reviewer": ("opus-4.7", "claude"),
        "worker": ("codex-gpt-5.4", "codex"),
        "architect": ("codex-gpt-5.4", "codex"),
        "advisor": ("codex-gpt-5.4", "codex"),
    }
    for role, (alias, provider) in expectations.items():
        # operator_pm is global-only — pass project_key=None
        project = None if role == "operator_pm" else "demo"
        resolved = resolve_role_assignment(role, project, config=config)
        assert resolved.alias == alias, f"role {role}: expected {alias}, got {resolved.alias}"
        assert resolved.provider == provider, f"role {role}: expected {provider}, got {resolved.provider}"
        assert resolved.source == "fallback"


def test_codex_only_fallback_routes_all_roles_to_codex(tmp_path: Path) -> None:
    # #1737 — single-provider (Codex only) fallback: every role
    # collapses to the strongest Codex alias because there's no
    # Claude account to route Opus-favoring roles to.
    config = _config(tmp_path)
    # Replace claude account with codex-only configuration.
    config.accounts.clear()
    config.accounts["codex_main"] = AccountConfig(
        name="codex_main",
        provider=ProviderKind.CODEX,
        home=tmp_path / ".pollypm" / "homes" / "codex_main",
    )

    for role in ("operator_pm", "architect", "worker", "reviewer", "advisor"):
        project = None if role == "operator_pm" else "demo"
        resolved = resolve_role_assignment(role, project, config=config)
        assert resolved.provider == "codex"
        assert resolved.alias == "codex-gpt-5.4"
        assert resolved.source == "fallback"


def test_claude_only_fallback_routes_all_roles_to_claude(tmp_path: Path) -> None:
    # #1737 — single-provider (Claude only) fallback: every role
    # routes to Opus.
    config = _config(tmp_path)
    # Default _config already has only a single claude account; no
    # additional setup needed.

    for role in ("operator_pm", "architect", "worker", "reviewer", "advisor"):
        project = None if role == "operator_pm" else "demo"
        resolved = resolve_role_assignment(role, project, config=config)
        assert resolved.provider == "claude"
        assert resolved.alias == "opus-4.7"
        assert resolved.source == "fallback"


def test_unknown_alias_falls_through_to_next_precedence_level(
    tmp_path: Path,
    caplog,
) -> None:
    config = _config(tmp_path)
    config.projects["demo"].role_assignments["architect"] = ModelAssignment(alias="missing")
    config.pollypm.role_assignments["architect"] = ModelAssignment(alias="opus-4.7")

    with caplog.at_level(logging.WARNING, logger="pollypm.role_routing"):
        resolved = resolve_role_assignment("architect", "demo", config=config)

    assert resolved.alias == "opus-4.7"
    assert resolved.source == "global"
    assert any("missing" in record.getMessage() for record in caplog.records)


def test_explicit_pair_bypasses_registry_lookup(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.pollypm.role_assignments["reviewer"] = ModelAssignment(
        provider="claude",
        model="claude-review-custom",
    )

    resolved = resolve_role_assignment(
        "reviewer",
        "demo",
        config=config,
        registry=Registry(),
    )

    assert resolved.alias is None
    assert resolved.provider == "claude"
    assert resolved.model == "claude-review-custom"
    assert resolved.source == "global"


def test_live_inheritance_reflects_global_changes_without_project_copy(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.pollypm.role_assignments["worker"] = ModelAssignment(alias="codex-gpt-5.4")

    first = resolve_role_assignment("worker", "demo", config=config)
    config.pollypm.role_assignments["worker"] = ModelAssignment(alias="sonnet-4.6")
    second = resolve_role_assignment("worker", "demo", config=config)

    assert first.alias == "codex-gpt-5.4"
    assert second.alias == "sonnet-4.6"
    assert second.source == "global"
