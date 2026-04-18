"""Unit tests for account capacity probes and auto-failover."""

from pathlib import Path

import pytest

from pollypm.capacity import (
    CapacityProbeResult,
    CapacityState,
    FailoverCandidate,
    FailoverDecision,
    FAILOVER_TRIGGERS,
    PROACTIVE_ROLLOVER_THRESHOLD_PCT,
    RECOVERY_PRIORITY,
    _health_to_state,
    _parse_remaining_pct,
    account_needs_proactive_rollover,
    probe_capacity,
    probe_all_accounts,
    select_failover_account,
    can_failover_session,
    recovery_order,
    persist_capacity_probe,
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
from pollypm.storage.state import StateStore


def _config(tmp_path: Path) -> PollyPMConfig:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return PollyPMConfig(
        project=ProjectSettings(
            name="TestProject",
            root_dir=project_root,
            base_dir=project_root / ".pollypm",
            logs_dir=project_root / ".pollypm/logs",
            snapshots_dir=project_root / ".pollypm/snapshots",
            state_db=project_root / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="claude_main",
            failover_enabled=True,
            failover_accounts=["claude_backup", "codex_main"],
        ),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm" / "homes" / "claude_main",
            ),
            "claude_backup": AccountConfig(
                name="claude_backup",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm" / "homes" / "claude_backup",
            ),
            "codex_main": AccountConfig(
                name="codex_main",
                provider=ProviderKind.CODEX,
                home=project_root / ".pollypm" / "homes" / "codex_main",
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
            "worker_a": SessionConfig(
                name="worker_a",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=project_root,
            ),
        },
        projects={},
    )


def _store(tmp_path: Path) -> StateStore:
    db_path = tmp_path / "state.db"
    return StateStore(db_path)


# ---------------------------------------------------------------------------
# CapacityState
# ---------------------------------------------------------------------------


class TestCapacityState:
    def test_failover_triggers_include_expected_states(self) -> None:
        assert CapacityState.EXHAUSTED in FAILOVER_TRIGGERS
        assert CapacityState.AUTH_BROKEN in FAILOVER_TRIGGERS
        assert CapacityState.THROTTLED in FAILOVER_TRIGGERS
        assert CapacityState.SIGNED_OUT in FAILOVER_TRIGGERS

    def test_healthy_not_in_triggers(self) -> None:
        assert CapacityState.HEALTHY not in FAILOVER_TRIGGERS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHealthToState:
    def test_known_states(self) -> None:
        assert _health_to_state("healthy") == CapacityState.HEALTHY
        assert _health_to_state("capacity-exhausted") == CapacityState.EXHAUSTED
        assert _health_to_state("auth-broken") == CapacityState.AUTH_BROKEN

    def test_unknown_state(self) -> None:
        assert _health_to_state("something-weird") == CapacityState.UNKNOWN


class TestParseRemainingPct:
    def test_parses_percentage(self) -> None:
        assert _parse_remaining_pct("75% left this week") == 75

    def test_no_percentage(self) -> None:
        assert _parse_remaining_pct("usage unavailable") is None

    def test_zero_percent(self) -> None:
        assert _parse_remaining_pct("0% left") == 0


# ---------------------------------------------------------------------------
# Capacity probing
# ---------------------------------------------------------------------------


