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


# ---------------------------------------------------------------------------
# pp03 — gates: wait_for_children, output_present, log_present, user_level_tests_pass
# ---------------------------------------------------------------------------


PLANNER_GATES = (
    "wait_for_children",
    "output_present",
    "log_present",
    "user_level_tests_pass",
)


@pytest.mark.parametrize("gate_name", PLANNER_GATES)
def test_planner_gate_registered(gate_name: str) -> None:
    from pollypm.work.gates import GateRegistry

    reg = GateRegistry()
    gate = reg.get(gate_name)
    assert gate is not None, f"{gate_name} not registered"
    assert gate.gate_type == "hard"


def _make_task(**overrides):
    from pollypm.work.models import Task, TaskType, WorkStatus

    defaults = dict(
        project="demo",
        task_number=1,
        title="t",
        type=TaskType.TASK,
        work_status=WorkStatus.IN_PROGRESS,
        flow_template_id="plan_project",
    )
    defaults.update(overrides)
    return Task(**defaults)


def test_wait_for_children_passes_when_no_children() -> None:
    from pollypm.work.gates import GateRegistry

    gate = GateRegistry().get("wait_for_children")
    task = _make_task(children=[])
    result = gate.check(task)
    assert result.passed is True


def test_wait_for_children_blocks_when_child_in_progress() -> None:
    from pollypm.work.gates import GateRegistry
    from pollypm.work.models import WorkStatus

    gate = GateRegistry().get("wait_for_children")
    parent = _make_task(children=[("demo", 2)])
    child = _make_task(task_number=2, work_status=WorkStatus.IN_PROGRESS)

    def get_task(task_id: str):
        assert task_id == "demo/2"
        return child

    result = gate.check(parent, get_task=get_task)
    assert result.passed is False
    assert "in_progress" in result.reason


def test_wait_for_children_passes_when_all_children_terminal() -> None:
    from pollypm.work.gates import GateRegistry
    from pollypm.work.models import WorkStatus

    gate = GateRegistry().get("wait_for_children")
    parent = _make_task(children=[("demo", 2), ("demo", 3)])
    c1 = _make_task(task_number=2, work_status=WorkStatus.DONE)
    c2 = _make_task(task_number=3, work_status=WorkStatus.CANCELLED)

    def get_task(task_id: str):
        return {"demo/2": c1, "demo/3": c2}[task_id]

    result = gate.check(parent, get_task=get_task)
    assert result.passed is True


def test_output_present_blocks_with_no_executions() -> None:
    from pollypm.work.gates import GateRegistry

    gate = GateRegistry().get("output_present")
    result = gate.check(_make_task())
    assert result.passed is False


def test_output_present_blocks_with_empty_summary() -> None:
    from pollypm.work.gates import GateRegistry
    from pollypm.work.models import (
        Artifact, ArtifactKind, FlowNodeExecution, OutputType, WorkOutput,
    )

    gate = GateRegistry().get("output_present")
    execution = FlowNodeExecution(
        task_id="demo/1",
        node_id="critique",
        visit=1,
        work_output=WorkOutput(
            type=OutputType.DOCUMENT,
            summary="",
            artifacts=[Artifact(kind=ArtifactKind.NOTE, description="x")],
        ),
    )
    task = _make_task(executions=[execution])
    result = gate.check(task)
    assert result.passed is False


def test_output_present_passes_with_structured_output() -> None:
    from pollypm.work.gates import GateRegistry
    from pollypm.work.models import (
        Artifact, ArtifactKind, FlowNodeExecution, OutputType, WorkOutput,
    )

    gate = GateRegistry().get("output_present")
    execution = FlowNodeExecution(
        task_id="demo/1",
        node_id="critique",
        visit=1,
        work_output=WorkOutput(
            type=OutputType.DOCUMENT,
            summary="Simplicity critique",
            artifacts=[Artifact(kind=ArtifactKind.NOTE, description="x")],
        ),
    )
    task = _make_task(executions=[execution])
    result = gate.check(task)
    assert result.passed is True


