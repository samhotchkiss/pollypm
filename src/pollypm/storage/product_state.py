"""Product-state flag plumbing (#1546 heartbeat-cascade foundation).

Contract:
- Inputs: a :class:`pollypm.storage.state.StateStore` plus optional
  metadata for set/clear operations.
- Outputs: structured ``ProductState`` records (or ``None`` when
  unset).
- Side effects: writes one row to ``workspace_state`` keyed
  ``"product_state"`` on set, deletes it on clear.
- Invariants: only one product-state row exists at a time. The flag
  is the *workspace-wide* health gate — when value=="broken", any new
  task queueing path must refuse with a clear error pointing at the
  forensics path.

This module ships the plumbing only — the auto-trip path (tier-4
"Polly has exhausted authority") and the UI surfaces (rail banner,
dashboard takeover) live in follow-up issues. Today the only writers
are explicit ``pm doctor`` / debug paths and tests; the read path is
consulted by ``pollypm.work.service_queries.create_task`` to refuse
new task queueing when the flag is set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


PRODUCT_STATE_KEY = "product_state"

# Reserved values. ``"broken"`` is the only state that gates task
# queueing today; ``"healthy"`` is the absence of the row (we don't
# write a healthy row to keep the read path's "no row → fine" default
# load-bearing).
PRODUCT_STATE_BROKEN = "broken"


@dataclass(slots=True, frozen=True)
class ProductState:
    """One product-state row.

    Mirrors the workspace_state row layout but adds the typed
    ``state`` discriminator + ``forensics_path`` so callers don't
    need to JSON-decode by hand.
    """

    state: str
    reason: str
    set_at: str
    set_by: str
    forensics_path: str
    extra: dict[str, Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_product_state_broken(
    store: Any,
    *,
    reason: str,
    forensics_path: str,
    set_by: str = "system",
    extra: dict[str, Any] | None = None,
) -> ProductState:
    """Write ``product_state="broken"``.

    Refuses empty ``reason`` / ``forensics_path`` because both are
    load-bearing on the read side: the gate raises an error message
    that includes the reason, and the operator drills in via the path.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            "set_product_state_broken: reason must be a non-empty "
            "string — the gate error surfaces it to the operator."
        )
    if not isinstance(forensics_path, str) or not forensics_path.strip():
        raise ValueError(
            "set_product_state_broken: forensics_path must be a "
            "non-empty path — the operator drills in via this link."
        )
    payload: dict[str, Any] = {
        "state": PRODUCT_STATE_BROKEN,
        "reason": reason.strip(),
        "set_at": _now_iso(),
        "set_by": set_by or "system",
        "forensics_path": forensics_path.strip(),
        "extra": dict(extra or {}),
    }
    store.set_workspace_state(
        PRODUCT_STATE_KEY,
        payload,
        actor=set_by or "system",
    )
    return ProductState(
        state=payload["state"],
        reason=payload["reason"],
        set_at=payload["set_at"],
        set_by=payload["set_by"],
        forensics_path=payload["forensics_path"],
        extra=payload["extra"],
    )


def get_product_state(store: Any) -> ProductState | None:
    """Return the current ProductState, or ``None`` when unset.

    Returns ``None`` on:
    * No row in workspace_state (the default "healthy" shape).
    * Row exists but the payload doesn't decode to a dict (corrupt).
    * Row exists but the payload doesn't carry a ``state`` field.
    """
    if store is None:
        return None
    getter = getattr(store, "get_workspace_state", None)
    if not callable(getter):
        return None
    raw = getter(PRODUCT_STATE_KEY)
    if not isinstance(raw, dict):
        return None
    state = raw.get("state")
    if not isinstance(state, str):
        return None
    return ProductState(
        state=state,
        reason=str(raw.get("reason") or ""),
        set_at=str(raw.get("set_at") or ""),
        set_by=str(raw.get("set_by") or ""),
        forensics_path=str(raw.get("forensics_path") or ""),
        extra=dict(raw.get("extra") or {}),
    )


def clear_product_state(store: Any) -> bool:
    """Delete the product_state row. Returns True iff a row was deleted."""
    if store is None:
        return False
    clearer = getattr(store, "clear_workspace_state", None)
    if not callable(clearer):
        return False
    return bool(clearer(PRODUCT_STATE_KEY))


def is_product_broken(store: Any) -> ProductState | None:
    """Return the ProductState iff the workspace is in the broken state.

    Convenience wrapper used by the create-task gate. Returns
    ``None`` (the falsy sentinel) when the workspace is healthy so
    the gate can short-circuit with ``if state := is_product_broken(store):``.
    """
    state = get_product_state(store)
    if state is None:
        return None
    if state.state != PRODUCT_STATE_BROKEN:
        return None
    return state


class ProductBrokenError(RuntimeError):
    """Raised by the create-task gate when the workspace is broken.

    Carries the ``ProductState`` so callers can render the reason +
    forensics path back to the operator.
    """

    def __init__(self, product_state: ProductState) -> None:
        message = (
            f"PollyPM is in product_state=broken: {product_state.reason}. "
            f"Forensics: {product_state.forensics_path}. New task "
            f"queueing is refused until the flag is cleared via "
            f"`pm doctor` or "
            f"``clear_product_state(store)``."
        )
        super().__init__(message)
        self.product_state = product_state


__all__ = [
    "ProductBrokenError",
    "ProductState",
    "PRODUCT_STATE_BROKEN",
    "PRODUCT_STATE_KEY",
    "clear_product_state",
    "get_product_state",
    "is_product_broken",
    "set_product_state_broken",
]
