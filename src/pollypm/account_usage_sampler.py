"""Background account-usage sampling.

Contract:
- Input: a PollyPM config path plus one or more configured account keys.
- Output: structured usage samples persisted to ``account_usage``.
- Side effects: launches short-lived tmux probe sessions, parses provider
  usage output, writes cached rows into the state store, and tears the probe
  sessions down.
- Invariants: readers consume cached usage from the database; live provider
  probes belong only in this module.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from pollypm.acct import collect_usage_snapshot
from pollypm.config import load_config
from pollypm.models import ProviderKind, SessionConfig
from pollypm.providers import get_provider
from pollypm.runtimes import get_runtime
from pollypm.session_services import create_tmux_client
from pollypm.storage.state import AccountUsageRecord, StateStore

logger = logging.getLogger(__name__)

# Session-name prefix for the throwaway tmux sessions this module spawns.
# Boot-time orphan sweeps and the per-probe cleanup both pivot on this
# prefix — keep them in sync. (#1009)
USAGE_PROBE_SESSION_PREFIX = "pm-usage-"


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
    config_path: Path,
    account_name: str,
    *,
    tmux_client=None,
) -> AccountUsageSample:
    """Refresh one account's cached usage row from a live provider probe."""
    config = load_config(config_path)
    try:
        account = config.accounts[account_name]
    except KeyError as exc:
        raise KeyError(f"Unknown account {account_name!r}") from exc
    if account.home is None:
        raise ValueError(
            f"Account {account_name!r} does not have an isolated home configured."
        )

    with StateStore(config.project.state_db) as store:
        cached = store.get_account_usage(account_name)

    try:
        sample = collect_account_usage_sample(
            config_path,
            account_name,
            tmux_client=tmux_client,
        )
    except Exception as exc:  # noqa: BLE001
        raw_text = str(exc)
        lowered = raw_text.lower()
        if account.provider is ProviderKind.CLAUDE and "not authenticated" in lowered:
            health = "auth-broken"
            usage_summary = "usage refresh failed · Claude still opens the login flow"
        else:
            health = cached.health if cached is not None else "unknown"
            usage_summary = f"usage refresh failed · {raw_text}"
        sample = AccountUsageSample(
            account_name=account_name,
            provider=account.provider,
            plan=(cached.plan if cached is not None else "unknown"),
            health=health,
            usage_summary=usage_summary,
            raw_text=raw_text,
            used_pct=(cached.used_pct if cached is not None else None),
            remaining_pct=(cached.remaining_pct if cached is not None else None),
            reset_at=(cached.reset_at if cached is not None else None),
            period_label=(cached.period_label if cached is not None else None),
        )

    persist_account_usage_sample(config_path, sample)
    return sample


def refresh_all_account_usage(
    config_path: Path,
    *,
    account_names: list[str] | None = None,
    tmux_client=None,
) -> list[AccountUsageSample]:
    """Refresh every requested account and continue past per-account failures."""
    config = load_config(config_path)
    names = list(config.accounts.keys()) if account_names is None else account_names
    samples: list[AccountUsageSample] = []
    for account_name in names:
        try:
            samples.append(
                refresh_account_usage(
                    config_path,
                    account_name,
                    tmux_client=tmux_client,
                )
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "account_usage_sampler: refresh failed for %s",
                account_name,
                exc_info=True,
            )
    return samples


def collect_account_usage_sample(
    config_path: Path,
    account_name: str,
    *,
    tmux_client=None,
) -> AccountUsageSample:
    """Collect one live usage sample without persisting it."""
    config = load_config(config_path)
    account = config.accounts[account_name]
    session = _probe_session_spec(config.project.root_dir, account_name, account.provider)
    tmux = tmux_client or create_tmux_client()
    probe_session = f"{USAGE_PROBE_SESSION_PREFIX}{account_name}-{int(time.time())}"
    try:
        tmux.create_session(
            probe_session,
            "probe",
            _build_probe_command(config, account, session),
        )
        snapshot = collect_usage_snapshot(
            account,
            tmux=tmux,
            target=f"{probe_session}:0",
            session=session,
        )
        return AccountUsageSample(
            account_name=account_name,
            provider=account.provider,
            plan=snapshot.plan or "unknown",
            health=snapshot.health or "unknown",
            usage_summary=snapshot.summary or "usage unavailable",
            raw_text=snapshot.raw_text or "",
            used_pct=snapshot.used_pct,
            remaining_pct=snapshot.remaining_pct,
            reset_at=snapshot.reset_at,
            period_label=snapshot.period_label,
        )
    finally:
        # Per-probe cleanup. Belt-and-suspenders for #1009 — first try the
        # wrapped client (lets fakes track the kill in tests); if THAT
        # raises, fall through to ``tmux kill-session`` directly so a
        # broken client wrapper can't leak the session. Either way the
        # ``=`` exact-target prefix prevents accidentally killing
        # something whose name starts with our probe prefix.
        _kill_probe_session(probe_session, tmux=tmux)