class TestProbeCapacity:
    def test_unknown_account(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        result = probe_capacity(config, store, "nonexistent")
        assert result.state == CapacityState.UNKNOWN
        assert "not found" in result.reason

    def test_no_usage_data(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        result = probe_capacity(config, store, "claude_main")
        assert result.state == CapacityState.UNKNOWN
        assert "No usage data" in result.reason

    def test_healthy_from_usage(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        store.upsert_account_usage(
            account_name="claude_main",
            provider="claude",
            plan="max",
            health="healthy",
            usage_summary="75% left this week",
            raw_text="",
        )
        result = probe_capacity(config, store, "claude_main")
        assert result.state == CapacityState.HEALTHY
        assert result.remaining_pct == 75

    def test_exhausted_from_runtime(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        store.upsert_account_usage(
            account_name="claude_main",
            provider="claude",
            plan="max",
            health="healthy",
            usage_summary="was healthy",
            raw_text="",
        )
        store.upsert_account_runtime(
            account_name="claude_main",
            provider="claude",
            status="capacity-exhausted",
            reason="rate limited",
        )
        result = probe_capacity(config, store, "claude_main")
        assert result.state == CapacityState.EXHAUSTED


class TestProbeAllAccounts:
    def test_probes_all(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        results = probe_all_accounts(config, store)
        assert len(results) == 3
        assert all(r.account_name in config.accounts for r in results)


class TestAccountNeedsProactiveRollover:
    def test_threshold_constant_is_ten(self) -> None:
        # Pins the 90%-used trigger from issue #103.
        assert PROACTIVE_ROLLOVER_THRESHOLD_PCT == 10

    def test_healthy_account_above_threshold(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        store.upsert_account_usage(
            account_name="claude_main", provider="claude", plan="max",
            health="healthy", usage_summary="25% left this week", raw_text="",
        )
        needs_roll, probe = account_needs_proactive_rollover(config, store, "claude_main")
        assert needs_roll is False
        assert probe.remaining_pct == 25

    def test_healthy_at_threshold_triggers(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        store.upsert_account_usage(
            account_name="claude_main", provider="claude", plan="max",
            health="healthy", usage_summary="10% left this week", raw_text="",
        )
        needs_roll, probe = account_needs_proactive_rollover(config, store, "claude_main")
        assert needs_roll is True
        assert probe.remaining_pct == 10

    def test_healthy_below_threshold_triggers(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        store.upsert_account_usage(
            account_name="claude_main", provider="claude", plan="max",
            health="healthy", usage_summary="5% left this week", raw_text="",
        )
        needs_roll, _ = account_needs_proactive_rollover(config, store, "claude_main")
        assert needs_roll is True

    def test_unknown_usage_does_not_trigger(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        # No usage_summary with a percentage.
        store.upsert_account_usage(
            account_name="claude_main", provider="claude", plan="max",
            health="healthy", usage_summary="max", raw_text="",
        )
        needs_roll, _ = account_needs_proactive_rollover(config, store, "claude_main")
        assert needs_roll is False

    def test_already_exhausted_handled_by_hard_path(self, tmp_path: Path) -> None:
        # An exhausted account is handled by FAILOVER_TRIGGERS, not proactive.
        config = _config(tmp_path)
        store = _store(tmp_path)
        store.upsert_account_usage(
            account_name="claude_main", provider="claude", plan="max",
            health="capacity-exhausted", usage_summary="0% left", raw_text="",
        )
        needs_roll, _ = account_needs_proactive_rollover(config, store, "claude_main")
        assert needs_roll is False

    def test_custom_threshold(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        store.upsert_account_usage(
            account_name="claude_main", provider="claude", plan="max",
            health="healthy", usage_summary="15% left this week", raw_text="",
        )
        # At default 10 threshold, 15% should not roll.
        assert account_needs_proactive_rollover(config, store, "claude_main")[0] is False
        # At a custom 20% threshold, 15% should roll.
        assert account_needs_proactive_rollover(
            config, store, "claude_main", threshold_pct=20,
        )[0] is True


# ---------------------------------------------------------------------------
# Failover selection
# ---------------------------------------------------------------------------


class TestSelectFailoverAccount:
    def test_failover_disabled(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        config.pollypm.failover_enabled = False
        store = _store(tmp_path)
        decision = select_failover_account(config, store, "claude_main")
        assert not decision.should_failover
        assert "not enabled" in decision.reason

    def test_no_failover_if_healthy(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        store.upsert_account_usage(
            account_name="claude_main",
            provider="claude",
            plan="max",
            health="healthy",
            usage_summary="75% left",
            raw_text="",
        )
        decision = select_failover_account(config, store, "claude_main")
        assert not decision.should_failover

    def test_selects_same_provider_non_controller(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        # Mark claude_main as exhausted
        store.upsert_account_runtime(
            account_name="claude_main",
            provider="claude",
            status="capacity-exhausted",
            reason="rate limited",
        )
        # Mark claude_backup as healthy
        store.upsert_account_usage(
            account_name="claude_backup",
            provider="claude",
            plan="max",
            health="healthy",
            usage_summary="50% left",
            raw_text="",
        )
        # Mark codex_main as healthy too
        store.upsert_account_usage(
            account_name="codex_main",
            provider="codex",
            plan="pro",
            health="healthy",
            usage_summary="80% left",
            raw_text="",
        )

        decision = select_failover_account(config, store, "claude_main")
        assert decision.should_failover
        # Should prefer claude_backup (same provider, non-controller)
        assert decision.selected_account == "claude_backup"

    def test_selects_different_provider_when_same_unavailable(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        # Mark claude_main as exhausted
        store.upsert_account_runtime(
            account_name="claude_main",
            provider="claude",
            status="capacity-exhausted",
            reason="rate limited",
        )
        # Mark claude_backup as also exhausted
        store.upsert_account_runtime(
            account_name="claude_backup",
            provider="claude",
            status="capacity-exhausted",
            reason="also rate limited",
        )
        # codex_main is healthy
        store.upsert_account_usage(
            account_name="codex_main",
            provider="codex",
            plan="pro",
            health="healthy",
            usage_summary="80% left",
            raw_text="",
        )

        decision = select_failover_account(config, store, "claude_main")
        assert decision.should_failover
        assert decision.selected_account == "codex_main"

    def test_no_candidates_available(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        # All accounts exhausted
        for name in ("claude_main", "claude_backup", "codex_main"):
            store.upsert_account_runtime(
                account_name=name,
                provider="claude",
                status="capacity-exhausted",
                reason="rate limited",
            )

        decision = select_failover_account(config, store, "claude_main")
        assert decision.should_failover
        assert decision.selected_account is None
        assert "No healthy accounts" in decision.reason

    def test_unknown_failed_account(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        decision = select_failover_account(config, store, "nonexistent")
        assert not decision.should_failover


# ---------------------------------------------------------------------------
# Lease-aware failover
# ---------------------------------------------------------------------------


class TestCanFailoverSession:
    def test_no_lease_allows_failover(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        allowed, reason = can_failover_session(store, "worker_a")
        assert allowed
        assert reason == ""

    def test_human_lease_blocks_failover(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_lease("worker_a", "human", "user is typing")
        allowed, reason = can_failover_session(store, "worker_a")
        assert not allowed
        assert "human lease" in reason

    def test_non_human_lease_allows_failover(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_lease("worker_a", "system", "automated task")
        allowed, reason = can_failover_session(store, "worker_a")
        assert allowed


# ---------------------------------------------------------------------------
# Recovery priority
# ---------------------------------------------------------------------------


class TestRecoveryOrder:
    def test_heartbeat_first(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        store = _store(tmp_path)
        order = recovery_order(config, store)
        assert len(order) == 3
        assert order[0] == ("heartbeat", "heartbeat")
        assert order[1] == ("operator", "operator")

    def test_priority_constants(self) -> None:
        assert RECOVERY_PRIORITY[0] == "heartbeat"
        assert RECOVERY_PRIORITY[1] == "operator"


# ---------------------------------------------------------------------------
# Persist capacity state
# ---------------------------------------------------------------------------


class TestPersistCapacityProbe:
    def test_persists_to_store(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        result = CapacityProbeResult(
            account_name="test",
            provider=ProviderKind.CLAUDE,
            state=CapacityState.HEALTHY,
            remaining_pct=75,
            reason="75% left",
        )
        persist_capacity_probe(store, result)

        usage = store.get_account_usage("test")
        assert usage is not None
        assert usage.health == "healthy"
        assert usage.usage_summary == "75% left"
