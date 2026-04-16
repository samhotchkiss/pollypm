"""Tests for the project_planning plugin scaffold (pp01–pp09).

Covers skeleton registration, agent profiles, flow templates, and gates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.plugin_host import ExtensionHost
from pollypm.work.flow_engine import (
    available_flows,
    resolve_flow,
    validate_flow,
)
from pollypm.work.models import ActorType, NodeType


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


# ---------------------------------------------------------------------------
# pp02 — flow templates: plan_project, critique_flow, implement_module
# ---------------------------------------------------------------------------


PLANNER_FLOWS = ("plan_project", "critique_flow", "implement_module")


@pytest.mark.parametrize("flow_name", PLANNER_FLOWS)
def test_planner_flow_resolves_and_validates(flow_name: str) -> None:
    template = resolve_flow(flow_name)
    # resolve_flow already calls validate_flow; an explicit re-check
    # catches any regression where that contract changes.
    validate_flow(template)
    assert template.name == flow_name
    assert template.start_node in template.nodes
    # Every flow has at least one terminal node.
    terminals = [n for n in template.nodes.values() if n.type == NodeType.TERMINAL]
    assert terminals, f"{flow_name} has no terminal node"


def test_planner_flows_listed_in_available() -> None:
    flows = available_flows()
    for flow_name in PLANNER_FLOWS:
        assert flow_name in flows, f"{flow_name} missing from available_flows()"


def test_plan_project_has_nine_active_stages() -> None:
    """The plan_project flow follows the 9-stage spec (§3):
    research, discover, decompose, test_strategy, magic, critic_panel,
    synthesize, user_approval, emit, + done terminal = 10 nodes.
    """
    template = resolve_flow("plan_project")
    expected_stage_names = {
        "research", "discover", "decompose", "test_strategy", "magic",
        "critic_panel", "synthesize", "user_approval", "emit", "done",
    }
    assert set(template.nodes.keys()) == expected_stage_names
    assert template.start_node == "research"


def test_plan_project_user_approval_is_human_review() -> None:
    template = resolve_flow("plan_project")
    node = template.nodes["user_approval"]
    assert node.type == NodeType.REVIEW
    assert node.actor_type == ActorType.HUMAN
    # Rejection sends the architect back to synthesize (fold in user feedback).
    assert node.reject_node_id == "synthesize"


def test_plan_project_synthesize_requires_log_present() -> None:
    template = resolve_flow("plan_project")
    assert "log_present" in template.nodes["synthesize"].gates


def test_plan_project_critic_panel_waits_for_children() -> None:
    template = resolve_flow("plan_project")
    assert "wait_for_children" in template.nodes["critic_panel"].gates


def test_critique_flow_has_output_present_gate() -> None:
    template = resolve_flow("critique_flow")
    node = template.nodes["critique"]
    assert node.actor_type == ActorType.ROLE
    # actor_role=critic — generic so the panel can assign any critic persona.
    assert node.actor_role == "critic"
    assert "output_present" in node.gates


def test_implement_module_review_enforces_user_level_tests() -> None:
    template = resolve_flow("implement_module")
    review = template.nodes["code_review"]
    assert review.type == NodeType.REVIEW
    assert "user_level_tests_pass" in review.gates


def test_task_create_with_plan_project_flow_succeeds(tmp_path: Path) -> None:
    """Acceptance gate for pp02: a task can be created with --flow plan_project."""
    from pollypm.work.mock_service import MockWorkService

    svc = MockWorkService(project_path=tmp_path)
    task = svc.create(
        title="Plan my new project",
        description="Decompose the new project into modules.",
        type="task",
        project="demo",
        flow_template="plan_project",
        roles={"architect": "architect"},
        priority="normal",
    )
    assert task.flow_template_id == "plan_project"
    # Draft tasks do not yet set current_node_id (that's set on queue/claim);
    # the create succeeding is itself the pp02 acceptance gate.
