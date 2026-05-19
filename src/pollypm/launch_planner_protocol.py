"""Core protocol surface for launch planners.

This leaf module owns :class:`DefaultLaunchPlannerContext` — the dataclass
that callers (today: :class:`pollypm.supervisor.Supervisor`) hand to the
default launch planner factory when resolving a planner through the
plugin host.

Boundary note
-------------

The default planner ships as a built-in plugin under
``pollypm.plugins_builtin.default_launch_planner``, but the *context*
the planner receives is fundamentally the host's contract — it spells
out which callables the host promises to provide. Keeping that contract
in core means :mod:`pollypm.supervisor` can build the context object
without importing from the optional plugin tree, preserving the
"plugin is optional" invariant tracked under #1363 (siblings: #1597 /
#1621 / #1626 / #1672 / #1682).

The plugin's own ``planner.py`` module re-exports
``DefaultLaunchPlannerContext`` from here for back-compat with any
out-of-tree planners that built against the previous public surface.

Keep this module dependency-light: importing from heavyweight modules
would defeat the seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from pollypm.models import AccountConfig, SessionConfig
from pollypm.providers.base import LaunchCommand

if TYPE_CHECKING:
    from pollypm.config import PollyPMConfig
    from pollypm.storage.state import StateStore
@dataclass(slots=True)
class DefaultLaunchPlannerContext:
    """Callables the default planner needs from its host.

    The planner doesn't own auth-sync, worker sandboxing, or agent
    profile resolution — those live elsewhere (Supervisor today). The
    context threads the relevant callables through so the planner can
    call them without a hard Supervisor dependency.
    """

    config: "PollyPMConfig"
    store: "StateStore"
    readonly_state: bool
    effective_account: Callable[[SessionConfig, AccountConfig], AccountConfig]
    apply_role_launch_restrictions: Callable[[SessionConfig, LaunchCommand], LaunchCommand]
    resolve_profile_prompt: Callable[[SessionConfig, AccountConfig], str | None]
    storage_closet_session_name: Callable[[], str]


__all__ = ["DefaultLaunchPlannerContext"]