def test_log_present_blocks_when_log_missing(tmp_path: Path) -> None:
    from pollypm.work.gates import GateRegistry

    gate = GateRegistry().get("log_present")
    result = gate.check(_make_task(), project_root=tmp_path)
    assert result.passed is False


def test_log_present_blocks_when_log_empty(tmp_path: Path) -> None:
    from pollypm.work.gates import GateRegistry

    gate = GateRegistry().get("log_present")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "planning-session-log.md").write_text("   \n")
    result = gate.check(_make_task(), project_root=tmp_path)
    assert result.passed is False


def test_log_present_passes_with_populated_log(tmp_path: Path) -> None:
    from pollypm.work.gates import GateRegistry

    gate = GateRegistry().get("log_present")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "planning-session-log.md").write_text(
        "# Session log\n\nThe architect said..."
    )
    result = gate.check(_make_task(), project_root=tmp_path)
    assert result.passed is True


def test_user_level_tests_pass_blocks_without_receipt(tmp_path: Path) -> None:
    from pollypm.work.gates import GateRegistry

    gate = GateRegistry().get("user_level_tests_pass")
    result = gate.check(_make_task(), project_root=tmp_path)
    assert result.passed is False


def test_user_level_tests_pass_blocks_with_failing_receipt(tmp_path: Path) -> None:
    import json as _json

    from pollypm.work.gates import GateRegistry

    gate = GateRegistry().get("user_level_tests_pass")
    receipts = tmp_path / ".pollypm-state" / "test-receipts"
    receipts.mkdir(parents=True)
    receipts.joinpath("demo-1.json").write_text(
        _json.dumps({"passed": False, "details": "3/5 scenarios failed"})
    )
    result = gate.check(_make_task(), project_root=tmp_path)
    assert result.passed is False
    assert "3/5" in result.reason


def test_user_level_tests_pass_passes_with_receipt(tmp_path: Path) -> None:
    import json as _json

    from pollypm.work.gates import GateRegistry

    gate = GateRegistry().get("user_level_tests_pass")
    receipts = tmp_path / ".pollypm-state" / "test-receipts"
    receipts.mkdir(parents=True)
    receipts.joinpath("demo-1.json").write_text(
        _json.dumps({"passed": True, "details": "Playwright 5/5"})
    )
    result = gate.check(_make_task(), project_root=tmp_path)
    assert result.passed is True


# ---------------------------------------------------------------------------
# pp04 — ReAct research stage (stage 0 of plan_project)
# ---------------------------------------------------------------------------


def test_research_stage_prompt_contains_react_loop() -> None:
    from pollypm.plugins_builtin.project_planning.research_stage import (
        research_stage_prompt,
    )
    text = research_stage_prompt()
    assert "ReAct" in text
    for tool in ("grep", "read", "list_files", "web_search"):
        assert tool in text, f"ReAct prompt missing tool '{tool}'"
    assert "budget" in text.lower()
    assert "docs/planning-context.md" in text


def test_research_stage_prompt_respects_custom_budget() -> None:
    from pollypm.plugins_builtin.project_planning.research_stage import (
        research_stage_prompt,
    )
    text = research_stage_prompt(budget_seconds=900)
    assert "900 seconds" in text
    assert "15 min" in text


def test_research_budget_expires_with_zero_total() -> None:
    from pollypm.plugins_builtin.project_planning.research_stage import (
        ResearchBudget,
    )
    budget = ResearchBudget(total_seconds=0)
    budget.start()
    assert budget.expired() is True


def test_research_budget_reports_time_remaining() -> None:
    from pollypm.plugins_builtin.project_planning.research_stage import (
        ResearchBudget,
    )
    budget = ResearchBudget(total_seconds=600)
    # Before start — full budget.
    assert budget.seconds_remaining() == 600.0
    budget.start()
    assert budget.seconds_remaining() <= 600.0
    assert budget.expired() is False


