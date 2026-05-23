"""Sanctioned core → ``plugins_builtin`` edge for briefings wiring.

``pm serve`` does not boot the full plugin host (which owns roster +
job scheduling + cockpit rail registration — none of which the
read/regenerate path needs). The morning_briefing plugin's
``initialize`` hook is the canonical wiring point — it installs both
the inbox-list provider and the render facade into
:mod:`pollypm.briefings_registry`. This module mirrors that single
wiring step for surfaces that bypass the plugin host (currently only
the Web API in ``pm serve``).

Boundary justification:
The repo's documented core boundary forbids ``plugins_builtin``
imports from arbitrary core modules; ``cli.py:201-207`` is the
sanctioned edge for the CLI Typer apps. This module is the equivalent
edge for the Web API (#2059 round-9): a single, narrowly-scoped seam
that lets the API surface a plugin's render/regenerate without each
route owning the plugin's import path. The Web API imports from
:mod:`pollypm.briefings_bootstrap` — never from
``pollypm.plugins_builtin.morning_briefing``.

Honors ``config.plugins.disabled`` before wiring so the operator's
rollback path takes effect immediately (the plugin host's
filter-before-register contract).
"""
from __future__ import annotations

import logging

from pollypm.briefings_registry import (
    is_plugin_disabled_in_config,
    register_briefing_provider,
    register_briefing_render_provider,
)
from pollypm.config import PollyPMConfig

logger = logging.getLogger(__name__)

MORNING_BRIEFING_PLUGIN_NAME = "morning_briefing"
MORNING_BRIEFING_TYPE_NAME = "morning"


def bootstrap_builtin_briefings(config: PollyPMConfig) -> None:
    """Install the built-in morning-briefing providers into the registry.

    Idempotent: registry slot assignment just rebinds. Safe to call on
    every ``create_app`` invocation. When the operator has disabled
    ``morning_briefing`` via ``[plugins].disabled`` we clear any prior
    registration instead of re-installing, mirroring the host's
    filter-before-register contract (Codex round-3 / round-9 on #2059).

    Import failures are logged and swallowed so a missing/broken plugin
    downgrades availability to ``false`` (the route's fail-soft
    posture) rather than crashing app startup.
    """
    if is_plugin_disabled_in_config(config, MORNING_BRIEFING_PLUGIN_NAME):
        # Clear so a previously-enabled run's registration doesn't leak
        # into this disabled-config app instance.
        register_briefing_provider(None)
        register_briefing_render_provider(MORNING_BRIEFING_TYPE_NAME, None)
        logger.info(
            "briefings_bootstrap: %s disabled via [plugins].disabled; "
            "Web API will report morning.available=false",
            MORNING_BRIEFING_PLUGIN_NAME,
        )
        return

    try:
        from pollypm.plugins_builtin.morning_briefing.inbox import (
            list_briefings as _list_briefings,
        )
        from pollypm.plugins_builtin.morning_briefing.render_facade import (
            MorningBriefingRenderProvider,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "briefings_bootstrap: %s unavailable; "
            "Web API will report morning.available=false",
            MORNING_BRIEFING_PLUGIN_NAME,
            exc_info=True,
        )
        return

    register_briefing_provider(_list_briefings)
    register_briefing_render_provider(
        MORNING_BRIEFING_TYPE_NAME,
        MorningBriefingRenderProvider(),
    )


__all__ = [
    "MORNING_BRIEFING_PLUGIN_NAME",
    "MORNING_BRIEFING_TYPE_NAME",
    "bootstrap_builtin_briefings",
]
