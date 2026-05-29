"""Lever 2 (#2012) emit-side tests — PR 2.

Verifies that ``format_unstick_brief`` and the ``RecoveryPrompt``
renderer prepend ``[PollyPM-Auth: <token>]\\n`` when a token is
provided, and render the legacy un-marked shape when not. Also covers
the dispatcher helper that looks up the architect session's token by
project key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.audit.watchdog import (
    EVENT_WATCHDOG_ESCALATION_DISPATCHED,
    Finding,
    RULE_STUCK_DRAFT,
    RULE_TASK_ON_HOLD_STALE,
    emit_escalation_dispatched,
    format_unstick_brief,
)
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
from pollypm.recovery_prompt import (
    RecoveryPrompt,
    RecoveryPromptSection,
    _session_auth_token,
    build_recovery_prompt,
)
from pollypm.session_auth import AUTH_MARKER_PREFIX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config_with_sessions(
    tmp_path: Path,
    *,
    architect_token: str = "",
    architect_alias: str = "demo",
) -> PollyPMConfig:
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
            f"architect_{architect_alias}": SessionConfig(
                name=f"architect_{architect_alias}", role="architect",
                provider=ProviderKind.CLAUDE, account="acc", cwd=root,
                project=architect_alias,
                auth_token=architect_token,
            ),
        },
        projects={
            architect_alias: KnownProject(
                key=architect_alias, path=root, name="Demo",
                kind=ProjectKind.FOLDER,
            ),
        },
    )


def _stuck_draft_finding() -> Finding:
    return Finding(
        rule=RULE_STUCK_DRAFT,
        project="pollypm",
        subject="pollypm/29",
        message="Draft task pollypm/29 has sat unpromoted for >30 min.",
        recommendation=(
            "Promote with `pm task queue pollypm/29` or discard "
            "with `pm task cancel pollypm/29`."
        ),
        metadata={"detected_via": "state"},
    )


# ---------------------------------------------------------------------------
# format_unstick_brief — auth marker
# ---------------------------------------------------------------------------


def test_brief_unmarked_when_token_missing() -> None:
    """No token / empty token -> legacy un-marked brief shape.

    The receiving agent currently in flight must still be able to act
    on briefs emitted before any token landed; the legacy shape is the
    fallback PR 1 documented in the contract.
    """
    brief = format_unstick_brief(_stuck_draft_finding())
    assert "WATCHDOG ESCALATION" in brief
    assert AUTH_MARKER_PREFIX not in brief

    # Empty string and None must both yield the same legacy shape.
    assert format_unstick_brief(_stuck_draft_finding(), auth_token="") == brief
    assert format_unstick_brief(_stuck_draft_finding(), auth_token=None) == brief


def test_brief_signed_when_token_present() -> None:
    """A real token is prepended with the marker before the header.

    Once the marker lands the agent's contract block (PR 1) recognises
    the message as a legitimate PollyPM dispatch and will execute its
    ACTION REQUIRED instructions instead of refusing.
    """
    token = "deadbeef" * 8
    brief = format_unstick_brief(_stuck_draft_finding(), auth_token=token)
    assert brief.startswith(f"{AUTH_MARKER_PREFIX}{token}]\n")
    assert "WATCHDOG ESCALATION" in brief
    # The body still satisfies the imperative-format invariant from #1979.
    assert "ACTION REQUIRED" in brief


def test_brief_marker_distinct_per_token() -> None:
    """Different tokens produce different markers — the per-session
    secret is load-bearing for distinguishing one architect from
    another."""
    f = _stuck_draft_finding()
    t1 = "1" * 64
    t2 = "2" * 64
    b1 = format_unstick_brief(f, auth_token=t1)
    b2 = format_unstick_brief(f, auth_token=t2)
    assert b1 != b2
    assert t1 in b1 and t1 not in b2
    assert t2 in b2 and t2 not in b1


def test_brief_marker_lands_before_header_line() -> None:
    """The marker prefixes the whole brief — the ``WATCHDOG ESCALATION``
    line that the agent's tooling greps for must not move past the
    first line of output, so the marker uses a trailing ``\\n`` (not a
    leading one) and the header stays on line 2."""
    brief = format_unstick_brief(
        _stuck_draft_finding(), auth_token="c" * 64,
    )
    lines = brief.split("\n")
    assert lines[0].startswith(AUTH_MARKER_PREFIX)
    assert lines[1] == "WATCHDOG ESCALATION"


def test_brief_marker_with_tier2_template() -> None:
    """Tier-2 template (``task_on_hold_stale``) is signed too.

    The marker logic lives at the bottom of ``format_unstick_brief``
    after the per-rule body builder runs, so every rule template
    (fallback + tier-2 templates) is covered by one composition site.
    """
    finding = Finding(
        rule=RULE_TASK_ON_HOLD_STALE,
        project="demo",
        subject="demo/7",
        message="Task demo/7 has been at status=on_hold for ~30 min.",
        metadata={"routing": "architect-actionable"},
    )
    brief = format_unstick_brief(finding, auth_token="d" * 64)
    assert brief.startswith(AUTH_MARKER_PREFIX + ("d" * 64))
    assert "WATCHDOG ESCALATION" in brief


def test_escalation_dispatch_audit_metadata_strips_auth_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sent brief is signed, but persisted audit metadata is not."""
    captured: dict[str, object] = {}

    def fake_emit(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("pollypm.audit.log.emit", fake_emit)
    token = "feedface" * 8
    signed = format_unstick_brief(_stuck_draft_finding(), auth_token=token)

    emit_escalation_dispatched(
        project="demo",
        finding_type=RULE_STUCK_DRAFT,
        subject="demo/1",
        brief=signed,
        dedup_hash="abc123",
    )

    assert captured["event"] == EVENT_WATCHDOG_ESCALATION_DISPATCHED
    metadata = captured["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["brief"].startswith("WATCHDOG ESCALATION")
    assert AUTH_MARKER_PREFIX not in metadata["brief"]
    assert token not in metadata["brief"]


# ---------------------------------------------------------------------------
# RecoveryPrompt — auth marker
# ---------------------------------------------------------------------------


def test_recovery_prompt_unsigned_when_token_empty() -> None:
    """Default RecoveryPrompt has no token -> legacy shape."""
    prompt = RecoveryPrompt(
        sections=[RecoveryPromptSection(key="x", heading="Test", content="hi")],
        provider=ProviderKind.CLAUDE,
    )
    rendered = prompt.render()
    assert AUTH_MARKER_PREFIX not in rendered


def test_recovery_prompt_signed_when_token_set() -> None:
    """auth_token field on RecoveryPrompt prepends the marker."""
    token = "e" * 64
    prompt = RecoveryPrompt(
        sections=[RecoveryPromptSection(key="x", heading="Test", content="hi")],
        provider=ProviderKind.CLAUDE,
        auth_token=token,
    )
    rendered = prompt.render()
    assert rendered.startswith(f"{AUTH_MARKER_PREFIX}{token}]\n")


def test_recovery_prompt_signed_for_codex_provider() -> None:
    """Both Claude and Codex preambles get the marker — the contract
    doesn't gate on provider."""
    token = "f" * 64
    prompt = RecoveryPrompt(
        sections=[RecoveryPromptSection(key="x", heading="Test", content="hi")],
        provider=ProviderKind.CODEX,
        auth_token=token,
    )
    rendered = prompt.render()
    assert rendered.startswith(f"{AUTH_MARKER_PREFIX}{token}]\n")
    assert "RECOVERY CONTEXT" in rendered


# ---------------------------------------------------------------------------
# build_recovery_prompt threads session.auth_token
# ---------------------------------------------------------------------------


def test_build_recovery_prompt_threads_session_token(tmp_path: Path) -> None:
    """The fallback build path pulls ``auth_token`` from
    ``config.sessions[session_name]`` so the rendered preamble carries
    the marker without callers having to plumb it themselves."""
    token = "abcdef00" * 8
    config = _config_with_sessions(
        tmp_path, architect_token=token, architect_alias="demo",
    )
    prompt = build_recovery_prompt(
        config, session_name="architect_demo",
        project_key="demo", task_prompt="do the thing",
    )
    rendered = prompt.render()
    assert rendered.startswith(f"{AUTH_MARKER_PREFIX}{token}]\n")
    assert prompt.is_fallback is True


def test_build_recovery_prompt_no_token_renders_legacy(
    tmp_path: Path,
) -> None:
    """Session with no auth_token -> preamble renders without marker.

    Backward compat invariant: sessions launched before Lever 2
    migrated continue to receive the un-marked recovery preamble until
    the next ``write_config`` mints them a token.
    """
    config = _config_with_sessions(
        tmp_path, architect_token="", architect_alias="demo",
    )
    prompt = build_recovery_prompt(
        config, session_name="architect_demo",
        project_key="demo", task_prompt="do the thing",
    )
    rendered = prompt.render()
    assert AUTH_MARKER_PREFIX not in rendered


def test_session_auth_token_lookup_returns_empty_for_unknown(
    tmp_path: Path,
) -> None:
    config = _config_with_sessions(tmp_path)
    assert _session_auth_token(config, "ghost_session") == ""
    assert _session_auth_token(config, None) == ""
    assert _session_auth_token(config, "") == ""


# ---------------------------------------------------------------------------
# _architect_auth_token (dispatcher helper)
# ---------------------------------------------------------------------------


def test_architect_auth_token_returns_token_for_configured_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatcher helper loads the config and returns the
    architect session's token. Smoke-tests the integration boundary;
    the unit-level marker formatting is covered above."""
    from pollypm.config import write_config
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _architect_auth_token,
    )

    token = "01" * 32
    config = _config_with_sessions(
        tmp_path, architect_token=token, architect_alias="demo",
    )
    # Add the boilerplate that ``write_config`` expects.
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

    looked_up = _architect_auth_token(config_path, "demo")
    assert looked_up == token


def test_architect_auth_token_returns_empty_for_unknown_project(
    tmp_path: Path,
) -> None:
    from pollypm.config import write_config
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _architect_auth_token,
    )

    config = _config_with_sessions(
        tmp_path, architect_token="ff" * 32, architect_alias="demo",
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

    assert _architect_auth_token(config_path, "no_such_project") == ""


def test_architect_auth_token_handles_missing_config(tmp_path: Path) -> None:
    """Missing config path -> empty string fallback (no crash)."""
    from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
        _architect_auth_token,
    )

    assert _architect_auth_token(tmp_path / "nonexistent.toml", "demo") == ""