def test_context_artifact_ready_requires_non_empty_file(tmp_path: Path) -> None:
    from pollypm.plugins_builtin.project_planning.research_stage import (
        context_artifact_ready,
        context_artifact_path,
        write_context_artifact,
    )
    assert context_artifact_ready(tmp_path) is False

    # Writing a non-empty body makes the guard pass.
    path = write_context_artifact(
        tmp_path,
        "# Context\n\nProject: demo\nStack: Python 3.14\n",
    )
    assert path == context_artifact_path(tmp_path)
    assert context_artifact_ready(tmp_path) is True


def test_context_artifact_rejects_empty_body(tmp_path: Path) -> None:
    import pytest

    from pollypm.plugins_builtin.project_planning.research_stage import (
        write_context_artifact,
    )
    with pytest.raises(ValueError):
        write_context_artifact(tmp_path, "   \n")


def test_architect_profile_points_at_research_stage() -> None:
    """The architect persona prompt references the research stage so
    the agent knows where the ReAct loop fits in the overall flow.
    """
    root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "pollypm"
        / "plugins_builtin"
        / "project_planning"
        / "profiles"
        / "architect.md"
    )
    text = root.read_text(encoding="utf-8")
    assert "planning-context.md" in text
    assert "research" in text.lower()


# ---------------------------------------------------------------------------
# pp05 — tree-of-plans: 2-3 candidates, critics evaluate all, synthesis picks winner
# ---------------------------------------------------------------------------


def test_decompose_stage_prompt_enforces_multiple_candidates() -> None:
    from pollypm.plugins_builtin.project_planning.tree_of_plans import (
        decompose_stage_prompt,
    )
    text = decompose_stage_prompt()
    # Must mandate 2-3 candidates and list the per-candidate sections.
    assert "2" in text and "3" in text
    assert "candidate_A" in text or "candidate_<ID>" in text
    for section in ("Thesis", "Modules", "Tradeoffs", "Sequencing"):
        assert section in text


def test_candidate_artifact_path_limits_to_abc(tmp_path: Path) -> None:
    import pytest

    from pollypm.plugins_builtin.project_planning.tree_of_plans import (
        candidate_artifact_path,
    )
    for cid in ("A", "B", "C"):
        path = candidate_artifact_path(tmp_path, cid)
        assert path.name == f"candidate_{cid}.md"
    with pytest.raises(ValueError):
        candidate_artifact_path(tmp_path, "D")
    with pytest.raises(ValueError):
        candidate_artifact_path(tmp_path, "a")


def test_critic_panel_prompt_requires_per_candidate_scores() -> None:
    from pollypm.plugins_builtin.project_planning.tree_of_plans import (
        critic_panel_prompt,
    )
    text = critic_panel_prompt()
    assert "EVERY candidate" in text
    assert "preferred_candidate" in text
    assert "objections_for_risk_ledger" in text
    assert "output_present" in text


def test_critic_verdict_from_payload() -> None:
    from pollypm.plugins_builtin.project_planning.tree_of_plans import (
        CriticVerdict,
    )
    payload = {
        "candidates": [
            {"id": "A", "score": 8, "verdict": "approve"},
            {"id": "B", "score": 6, "verdict": "approve_with_changes"},
        ],
        "preferred_candidate": "A",
        "objections_for_risk_ledger": [
            "Module FooRegistry is premature plugin boundary",
        ],
    }
    verdict = CriticVerdict.from_payload("critic_simplicity", payload)
    assert verdict.candidate_scores == {"A": 8.0, "B": 6.0}
    assert verdict.preferred_candidate == "A"
    assert len(verdict.objections) == 1


