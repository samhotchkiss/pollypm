"""#1648 — Operator dashboard belongs in the cockpit's static fast-path sets.

``operator`` is a registered ``StaticViewRoute`` (see
:mod:`pollypm.cockpit_rail_routes`), so clicking it must take the same
optimistic, supervisor-free render path that Dashboard / Inbox /
Activity already use. The two relevant sets are:

* ``CockpitRouter._SUPERVISOR_FREE_STATIC_KEYS`` — gates whether the
  route worker skips loading the supervisor and ``ensure_cockpit_layout``
  before showing a static pane.
* ``PollyCockpitApp._STATIC_RAIL_KEYS`` — gates whether the rail click
  applies the cheap optimistic active-marker update vs. doing a heavy
  synchronous ``_refresh_rows``.

These tests just assert membership; the integration paths that *use*
the sets are covered by the broader cockpit suite.
"""

from __future__ import annotations

from pollypm.cockpit_rail import CockpitRouter
from pollypm.cockpit_rail_routes import resolve_static_view_route
from pollypm.cockpit_ui import PollyCockpitApp


def test_operator_is_registered_as_static_route() -> None:
    """Guard against the static-route registry drifting out from under us."""
    route = resolve_static_view_route("operator")
    assert route is not None
    assert route.kind == "operator"
    assert route.selected_key == "operator"


def test_operator_in_supervisor_free_static_keys() -> None:
    """Operator clicks must skip supervisor/layout work like Dashboard."""
    assert "operator" in CockpitRouter._SUPERVISOR_FREE_STATIC_KEYS


def test_operator_in_static_rail_keys() -> None:
    """Operator clicks must use the optimistic active-marker path,
    not the heavy synchronous ``_refresh_rows`` rebuild."""
    assert "operator" in PollyCockpitApp._STATIC_RAIL_KEYS


def test_all_registered_static_kinds_are_fast_path() -> None:
    """Catch the same drift for any future static route — every
    registered static view kind should be on the fast paths so its
    click feels as snappy as the existing static destinations.

    Excludes ``activity`` because it is resolved dynamically via the
    ``activity:<project_key>`` form rather than the bare key, and
    ``polly`` (a live session, not a static view) is intentionally on
    ``_STATIC_RAIL_KEYS`` but not ``_SUPERVISOR_FREE_STATIC_KEYS``.
    """
    static_kinds = {"dashboard", "inbox", "workers", "metrics", "settings", "operator"}
    assert static_kinds <= CockpitRouter._SUPERVISOR_FREE_STATIC_KEYS
    assert static_kinds <= PollyCockpitApp._STATIC_RAIL_KEYS
