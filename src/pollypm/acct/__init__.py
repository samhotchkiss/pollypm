"""``pollypm.acct`` — provider substrate for the account-management refactor.

Phase A of #397 ships this package as a new namespace that sits
alongside the existing ``pollypm.accounts`` module. The substrate
contains:

* :mod:`pollypm.acct.model` — data shapes (``AccountConfig``,
  ``AccountStatus``, ``RuntimeStatus``).
* :mod:`pollypm.acct.protocol` — the ``ProviderAdapter`` Protocol every
  provider package implements.
* :mod:`pollypm.acct.registry` — ``get_provider()`` / ``list_providers()``
  backed by the ``pollypm.provider`` entry-point group.
* :mod:`pollypm.acct.errors` — ``ProviderNotFound`` / ``AccountNotFound``
  with three-question-rule messages.

Phase B (#397) moves the Claude implementation into a dedicated package
under this namespace; Phase C does the same for Codex; Phase D
introduces the higher-level manager and migrates callers away from
``pollypm.accounts``.

During Phase A this ``__init__`` re-exports the **stable** public
surface only. The ``_legacy_adapters`` module is intentionally private
— it exists solely to give the entry-point registry something to
resolve during Phase A and will be deleted once Phases B/C land.
"""

from __future__ import annotations

from .errors import AccountNotFound, AcctError, ProviderNotFound
from .model import AccountConfig, AccountStatus, RuntimeStatus
from .protocol import ProviderAdapter
from .registry import get_provider, list_providers

__all__ = [
    "AccountConfig",
    "AccountNotFound",
    "AccountStatus",
    "AcctError",
    "ProviderAdapter",
    "ProviderNotFound",
    "RuntimeStatus",
    "get_provider",
    "list_providers",
]
