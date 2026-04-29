"""Controller-account probe orchestration.

Contract:
- Inputs: a controller account name plus callbacks that resolve the
  effective operator session/account for probing.
- Outputs: ``None`` on success; raises a three-question-rule
  ``RuntimeError`` on probe failure.
- Side effects: invokes the probe runner and inspects probe output.
- Invariants: probe errors always name the account, provider, and an
  operator fix path.
- Allowed dependencies: config models, probe execution, and error-format
  helpers. Does not own tmux, storage, or supervisor lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pollypm.errors import _last_lines, format_probe_failure
from pollypm.models import AccountConfig, PollyPMConfig, ProviderKind, SessionConfig


class _EffectiveSession(Protocol):
    def __call__(self, session: SessionConfig, controller_account: str | None = None) -> SessionConfig: ...


class _EffectiveAccount(Protocol):
    def __call__(self, session: SessionConfig, account: AccountConfig) -> AccountConfig: ...


class _ProbeAccount(Protocol):
    def __call__(self, account: AccountConfig) -> str: ...


@dataclass(slots=True)
class ControllerProbeService:
    """Validate the selected controller account before bootstrap."""

    config: PollyPMConfig
    effective_session: _EffectiveSession
    effective_account: _EffectiveAccount
    probe_account: _ProbeAccount

    def probe_controller_account(self, account_name: str) -> None:
        account = self.config.accounts[account_name]
        operator_session = self.effective_session(
            self.config.sessions["operator"],
            controller_account=account_name,
        )
        effective_account = self.effective_account(
            operator_session,
            self.config.accounts[operator_session.account],
        )
        output = self.probe_account(effective_account)
        lowered = output.lower()
        pane_tail = _last_lines(output, n=5)
        if effective_account.provider is ProviderKind.CLAUDE:
            if "ok" in lowered and "authentication" not in lowered:
                return
            raise RuntimeError(
                format_probe_failure(
                    provider="Claude",
                    account_name=account_name,
                    account_email=account.email,
                    reason=(
                        "the `claude -p 'Reply with ok'` probe did not "
                        "return 'ok' within the probe window"
                    ),
                    pane_tail=pane_tail,
                    fix=(
                        "open Settings > Accounts to check login state, then "
                        f"reconnect '{account_name}' if the session expired."
                    ),
                )
            )
        if effective_account.provider is ProviderKind.CODEX:
            if "usage limit" in lowered:
                raise RuntimeError(
                    format_probe_failure(
                        provider="Codex",
                        account_name=account_name,
                        account_email=account.email,
                        reason="the account is out of credits",
                        pane_tail=pane_tail,
                        fix=(
                            "open Settings > Accounts to switch the controller "
                            f"to a healthy account, or top up '{account_name}' "
                            "and restart Polly."
                        ),
                    )
                )
            if "not logged" in lowered or "login" in lowered:
                raise RuntimeError(
                    format_probe_failure(
                        provider="Codex",
                        account_name=account_name,
                        account_email=account.email,
                        reason="the account is not authenticated",
                        pane_tail=pane_tail,
                        fix=(
                            f"reconnect '{account_name}' in Settings > Accounts "
                            "and restart Polly."
                        ),
                    )
                )
            if "error:" in lowered:
                raise RuntimeError(
                    format_probe_failure(
                        provider="Codex",
                        account_name=account_name,
                        account_email=account.email,
                        reason=(
                            "the `codex exec 'Reply with ok'` probe "
                            "returned an unexpected response"
                        ),
                        pane_tail=pane_tail,
                        fix=(
                            f"reconnecting '{account_name}' in Settings > Accounts "
                            "usually clears this. If it persists, open the "
                            "account detail and inspect the raw provider output."
                        ),
                    )
                )
            return
        raise RuntimeError(
            f"Unsupported controller provider: {effective_account.provider.value}"
        )
