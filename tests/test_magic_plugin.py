from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from pollypm.plugin_host import ExtensionHost
from pollypm.plugins_builtin.magic.plugin import (
    build_deploy_instructions,
    read_deploy_token,
    read_owner_token,
)


def test_read_owner_token_missing(tmp_path: Path) -> None:
    with patch("pollypm.itsalive.GLOBAL_CONFIG_FILE", tmp_path / "nope"):
        assert read_owner_token() is None


def test_read_owner_token_present(tmp_path: Path) -> None:
    token_file = tmp_path / ".itsalive"
    token_file.write_text(json.dumps({"ownerToken": "tok_abc123"}))
    with patch("pollypm.itsalive.GLOBAL_CONFIG_FILE", token_file):
        assert read_owner_token() == "tok_abc123"


def test_read_deploy_token_present(tmp_path: Path) -> None:
    config = tmp_path / ".itsalive"
    config.write_text(json.dumps({"deployToken": "dtok_xyz"}))
    assert read_deploy_token(tmp_path) == "dtok_xyz"


def test_build_deploy_instructions_contains_async_flow_and_capabilities(tmp_path: Path) -> None:
    with patch("pollypm.itsalive.GLOBAL_CONFIG_FILE", tmp_path / "nope"):
        text = build_deploy_instructions()
    assert "pm itsalive deploy" in text
    assert "24 hours" in text
    assert "heartbeat" in text.lower()
    assert "/_auth/login" in text
    assert "/_db/:collection/:id" in text
    assert "/_ai/chat" in text
    assert "/_email/send" in text
    assert "/_subscribers" in text
    assert "/cron" in text
    assert "/jobs" in text
    assert "/_og/routes" in text


def test_extension_host_worker_profile_is_overridden_with_itsalive_knowledge(tmp_path: Path) -> None:
    host = ExtensionHost(tmp_path)
    profile = host.get_agent_profile("worker")
    assert profile.name == "worker"
    assert "pm itsalive deploy" in profile.prompt
    assert "24 hours" in profile.prompt


def test_magic_observer_registered(tmp_path: Path) -> None:
    host = ExtensionHost(tmp_path)
    failures = host.run_observers("session.after_launch", {"session": "test"})
    assert isinstance(failures, list)
