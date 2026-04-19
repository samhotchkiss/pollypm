"""Data models for the ``pollypm.acct`` provider substrate.

This module is part of Phase A of the account-management refactor
(#397). It re-exports the existing ``AccountConfig`` and
``AccountStatus`` shapes under the new ``pollypm.acct`` namespace so
subsequent phases can migrate callers without a flag-day rename.

``RuntimeStatus`` is a lightweight read-only view of the runtime health
data that ``pollypm.storage.state.StateStore`` records for each account.
The dataclass lives here (rather than being re-exported) because the
legacy code path reads the fields directly off the ``StateStore`` row
dataclass — the shape is defined here as the public contract the
provider substrate exposes, independent of storage schema drift.

No behavior change: ``AccountConfig`` and ``AccountStatus`` are imported
from their current homes. Phase D of #397 will physically move the
definitions into this module and drop the re-exports.
"""

from __future__ import annotations

from dataclasses import dataclass

# Re-export the existing shapes. Callers that want the provider
# substrate's public surface can now write::
#
#     from pollypm.acct.model import AccountConfig, AccountStatus
#
# while the legacy modules keep working unchanged.
from pollypm.accounts import AccountStatus  # noqa: F401 — public re-export
from pollypm.models import AccountConfig  # noqa: F401 — public re-export


@dataclass(slots=True, frozen=True)
class RuntimeStatus:
    """Read-only snapshot of an account's runtime-health record.

    Mirrors the subset of ``StateStore.get_account_runtime()`` columns
    the provider substrate cares about. Kept separate from the storage
    dataclass so provider adapters don't depend on the state-db schema.
    """

    status: str = "unknown"
    reason: str = ""
    available_at: str | None = None
    access_expires_at: str | None = None


__all__ = ["AccountConfig", "AccountStatus", "RuntimeStatus"]
