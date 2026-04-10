"""Account capacity probes and automatic failover.

Probes account capacity state (exhausted, throttled, auth-broken) and
selects failover accounts using a defined priority order. Respects
human leases during failover.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pollypm.models import AccountConfig, PollyPMConfig, ProviderKind
from pollypm.storage.state import StateStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Capacity states
# ---------------------------------------------------------------------------


class CapacityState(StrEnum):
    HEALTHY = "healthy"
    THROTTLED = "throttled"
    EXHAUSTED = "capacity-exhausted"
    AUTH_BROKEN = "auth-broken"
    SIGNED_OUT = "signed-out"
    UNKNOWN = "unknown"


# Health values that trigger failover
FAILOVER_TRIGGERS = frozenset({
    CapacityState.EXHAUSTED,
    CapacityState.AUTH_BROKEN,
    CapacityState.SIGNED_OUT,
    CapacityState.THROTTLED,
})

# Recovery priority: which sessions to recover first when capacity returns
RECOVERY_PRIORITY = (
    "heartbeat",
    "operator",
    "human-interrupted",
    "preempted",
    "new-work",
)


@dataclass(slots=True)
class CapacityProbeResult:
    """Result of probing a single account's capacity."""

    account_name: str
    provider: ProviderKind
    state: CapacityState
    remaining_pct: int | None = None
    reset_time: str | None = None
    reason: str = ""


@dataclass(slots=True)
class FailoverCandidate:
    """A candidate account for failover."""

    account_name: str
    provider: ProviderKind
    priority: int  # lower is better
    reason: str = ""


@dataclass(slots=True)
class FailoverDecision:
    """The result of a failover evaluation."""

    should_failover: bool
    failed_account: str
    selected_account: str | None = None
    reason: str = ""
    candidates_evaluated: int = 0


# ---------------------------------------------------------------------------
# Capacity probing
# ---------------------------------------------------------------------------


def probe_capacity(
    config: PollyPMConfig,
    store: StateStore,
    account_name: str,
) -> CapacityProbeResult:
    """Probe the capacity state for a single account from stored data.

    This reads from the SQLite registry (populated by the usage probe
    in accounts.py) rather than making live API calls.
    """
    account = config.accounts.get(account_name)
    if account is None:
        return CapacityProbeResult(
            account_name=account_name,
            provider=ProviderKind.CLAUDE,
            state=CapacityState.UNKNOWN,
            reason="Account not found in config",
        )

    # Read cached capacity from SQLite
    usage = store.get_account_usage(account_name)
    runtime = store.get_account_runtime(account_name)

    # Runtime status takes precedence (captures live failures)
    if runtime and runtime.status in FAILOVER_TRIGGERS:
        return CapacityProbeResult(
            account_name=account_name,
            provider=account.provider,
            state=CapacityState(runtime.status),
            reason=runtime.reason,
            reset_time=runtime.available_at,
        )

    if usage is None:
        return CapacityProbeResult(
            account_name=account_name,
            provider=account.provider,
            state=CapacityState.UNKNOWN,
            reason="No usage data available",
        )

    state = _health_to_state(usage.health)
    remaining = _parse_remaining_pct(usage.usage_summary)

    return CapacityProbeResult(
        account_name=account_name,
        provider=account.provider,
        state=state,
        remaining_pct=remaining,
        reason=usage.usage_summary,
    )


def probe_all_accounts(
    config: PollyPMConfig,
    store: StateStore,
) -> list[CapacityProbeResult]:
    """Probe capacity for all configured accounts."""
    return [
        probe_capacity(config, store, name)
        for name in config.accounts
    ]


# ---------------------------------------------------------------------------
# Failover selection
# ---------------------------------------------------------------------------


