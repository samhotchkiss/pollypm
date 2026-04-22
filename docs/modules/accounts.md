**Last Verified:** 2026-04-22

## Summary

Accounts are declared in `pollypm.toml` under `[accounts.<name>]` and represent a specific provider + home + env triple (e.g. "claude signed in as pearl@swh.me, home at `~/.pollypm/homes/claude_1/`"). `pollypm.accounts` manages login state, cached usage snapshots, failover eligibility, and the `controller_account` designation.

The account layer does **not** decide launch commands (that's providers) or isolation (that's runtimes). It *does* decide whether an account is healthy, whether its cached usage is fresh, and which accounts a worker session can pick from.

Touch this module when changing login detection, usage-refresh cadence, or the `AccountStatus` shape the TUIs consume. Do not make provider-specific decisions here — delegate to the adapter or the accounts `acct/` protocol.

## Core Contracts

```python
# src/pollypm/accounts.py
@dataclass(slots=True)
class AccountStatus:
    key: str
    provider: ProviderKind
    email: str
    home: Path | None
    logged_in: bool
    plan: str
    health: str           # "healthy" | "auth-broken" | "exhausted" | "provider_outage" | "blocked"
    usage_summary: str
    # plus reason, available_at, access_expires_at, usage_updated_at, raw_text, percents, reset_at, period_label
    ...

def list_account_statuses(config_path: Path) -> list[AccountStatus]: ...
def list_cached_account_statuses(config_path: Path) -> list[AccountStatus]: ...
def add_account_via_login(config_path: Path, provider: ProviderKind) -> tuple[str, str]: ...
def relogin_account(config_path: Path, identifier: str) -> tuple[str, str]: ...
def remove_account(config_path: Path, identifier: str, *, delete_home: bool = False) -> tuple[str, str]: ...
def set_controller_account(config_path: Path, identifier: str) -> tuple[str, str]: ...
def set_open_permissions_default(config_path: Path, enabled: bool) -> bool: ...
def toggle_failover_account(config_path: Path, identifier: str) -> tuple[str, bool]: ...

# src/pollypm/account_usage_sampler.py
@dataclass(slots=True)
class AccountUsageSample:
    account_name: str
    provider: ProviderKind
    plan: str
    health: str
    usage_summary: str
    raw_text: str
    used_pct: int | None = None
    remaining_pct: int | None = None
    reset_at: str | None = None
    period_label: str | None = None

def refresh_account_usage(
    config_path: Path, account_name: str, *, tmux_client=None,
) -> AccountUsageSample: ...

# src/pollypm/workers.py
def auto_select_worker_account(
    config_path: Path, *, provider: ProviderKind | None = None,
) -> str: ...
```

## File Structure

- `src/pollypm/accounts.py` — `AccountStatus`, TUI-facing list/add/remove/relogin helpers.
- `src/pollypm/account_tui.py` — `pm account` TUI wiring.
- `src/pollypm/account_usage_sampler.py` — live probe → cached `account_usage` row.
- `src/pollypm/acct/` — substrate: `manager.py`, `model.py`, `protocol.py`, `registry.py`, `errors.py`.
- `src/pollypm/onboarding.py` — login windows, account home priming.
- `src/pollypm/capacity.py` — `CapacityState` + `FAILOVER_TRIGGERS`.
- `src/pollypm/supervision/account_probe.py` — background controller-probe for capacity classification.

## Implementation Details

- **Cached vs live.** `list_cached_account_statuses` reads `account_runtime` and `account_usage` rows from the state DB without probing. `list_account_statuses` also runs `detect_logged_in(account)` per account — more expensive, used when a user actively opens the accounts panel.
- **Usage refresh.** `account_usage_sampler.refresh_account_usage` launches a short-lived tmux probe session, runs the provider's usage command, parses the output, writes to `account_usage`, and tears down. Driven by the `account.usage_refresh` recurring handler (`@every 5m` — see `plugins_builtin/core_recurring/plugin.py`).
- **Health classification.** `health` values — `"healthy"`, `"auth-broken"`, `"exhausted"`, `"provider_outage"`, `"blocked"` — come from parsing the usage snapshot and from `account_runtime.status`. Only `"healthy"` accounts are eligible for new worker sessions.
- **Controller and failover.** `pollypm.controller_account` is the canonical account for control sessions (heartbeat + operator). `failover_accounts` lists backup accounts that `FAILOVER_TRIGGERS` can rotate to when the controller hits an exhausted / auth-broken state.
- **`auto_select_worker_account`.** Picks the best healthy logged-in account for a new worker. Ranks by (is-controller > is-used-by-control > other), then by provider (Codex > Claude by default), then by name. Raises `typer.BadParameter` if no candidate is healthy.
- **Home priming.** First launch writes the provider's CLI-config skeleton under `account.home`. `onboarding._prime_claude_home` handles Claude; Codex is handled by its adapter.

## Related Docs

- [modules/providers.md](providers.md) — executes usage probes via adapters.
- [modules/runtimes.md](runtimes.md) — consumes `account.home` + env vars.
- [modules/recovery.md](recovery.md) — reads `CapacityState` to pick failover action.
- [features/service-api.md](../features/service-api.md) — stable account-management surface.