def test_critic_verdict_handles_tie_preference() -> None:
    from pollypm.plugins_builtin.project_planning.tree_of_plans import (
        CriticVerdict,
    )
    verdict = CriticVerdict.from_payload(
        "critic_user",
        {
            "candidates": [
                {"id": "A", "score": 7},
                {"id": "B", "score": 7},
            ],
            "preferred_candidate": "tie:A,B",
        },
    )
    # Tie preference is preserved as a string so the session log can
    # narrate it; synthesis ignores ties for the vote count.
    assert verdict.preferred_candidate == "tie:A,B"


def test_synthesize_picks_highest_average_score() -> None:
    from pollypm.plugins_builtin.project_planning.tree_of_plans import (
        CriticVerdict, synthesize,
    )
    verdicts = [
        CriticVerdict(
            critic_name="critic_simplicity",
            candidate_scores={"A": 9, "B": 5},
            preferred_candidate="A",
            objections=["B: over-engineered registry"],
        ),
        CriticVerdict(
            critic_name="critic_maintainability",
            candidate_scores={"A": 8, "B": 6},
            preferred_candidate="A",
            objections=["B: hidden coupling in shared config"],
        ),
    ]
    result = synthesize(verdicts)
    assert result.winner == "A"
    assert result.average_scores == {"A": 8.5, "B": 5.5}
    assert result.preferred_votes["A"] == 2
    assert len(result.risk_ledger_seeds) == 2
    assert "critic_simplicity" in result.risk_ledger_seeds[0]


def test_synthesize_breaks_score_ties_by_preferred_votes() -> None:
    from pollypm.plugins_builtin.project_planning.tree_of_plans import (
        CriticVerdict, synthesize,
    )
    verdicts = [
        CriticVerdict(
            critic_name="c1",
            candidate_scores={"A": 7, "B": 7},
            preferred_candidate="B",
        ),
        CriticVerdict(
            critic_name="c2",
            candidate_scores={"A": 7, "B": 7},
            preferred_candidate="B",
        ),
        CriticVerdict(
            critic_name="c3",
            candidate_scores={"A": 7, "B": 7},
            preferred_candidate="A",
        ),
    ]
    result = synthesize(verdicts)
    # Same scores, but B has 2 preferred votes vs A's 1.
    assert result.winner == "B"


def test_synthesize_rejects_single_candidate() -> None:
    import pytest

    from pollypm.plugins_builtin.project_planning.tree_of_plans import (
        CriticVerdict, synthesize,
    )
    verdicts = [
        CriticVerdict(
            critic_name="c1",
            candidate_scores={"A": 7},
            preferred_candidate="A",
        ),
    ]
    with pytest.raises(ValueError):
        synthesize(verdicts)


def test_synthesize_rationale_included_in_result() -> None:
    from pollypm.plugins_builtin.project_planning.tree_of_plans import (
        CriticVerdict, synthesize,
    )
    verdicts = [
        CriticVerdict(
            critic_name="c1",
            candidate_scores={"A": 8, "B": 5},
            preferred_candidate="A",
        ),
        CriticVerdict(
            critic_name="c2",
            candidate_scores={"A": 7, "B": 6},
            preferred_candidate="A",
        ),
    ]
    result = synthesize(verdicts)
    assert "Selected candidate A" in result.rationale
    assert "Average scores" in result.rationale
    assert "A:" in result.rationale and "B:" in result.rationale


# ---------------------------------------------------------------------------
# pp06 — critic panel provisioning + diversity resolver
# ---------------------------------------------------------------------------


def test_critic_panel_single_provider_all_same() -> None:
    from pollypm.plugins_builtin.project_planning.critic_panel import (
        resolve_critic_providers,
    )
    result = resolve_critic_providers(
        registered_providers=["claude"],
        planner_provider="claude",
    )
    assert set(result.assignments.values()) == {"claude"}
    assert result.forced_cross_provider is None


