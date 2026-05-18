"""Core seam for tier-4 cascade actions exposed to the CLI.

Contract:
- Inputs: kwargs matching the original ``audit_watchdog`` callables
  (a :class:`Finding` + dispatch context for ``dispatch_to_operator_tier4``;
  audit metadata for ``emit_tier4_demoted`` and ``emit_tier4_global_action``).
- Outputs: the underlying callable's return value, or a documented
  degraded default (``"unavailable"`` / ``None`` / ``False``) when no
  provider is registered.
- Side effects: whatever the registered handler does (tracker mutations,
  audit-log writes, inbox writes, desktop notifications). All handlers are
  best-effort and isolated from each other — a failure in one does not
  cascade into another.
- Invariants: when no provider is registered, callers see the documented
  degraded default and can surface a "feature unavailable" message rather
  than crashing on import.

Boundary note:
This module is core — it must not import from ``pollypm.plugins_builtin``.
``pm tier4 self-promote``, ``pm tier4 clear``, and ``pm system restart-*``
all resolve tier-4 actions through this registry instead of taking a
hard import on the optional ``core_recurring`` plugin tree. The
``core_recurring`` plugin installs the real callables here during
plugin ``initialize``.

Mirrors the registration-seam pattern established in
:mod:`pollypm.approval_notifications` (#1597),
:mod:`pollypm.briefings_registry` (#1621), and
:mod:`pollypm.maintenance_handlers_registry` (#1626). See #1363 for the
full boundary-debt roll-up.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols — describe the shape each registered callable must satisfy.
# ---------------------------------------------------------------------------


class DispatchToOperatorTier4(Protocol):
    """Tier-4 dispatch callable signature.

    Mirrors ``audit_watchdog._dispatch_to_operator_tier4``: routes a
    finding to broader-authority Polly. Returns a status string
    (``"dispatched"`` / ``"throttled"`` / ``"send_failed"``).
    """

    def __call__(
        self,
        finding: Any,
        *,
        project_path: Path | None,
        now: datetime,
        promotion_path: str,
        justification: str = ...,
        tracker: Any | None = ...,
    ) -> str: ...


class EmitTier4Demoted(Protocol):
    """``audit.tier4_demoted`` audit-emit callable signature."""

    def __call__(
        self,
        *,
        project: str,
        root_cause_hash_value: str,
        reason: str,
        project_path: Path | None = ...,
    ) -> None: ...


class EmitTier4GlobalAction(Protocol):
    """``audit.tier4_global_action`` audit-emit + push callable signature.

    Returns ``True`` iff at least one notifier accepted the payload.
    """

    def __call__(
        self,
        *,
        action: str,
        root_cause_hash_value: str,
        actor: str = ...,
        metadata: dict[str, Any] | None = ...,
        project_path: Path | None = ...,
        project: str = ...,
        push_title: str | None = ...,
        push_body: str | None = ...,
    ) -> bool: ...


# ---------------------------------------------------------------------------
# Module-level registration slots.
# ---------------------------------------------------------------------------

_dispatch_to_operator_tier4: DispatchToOperatorTier4 | None = None
_emit_tier4_demoted: EmitTier4Demoted | None = None
_emit_tier4_global_action: EmitTier4GlobalAction | None = None


# ---------------------------------------------------------------------------
# Registration entry points (called by the plugin's ``_initialize``).
# ---------------------------------------------------------------------------


def register_dispatch_to_operator_tier4(
    handler: DispatchToOperatorTier4 | None,
) -> None:
    """Install (or clear) the tier-4 dispatch callable.

    Pass ``None`` to clear (used by tests).
    """
    global _dispatch_to_operator_tier4
    _dispatch_to_operator_tier4 = handler


def register_emit_tier4_demoted(handler: EmitTier4Demoted | None) -> None:
    """Install (or clear) the ``audit.tier4_demoted`` emit callable."""
    global _emit_tier4_demoted
    _emit_tier4_demoted = handler


def register_emit_tier4_global_action(
    handler: EmitTier4GlobalAction | None,
) -> None:
    """Install (or clear) the ``audit.tier4_global_action`` emit callable."""
    global _emit_tier4_global_action
    _emit_tier4_global_action = handler


# ---------------------------------------------------------------------------
# Resolution entry points (called by the CLI).
# ---------------------------------------------------------------------------


def dispatch_to_operator_tier4(
    finding: Any,
    *,
    project_path: Path | None,
    now: datetime,
    promotion_path: str,
    justification: str = "",
    tracker: Any | None = None,
) -> str:
    """Invoke the registered tier-4 dispatch callable.

    Returns the underlying status string on success, ``"unavailable"``
    when no provider is registered. The CLI treats ``"unavailable"`` as
    "plugin disabled" and surfaces a non-zero exit code with a clear
    message rather than crashing on import.
    """
    handler = _dispatch_to_operator_tier4
    if handler is None:
        logger.debug(
            "tier4_actions_registry: no dispatch_to_operator_tier4 provider"
        )
        return "unavailable"
    return handler(
        finding,
        project_path=project_path,
        now=now,
        promotion_path=promotion_path,
        justification=justification,
        tracker=tracker,
    )


def emit_tier4_demoted(
    *,
    project: str,
    root_cause_hash_value: str,
    reason: str,
    project_path: Path | None = None,
) -> bool:
    """Invoke the registered ``audit.tier4_demoted`` emit callable.

    Returns ``True`` if a provider was registered (audit emit is
    best-effort inside the handler itself), ``False`` if no provider is
    registered so the caller can note the degraded state. Never raises.
    """
    handler = _emit_tier4_demoted
    if handler is None:
        logger.debug(
            "tier4_actions_registry: no emit_tier4_demoted provider"
        )
        return False
    try:
        handler(
            project=project,
            root_cause_hash_value=root_cause_hash_value,
            reason=reason,
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "tier4_actions_registry: emit_tier4_demoted handler failed",
            exc_info=True,
        )
        return False
    return True


def emit_tier4_global_action(
    *,
    action: str,
    root_cause_hash_value: str,
    actor: str = "polly",
    metadata: dict[str, Any] | None = None,
    project_path: Path | None = None,
    project: str = "",
    push_title: str | None = None,
    push_body: str | None = None,
) -> bool:
    """Invoke the registered ``audit.tier4_global_action`` emit callable.

    Returns ``True`` iff a notifier accepted the push payload (mirrors
    the underlying handler's contract). Returns ``False`` and logs at
    DEBUG when no provider is registered so the CLI can still print a
    machine-readable status line.
    """
    handler = _emit_tier4_global_action
    if handler is None:
        logger.debug(
            "tier4_actions_registry: no emit_tier4_global_action provider"
        )
        return False
    return handler(
        action=action,
        root_cause_hash_value=root_cause_hash_value,
        actor=actor,
        metadata=metadata,
        project_path=project_path,
        project=project,
        push_title=push_title,
        push_body=push_body,
    )


__all__ = [
    "DispatchToOperatorTier4",
    "EmitTier4Demoted",
    "EmitTier4GlobalAction",
    "dispatch_to_operator_tier4",
    "emit_tier4_demoted",
    "emit_tier4_global_action",
    "register_dispatch_to_operator_tier4",
    "register_emit_tier4_demoted",
    "register_emit_tier4_global_action",
]
