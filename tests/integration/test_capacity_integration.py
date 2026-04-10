"""Integration tests for capacity probes and auto-failover."""

from pathlib import Path

from pollypm.capacity import (
    CapacityState,
    probe_all_accounts,
    probe_capacity,
    persist_capacity_probe,
    select_failover_account,
    can_failover_session,
    recovery_order,
    CapacityProbeResult,
)
from pollypm.models import (
    AccountConfig,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.storage.state import StateStore


def _config(tmp_path: Path) -> PollyPMConfig:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return PollyPMConfig(
        project=ProjectSettings(
            name="TestProject",
            root_dir=project_root,
            base_dir=project_root / ".pollypm-state",
            logs_dir=project_root / ".pollypm-state/logs",
            snapshots_dir=project_root / ".pollypm-state/snapshots",
            state_db=project_root / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="claude_main",
            failover_enabled=True,
            failover_accounts=["claude_backup"],
        ),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm-state" / "homes" / "claude_main",
            ),
            "claude_backup": AccountConfig(
                name="claude_backup",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm-state" / "homes" / "claude_backup",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            ),
            "worker": SessionConfig(
                name="worker",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            ),
        },
        projects={},
    )


def test_full_failover_lifecycle(tmp_path: Path) -> None:
    """Test the complete lifecycle: probe → detect exhaustion → failover → recovery."""
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)

    # 1. Initially both accounts healthy
    store.upsert_account_usage(account_name="claude_main", provider="claude", plan="max", health="healthy", usage_summary="90% left", raw_text="")
    store.upsert_account_usage(account_name="claude_backup", provider="claude", plan="max", health="healthy", usage_summary="80% left", raw_text="")

    probe_main = probe_capacity(config, store, "claude_main")
    assert probe_main.state == CapacityState.HEALTHY
    assert probe_main.remaining_pct == 90

    # 2. Main account becomes exhausted
    store.upsert_account_runtime(account_name="claude_main", provider="claude", status="capacity-exhausted", reason="rate limited")

    probe_main = probe_capacity(config, store, "claude_main")
    assert probe_main.state == CapacityState.EXHAUSTED

    # 3. Failover selects backup
    decision = select_failover_account(config, store, "claude_main")
    assert decision.should_failover
    assert decision.selected_account == "claude_backup"
    assert decision.candidates_evaluated == 1

    # 4. Check lease doesn't block non-human sessions
    allowed, reason = can_failover_session(store, "worker")
    assert allowed

    # 5. Set human lease — should block failover
    store.set_lease("worker", "human", "user typing")
    allowed, reason = can_failover_session(store, "worker")
    assert not allowed

    # 6. Release lease and verify failover is allowed again
    store.clear_lease("worker")
    allowed, reason = can_failover_session(store, "worker")
    assert allowed

    # 7. Recovery order
    order = recovery_order(config, store)
    assert order[0][1] == "heartbeat"
    assert order[1][1] == "operator"


def test_persist_and_read_back(tmp_path: Path) -> None:
    """Test that capacity results survive persistence and read back."""
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)

    result = CapacityProbeResult(
        account_name="claude_main",
        provider=ProviderKind.CLAUDE,
        state=CapacityState.HEALTHY,
        remaining_pct=65,
        reason="65% left this week",
    )
    persist_capacity_probe(store, result)

    # Read back via probe
    probed = probe_capacity(config, store, "claude_main")
    assert probed.state == CapacityState.HEALTHY
    assert probed.remaining_pct == 65


def test_all_accounts_exhausted_no_failover_target(tmp_path: Path) -> None:
    """When all accounts are exhausted, failover has no target."""
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)

    store.upsert_account_runtime(account_name="claude_main", provider="claude", status="capacity-exhausted", reason="rate limited")
    store.upsert_account_runtime(account_name="claude_backup", provider="claude", status="capacity-exhausted", reason="also limited")

    decision = select_failover_account(config, store, "claude_main")
    assert decision.should_failover
    assert decision.selected_account is None

    # All probes should show exhausted
    all_probes = probe_all_accounts(config, store)
    assert all(p.state == CapacityState.EXHAUSTED for p in all_probes)


def test_auth_broken_triggers_failover(tmp_path: Path) -> None:
    """Auth-broken state should trigger failover."""
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)

    store.upsert_account_runtime(account_name="claude_main", provider="claude", status="auth-broken", reason="login expired")
    store.upsert_account_usage(account_name="claude_backup", provider="claude", plan="max", health="healthy", usage_summary="70% left", raw_text="")

    decision = select_failover_account(config, store, "claude_main")
    assert decision.should_failover
    assert decision.selected_account == "claude_backup"
