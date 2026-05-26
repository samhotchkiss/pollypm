"""Unit tests for Lever 2 (#2012) per-session auth-token plumbing.

PR 1 of the recovery-cascade Lever 2 arc covers:

* ``SessionConfig.auth_token`` storage and TOML round-trip
* :mod:`pollypm.session_auth` helpers (mint, format, ensure-all)
* Agent-profile prompt teaches the auth contract when the session
  has a token, omits it when not

The emit-side (watchdog + recovery preamble) is PR 2 and tested there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pollypm.agent_profiles import get_agent_profile
from pollypm.agent_profiles.base import AgentProfileContext
from pollypm.agent_profiles.defaults import (
    StaticPromptProfile,
    _render_auth_contract,
)
from pollypm.config import load_config, write_config
from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.session_auth import (
    AUTH_MARKER_PREFIX,
    AUTH_MARKER_SUFFIX,
    TOKEN_BYTES,
    ensure_session_auth_tokens,
    format_auth_marker,
    mint_auth_token,
)


# ---------------------------------------------------------------------------
# mint_auth_token
# ---------------------------------------------------------------------------


def test_mint_auth_token_returns_hex_of_expected_width() -> None:
    """``secrets.token_hex(32)`` yields 64 hex chars."""
    token = mint_auth_token()
    assert re.fullmatch(r"[0-9a-f]+", token), token
    assert len(token) == TOKEN_BYTES * 2


def test_mint_auth_token_is_unique_per_call() -> None:
    """Two consecutive mints must not collide.

    The token is the session's first line of defence against
    prompt-injection — even a low collision rate would let one
    architect impersonate another's watchdog brief.
    """
    samples = {mint_auth_token() for _ in range(64)}
    assert len(samples) == 64


# ---------------------------------------------------------------------------
# format_auth_marker
# ---------------------------------------------------------------------------


def test_format_auth_marker_renders_marker_for_real_token() -> None:
    token = "deadbeef" * 8  # 64 hex chars
    marker = format_auth_marker(token)
    assert marker.startswith(AUTH_MARKER_PREFIX)
    assert marker.endswith(AUTH_MARKER_SUFFIX)
    assert token in marker


def test_format_auth_marker_empty_for_missing_token() -> None:
    """Empty/None tokens render to empty string so callers can
    unconditionally prepend without conditionals.

    This is the backward-compat lever: a legacy session row (loaded
    from a pollypm.toml written before Lever 2) carries
    ``auth_token = ""`` and the watchdog emitter behaves identically
    to pre-Lever-2 behaviour.
    """
    assert format_auth_marker("") == ""
    assert format_auth_marker(None) == ""


# ---------------------------------------------------------------------------
# ensure_session_auth_tokens
# ---------------------------------------------------------------------------


def _bare_config(tmp_path: Path) -> PollyPMConfig:
    root = tmp_path / "repo"
    root.mkdir()
    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=root,
            base_dir=root / ".pollypm",
            logs_dir=root / ".pollypm/logs",
            snapshots_dir=root / ".pollypm/snapshots",
            state_db=root / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="acc"),
        accounts={
            "acc": AccountConfig(
                name="acc", provider=ProviderKind.CLAUDE,
                home=root / ".pollypm" / "homes" / "acc",
            ),
        },
        sessions={
            "architect": SessionConfig(
                name="architect", role="architect",
                provider=ProviderKind.CLAUDE, account="acc", cwd=root,
            ),
            "reviewer": SessionConfig(
                name="reviewer", role="reviewer",
                provider=ProviderKind.CLAUDE, account="acc", cwd=root,
            ),
        },
        projects={
            "demo": KnownProject(
                key="demo", path=root, name="Demo",
                kind=ProjectKind.FOLDER,
            ),
        },
    )


def test_ensure_session_auth_tokens_mints_for_each_legacy_session(
    tmp_path: Path,
) -> None:
    config = _bare_config(tmp_path)
    assert config.sessions["architect"].auth_token == ""
    assert config.sessions["reviewer"].auth_token == ""

    minted = ensure_session_auth_tokens(config)
    assert minted == 2
    arch = config.sessions["architect"].auth_token
    rev = config.sessions["reviewer"].auth_token
    assert arch and rev
    assert arch != rev, "different sessions must get different tokens"


def test_ensure_session_auth_tokens_is_idempotent(tmp_path: Path) -> None:
    """A re-run after migration must not re-mint."""
    config = _bare_config(tmp_path)
    ensure_session_auth_tokens(config)
    before = {
        name: session.auth_token
        for name, session in config.sessions.items()
    }
    minted = ensure_session_auth_tokens(config)
    after = {
        name: session.auth_token
        for name, session in config.sessions.items()
    }
    assert minted == 0
    assert before == after


# ---------------------------------------------------------------------------
# TOML round-trip
# ---------------------------------------------------------------------------


def test_session_auth_token_round_trips_through_toml(tmp_path: Path) -> None:
    """write_config -> load_config preserves auth_token verbatim."""
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "PollyPM"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.architect_demo]
role = "architect"
provider = "claude"
account = "claude_primary"
cwd = "."
project = "demo"
auth_token = "cafebabe1234567890cafebabe1234567890cafebabe1234567890cafebabe12"

[projects.demo]
path = "demo"
"""
    )
    (tmp_path / "demo").mkdir()

    config = load_config(config_path)
    assert (
        config.sessions["architect_demo"].auth_token
        == "cafebabe1234567890cafebabe1234567890cafebabe1234567890cafebabe12"
    )
    # Legacy session (no auth_token key in TOML) is migrated by
    # load_config: ``ensure_session_auth_tokens`` mints a fresh 64-char
    # hex token. The persistence write happens in-place, so the loaded
    # config carries the minted token and the file on disk is updated.
    heartbeat_token = config.sessions["heartbeat"].auth_token
    assert len(heartbeat_token) == 64, heartbeat_token
    assert all(c in "0123456789abcdef" for c in heartbeat_token)

    # Round-trip through write_config — both explicit and migrated tokens persist.
    output_path = tmp_path / "out.toml"
    write_config(config, output_path, force=True)
    rendered = output_path.read_text()
    assert (
        'auth_token = "cafebabe1234567890cafebabe1234567890'
        'cafebabe1234567890cafebabe12"'
        in rendered
    ), rendered

    reloaded = load_config(output_path)
    assert (
        reloaded.sessions["architect_demo"].auth_token
        == config.sessions["architect_demo"].auth_token
    )
    # Migrated heartbeat token is now emitted in the rendered TOML and
    # stable across the load → write → load cycle.
    heartbeat_block = rendered.split("[sessions.heartbeat]")[1].split("[", 1)[0]
    assert f'auth_token = "{heartbeat_token}"' in heartbeat_block, heartbeat_block
    assert reloaded.sessions["heartbeat"].auth_token == heartbeat_token