def test_critic_panel_two_providers_forces_diversity() -> None:
    from pollypm.plugins_builtin.project_planning.critic_panel import (
        resolve_critic_providers,
    )
    result = resolve_critic_providers(
        registered_providers=["claude", "codex"],
        planner_provider="claude",
    )
    # Default diversity target is critic_simplicity.
    assert result.forced_cross_provider == "critic_simplicity"
    assert result.assignments["critic_simplicity"] == "codex"
    # At least one critic is on non-planner provider.
    non_planner_critics = [
        name for name, prov in result.assignments.items() if prov != "claude"
    ]
    assert len(non_planner_critics) >= 1
    # Other critics default to the planner's provider.
    for name in (
        "critic_maintainability", "critic_user", "critic_operational",
        "critic_security",
    ):
        assert result.assignments[name] == "claude"


def test_critic_panel_user_override_takes_absolute_precedence() -> None:
    from pollypm.plugins_builtin.project_planning.critic_panel import (
        resolve_critic_providers,
    )
    result = resolve_critic_providers(
        registered_providers=["claude", "codex"],
        planner_provider="claude",
        user_overrides={"critic_security": "codex"},
    )
    assert result.assignments["critic_security"] == "codex"
    # Since critic_security already satisfies cross-provider diversity,
    # critic_simplicity should stay on the planner's provider.
    assert result.assignments["critic_simplicity"] == "claude"
    assert result.forced_cross_provider is None


def test_critic_panel_override_on_default_target_falls_through() -> None:
    """User pinned critic_simplicity to claude — resolver must force
    diversity onto a different critic."""
    from pollypm.plugins_builtin.project_planning.critic_panel import (
        resolve_critic_providers,
    )
    result = resolve_critic_providers(
        registered_providers=["claude", "codex"],
        planner_provider="claude",
        user_overrides={"critic_simplicity": "claude"},
    )
    assert result.assignments["critic_simplicity"] == "claude"
    assert result.forced_cross_provider is not None
    assert result.forced_cross_provider != "critic_simplicity"
    assert result.assignments[result.forced_cross_provider] == "codex"


def test_critic_panel_unknown_critic_override_ignored() -> None:
    from pollypm.plugins_builtin.project_planning.critic_panel import (
        resolve_critic_providers,
    )
    result = resolve_critic_providers(
        registered_providers=["claude", "codex"],
        planner_provider="claude",
        user_overrides={"critic_bogus": "codex"},
    )
    assert "critic_bogus" not in result.assignments
    assert any("unknown critic" in n for n in result.notes)


def test_critic_panel_unknown_provider_override_ignored() -> None:
    from pollypm.plugins_builtin.project_planning.critic_panel import (
        resolve_critic_providers,
    )
    result = resolve_critic_providers(
        registered_providers=["claude", "codex"],
        planner_provider="claude",
        user_overrides={"critic_user": "gemini"},
    )
    # Unknown provider override is ignored; critic_user falls back to
    # planner's provider.
    assert result.assignments["critic_user"] == "claude"
    assert any("provider not registered" in n for n in result.notes)


def test_critic_panel_rejects_unknown_planner_provider() -> None:
    import pytest

    from pollypm.plugins_builtin.project_planning.critic_panel import (
        resolve_critic_providers,
    )
    with pytest.raises(ValueError):
        resolve_critic_providers(
            registered_providers=["claude"],
            planner_provider="codex",
        )


def test_critic_panel_rejects_empty_provider_list() -> None:
    import pytest

    from pollypm.plugins_builtin.project_planning.critic_panel import (
        resolve_critic_providers,
    )
    with pytest.raises(ValueError):
        resolve_critic_providers(
            registered_providers=[],
            planner_provider="claude",
        )


def test_critic_panel_all_five_critics_assigned() -> None:
    from pollypm.plugins_builtin.project_planning.critic_panel import (
        CRITIC_NAMES, resolve_critic_providers,
    )
    result = resolve_critic_providers(
        registered_providers=["claude", "codex"],
        planner_provider="claude",
    )
    assert set(result.assignments.keys()) == set(CRITIC_NAMES)
    assert len(CRITIC_NAMES) == 5


