"""Claude usage-probe implementation.

Phase B of #397 moves the Claude ``/usage`` probe loop into this
module. The two entry points are:

* :func:`collect_usage_snapshot` — the live tmux loop that sends
  ``/usage`` to an interactive Claude pane and parses the response.
  Used by :mod:`pollypm.accounts`'s probe runner. Returns a rich
  :class:`ProviderUsageSnapshot` with weekly percent + reset date.
* :func:`probe_usage` — the Protocol-shape entry point. Phase B ships
  it as a stub that redirects to :func:`pollypm.accounts.probe_account_usage`
  because the live probe needs the project's state DB, which the
  Protocol does not yet carry.

The tmux loop used to live inline in ``providers/claude.py``; it was
identical to the logic in :mod:`pollypm.accounts` apart from a few
health-tier thresholds. Consolidating the parser in
:mod:`pollypm.providers.claude.usage_parse` lets both the live tmux
loop and the batch-list screen share the same regex.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pollypm.models import AccountConfig, SessionConfig
from pollypm.provider_sdk import ProviderUsageSnapshot

from .usage_parse import parse_claude_usage_snapshot

if TYPE_CHECKING:
    from pollypm.acct.model import AccountStatus
    from pollypm.tmux.client import TmuxClient


def collect_usage_snapshot(
    tmux: TmuxClient,
    target: str,
    *,
    account: AccountConfig,
    session: SessionConfig,
) -> ProviderUsageSnapshot:
    """Drive a Claude pane through ``/usage`` and parse the response.

    The loop handles the three onboarding prompts the CLI can surface
    (theme, trust-folder, effort) and raises if the pane is not
    authenticated — callers translate that exception into the
    ``auth-broken`` probe failure.

    Raises:
        RuntimeError: the pane showed a ``please run /login`` or the
            probe timed out before reaching the prompt.
    """
    del account, session  # reserved for Phase D (per-account pacing)
    deadline = time.monotonic() + 60
    last_action = ""
    while time.monotonic() < deadline:
        pane = tmux.capture_pane(target, lines=320)
        lowered = pane.lower()
        if "select login method:" in lowered or "please run /login" in lowered:
            raise RuntimeError("Claude probe session is not authenticated.")
        if (
            "choose the text style that looks best with your terminal" in lowered
            and last_action != "theme"
        ):
            tmux.send_keys(target, "", press_enter=True)
            last_action = "theme"
            time.sleep(1)
            continue
        if (
            "quick safety check" in lowered
            and "yes, i trust this folder" in lowered
            and last_action != "trust"
        ):
            tmux.send_keys(target, "", press_enter=True)
            last_action = "trust"
            time.sleep(1)
            continue
        if (
            "we recommend medium effort for opus" in lowered
            and last_action != "effort"
        ):
            tmux.send_keys(target, "", press_enter=True)
            last_action = "effort"
            time.sleep(1)
            continue
        if "❯" in pane and (
            "welcome back" in lowered or "/usage" not in lowered
        ):
            tmux.send_keys(target, "/usage", press_enter=True)
            time.sleep(3)
            text = tmux.capture_pane(target, lines=320)
            return parse_claude_usage_snapshot(text)
        time.sleep(1)
    raise RuntimeError(
        "Claude probe session did not reach an interactive prompt in time."
    )


def probe_usage(account: AccountConfig) -> "AccountStatus":
    """Protocol-shape entry point for the Claude usage probe.

    Phase B keeps this as a redirect: the live probe needs a
    ``PollyPMConfig`` to locate the state DB and pass isolation context
    to the tmux runner. The Protocol signature intentionally takes only
    an :class:`AccountConfig`; Phase D widens it.

    Raises:
        NotImplementedError: always, with a pointer to the legacy entry.
    """
    del account  # unused in Phase B — see docstring
    raise NotImplementedError(
        "Phase B of #397 exposes Claude probe through the provider "
        "package but does not yet widen the ProviderAdapter Protocol "
        "to carry a config path for the state DB.\n\n"
        "Why: the probe writes refreshed usage into the project's "
        "state.db and layers the runtime-health record on top of the "
        "raw snapshot. The AccountConfig alone does not locate the DB.\n\n"
        "Fix: call `pollypm.accounts.probe_account_usage(config_path, "
        "account_name)` for now; Phase D routes this through the "
        "substrate."
    )


__all__ = ["collect_usage_snapshot", "probe_usage"]
