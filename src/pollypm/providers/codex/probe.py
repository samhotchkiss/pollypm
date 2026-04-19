"""Codex usage probe — Phase C of #397.

The probe boots a Codex session in a throwaway tmux pane, waits for the
``openai codex`` banner to render (Codex prints its quota summary on
the first screen), and parses the ``<n>% left`` fragment out of the
scrollback. This module owns the high-level entry point; the regex
lives in :mod:`.usage_parse` and the tmux plumbing stays in
``pollypm.accounts._run_usage_probe``.

The Phase A Protocol's ``probe_usage(AccountConfig) -> AccountStatus``
signature does not carry the ``config_path`` the legacy probe needs to
locate the state DB. Phase D will thread that context through; until
then :func:`probe_usage` raises ``NotImplementedError`` with a
three-question-rule message that points callers at
``pollypm.accounts.probe_account_usage``.
"""

from __future__ import annotations

from pollypm.acct.model import AccountConfig, AccountStatus


def probe_usage(account: AccountConfig) -> AccountStatus:
    """Return a fresh ``AccountStatus`` snapshot for ``account``.

    Not wired in Phase C — see module docstring for why. Callers get a
    three-question-rule ``NotImplementedError`` instead of a silent no-op
    so the failure surfaces at the call site, not three layers deeper.
    """
    raise NotImplementedError(
        f"The Codex provider's probe_usage is not yet wired through the "
        f"pollypm.acct substrate (account {account.name!r}).\n\n"
        f"Why: the legacy probe reads the project config path to locate "
        f"the state DB and capture isolation context, which the Phase A "
        f"Protocol does not yet carry. Phase D introduces the manager "
        f"that threads that context through.\n\n"
        f"Fix: call `pollypm.accounts.probe_account_usage(config_path, "
        f"account_name)` for now; it returns the same AccountStatus "
        f"shape and handles the state-db write."
    )


__all__ = ["probe_usage"]
