"""Tests for the architect 2-hour idle-close + resume-token mechanism.

Covers the three pieces in :mod:`pollypm.architect_lifecycle` plus
the manager-level :func:`pollypm.acct.manager.architect_launch_cmd`
that wraps them. These all live above the tmux + supervisor layer so
the tests don't need a tmux daemon — a temp ``StateStore`` is enough.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.acct import manager
from pollypm.acct.model import AccountConfig
from pollypm.architect_lifecycle import (
    DEFAULT_IDLE_THRESHOLD,
    architect_idle_for,
    clear_resume_token,
    close_idle_architect,
    resolve_launch_argv,
    should_close_architect,
)
from pollypm.models import ProviderKind
from pollypm.providers.claude import ClaudeProvider
from pollypm.providers.codex import CodexProvider
from pollypm.storage.state import StateStore


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


@pytest.fixture
def claude_account() -> AccountConfig:
    return AccountConfig(
        name="claude_test", provider=ProviderKind.CLAUDE, home=Path.home(),
    )


@pytest.fixture
def codex_account() -> AccountConfig:
    return AccountConfig(
        name="codex_test", provider=ProviderKind.CODEX, home=Path.home(),
    )


def _record_consistent_heartbeats(store: StateStore, session_name: str, count: int = 3) -> None:
    for _ in range(count):
        store.record_heartbeat(
            session_name=session_name, tmux_window=session_name, pane_id="%0",
            pane_command="claude", pane_dead=False, log_bytes=0,
            snapshot_path="/tmp/snap", snapshot_hash="deadbeef",
        )


def test_architect_idle_for_returns_none_with_too_few_heartbeats(store):
    _record_consistent_heartbeats(store, "architect_x", count=2)
    assert architect_idle_for(store, "architect_x") is None


def test_architect_idle_for_returns_none_when_hashes_diverge(store):
    for h in ("a", "b", "c"):
        store.record_heartbeat(
            session_name="architect_y", tmux_window="architect-y", pane_id="%0",
            pane_command="claude", pane_dead=False, log_bytes=0,
            snapshot_path="/tmp/snap", snapshot_hash=h,
        )
    assert architect_idle_for(store, "architect_y") is None


def test_architect_idle_for_returns_duration_on_consistent_run(store):
    _record_consistent_heartbeats(store, "architect_z")
    idle = architect_idle_for(store, "architect_z")
    assert idle is not None
    # Three back-to-back inserts measure as near-zero idle.
    assert idle < timedelta(seconds=10)


def test_should_close_architect_role_guard(store):
    _record_consistent_heartbeats(store, "worker_z")
    assert not should_close_architect(
        store, "worker_z", "worker", threshold=timedelta(seconds=0),
    )


def test_should_close_architect_threshold_respected(store):
    _record_consistent_heartbeats(store, "architect_a")
    # Default 2hr threshold won't trigger on fresh inserts.
    assert not should_close_architect(store, "architect_a", "architect")
    # Zero threshold trips immediately.
    assert should_close_architect(
        store, "architect_a", "architect", threshold=timedelta(seconds=0),
    )


def test_close_idle_architect_persists_token_when_session_exists(store, claude_account):
    """Real session UUID lookup against the live test environment."""
    killed: list[str] = []
    captured = close_idle_architect(
        store=store,
        provider=ClaudeProvider(),
        account=claude_account,
        project_key="passgen",
        cwd=Path("/Users/sam/dev/pollypm"),  # has real Claude transcripts
        tmux_kill_window=killed.append,
        window_target="storage:architect-passgen",
        last_active_at="2026-04-19T10:00:00+00:00",
    )
    assert captured is not None  # we ARE running under a Claude session
    assert killed == ["storage:architect-passgen"]
    rec = store.get_architect_resume_token("passgen")
    assert rec is not None
    assert rec.provider == "claude"
    assert rec.session_id == captured


def test_close_idle_architect_kills_window_even_without_session(store, tmp_path):
    """If the provider has no session for cwd, the window still gets killed.

    A leak is worse than a missing resume token — the close path
    proceeds even when the provider has nothing to capture.
    """
    empty_account = AccountConfig(
        name="empty", provider=ProviderKind.CLAUDE, home=tmp_path,  # empty home
    )
    killed: list[str] = []
    captured = close_idle_architect(
        store=store,
        provider=ClaudeProvider(),
        account=empty_account,
        project_key="empty_proj",
        cwd=Path("/nonexistent"),
        tmux_kill_window=killed.append,
        window_target="storage:architect-empty",
        last_active_at="2026-04-19T10:00:00+00:00",
    )
    assert captured is None
    assert killed == ["storage:architect-empty"]
    # No token persisted when there was nothing to capture.
    assert store.get_architect_resume_token("empty_proj") is None


def test_resolve_launch_argv_falls_back_to_fresh_when_no_token(store, claude_account):
    argv, resumed = resolve_launch_argv(
        store=store, provider=ClaudeProvider(), account=claude_account,
        project_key="passgen", fresh_args=["--agent", "architect"],
    )
    assert resumed is False
    assert argv[0] == "claude"
    assert "--resume" not in argv


def test_resolve_launch_argv_uses_token_when_available(store, claude_account):
    store.upsert_architect_resume_token(
        project_key="passgen", provider="claude",
        session_id="warm-uuid", last_active_at="2026-04-19T10:00:00+00:00",
    )
    argv, resumed = resolve_launch_argv(
        store=store, provider=ClaudeProvider(), account=claude_account,
        project_key="passgen", fresh_args=["--agent", "architect"],
    )
    assert resumed is True
    assert "--dangerously-skip-permissions" in argv
    assert "warm-uuid" in argv


def test_resolve_launch_argv_skips_token_for_different_provider(store, codex_account):
    """Token persisted under claude must NOT be used to resume codex.

    Provider mismatch (e.g. account was switched) means the warm
    UUID isn't valid for the current provider — we'd rather restart
    fresh than launch with a bogus session ID.
    """
    store.upsert_architect_resume_token(
        project_key="passgen", provider="claude",
        session_id="claude-uuid", last_active_at="2026-04-19T10:00:00+00:00",
    )
    argv, resumed = resolve_launch_argv(
        store=store, provider=CodexProvider(), account=codex_account,
        project_key="passgen", fresh_args=["--agent", "architect"],
    )
    assert resumed is False
    assert argv[0] == "codex"


def test_clear_resume_token(store):
    store.upsert_architect_resume_token(
        project_key="x", provider="claude", session_id="u",
        last_active_at="2026-04-19T10:00:00+00:00",
    )
    assert store.get_architect_resume_token("x") is not None
    clear_resume_token(store, "x")
    assert store.get_architect_resume_token("x") is None


def test_codex_resume_argv_uses_subcommand_form():
    """Codex resume is a subcommand (`resume <id>`), not a flag (`--resume <id>`)."""
    from pollypm.providers.codex.resume import resume_argv

    argv = resume_argv("u-1", ["--agent", "architect"])
    assert argv == [
        "codex", "--dangerously-skip-permissions",
        "resume", "u-1", "--agent", "architect",
    ]


def test_claude_resume_argv_uses_flag_form():
    from pollypm.providers.claude.resume import resume_argv

    argv = resume_argv("u-2", ["--agent", "architect"])
    assert argv == [
        "claude", "--dangerously-skip-permissions",
        "--resume", "u-2", "--agent", "architect",
    ]


def test_manager_architect_launch_cmd_routes_through_token(store, claude_account):
    store.upsert_architect_resume_token(
        project_key="passgen", provider="claude", session_id="warm-id",
        last_active_at="2026-04-19T10:00:00+00:00",
    )
    argv, resumed = manager.architect_launch_cmd(
        claude_account, ["--agent", "architect"],
        project_key="passgen", state_store=store,
    )
    assert resumed
    assert "warm-id" in argv


def test_manager_latest_session_id_passes_through(claude_account):
    sid = manager.latest_session_id(claude_account, Path("/Users/sam/dev/pollypm"))
    # Test envs that have run claude here will have a session; tolerate
    # both outcomes — what matters is the function returns a string or None,
    # not that it raises.
    assert sid is None or isinstance(sid, str)


def test_default_threshold_is_two_hours():
    assert DEFAULT_IDLE_THRESHOLD == timedelta(hours=2)
