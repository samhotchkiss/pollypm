"""Tests for ``pollypm.work.role_validation``.

savethenovel forensic — Polly typed ``--role worker=user --role
reviewer=user``. The validation module must reject those values
without a closed whitelist, so opaque worker IDs (``agent-1``,
``worker-pollypm``, …) keep working in existing fixtures.
"""

from __future__ import annotations

import pytest

from pollypm.work.models import ActorType, FlowNode, FlowTemplate, NodeType
from pollypm.work.role_validation import (
    NON_AGENT_VALUES,
    autonomous_agent_roles,
    is_non_agent_value,
    validate_role_assignments,
)
from pollypm.work.service_support import ValidationError


def _standard_template() -> FlowTemplate:
    """Mirror ``src/pollypm/work/flows/standard.yaml`` in code."""
    return FlowTemplate(
        name="standard",
        description="test fixture",
        roles={
            "worker": {"description": "implements"},
            "reviewer": {"description": "reviews"},
            "requester": {"description": "asker", "optional": True},
        },
        nodes={
            "implement": FlowNode(
                name="implement",
                type=NodeType.WORK,
                actor_type=ActorType.ROLE,
                actor_role="worker",
                next_node_id="code_review",
            ),
            "code_review": FlowNode(
                name="code_review",
                type=NodeType.REVIEW,
                actor_type=ActorType.ROLE,
                actor_role="reviewer",
                next_node_id="done",
            ),
            "done": FlowNode(name="done", type=NodeType.TERMINAL),
        },
        start_node="implement",
    )


class TestAutonomousAgentRoles:
    def test_extracts_role_actor_roles_only(self):
        roles = autonomous_agent_roles(_standard_template())
        assert roles == {"worker", "reviewer"}

    def test_ignores_metadata_roles(self):
        # ``requester`` is declared in template.roles but no node uses it,
        # so it isn't autonomous.
        assert "requester" not in autonomous_agent_roles(_standard_template())

    def test_ignores_optional_roles(self):
        # The chat flow marks ``operator`` as optional ("defaults to
        # Polly"). Existing fixtures use ``operator=user`` as a
        # feedback-task marker even though that node never fires;
        # validation must let those through.
        chat = FlowTemplate(
            name="chat",
            description="",
            roles={
                "operator": {"optional": True},
                "requester": {"optional": True},
            },
            nodes={
                "user_message": FlowNode(
                    name="user_message",
                    type=NodeType.WORK,
                    actor_type=ActorType.HUMAN,
                ),
                "agent_response": FlowNode(
                    name="agent_response",
                    type=NodeType.WORK,
                    actor_type=ActorType.ROLE,
                    actor_role="operator",
                ),
            },
            start_node="user_message",
        )
        assert autonomous_agent_roles(chat) == set()


class TestValidateRoleAssignments:
    def test_user_on_worker_raises(self):
        template = _standard_template()
        with pytest.raises(ValidationError) as exc:
            validate_role_assignments(
                template, {"worker": "user", "reviewer": "russell"}
            )
        assert "worker='user'" in str(exc.value)
        assert "not an autonomous agent" in str(exc.value)

    def test_user_on_reviewer_raises(self):
        with pytest.raises(ValidationError):
            validate_role_assignments(
                _standard_template(),
                {"worker": "worker", "reviewer": "user"},
            )

    def test_user_on_requester_is_allowed(self):
        # Metadata-only role — inbox-view relies on ``requester=user``
        # for the user-facing membership signal.
        validate_role_assignments(
            _standard_template(),
            {"worker": "worker", "reviewer": "russell", "requester": "user"},
        )

    def test_human_and_placeholder_values_are_rejected(self):
        # Personal names like "sam" are NOT in the blocklist — existing
        # fixtures use them as opaque worker IDs. Only canonical
        # human-markers and obvious placeholders are rejected.
        for bad in ("human", "nobody", "  USER  ", "tbd", "TODO", "?"):
            with pytest.raises(ValidationError):
                validate_role_assignments(
                    _standard_template(),
                    {"worker": bad, "reviewer": "russell"},
                )

    def test_opaque_agent_ids_pass(self):
        # The existing fixture pattern across the test suite — must
        # remain valid (this fix is a blacklist, not a whitelist).
        for ok in ("agent-1", "worker-pollypm", "pa/worker_xyz", "polly"):
            validate_role_assignments(
                _standard_template(),
                {"worker": ok, "reviewer": "russell"},
            )

    def test_collects_all_offences_in_one_error(self):
        with pytest.raises(ValidationError) as exc:
            validate_role_assignments(
                _standard_template(),
                {"worker": "user", "reviewer": "user"},
            )
        msg = str(exc.value)
        assert "worker='user'" in msg
        assert "reviewer='user'" in msg

    def test_empty_template_is_no_op(self):
        empty = FlowTemplate(
            name="empty", description="", roles={}, nodes={}, start_node=""
        )
        validate_role_assignments(empty, {"anything": "user"})


class TestIsNonAgentValue:
    def test_user_is_non_agent(self):
        assert is_non_agent_value("user")
        assert is_non_agent_value("USER")
        assert is_non_agent_value("  user  ")

    def test_known_agents_are_not_non_agent(self):
        for name in ("polly", "russell", "architect", "agent-1"):
            assert not is_non_agent_value(name), name

    def test_none_is_not_non_agent(self):
        assert not is_non_agent_value(None)


def test_non_agent_values_constant_includes_savethenovel_culprit():
    """Defensive: make sure ``user`` doesn't get accidentally removed."""
    assert "user" in NON_AGENT_VALUES
    assert "human" in NON_AGENT_VALUES
