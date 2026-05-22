"""Core seam for surfacing morning-briefing inbox entries on the dashboard.

Contract:
- Inputs: a base directory + filter knobs (``status``, ``limit``).
- Outputs: an iterable of briefing-entry-shaped objects, newest first.
- Side effects: read-only — this seam never writes briefings.
- Invariants: when no provider is registered, returns an empty list so
  the dashboard renders without the briefing banner instead of erroring.

Boundary note:
This module is core — it must not import from ``pollypm.plugins_builtin``.
The actual briefing-inbox implementation lives in the ``morning_briefing``
plugin; it installs its ``list_briefings`` at plugin ``initialize`` time
via :func:`register_briefing_provider`. The dashboard reads through
:func:`list_briefings` so disabling the plugin downgrades the briefing
banner to "absent" rather than breaking imports.

Mirrors the registration-seam pattern established in
:mod:`pollypm.approval_notifications` (see #1597 / #1363).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable, Protocol, runtime_checkable


logger = logging.getLogger(__name__)


@runtime_checkable
class BriefingEntryLike(Protocol):
    """Duck-typed briefing entry the dashboard renders.

    The dashboard only reads ``created_at`` (ISO string) and
    ``date_local`` (``YYYY-MM-DD``). Anything that exposes those two
    attributes is rendered correctly — third-party briefing plugins can
    swap their own dataclass in without depending on the
    ``morning_briefing`` plugin's :class:`BriefingEntry` type.
    """

    @property
    def created_at(self) -> str: ...

    @property
    def date_local(self) -> str: ...


BriefingProvider = Callable[..., Iterable[BriefingEntryLike]]


# Module-level registration slot. The ``morning_briefing`` plugin (or
# a replacement) installs its ``list_briefings`` here during plugin
# ``initialize``; when nothing is registered, ``list_briefings`` below
# returns an empty list so the dashboard's briefing banner is simply
# absent instead of raising.
_briefing_provider: BriefingProvider | None = None


def register_briefing_provider(provider: BriefingProvider | None) -> None:
    """Install (or clear) the default briefing-inbox provider.

    Called by the ``morning_briefing`` plugin during ``initialize`` so
    the dashboard can surface briefings without taking a hard import on
    the plugin tree. Pass ``None`` to clear (used by tests).

    ``provider`` must accept ``(base_dir, *, status=..., limit=...)``
    and return briefing entries newest-first. The expected signature
    matches :func:`pollypm.plugins_builtin.morning_briefing.inbox.list_briefings`
    exactly; mismatches are caught at call time and logged.
    """
    global _briefing_provider
    _briefing_provider = provider


def is_briefing_provider_registered() -> bool:
    """Return True iff a briefing-inbox provider is currently registered.

    Public availability probe so surfaces (web API, cockpit) can decide
    whether to advertise the briefing type without reaching into the
    module-private ``_briefing_provider`` slot. Mirrors host semantics:
    when the ``morning_briefing`` plugin is disabled via
    ``[plugins].disabled`` (or absent), no provider is registered and
    this returns ``False`` (Codex round-3 on PR #2059: route was reading
    the private slot to gate availability).
    """
    return _briefing_provider is not None


def list_briefings(
    base_dir: Path,
    *,
    status: str = "open",
    limit: int | None = None,
) -> list[BriefingEntryLike]:
    """Return briefings via the registered provider, or ``[]`` if none.

    Errors from the provider are logged and swallowed — a flaky plugin
    must never break the dashboard render. The dashboard treats "no
    entries" identically to "plugin not loaded".
    """
    provider = _briefing_provider
    if provider is None:
        return []
    try:
        return list(provider(base_dir, status=status, limit=limit))
    except Exception:  # noqa: BLE001
        logger.exception(
            "briefings_registry: provider failed (base_dir=%s, status=%s)",
            base_dir,
            status,
        )
        return []


__all__ = [
    "BriefingEntryLike",
    "BriefingProvider",
    "is_briefing_provider_registered",
    "list_briefings",
    "register_briefing_provider",
]