def _kill_probe_session(name: str, *, tmux=None) -> None:
    """Kill a single ``pm-usage-*`` session, swallowing any error."""
    if tmux is not None:
        try:
            if tmux.has_session(name):
                tmux.kill_session(name)
                return
            # Session already gone — nothing to clean up.
            return
        except Exception:  # noqa: BLE001
            logger.debug(
                "account_usage_sampler: wrapped tmux client failed to kill "
                "probe session %r; falling back to direct subprocess",
                name,
                exc_info=True,
            )
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={name}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "account_usage_sampler: direct tmux kill-session failed for %r; "
            "boot-time orphan sweep will catch this on next supervisor start",
            name,
            exc_info=True,
        )


def sweep_orphan_usage_sessions() -> int:
    """Kill every leaked ``pm-usage-*`` tmux session.

    Defense-in-depth for #1009 — even with a per-probe ``try/finally`` in
    :func:`collect_account_usage_sample`, a probe whose parent process is
    killed (cockpit restart, OS signal, scheduler timeout) leaves the
    tmux session behind. The supervisor calls this on boot to reap
    anything left from the previous lifetime.

    Returns the number of sessions killed (0 if tmux isn't running or no
    orphans exist). Never raises — failures fall through to ``debug``
    logging so a busted tmux server can't keep the supervisor from
    starting.
    """
    try:
        listing = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "account_usage_sampler.sweep: tmux list-sessions failed",
            exc_info=True,
        )
        return 0
    if listing.returncode != 0:
        # ``no server running`` exits non-zero; nothing to sweep.
        return 0
    killed = 0
    for raw in listing.stdout.splitlines():
        name = raw.strip()
        if not name.startswith(USAGE_PROBE_SESSION_PREFIX):
            continue
        try:
            result = subprocess.run(
                ["tmux", "kill-session", "-t", f"={name}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "account_usage_sampler.sweep: kill-session failed for %r",
                name, exc_info=True,
            )
            continue
        if result.returncode == 0:
            killed += 1
    if killed:
        logger.info(
            "account_usage_sampler.sweep: reaped %d orphan pm-usage-* "
            "session(s) on supervisor boot",
            killed,
        )
    return killed


def persist_account_usage_sample(config_path: Path, sample: AccountUsageSample) -> None:
    """Persist one usage sample into ``account_usage``."""
    config = load_config(config_path)
    with StateStore(config.project.state_db) as store:
        store.upsert_account_usage(
            account_name=sample.account_name,
            provider=sample.provider.value,
            plan=sample.plan,
            health=sample.health,
            usage_summary=sample.usage_summary,
            raw_text=sample.raw_text,
            used_pct=sample.used_pct,
            remaining_pct=sample.remaining_pct,
            reset_at=sample.reset_at,
            period_label=sample.period_label,
        )


def load_cached_account_usage(config_path: Path) -> dict[str, AccountUsageRecord]:
    """Return cached usage rows for configured accounts only."""
    config = load_config(config_path)
    account_names = list(getattr(config, "accounts", {}) or {})
    if not account_names:
        return {}
    with StateStore(config.project.state_db) as store:
        cached: dict[str, AccountUsageRecord] = {}
        for account_name in account_names:
            usage = store.get_account_usage(account_name)
            if usage is not None:
                cached[account_name] = usage
        return cached


def _probe_session_spec(root_dir: Path, account_name: str, provider: ProviderKind) -> SessionConfig:
    return SessionConfig(
        name=f"probe_{account_name}",
        role="usage-probe",
        provider=provider,
        account=account_name,
        cwd=root_dir,
        args=[],
    )


def _build_probe_command(config, account, session: SessionConfig) -> str:
    provider = get_provider(account.provider, root_dir=config.project.root_dir)
    launch = provider.build_launch_command(session, account)
    runtime = get_runtime(account.runtime, root_dir=config.project.root_dir)
    return runtime.wrap_command(launch, account, config.project)


__all__ = [
    "AccountUsageSample",
    "USAGE_PROBE_SESSION_PREFIX",
    "collect_account_usage_sample",
    "load_cached_account_usage",
    "persist_account_usage_sample",
    "refresh_account_usage",
    "refresh_all_account_usage",
    "sweep_orphan_usage_sessions",
]