def select_failover_account(
    config: PollyPMConfig,
    store: StateStore,
    failed_account: str,
) -> FailoverDecision:
    """Select the best failover account when one fails.

    Selection priority:
    1. Healthy non-controller same provider
    2. Healthy non-controller different provider
    3. Controller same provider (if not the failed one)
    4. Controller different provider (if not the failed one)
    """
    if not config.pollypm.failover_enabled:
        return FailoverDecision(
            should_failover=False,
            failed_account=failed_account,
            reason="Failover is not enabled",
        )

    failed_config = config.accounts.get(failed_account)
    if failed_config is None:
        return FailoverDecision(
            should_failover=False,
            failed_account=failed_account,
            reason="Failed account not found in config",
        )

    # Check if the failed account actually needs failover
    probe = probe_capacity(config, store, failed_account)
    if probe.state not in FAILOVER_TRIGGERS:
        return FailoverDecision(
            should_failover=False,
            failed_account=failed_account,
            reason=f"Account state is {probe.state}, no failover needed",
        )

    controller = config.pollypm.controller_account
    failed_provider = failed_config.provider

    # Build candidate list with priorities
    candidates: list[FailoverCandidate] = []

    for name, account in config.accounts.items():
        if name == failed_account:
            continue

        capacity = probe_capacity(config, store, name)
        if capacity.state in FAILOVER_TRIGGERS:
            continue  # Skip accounts that are also failing

        is_controller = (name == controller)
        same_provider = (account.provider == failed_provider)

        if not is_controller and same_provider:
            priority = 0  # Best: non-controller, same provider
        elif not is_controller and not same_provider:
            priority = 1  # Good: non-controller, different provider
        elif is_controller and same_provider:
            priority = 2  # Acceptable: controller, same provider
        else:
            priority = 3  # Last resort: controller, different provider

        candidates.append(FailoverCandidate(
            account_name=name,
            provider=account.provider,
            priority=priority,
            reason=f"priority={priority}, state={capacity.state}",
        ))

    if not candidates:
        return FailoverDecision(
            should_failover=True,
            failed_account=failed_account,
            reason="No healthy accounts available for failover",
            candidates_evaluated=0,
        )

    # Sort by priority (lower is better)
    candidates.sort(key=lambda c: c.priority)
    best = candidates[0]

    return FailoverDecision(
        should_failover=True,
        failed_account=failed_account,
        selected_account=best.account_name,
        reason=f"Selected {best.account_name} ({best.provider}) with priority {best.priority}",
        candidates_evaluated=len(candidates),
    )


# ---------------------------------------------------------------------------
# Lease-aware failover
# ---------------------------------------------------------------------------


def can_failover_session(
    store: StateStore,
    session_name: str,
) -> tuple[bool, str]:
    """Check if a session can be failed over (not blocked by human lease)."""
    lease = store.get_lease(session_name)
    if lease is None:
        return True, ""

    if lease.owner == "human":
        return False, f"Session '{session_name}' has an active human lease: {lease.note}"

    return True, ""


# ---------------------------------------------------------------------------
# Recovery priority
# ---------------------------------------------------------------------------


def recovery_order(
    config: PollyPMConfig,
    store: StateStore,
) -> list[tuple[str, str]]:
    """Determine recovery order for sessions when capacity returns.

    Returns list of (session_name, recovery_category) sorted by priority.

    Priority order:
    1. heartbeat - must always be running
    2. operator - PM session
    3. human-interrupted - sessions that were using human lease
    4. preempted - sessions that were preempted by failover
    5. new-work - sessions waiting for capacity
    """
    sessions: list[tuple[str, str, int]] = []

    for session_name, session_config in config.sessions.items():
        runtime = store.get_session_runtime(session_name)

        if session_config.role == "heartbeat-supervisor":
            category = "heartbeat"
            priority = 0
        elif session_config.role == "operator-pm":
            category = "operator"
            priority = 1
        elif runtime and runtime.last_failure_type == "human-interrupted":
            category = "human-interrupted"
            priority = 2
        elif runtime and runtime.last_failure_type == "preempted":
            category = "preempted"
            priority = 3
        else:
            category = "new-work"
            priority = 4

        sessions.append((session_name, category, priority))

    sessions.sort(key=lambda s: s[2])
    return [(name, category) for name, category, _ in sessions]


# ---------------------------------------------------------------------------
# Persist capacity state
# ---------------------------------------------------------------------------


def persist_capacity_probe(
    store: StateStore,
    result: CapacityProbeResult,
) -> None:
    """Write probe results to the SQLite capacity registry."""
    store.upsert_account_usage(
        account_name=result.account_name,
        provider=result.provider.value,
        plan="",
        health=result.state.value,
        usage_summary=result.reason,
        raw_text="",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _health_to_state(health: str) -> CapacityState:
    """Convert a health string to a CapacityState enum."""
    try:
        return CapacityState(health)
    except ValueError:
        return CapacityState.UNKNOWN


def _parse_remaining_pct(summary: str) -> int | None:
    """Extract remaining percentage from a usage summary string."""
    import re
    match = re.search(r"(\d+)%\s*left", summary)
    if match:
        return int(match.group(1))
    return None
