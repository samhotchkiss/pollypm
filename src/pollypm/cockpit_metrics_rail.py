"""Rail-item registration for the cockpit Metrics pane (#1367).

Extracted from :mod:`pollypm.cockpit` so :mod:`pollypm.cockpit_rail` can
import the registration helper at module load time. Previously the helper
lived in ``cockpit.py``, which top-imports ``cockpit_rail`` — closing the
cycle and forcing ``cockpit_rail`` to use a lazy in-function import.

This module is deliberately thin: it only owns the small ``RailItemRegistration``
factory for the Metrics row. The pane render + data-gather helpers stay in
``pollypm.cockpit`` / ``pollypm.cockpit_metrics`` where they belong.
"""

from __future__ import annotations


def _register_metrics_rail_item(registry, router) -> None:
    """Add the ``top.Metrics`` rail row if not already registered.

    Kept next to the worker-roster registration so both observability
    rows sit at the top of the rail. Safe to call repeatedly — the
    registry dedupes on ``(plugin_name, section, label)``.
    """
    try:
        from pollypm.plugin_api.v1 import RailItemRegistration, PanelSpec
    except Exception:  # noqa: BLE001
        return

    def _state(_ctx) -> str:
        return "watch"

    def _handler(ctx):
        try:
            router.route_selected("metrics")
        except Exception:  # noqa: BLE001
            pass
        return PanelSpec(widget=None, focus_hint="metrics")

    reg = RailItemRegistration(
        plugin_name="cockpit_metrics",
        section="top",
        index=28,  # after Workers (25), before Projects (30+)
        label="Metrics",
        handler=_handler,
        key="metrics",
        state_provider=_state,
    )
    try:
        registry.add(reg)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["_register_metrics_rail_item"]