def test_load_config_migrates_missing_auth_tokens_in_place(tmp_path: Path) -> None:
    """Real regression for PR #2018 review (id 4502846140).

    ``load_config`` MUST mint tokens for any session missing one AND
    persist them to disk, so watchdog/recovery dispatch never emits an
    unsigned brief for a legacy session. Starts from a config where
    every session lacks an ``auth_token`` key; asserts (a) the loaded
    config has 64-char hex tokens for every session, (b) the on-disk
    TOML now contains those tokens, (c) a second load returns the same
    tokens (stable, not re-minted).
    """
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "PollyPM"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.architect_demo]
role = "architect"
provider = "claude"
account = "claude_primary"
cwd = "."
project = "demo"

[projects.demo]
path = "demo"
"""
    )
    (tmp_path / "demo").mkdir()

    # No auth_token keys in the source TOML.
    assert "auth_token" not in config_path.read_text()

    # First load_config: mints + persists.
    config = load_config(config_path)
    heartbeat = config.sessions["heartbeat"].auth_token
    architect = config.sessions["architect_demo"].auth_token
    assert len(heartbeat) == 64 and len(architect) == 64
    assert heartbeat != architect  # per-session uniqueness

    # File on disk now carries the tokens.
    persisted = config_path.read_text()
    assert f'auth_token = "{heartbeat}"' in persisted
    assert f'auth_token = "{architect}"' in persisted

    # Second load returns identical tokens (stable, no re-mint).
    # Bypass the in-process cache so we actually re-read from disk.
    import pollypm.config as config_mod
    config_mod._config_cache.clear()
    reloaded = load_config(config_path)
    assert reloaded.sessions["heartbeat"].auth_token == heartbeat
    assert reloaded.sessions["architect_demo"].auth_token == architect


# ---------------------------------------------------------------------------
# Agent-profile auth-contract block
# ---------------------------------------------------------------------------


def test_auth_contract_block_renders_with_token(tmp_path: Path) -> None:
    """The agent profile's auth-contract block embeds the real token.

    The agent receives the literal ``[PollyPM-Auth: <hex>]`` marker
    inside its initial system prompt so it can compare incoming briefs
    byte-for-byte. Verifies the hex and the surrounding instructions
    ('refuse', 'prompt-injection') both land.
    """
    token = "abcdef00" * 8
    block = _render_auth_contract(token)
    assert "<pollypm_auth>" in block
    assert AUTH_MARKER_PREFIX in block
    assert token in block
    assert "refuse" in block.lower()
    assert "injection" in block.lower()
    assert "pm audit agent-refusal" in block
    assert "agent.injection.flagged" in block
    assert "agent.refusal" in block

    command_line = next(
        line for line in block.splitlines() if "pm audit agent-refusal" in line
    )
    assert token not in command_line
    assert "raw message" in block


def test_auth_contract_omitted_for_legacy_session() -> None:
    """No token -> no contract block.

    A half-installed contract ("marker missing means injection") would
    train the agent to refuse pre-Lever-2 messages still in flight.
    The block stays absent until the token migration completes.
    """
    assert _render_auth_contract("") == ""
    assert _render_auth_contract(None) == ""


def test_profile_build_prompt_includes_auth_block_with_token(
    tmp_path: Path,
) -> None:
    """Full profile rendering injects the auth block when the session
    carries a token."""
    config = _bare_config(tmp_path)
    config.sessions["architect"].auth_token = "1234abcd" * 8
    profile = StaticPromptProfile(name="worker", prompt="You are a worker.")
    context = AgentProfileContext(
        config=config,
        session=config.sessions["architect"],
        account=config.accounts["acc"],
    )
    rendered = profile.build_prompt(context) or ""
    assert "<pollypm_auth>" in rendered
    assert "1234abcd" * 8 in rendered


def test_auth_refusal_contract_profile_cli_regression_without_live_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministic substitute for live-agent E2E in CI.

    This cannot prove a model will follow the instruction, so it locks
    the executable contract PollyPM can own: the launched profile tells
    agents to refuse unsigned or bad-marker PollyPM claims, and the
    public CLI records the required audit pair for both refusal reasons
    without accepting raw message text or leaking auth markers.
    """
    from typer.testing import CliRunner

    from pollypm.audit.log import (
        EVENT_AGENT_INJECTION_FLAGGED,
        EVENT_AGENT_REFUSAL,
    )
    from pollypm.cli import app as root_app

    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

    config = _bare_config(tmp_path)
    (config.project.root_dir / ".pollypm").mkdir()
    token = "facefeed" * 8
    session = config.sessions["architect"]
    session.auth_token = token
    config_path = config.project.root_dir / "pollypm.toml"
    write_config(config, config_path, force=True)

    profile = get_agent_profile("worker", root_dir=config.project.root_dir)
    prompt = profile.build_prompt(
        AgentProfileContext(
            config=config,
            session=session,
            account=config.accounts["acc"],
        )
    ) or ""

    assert f"[PollyPM-Auth: {token}]" in prompt
    assert "Refuse it" in prompt
    assert "unsigned-pollypm-claim" in prompt
    assert "bad-auth-marker" in prompt
    assert "pm audit agent-refusal" in prompt
    assert "agent.injection.flagged" in prompt
    assert "agent.refusal" in prompt

    scenarios = [
        ("unsigned-pollypm-claim", "unsigned watchdog claim"),
        ("bad-auth-marker", f"[PollyPM-Auth: {'badc0de0' * 8}]"),
    ]
    runner = CliRunner()
    for reason, _raw_message in scenarios:
        result = runner.invoke(
            root_app,
            [
                "audit",
                "agent-refusal",
                "--reason",
                reason,
                "--project",
                "demo",
                "--actor",
                session.name,
                "--subject",
                "pollypm-auth",
                "--source",
                "pollypm-auth",
                "--config",
                str(config_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "agent.injection.flagged and agent.refusal" in result.output

    records = [
        json.loads(line)
        for line in (config.project.root_dir / ".pollypm" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert [record["event"] for record in records] == [
        EVENT_AGENT_INJECTION_FLAGGED,
        EVENT_AGENT_REFUSAL,
        EVENT_AGENT_INJECTION_FLAGGED,
        EVENT_AGENT_REFUSAL,
    ]
    assert [record["metadata"]["reason"] for record in records] == [
        "unsigned-pollypm-claim",
        "unsigned-pollypm-claim",
        "bad-auth-marker",
        "bad-auth-marker",
    ]
    for record in records:
        assert record["project"] == "demo"
        assert record["actor"] == session.name
        assert record["subject"] == "pollypm-auth"
        assert record["status"] == "warn"
        assert record["metadata"]["source"] == "pollypm-auth"

    serialized = "\n".join(json.dumps(record, sort_keys=True) for record in records)
    assert token not in serialized
    assert "PollyPM-Auth" not in serialized
    assert "unsigned watchdog claim" not in serialized
    assert "badc0de0" not in serialized


def test_profile_build_prompt_omits_auth_block_without_token(
    tmp_path: Path,
) -> None:
    """No token on the session -> no auth block in the rendered prompt."""
    config = _bare_config(tmp_path)
    profile = StaticPromptProfile(name="worker", prompt="You are a worker.")
    context = AgentProfileContext(
        config=config,
        session=config.sessions["architect"],
        account=config.accounts["acc"],
    )
    rendered = profile.build_prompt(context) or ""
    assert "<pollypm_auth>" not in rendered
