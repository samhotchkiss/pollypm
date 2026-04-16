"""Tests for the project_planning plugin scaffold (pp01–pp09).

Covers skeleton registration, agent profiles, flow templates, and gates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.plugin_host import ExtensionHost


# ---------------------------------------------------------------------------
# pp01 — plugin skeleton + six personas
# ---------------------------------------------------------------------------


EXPECTED_PROFILES = (
    "architect",
    "critic_simplicity",
    "critic_maintainability",
    "critic_user",
    "critic_operational",
    "critic_security",
)


def test_project_planning_plugin_loads(tmp_path: Path) -> None:
    host = ExtensionHost(tmp_path)
    plugins = host.plugins()
    assert "project_planning" in plugins

    plugin = plugins["project_planning"]
    names = set(plugin.agent_profiles.keys())
    assert names == set(EXPECTED_PROFILES)

    # All six capabilities declared with kind=agent_profile.
    kinds = {(c.kind, c.name) for c in plugin.capabilities}
    for profile_name in EXPECTED_PROFILES:
        assert ("agent_profile", profile_name) in kinds


def test_project_planning_has_no_load_errors(tmp_path: Path) -> None:
    host = ExtensionHost(tmp_path)
    host.plugins()  # force load
    relevant = [e for e in host.errors if "project_planning" in e]
    assert relevant == []


@pytest.mark.parametrize("profile_name", EXPECTED_PROFILES)
def test_profile_prompt_is_substantive(tmp_path: Path, profile_name: str) -> None:
    host = ExtensionHost(tmp_path)
    profile = host.get_agent_profile(profile_name)
    assert profile.name == profile_name

    # Prompt body is read from the shipped markdown file on each call.
    prompt = profile.build_prompt(context=None)  # MarkdownPromptProfile ignores ctx
    assert prompt is not None
    # Each profile must be > 150 words to enforce the opinionated-persona bar.
    assert len(prompt.split()) >= 150, (
        f"{profile_name} prompt is {len(prompt.split())} words (<150)"
    )


@pytest.mark.parametrize("profile_name", EXPECTED_PROFILES)
def test_profile_file_exists_at_shipped_path(profile_name: str) -> None:
    root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "pollypm"
        / "plugins_builtin"
        / "project_planning"
        / "profiles"
    )
    path = root / f"{profile_name}.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    # Frontmatter is YAML-ish and starts with ---
    assert text.startswith("---\n"), f"{profile_name} missing frontmatter"
    assert "preferred_providers" in text
