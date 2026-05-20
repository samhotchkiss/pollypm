"""Tests for the built-in chat flow template.

Covers:
  - The shipped YAML parses + validates through `flow_engine`.
  - `pm task create --flow chat --title "<question>"` succeeds with no extra
    flags — i.e. the flow round-trips through the CLI.
"""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.work.cli import task_app
from pollypm.work.flow_engine import parse_flow_yaml, resolve_flow
from pollypm.work.models import ActorType, NodeType


runner = CliRunner()


def _chat_yaml_path() -> Path:
    ref = importlib.resources.files("pollypm.work") / "flows" / "chat.yaml"
    return Path(str(ref))


class TestChatFlowParsesAndValidates:
    def test_file_exists(self):
        assert _chat_yaml_path().is_file()

    def test_flow_parses(self):
        flow = parse_flow_yaml(_chat_yaml_path().read_text(encoding="utf-8"))
        assert flow.name == "chat"
        assert flow.description
        # Three nodes: user_message, agent_response, done.
        assert set(flow.nodes.keys()) == {"user_message", "agent_response", "done"}

    def test_start_node(self):
        flow = parse_flow_yaml(_chat_yaml_path().read_text(encoding="utf-8"))
        assert flow.start_node == "user_message"

    def test_user_message_node_is_human(self):
        flow = parse_flow_yaml(_chat_yaml_path().read_text(encoding="utf-8"))
        node = flow.nodes["user_message"]
        assert node.type == NodeType.WORK
        assert node.actor_type == ActorType.HUMAN
        assert node.next_node_id == "agent_response"

    def test_agent_response_node_is_role_operator(self):
        flow = parse_flow_yaml(_chat_yaml_path().read_text(encoding="utf-8"))
        node = flow.nodes["agent_response"]
        assert node.type == NodeType.WORK
        assert node.actor_type == ActorType.ROLE
        assert node.actor_role == "operator"
        assert node.next_node_id == "done"

    def test_done_is_terminal(self):
        flow = parse_flow_yaml(_chat_yaml_path().read_text(encoding="utf-8"))
        assert flow.nodes["done"].type == NodeType.TERMINAL

    def test_operator_role_is_optional(self):
        # Defaults to Polly — should not require --role at create time.
        flow = parse_flow_yaml(_chat_yaml_path().read_text(encoding="utf-8"))
        operator = flow.roles.get("operator")
        assert isinstance(operator, dict)
        assert operator.get("optional") is True

    def test_resolve_flow_finds_chat(self):
        # Ensure the override chain picks up the built-in.
        flow = resolve_flow("chat")
        assert flow.name == "chat"


@pytest.mark.xfail(
    reason=(
        "pm task --db sqlite escape hatch removed (#1971 sqlite-ripout PR A). "
        "Test needs a pg-DSN-based harness like the rest of the CLI suite."
    ),
    strict=False,
)
class TestChatFlowRoundTripsThroughCreate:
    def test_create_with_just_title(self, tmp_path):
        result = runner.invoke(
            task_app,
            [
                "create", "Is this on?",
                "--project", "proj",
                "--flow", "chat",
                "--description", "hello world",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["task_id"] == "proj/1"
        assert payload["work_status"] == "draft"
        assert payload["title"] == "Is this on?"

    def test_create_with_operator_override(self, tmp_path):
        result = runner.invoke(
            task_app,
            [
                "create", "Ask Russell instead",
                "--project", "proj",
                "--flow", "chat",
                "--description", "hi",
                "--role", "operator=russell",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["roles"] == {"operator": "russell"}
