"""Error types for the ``pollypm.acct`` provider substrate.

Every message follows PollyPM's three-question rule (#240):

    * **what happened** — the trigger (e.g. "provider 'foo' is not
      registered").
    * **why it matters** — the consequence (e.g. "the account cannot be
      launched because no adapter knows how to drive it").
    * **how to fix it** — a concrete next step the user can take.
"""

from __future__ import annotations


class AcctError(Exception):
    """Base class for ``pollypm.acct`` errors.

    Provider substrate code raises subclasses so callers can catch the
    whole family with one ``except`` clause without pulling in unrelated
    ``Exception`` subclasses.
    """


class ProviderNotFound(AcctError):
    """Raised when ``get_provider(name)`` cannot resolve a provider.

    The message names the requested provider, explains why the call
    failed, and lists the providers that *are* registered so the user
    can pick one or fix the typo.
    """

    def __init__(self, name: str, *, available: list[str] | None = None) -> None:
        self.name = name
        self.available = list(available or [])
        available_str = ", ".join(self.available) if self.available else "(none registered)"
        super().__init__(
            f"No PollyPM provider is registered under the name {name!r}.\n\n"
            f"Why: provider adapters are loaded from the "
            f"`pollypm.provider` entry-point group; a missing name "
            f"means the plugin that ships this provider is not "
            f"installed or did not register itself.\n\n"
            f"Fix: install the plugin that ships the {name!r} provider, "
            f"or pick one of the providers that is already registered: "
            f"{available_str}."
        )


class AccountNotFound(AcctError):
    """Raised when an account name cannot be resolved in the config.

    Phase A does not yet use this — it is provided up front so Phases
    B-D can migrate call sites that currently raise
    ``typer.BadParameter`` with ad-hoc phrasing. Keeping the exception
    in the public surface lets callers write ``except AccountNotFound``
    instead of string-matching on the legacy phrasing.
    """

    def __init__(self, identifier: str, *, available: list[str] | None = None) -> None:
        self.identifier = identifier
        self.available = list(available or [])
        available_str = ", ".join(self.available) if self.available else "(none configured)"
        super().__init__(
            f"No account matches the identifier {identifier!r}.\n\n"
            f"Why: account operations look up accounts by their config "
            f"name or email address; the identifier did not match any "
            f"of the configured accounts.\n\n"
            f"Fix: run `pm accounts list` to see configured accounts, "
            f"or add the missing one with `pm accounts add`. Configured "
            f"accounts: {available_str}."
        )


__all__ = ["AcctError", "AccountNotFound", "ProviderNotFound"]
