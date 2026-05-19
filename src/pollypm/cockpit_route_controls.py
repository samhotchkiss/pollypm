"""Navigation route controls used by the cockpit rail.

Contract:
- Inputs: a ``NavigationCommand`` (rail click / keypress key) consumed by
  ``NavigationController``. ``_CockpitRouteWindowApplier`` also takes the
  owning ``PollyCockpitApp`` instance at construction so its ``apply``
  method can defer to ``_route_selected_with_deadline``.
- Outputs: ``_CockpitRouteContentResolver.resolve`` returns a
  ``NavigationContent`` wrapping the rail key — the router still owns
  full content resolution during this integration step, so the resolver
  is intentionally a thin pass-through. ``_CockpitRouteWindowApplier.apply``
  returns whatever string ``_route_selected_with_deadline`` produces
  (the resolved route key after the deadline check fires).
- Side effects: ``_CockpitRouteWindowApplier.apply`` delegates to the
  owning app, so any side effect comes from there (window swap,
  acknowledgement bookkeeping, etc.). Neither class touches global state.
- Invariants: this module owns the two thin route-control hooks the
  navigation controller binds to, and nothing else. Construction lives
  in ``cockpit_ui`` (so the bound app reference stays out of import-cycle
  territory).
- Allowed dependencies: ``NavigationCommand`` / ``NavigationContent``
  from ``pollypm.cockpit_navigation``; ``PollyCockpitApp`` is referenced
  only as a forward string for typing.
- Private: both classes are underscore-prefixed and re-exported via
  ``cockpit_ui`` for back-compat (see #1354).

Wedge of the cockpit_ui.py god-module split tracked by #1354.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pollypm.cockpit_navigation import NavigationCommand, NavigationContent

if TYPE_CHECKING:
    from pollypm.cockpit_ui import PollyCockpitApp


class _CockpitRouteContentResolver:
    """Navigation resolver for the root cockpit rail.

    The router still owns full content resolution during this integration
    step; the navigation controller owns acknowledgement/cancellation state.
    """

    def resolve(self, request: NavigationCommand) -> NavigationContent:
        return NavigationContent(request.key)


class _CockpitRouteWindowApplier:
    def __init__(self, app: "PollyCockpitApp") -> None:
        self._app = app

    def apply(self, request: NavigationCommand, _content: object) -> str:
        return self._app._route_selected_with_deadline(request.key)


__all__ = ["_CockpitRouteContentResolver", "_CockpitRouteWindowApplier"]
