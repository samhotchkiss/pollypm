from __future__ import annotations

from importlib import resources
import logging
from pathlib import Path

from pollypm.model_registry import (
    AliasRecord,
    Registry,
    RoleRequirements,
    advisories_for,
    load_registry,
    resolve_alias,
)
from pollypm.models import ModelAssignment


def test_shipped_registry_includes_minimum_aliases() -> None:
    registry = load_registry(overlay_path=Path("/tmp/does-not-exist"))

    assert {
        "opus-4.7",
        "sonnet-4.6",
        "haiku-4.5",
        "codex-gpt-5.4",
    }.issubset(registry.aliases)
    shipped_text = resources.files("pollypm").joinpath("model_registry.toml").read_text(
        encoding="utf-8"
    )
    assert "opus-4.7" in shipped_text


def test_overlay_merge_adds_and_overrides_aliases(tmp_path: Path) -> None:
    overlay_path = tmp_path / "model_registry.toml"
    overlay_path.write_text(
        """
[aliases.opus-4.7]
provider = "claude"
model = "claude-opus-4-7-hotfix"
capabilities = ["reasoning"]

[aliases.custom-codex]
provider = "codex"
model = "gpt-5.4-mini"
capabilities = ["tool_use"]
"""
    )

    registry = load_registry(overlay_path=overlay_path)

    assert registry.aliases["custom-codex"].model == "gpt-5.4-mini"
    assert registry.aliases["opus-4.7"].model == "claude-opus-4-7-hotfix"


def test_resolve_alias_returns_assignment_and_miss_none() -> None:
    registry = load_registry(overlay_path=Path("/tmp/does-not-exist"))

    assert resolve_alias("codex-gpt-5.4", registry=registry) == ModelAssignment(
        provider="codex",
        model="gpt-5.4",
    )
    assert resolve_alias("missing-alias", registry=registry) is None


def test_advisories_cover_preferred_discouraged_and_unknown_capabilities() -> None:
    registry = Registry(
        aliases={
            "good": AliasRecord(
                provider="claude",
                model="good-model",
                capabilities=("strong_planning", "reasoning", "experimental"),
            ),
            "bad": AliasRecord(
                provider="claude",
                model="bad-model",
                capabilities=("weak_planning",),
            ),
        },
        role_requirements={
            "architect": RoleRequirements(
                preferred=("strong_planning", "reasoning"),
                discouraged=("weak_planning",),
            )
        },
    )

    assert advisories_for("architect", ModelAssignment(alias="good"), registry=registry) == []
    bad = advisories_for("architect", ModelAssignment(alias="bad"), registry=registry)
    assert bad
    assert "weak_planning" in bad[0]
    assert advisories_for(
        "architect",
        ModelAssignment(provider="claude", model="good-model"),
        registry=registry,
    ) == []


def test_malformed_registry_tolerates_bad_shipped_and_overlay(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    overlay_path = tmp_path / "model_registry.toml"
    overlay_path.write_text("[aliases.bad")
    monkeypatch.setattr(
        "pollypm.model_registry._read_shipped_registry_text",
        lambda: "[aliases.broken",
    )

    with caplog.at_level(logging.WARNING, logger="pollypm.model_registry"):
        registry = load_registry(overlay_path=overlay_path)

    assert "opus-4.7" in registry.aliases
    messages = [record.getMessage() for record in caplog.records]
    assert any("shipped model registry" in message for message in messages)
    assert any(str(overlay_path) in message for message in messages)
