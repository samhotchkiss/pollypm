"""Centralized account manager for the ``pollypm.acct`` substrate.

Phase D of #397 introduces this module as the single provider-agnostic
orchestrator for account life-cycle operations. Every public helper in
this file takes an :class:`AccountConfig`, reads
``account.provider.value`` to resolve the adapter from the
entry-point-backed registry, and delegates to the
:class:`ProviderAdapter` Protocol method of the same name.

Import boundary (hard rule):
    This module **must not** import from ``pollypm.providers.*``. The
    manager is the consumer side of the Protocol; the provider packages
    are the producer side. Phase E removes the remaining legacy
    ``pollypm.accounts`` dispatcher once the last caller has migrated.

Public surface (re-exported from ``pollypm.acct``):
    * :func:`detect_logged_in` / :func:`detect_email`
    * :func:`probe_usage`
    * :func:`worker_launch_cmd` / :func:`isolated_env`
    * :func:`run_login_flow`
    * :func:`list_logged_in` / :func:`choose_healthy_for_worker`

The "choose" helper encodes the preference logic that used to live in
``pollypm.workers.auto_select_worker_account``: a caller-supplied
``preferred`` account wins if it is currently logged in; otherwise the
first healthy account in iteration order does; otherwise ``None``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .model import AccountConfig, AccountStatus
from .registry import get_provider


def _adapter_for(account: AccountConfig):
    """Resolve the ``ProviderAdapter`` for ``account.provider``.

    Thin internal helper that keeps the ``account.provider.value`` →
    ``get_provider(name)`` dispatch in one place. Raises
    :class:`pollypm.acct.errors.ProviderNotFound` when the provider
    string has no registered adapter.
    """
    return get_provider(account.provider.value)


def detect_logged_in(account: AccountConfig) -> bool:
    """Return True iff ``account`` currently holds valid credentials.

    Dispatches to the registered provider's
    :meth:`ProviderAdapter.detect_logged_in`. Side-effect-free.
    """
    return _adapter_for(account).detect_logged_in(account)


def detect_email(account: AccountConfig) -> str | None:
    """Return the authenticated email address for ``account``, or None.

    The return value is a presence check — for providers whose auth API
    does not expose the email (Claude Max 2.x) the adapter returns a
    stable non-None sentinel rather than leaking the raw ``null``.
    """
    return _adapter_for(account).detect_email(account)


def probe_usage(account: AccountConfig) -> AccountStatus:
    """Return a fresh :class:`AccountStatus` snapshot for ``account``.

    Delegates to the registered provider's
    :meth:`ProviderAdapter.probe_usage`. Callers that want cached data
    should read from the state store directly.
    """
    return _adapter_for(account).probe_usage(account)


def worker_launch_cmd(account: AccountConfig, args: list[str]) -> list[str]:
    """Return the argv to launch a worker session for ``account``.

    The returned list does not include env — env is delivered
    separately via :func:`isolated_env` so the runtime layer can merge
    it with its own env contributions.
    """
    return _adapter_for(account).worker_launch_cmd(account, args)


def architect_launch_cmd(
    account: AccountConfig,
    args: list[str],
    *,
    project_key: str,
    state_store: object,
) -> tuple[list[str], bool]:
    """Return ``(argv, resumed)`` for spawning the project's architect.

    Checks ``architect_resume_tokens`` for ``project_key`` and, if a
    token is present and was captured under the same provider as
    ``account``, builds the argv via
    :meth:`ProviderAdapter.resume_launch_cmd` so the architect comes
    back warm with its prior context. Otherwise falls through to
    :meth:`ProviderAdapter.worker_launch_cmd` for a fresh launch.

    Caller should clear the token (via
    ``state_store.clear_architect_resume_token``) once the resumed
    architect has confirmed startup. Leaving the token in place on a
    failed resume means the next attempt picks up from the same
    UUID — losing state only happens on an explicit clear.
    """
    from pollypm.architect_lifecycle import resolve_launch_argv

    return resolve_launch_argv(
        store=state_store,
        provider=_adapter_for(account),
        account=account,
        project_key=project_key,
        fresh_args=args,
    )


def latest_session_id(account: AccountConfig, cwd: object) -> str | None:
    """Return the newest provider session UUID for ``cwd`` under ``account``.

    Thin manager-side wrapper over
    :meth:`ProviderAdapter.latest_session_id` that lets callers stay
    on the manager surface instead of importing provider packages
    directly. Used by the architect-idle-close flow to capture a
    resume token before tearing down a quiet window.
    """
    return _adapter_for(account).latest_session_id(account, cwd)


def isolated_env(account: AccountConfig) -> dict[str, str]:
    """Return the env vars that pin the provider binary to ``account.home``.

    Returns an empty dict when ``account.home`` is ``None``. The
    non-empty result is purely additive — callers layer it onto
    whatever env the runtime provides.
    """
    if account.home is None:
        return {}
    return _adapter_for(account).isolated_env(account.home)


def run_login_flow(account: AccountConfig) -> None:
    """Drive the interactive login for ``account``.

    Blocks until the user completes login or aborts. Idempotent on
    already-logged-in accounts: running twice must not corrupt existing
    credentials.
    """
    _adapter_for(account).run_login_flow(account)


def list_logged_in(accounts: Iterable[AccountConfig]) -> list[AccountConfig]:
    """Return the subset of ``accounts`` that are currently logged in.

    Iteration order is preserved so callers can layer their own tiering
    on top (controller-first, provider-rank, etc.) without re-sorting.
    """
    return [account for account in accounts if detect_logged_in(account)]


def choose_healthy_for_worker(
    accounts: Iterable[AccountConfig],
    preferred: str | None = None,
) -> AccountConfig | None:
    """Pick a logged-in account to assign to a new worker session.

    Resolution order:

    1. If ``preferred`` names an account in ``accounts`` and that
       account is logged in, return it.
    2. Otherwise, return the first account in iteration order that is
       logged in.
    3. Otherwise, return ``None``.

    The caller owns the fallback behavior — the manager does not raise
    here because "no healthy account" is a routine condition during
    onboarding, not an error.
    """
    # Materialize once so we can scan twice without re-running the
    # upstream generator.
    materialized = list(accounts)

    if preferred is not None:
        for account in materialized:
            if account.name == preferred and detect_logged_in(account):
                return account

    for account in materialized:
        if detect_logged_in(account):
            return account

    return None


__all__ = [
    "choose_healthy_for_worker",
    "detect_email",
    "detect_logged_in",
    "isolated_env",
    "list_logged_in",
    "probe_usage",
    "run_login_flow",
    "worker_launch_cmd",
]