# ---------------------------------------------------------------------------
# pp07 — per-stage time budgets via flow-engine node schema
# ---------------------------------------------------------------------------


def test_plan_project_nodes_have_default_budgets() -> None:
    template = resolve_flow("plan_project")
    # Every "work" node that has a spec-§6 default should carry a
    # budget_seconds value on the YAML.
    for stage in ("research", "discover", "decompose", "test_strategy",
                  "magic", "synthesize"):
        assert template.nodes[stage].budget_seconds is not None, (
            f"{stage} missing budget_seconds"
        )
    # Terminal + user_approval + emit intentionally have no budgets.
    assert template.nodes["user_approval"].budget_seconds is None
    assert template.nodes["emit"].budget_seconds is None
    assert template.nodes["done"].budget_seconds is None


def test_critique_flow_has_per_critic_budget() -> None:
    template = resolve_flow("critique_flow")
    assert template.nodes["critique"].budget_seconds == 300


def test_effective_budget_defaults_when_no_config() -> None:
    from pollypm.plugins_builtin.project_planning.budgets import (
        DEFAULT_BUDGETS, effective_budget,
    )
    assert effective_budget("research") == DEFAULT_BUDGETS["research"]
    assert effective_budget("critic") == DEFAULT_BUDGETS["critic"]
    # Unknown stage with no node default → None.
    assert effective_budget("bogus") is None


def test_effective_budget_honours_node_default() -> None:
    from pollypm.plugins_builtin.project_planning.budgets import (
        effective_budget,
    )
    # Node default overrides DEFAULT_BUDGETS when config is absent.
    assert effective_budget("magic", node_default=1200) == 1200
    # …but only when positive.
    assert effective_budget("magic", node_default=0) == 600  # falls to default


def test_effective_budget_config_override_lifts_cap() -> None:
    """Acceptance gate for pp07: overriding [planner.budgets].decompose
    in pollypm.toml lifts the cap."""
    from pollypm.plugins_builtin.project_planning.budgets import (
        effective_budget,
    )
    config = {"planner": {"budgets": {"decompose": 900}}}
    assert effective_budget("decompose", config=config) == 900
    # Other stages still use their defaults.
    assert effective_budget("research", config=config) == 600


def test_effective_budget_config_overrides_node_default() -> None:
    from pollypm.plugins_builtin.project_planning.budgets import (
        effective_budget,
    )
    config = {"planner": {"budgets": {"magic": 1800}}}
    # Config beats node default.
    assert effective_budget("magic", config=config, node_default=1200) == 1800


def test_all_effective_budgets_merges_config_and_defaults() -> None:
    from pollypm.plugins_builtin.project_planning.budgets import (
        DEFAULT_BUDGETS, all_effective_budgets,
    )
    config = {"planner": {"budgets": {"decompose": 900}}}
    snapshot = all_effective_budgets(config=config)
    assert snapshot["decompose"] == 900
    # Other stages unchanged.
    assert snapshot["research"] == DEFAULT_BUDGETS["research"]


def test_flow_engine_rejects_non_positive_budget(tmp_path: Path) -> None:
    import pytest

    from pollypm.work.flow_engine import FlowValidationError, parse_flow_yaml

    text = """
name: bad
description: bad budget
nodes:
  work:
    type: work
    actor_type: human
    next_node: done
    budget_seconds: -5
  done:
    type: terminal
start_node: work
"""
    with pytest.raises(FlowValidationError):
        parse_flow_yaml(text)


# ---------------------------------------------------------------------------
# pp08 — present-plan-to-user approval gate
# ---------------------------------------------------------------------------


