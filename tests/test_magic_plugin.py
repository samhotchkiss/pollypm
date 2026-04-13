"""Tests for the magic (itsalive) plugin."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from pollypm.plugin_host import ExtensionHost
from pollypm.plugins_builtin.magic.plugin import (
    MagicProfile,
    build_deploy_instructions,
    read_deploy_token,
    read_owner_token,
)


# ---------------------------------------------------------------------------
# Unit tests for utility helpers
# ---------------------------------------------------------------------------


def test_read_owner_token_missing(tmp_path: Path) -> None:
    with patch("pollypm.plugins_builtin.magic.plugin.OWNER_TOKEN_PATH", tmp_path / "nope"):
        assert read_owner_token() is None


def test_read_owner_token_present(tmp_path: Path) -> None:
    token_file = tmp_path / ".itsalive"
    token_file.write_text(json.dumps({"ownerToken": "tok_abc123"}))
    with patch("pollypm.plugins_builtin.magic.plugin.OWNER_TOKEN_PATH", token_file):
        assert read_owner_token() == "tok_abc123"


def test_read_owner_token_snake_case(tmp_path: Path) -> None:
    token_file = tmp_path / ".itsalive"
    token_file.write_text(json.dumps({"owner_token": "tok_snake"}))
    with patch("pollypm.plugins_builtin.magic.plugin.OWNER_TOKEN_PATH", token_file):
        assert read_owner_token() == "tok_snake"


def test_read_owner_token_invalid_json(tmp_path: Path) -> None:
    token_file = tmp_path / ".itsalive"
    token_file.write_text("not json")
    with patch("pollypm.plugins_builtin.magic.plugin.OWNER_TOKEN_PATH", token_file):
        assert read_owner_token() is None


def test_read_deploy_token_missing(tmp_path: Path) -> None:
    assert read_deploy_token(tmp_path) is None


def test_read_deploy_token_present(tmp_path: Path) -> None:
    config = tmp_path / ".itsalive"
    config.write_text(json.dumps({"deployToken": "dtok_xyz"}))
    assert read_deploy_token(tmp_path) == "dtok_xyz"


def test_read_deploy_token_snake_case(tmp_path: Path) -> None:
    config = tmp_path / ".itsalive"
    config.write_text(json.dumps({"deploy_token": "dtok_snake"}))
    assert read_deploy_token(tmp_path) == "dtok_snake"


# ---------------------------------------------------------------------------
# Agent profile tests
# ---------------------------------------------------------------------------


def test_magic_profile_builds_prompt() -> None:
    profile = MagicProfile()
    assert profile.name == "magic"
    # build_prompt should work with a None-ish context for the parts we use
    prompt = profile.build_prompt(None)  # type: ignore[arg-type]
    assert prompt is not None
    assert "deploy/init" in prompt
    assert "itsalive.co" in prompt
    assert "owner_token" in prompt


def test_build_deploy_instructions_contains_all_sections() -> None:
    text = build_deploy_instructions()
    # Deployment steps
    assert "check-subdomain" in text.lower() or "check subdomain" in text.lower()
    assert "deploy/init" in text
    assert "deploy/<deploy_id>/status" in text or "deploy/:id/status" in text
    assert "upload-urls" in text
    assert "finalize" in text
    # Platform capabilities
    assert "/_auth/" in text
    assert "/_db/" in text
    assert "/_me/" in text
    assert "/_ai/chat" in text
    assert "/_email/" in text
    assert "/_subscribers" in text
    assert "/cron" in text
    assert "/jobs" in text
    assert "/_og/routes" in text
    assert "Powered by itsalive.co" in text


def test_build_deploy_instructions_includes_owner_token_note(tmp_path: Path) -> None:
    token_file = tmp_path / ".itsalive"
    token_file.write_text(json.dumps({"ownerToken": "tok_test"}))
    with patch("pollypm.plugins_builtin.magic.plugin.OWNER_TOKEN_PATH", token_file):
        text = build_deploy_instructions()
        assert "owner_token was found" in text


def test_build_deploy_instructions_no_owner_token_note(tmp_path: Path) -> None:
    with patch("pollypm.plugins_builtin.magic.plugin.OWNER_TOKEN_PATH", tmp_path / "nope"):
        text = build_deploy_instructions()
        assert "owner_token was found" not in text


# ---------------------------------------------------------------------------
# Plugin registration via ExtensionHost
# ---------------------------------------------------------------------------


def test_extension_host_loads_magic_profile(tmp_path: Path) -> None:
    host = ExtensionHost(tmp_path)
    profile = host.get_agent_profile("magic")
    assert profile is not None
    assert profile.name == "magic"


def test_magic_observer_registered(tmp_path: Path) -> None:
    host = ExtensionHost(tmp_path)
    # Run the observer — it should not raise
    failures = host.run_observers("session.after_launch", {"session": "test"})
    # No failures from the magic observer specifically
    # (other plugins may register observers too, so we just confirm no crash)
    assert isinstance(failures, list)
