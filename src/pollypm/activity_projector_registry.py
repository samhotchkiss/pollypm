"""Core seam for resolving the activity-feed projector.

Contract:
- Inputs: a host ``config`` (anything with ``project.state_db`` and a
  ``projects`` mapping — typically a ``Config``).
- Outputs: an :class:`EventProjector`-shaped object, or ``None`` when
  no factory is registered or the factory itself returns ``None``.
- Side effects: none — the projector reads from per-project SQLite DBs
  lazily on ``project()`` calls.
- Invariants: when no factory is registered, callers see ``None`` and
  degrade to an empty feed (the dashboard panel and ``cockpit_inbox``
  full-screen view both already handle this).

Boundary note:
This module is core — it must not import from ``pollypm.plugins_builtin``.
``cockpit_inbox.py`` and ``cockpit_ui.py`` (dashboard + activity panel)
read the live feed through :func:`build_activity_projector` without
taking a hard import on the optional ``activity_feed`` plugin. The
``activity_feed`` plugin installs its projector factory here during
plugin ``initialize``.

Mirrors the registration-seam pattern established in
:mod:`pollypm.approval_notifications` (#1597),
:mod:`pollypm.briefings_registry` (#1621), and
:mod:`pollypm.maintenance_handlers_registry` (#1626). See #1363 for the
full boundary-debt roll-up.
"""

from __future__ import annotations

import logging
from typing import Any, Callable


logger = logging.getLogger(__name__)


# Factory signature: ``(config) -> EventProjector | None``. We type as
# ``Any`` to avoid pulling the projector class — which lives in the
# plugin tree — into the core seam.
ActivityProjectorFactory = Callable[[Any], Any]


_factory: ActivityProjectorFactory | None = None


def register_activity_projector_factory(
    factory: ActivityProjectorFactory | None,
) -> None:
    """Install (or clear) the projector factory.

    Called by the ``activity_feed`` plugin during ``initialize`` so
    cockpit surfaces can resolve a projector without importing from the
    optional plugin tree. Pass ``factory=None`` to clear (used by tests
    that want to simulate the plugin being absent).
    """
    global _factory
    _factory = factory


def get_activity_projector_factory() -> ActivityProjectorFactory | None:
    """Return the registered factory, or ``None`` when no provider is installed."""
    return _factory


def build_activity_projector(config: Any) -> Any | None:
    """Resolve an activity-feed projector for ``config``.

    Returns ``None`` when no factory is registered (plugin disabled /
    missing) or when the factory itself returns ``None`` (e.g. config
    without a state DB). Callers must treat ``None`` as "no feed
    available" and degrade to an empty view.

    Any exception raised by the factory is re-raised so callers can
    surface the failure in their own format. Both current consumers
    wrap the call in a broad ``try/except`` that returns an empty list,
    so the runtime contract is "either a projector or an empty feed".
    """
    if _factory is None:
        logger.debug(
            "activity_projector_registry: no factory registered; "
            "returning None (activity_feed plugin disabled?)",
        )
        return None
    return _factory(config)


__all__ = [
    "ActivityProjectorFactory",
    "build_activity_projector",
    "get_activity_projector_factory",
    "register_activity_projector_factory",
]