def _populate_plan_artifacts(root: Path, *, with_ledger_section: bool = True) -> None:
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    plan_body = "# Project Plan\n\nModules: Foo, Bar, Baz.\n"
    if with_ledger_section:
        plan_body += "\n## Risk Ledger\n\n| risk | category | mitigation | raised-by | status |\n"
    (docs / "project-plan.md").write_text(plan_body)
    (docs / "planning-session-log.md").write_text(
        "# Session log\n\nThe architect said..."
    )


def test_approval_readiness_detects_missing_plan(tmp_path: Path) -> None:
    from pollypm.plugins_builtin.project_planning.approval import (
        check_plan_ready_for_user,
    )
    result = check_plan_ready_for_user(tmp_path)
    assert result.ready is False
    assert any("project-plan.md" in item for item in result.missing)


def test_approval_readiness_passes_with_all_artifacts(tmp_path: Path) -> None:
    from pollypm.plugins_builtin.project_planning.approval import (
        check_plan_ready_for_user,
    )
    _populate_plan_artifacts(tmp_path)
    result = check_plan_ready_for_user(tmp_path)
    assert result.ready is True
    assert result.missing == []


def test_approval_readiness_allows_sibling_risk_ledger(tmp_path: Path) -> None:
    from pollypm.plugins_builtin.project_planning.approval import (
        check_plan_ready_for_user,
    )
    _populate_plan_artifacts(tmp_path, with_ledger_section=False)
    # Sibling Risk Ledger file is also accepted.
    (tmp_path / "docs" / "project-plan-risk-ledger.md").write_text(
        "| risk | mitigation |\n| --- | --- |\n| R1 | M1 |\n"
    )
    result = check_plan_ready_for_user(tmp_path)
    assert result.ready is True


def test_approval_readiness_missing_risk_ledger(tmp_path: Path) -> None:
    from pollypm.plugins_builtin.project_planning.approval import (
        check_plan_ready_for_user,
    )
    _populate_plan_artifacts(tmp_path, with_ledger_section=False)
    result = check_plan_ready_for_user(tmp_path)
    assert result.ready is False
    assert any("Risk Ledger" in m for m in result.missing)


def test_record_approval_appends_to_log(tmp_path: Path) -> None:
    from pollypm.plugins_builtin.project_planning.approval import (
        record_approval,
    )
    log_path = tmp_path / "docs" / "planning-session-log.md"
    log_path.parent.mkdir()
    log_path.write_text("# Session log\n\nPrior entries.\n")
    record_approval(tmp_path, actor="alice", note="please ship Foo first")
    text = log_path.read_text()
    assert "Stage 7 approval received from alice" in text
    assert "please ship Foo first" in text
    # Prior content preserved.
    assert "Prior entries." in text


def test_record_rejection_captures_reason(tmp_path: Path) -> None:
    from pollypm.plugins_builtin.project_planning.approval import (
        record_rejection,
    )
    log_path = tmp_path / "docs" / "planning-session-log.md"
    log_path.parent.mkdir()
    log_path.write_text("# Session log\n")
    record_rejection(tmp_path, actor="alice", reason="scope is too big")
    text = log_path.read_text()
    assert "Stage 7 rejection from alice" in text
    assert "scope is too big" in text


def test_record_rejection_handles_empty_reason(tmp_path: Path) -> None:
    from pollypm.plugins_builtin.project_planning.approval import (
        record_rejection,
    )
    log_path = tmp_path / "docs" / "planning-session-log.md"
    log_path.parent.mkdir()
    log_path.write_text("# Session log\n")
    record_rejection(tmp_path, reason="   ")
    text = log_path.read_text()
    assert "no reason supplied" in text


def test_user_approval_node_waits_indefinitely() -> None:
    """pp08 acceptance: stage 7 has no budget (waits indefinitely)."""
    template = resolve_flow("plan_project")
    node = template.nodes["user_approval"]
    assert node.actor_type == ActorType.HUMAN
    assert node.budget_seconds is None
    assert node.next_node_id == "emit"
    assert node.reject_node_id == "synthesize"
