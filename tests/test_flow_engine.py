"""Tests for the flow engine — YAML loading, validation, and override chain."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from pollypm.work.flow_engine import (
    FlowValidationError,
    available_flows,
    parse_flow_yaml,
    resolve_flow,
)
from pollypm.work.models import ActorType, NodeType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_STANDARD = textwrap.dedent("""\
    name: standard
    description: Default work flow

    roles:
      worker:
        description: Implements the work
      reviewer:
        description: Reviews and approves

    nodes:
      implement:
        type: work
        actor_type: role
        actor_role: worker
        next_node: code_review
        gates: [has_assignee]

      code_review:
        type: review
        actor_type: role
        actor_role: reviewer
        next_node: done
        reject_node: implement
        gates: [has_work_output]

      done:
        type: terminal

    start_node: implement
""")


# ---------------------------------------------------------------------------
# Parsing: valid flows
# ---------------------------------------------------------------------------


class TestParseValidFlow:
    def test_standard_flow_parses(self):
        flow = parse_flow_yaml(VALID_STANDARD)
        assert flow.name == "standard"
        assert flow.description == "Default work flow"
        assert "worker" in flow.roles
        assert "reviewer" in flow.roles
        assert len(flow.nodes) == 3
        assert flow.start_node == "implement"

    def test_node_types(self):
        flow = parse_flow_yaml(VALID_STANDARD)
        assert flow.nodes["implement"].type == NodeType.WORK
        assert flow.nodes["code_review"].type == NodeType.REVIEW
        assert flow.nodes["done"].type == NodeType.TERMINAL

    def test_node_actor_types(self):
        flow = parse_flow_yaml(VALID_STANDARD)
        assert flow.nodes["implement"].actor_type == ActorType.ROLE
        assert flow.nodes["implement"].actor_role == "worker"
        assert flow.nodes["done"].actor_type is None

    def test_node_edges(self):
        flow = parse_flow_yaml(VALID_STANDARD)
        assert flow.nodes["implement"].next_node_id == "code_review"
        assert flow.nodes["implement"].reject_node_id is None
        assert flow.nodes["code_review"].next_node_id == "done"
        assert flow.nodes["code_review"].reject_node_id == "implement"
        assert flow.nodes["done"].next_node_id is None

    def test_node_gates(self):
        flow = parse_flow_yaml(VALID_STANDARD)
        assert flow.nodes["implement"].gates == ["has_assignee"]
        assert flow.nodes["code_review"].gates == ["has_work_output"]
        assert flow.nodes["done"].gates == []

    def test_version_defaults_to_1(self):
        flow = parse_flow_yaml(VALID_STANDARD)
        assert flow.version == 1

    def test_human_actor_type(self):
        yaml_text = textwrap.dedent("""\
            name: human-review
            description: Human review flow

            roles:
              worker:
                description: Implements the work

            nodes:
              implement:
                type: work
                actor_type: role
                actor_role: worker
                next_node: human_review

              human_review:
                type: review
                actor_type: human
                next_node: done
                reject_node: implement

              done:
                type: terminal

            start_node: implement
        """)
        flow = parse_flow_yaml(yaml_text)
        assert flow.nodes["human_review"].actor_type == ActorType.HUMAN


# ---------------------------------------------------------------------------
# Validation: each rule gets a failing test
# ---------------------------------------------------------------------------


class TestValidationInvalidNextNode:
    def test_next_node_references_nonexistent_node(self):
        yaml_text = textwrap.dedent("""\
            name: bad-next
            description: bad
            roles:
              worker:
                description: w
            nodes:
              implement:
                type: work
                actor_type: role
                actor_role: worker
                next_node: nonexistent
              done:
                type: terminal
            start_node: implement
        """)
        with pytest.raises(FlowValidationError, match="next_node 'nonexistent' does not exist"):
            parse_flow_yaml(yaml_text)


class TestValidationInvalidRejectNode:
    def test_reject_node_references_nonexistent_node(self):
        yaml_text = textwrap.dedent("""\
            name: bad-reject
            description: bad
            roles:
              worker:
                description: w
              reviewer:
                description: r
            nodes:
              implement:
                type: work
                actor_type: role
                actor_role: worker
                next_node: review
              review:
                type: review
                actor_type: role
                actor_role: reviewer
                next_node: done
                reject_node: nonexistent
              done:
                type: terminal
            start_node: implement
        """)
        with pytest.raises(FlowValidationError, match="reject_node 'nonexistent' does not exist"):
            parse_flow_yaml(yaml_text)


class TestValidationRejectOnNonReview:
    def test_reject_node_on_work_node(self):
        yaml_text = textwrap.dedent("""\
            name: bad-reject-on-work
            description: bad
            roles:
              worker:
                description: w
            nodes:
              implement:
                type: work
                actor_type: role
                actor_role: worker
                next_node: done
                reject_node: done
              done:
                type: terminal
            start_node: implement
        """)
        with pytest.raises(FlowValidationError, match="only review nodes may have a reject_node"):
            parse_flow_yaml(yaml_text)


class TestValidationReviewWithoutReject:
    def test_review_node_missing_reject_node(self):
        yaml_text = textwrap.dedent("""\
            name: review-no-reject
            description: bad
            roles:
              worker:
                description: w
              reviewer:
                description: r
            nodes:
              implement:
                type: work
                actor_type: role
                actor_role: worker
                next_node: review
              review:
                type: review
                actor_type: role
                actor_role: reviewer
                next_node: done
              done:
                type: terminal
            start_node: implement
        """)
        with pytest.raises(FlowValidationError, match="review nodes must have a reject_node"):
            parse_flow_yaml(yaml_text)


class TestValidationStartNodeMissing:
    def test_no_start_node(self):
        yaml_text = textwrap.dedent("""\
            name: no-start
            description: bad
            roles: {}
            nodes:
              done:
                type: terminal
        """)
        with pytest.raises(FlowValidationError, match="must define a 'start_node'"):
            parse_flow_yaml(yaml_text)

    def test_start_node_references_nonexistent(self):
        yaml_text = textwrap.dedent("""\
            name: bad-start
            description: bad
            roles: {}
            nodes:
              done:
                type: terminal
            start_node: nonexistent
        """)
        with pytest.raises(FlowValidationError, match="start_node 'nonexistent' does not exist"):
            parse_flow_yaml(yaml_text)


class TestValidationNoTerminalNode:
    def test_no_terminal_node(self):
        yaml_text = textwrap.dedent("""\
            name: no-terminal
            description: bad
            roles:
              worker:
                description: w
            nodes:
              implement:
                type: work
                actor_type: role
                actor_role: worker
            start_node: implement
        """)
        with pytest.raises(FlowValidationError, match="at least one terminal node"):
            parse_flow_yaml(yaml_text)


class TestValidationOrphanNodes:
    def test_orphan_node_not_reachable(self):
        yaml_text = textwrap.dedent("""\
            name: orphan
            description: bad
            roles:
              worker:
                description: w
            nodes:
              implement:
                type: work
                actor_type: role
                actor_role: worker
                next_node: done
              orphan:
                type: work
                actor_type: role
                actor_role: worker
                next_node: done
              done:
                type: terminal
            start_node: implement
        """)
        with pytest.raises(FlowValidationError, match="Orphan nodes.*orphan"):
            parse_flow_yaml(yaml_text)


class TestValidationRoleNotInRoles:
    def test_actor_role_not_defined(self):
        yaml_text = textwrap.dedent("""\
            name: bad-role
            description: bad
            roles:
              worker:
                description: w
            nodes:
              implement:
                type: work
                actor_type: role
                actor_role: missing_role
                next_node: done
              done:
                type: terminal
            start_node: implement
        """)
        with pytest.raises(FlowValidationError, match="actor_role 'missing_role' not found in flow roles"):
            parse_flow_yaml(yaml_text)

    def test_role_actor_type_without_actor_role(self):
        yaml_text = textwrap.dedent("""\
            name: no-actor-role
            description: bad
            roles:
              worker:
                description: w
            nodes:
              implement:
                type: work
                actor_type: role
                next_node: done
              done:
                type: terminal
            start_node: implement
        """)
        with pytest.raises(FlowValidationError, match="no actor_role specified"):
            parse_flow_yaml(yaml_text)


class TestValidationInvalidNodeType:
    def test_invalid_node_type(self):
        yaml_text = textwrap.dedent("""\
            name: bad-type
            description: bad
            roles: {}
            nodes:
              implement:
                type: bogus
            start_node: implement
        """)
        with pytest.raises(FlowValidationError, match="invalid type 'bogus'"):
            parse_flow_yaml(yaml_text)


class TestValidationInvalidActorType:
    def test_invalid_actor_type(self):
        yaml_text = textwrap.dedent("""\
            name: bad-actor
            description: bad
            roles: {}
            nodes:
              implement:
                type: work
                actor_type: bogus
                next_node: done
              done:
                type: terminal
            start_node: implement
        """)
        with pytest.raises(FlowValidationError, match="invalid actor_type 'bogus'"):
            parse_flow_yaml(yaml_text)


class TestValidationMalformedYAML:
    def test_top_level_not_a_mapping(self):
        with pytest.raises(FlowValidationError, match="must be a mapping"):
            parse_flow_yaml("- just a list")

    def test_missing_name(self):
        yaml_text = textwrap.dedent("""\
            description: no name
            roles: {}
            nodes:
              done:
                type: terminal
            start_node: done
        """)
        with pytest.raises(FlowValidationError, match="must have a 'name' field"):
            parse_flow_yaml(yaml_text)


# ---------------------------------------------------------------------------
# Built-in flows all load and validate
# ---------------------------------------------------------------------------


class TestBuiltinFlows:
    @pytest.mark.parametrize("flow_name", ["standard", "spike", "user-review", "bug"])
    def test_builtin_flow_loads(self, flow_name: str):
        flow = resolve_flow(flow_name)
        assert flow.name == flow_name

    def test_standard_structure(self):
        flow = resolve_flow("standard")
        assert set(flow.nodes.keys()) == {"implement", "code_review", "done"}
        assert flow.start_node == "implement"

    def test_spike_structure(self):
        flow = resolve_flow("spike")
        assert set(flow.nodes.keys()) == {"research", "done"}
        assert flow.start_node == "research"

    def test_user_review_structure(self):
        flow = resolve_flow("user-review")
        assert set(flow.nodes.keys()) == {"implement", "human_review", "done"}
        assert flow.nodes["human_review"].actor_type == ActorType.HUMAN

    def test_bug_structure(self):
        flow = resolve_flow("bug")
        assert set(flow.nodes.keys()) == {"reproduce", "fix", "code_review", "done"}
        assert flow.start_node == "reproduce"


# ---------------------------------------------------------------------------
# Override chain resolution
# ---------------------------------------------------------------------------


class TestOverrideChain:
    def test_builtin_used_when_no_overrides(self, tmp_path: Path):
        """Built-in flow loads when project and user dirs don't exist."""
        flow = resolve_flow("standard", project_path=tmp_path / "noproject")
        assert flow.name == "standard"

    def test_project_overrides_builtin(self, tmp_path: Path):
        """Project-local flow takes precedence over built-in."""
        proj_flows = tmp_path / ".pollypm" / "flows"
        proj_flows.mkdir(parents=True)
        (proj_flows / "standard.yaml").write_text(textwrap.dedent("""\
            name: standard
            description: Project-local override

            roles:
              worker:
                description: w

            nodes:
              custom_step:
                type: work
                actor_type: role
                actor_role: worker
                next_node: done

              done:
                type: terminal

            start_node: custom_step
        """))
        flow = resolve_flow("standard", project_path=tmp_path)
        assert flow.description == "Project-local override"
        assert "custom_step" in flow.nodes

    def test_user_global_overrides_builtin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """User-global flow takes precedence over built-in."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        user_flows = fake_home / ".pollypm" / "flows"
        user_flows.mkdir(parents=True)
        (user_flows / "standard.yaml").write_text(textwrap.dedent("""\
            name: standard
            description: User-global override

            roles:
              worker:
                description: w

            nodes:
              user_step:
                type: work
                actor_type: role
                actor_role: worker
                next_node: done

              done:
                type: terminal

            start_node: user_step
        """))
        flow = resolve_flow("standard")
        assert flow.description == "User-global override"
        assert "user_step" in flow.nodes

    def test_project_overrides_user_global(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Project-local takes precedence over user-global."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        user_flows = fake_home / ".pollypm" / "flows"
        user_flows.mkdir(parents=True)
        (user_flows / "standard.yaml").write_text(textwrap.dedent("""\
            name: standard
            description: User-global

            roles:
              worker:
                description: w
            nodes:
              user_step:
                type: work
                actor_type: role
                actor_role: worker
                next_node: done
              done:
                type: terminal
            start_node: user_step
        """))

        proj_flows = tmp_path / "project" / ".pollypm" / "flows"
        proj_flows.mkdir(parents=True)
        (proj_flows / "standard.yaml").write_text(textwrap.dedent("""\
            name: standard
            description: Project-local wins

            roles:
              worker:
                description: w
            nodes:
              proj_step:
                type: work
                actor_type: role
                actor_role: worker
                next_node: done
              done:
                type: terminal
            start_node: proj_step
        """))

        flow = resolve_flow("standard", project_path=tmp_path / "project")
        assert flow.description == "Project-local wins"

    def test_new_names_add_options(self, tmp_path: Path):
        """A project-local flow with a new name is available alongside built-ins."""
        proj_flows = tmp_path / ".pollypm" / "flows"
        proj_flows.mkdir(parents=True)
        (proj_flows / "custom.yaml").write_text(textwrap.dedent("""\
            name: custom
            description: A custom flow

            roles:
              worker:
                description: w
            nodes:
              step:
                type: work
                actor_type: role
                actor_role: worker
                next_node: done
              done:
                type: terminal
            start_node: step
        """))
        flow = resolve_flow("custom", project_path=tmp_path)
        assert flow.name == "custom"

    def test_flow_not_found(self, tmp_path: Path):
        with pytest.raises(FlowValidationError, match="not found"):
            resolve_flow("nonexistent", project_path=tmp_path)


# ---------------------------------------------------------------------------
# available_flows
# ---------------------------------------------------------------------------


class TestAvailableFlows:
    def test_lists_builtin_flows(self):
        flows = available_flows()
        assert "standard" in flows
        assert "spike" in flows
        assert "user-review" in flows
        assert "bug" in flows

    def test_project_flows_added(self, tmp_path: Path):
        proj_flows = tmp_path / ".pollypm" / "flows"
        proj_flows.mkdir(parents=True)
        (proj_flows / "custom.yaml").write_text(textwrap.dedent("""\
            name: custom
            description: custom
            roles:
              worker:
                description: w
            nodes:
              step:
                type: work
                actor_type: role
                actor_role: worker
                next_node: done
              done:
                type: terminal
            start_node: step
        """))
        flows = available_flows(project_path=tmp_path)
        assert "custom" in flows
        # Built-ins still present
        assert "standard" in flows

    def test_project_override_replaces_builtin_in_listing(self, tmp_path: Path):
        proj_flows = tmp_path / ".pollypm" / "flows"
        proj_flows.mkdir(parents=True)
        (proj_flows / "standard.yaml").write_text(textwrap.dedent("""\
            name: standard
            description: overridden
            roles:
              worker:
                description: w
            nodes:
              step:
                type: work
                actor_type: role
                actor_role: worker
                next_node: done
              done:
                type: terminal
            start_node: step
        """))
        flows = available_flows(project_path=tmp_path)
        # The path should point to the project-local version
        assert "standard" in flows
        assert str(tmp_path) in str(flows["standard"])
