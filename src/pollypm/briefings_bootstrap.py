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
from typing import Any

from pollypm.briefings_registry import (
    BriefingArtifact,
    is_plugin_disabled_in_config,
    register_briefing_provider,
    register_briefing_render_provider,
)
from pollypm.config import PollyPMConfig

logger = logging.getLogger(__name__)

MORNING_BRIEFING_PLUGIN_NAME = "morning_briefing"
MORNING_BRIEFING_TYPE_NAME = "morning"

# Mirror of ``pollypm.plugins_builtin.morning_briefing.plugin._MORNING_BRIEFING_DESCRIPTION``.
# Duplicated here so the disabled / import-failure branches (which
# must NOT import the plugin tree — that's the whole point of those
# branches) can still surface a meaningful blurb in
# ``GET /api/v1/briefings`` (Codex round-15 on #2059).
_MORNING_BRIEFING_DESCRIPTION_FALLBACK = (
    "Daily morning briefing — yesterday's progress, today's "
    "priorities, watch items. Fires at the configured local hour."
)


class _UnavailableMorningRenderProvider:
    """Stand-in provider that surfaces ``morning`` as known-but-unavailable.

    Registered in the disabled / import-failure branches so the Web
    API's discovery endpoint still lists ``morning`` (with
    ``available=false``) and ``GET/POST /briefings/morning`` return a
    typed 503 ``service_unavailable`` instead of 404 ``not_found`` —
    operators flipping ``[plugins].disabled = ["morning_briefing"]``
    should see "known built-in disabled" not "unknown briefing type"
    (Codex round-15 on #2059).

    The route layer's ``is_available`` gate short-circuits before any
    method here runs, so ``render_last`` / ``regenerate`` raising is
    purely defense-in-depth — if the gate is ever bypassed the
    exception becomes a typed 503 via the existing error envelope.
    """

    def render_last(self, _config: Any) -> BriefingArtifact | None:
        raise RuntimeError(
            "morning_briefing is disabled or unavailable; "
            "render_last must not be called (the is_available "
            "gate should have short-circuited the route)."
        )

    def regenerate(
        self,
        _config: Any,
        project: str | None = None,  # noqa: ARG002
    ) -> BriefingArtifact:
        raise RuntimeError(
            "morning_briefing is disabled or unavailable; "
            "regenerate must not be called (the is_available "
            "gate should have short-circuited the route)."
        )


def _morning_unavailable(_config: Any) -> bool:
    """Availability adapter for the disabled / import-failure stub."""
    return False


def bootstrap_builtin_briefings(config: PollyPMConfig) -> None:
    """Install the built-in morning-briefing providers into the registry.

    Idempotent: registry slot assignment just rebinds. Safe to call on
    every ``create_app`` invocation. When the operator has disabled
    ``morning_briefing`` via ``[plugins].disabled`` we register a
    known-disabled stub (``is_available`` returns ``False``) instead of
    the real provider, mirroring the host's filter-before-register
    contract (Codex round-3 / round-9 on #2059) while keeping
    ``morning`` discoverable via ``GET /briefings`` so clients can
    distinguish "built-in disabled" from "unknown type" (Codex round-15
    on #2059).

    Import failures are logged and swallowed so a missing/broken plugin
    downgrades availability to ``false`` (the route's fail-soft
    posture) rather than crashing app startup. The except path also
    replaces any prior registration with the same known-disabled stub
    so a stale provider from a previous app instance doesn't keep the
    registry reporting ``available=true`` (Codex round-14 on #2059).
    """
    if is_plugin_disabled_in_config(config, MORNING_BRIEFING_PLUGIN_NAME):
        # Clear the inbox-list slot so the dashboard's briefing banner
        # disappears, but register a known-disabled render provider so
        # the Web API still surfaces ``morning`` in its discovery
        # response (with ``available=false``) instead of treating it as
        # an unknown briefing type — operators must be able to tell
        # "built-in disabled" from "typo in URL" (Codex round-15 on
        # #2059). The route's ``is_available`` gate short-circuits
        # render/regenerate into a typed 503 ``service_unavailable``
        # before the stub provider's methods can execute.
        register_briefing_provider(None)
        register_briefing_render_provider(
            MORNING_BRIEFING_TYPE_NAME,
            _UnavailableMorningRenderProvider(),
            description=_MORNING_BRIEFING_DESCRIPTION_FALLBACK,
            is_available=_morning_unavailable,
        )
        logger.info(
            "briefings_bootstrap: %s disabled via [plugins].disabled; "
            "Web API will report morning.available=false (type still "
            "discoverable via GET /briefings)",
            MORNING_BRIEFING_PLUGIN_NAME,
        )
        return

    try:
        from pollypm.plugins_builtin.morning_briefing.inbox import (
            list_briefings as _list_briefings,
        )
        from pollypm.plugins_builtin.morning_briefing.plugin import (
            _MORNING_BRIEFING_DESCRIPTION,
            _morning_is_available,
        )
        from pollypm.plugins_builtin.morning_briefing.render_facade import (
            MorningBriefingRenderProvider,
        )
    except Exception:  # noqa: BLE001
        # Replace any prior registration with a known-disabled stub so
        # a previously-installed provider (e.g. from an earlier app
        # instance, a test that pre-seeded the slot, or a plugin host
        # run before this bootstrap was invoked) doesn't continue
        # masquerading as the morning provider after we logged
        # ``available=false``. Without this, ``GET /briefings`` keeps
        # reporting ``morning.available=true`` against a stale provider
        # while the operator sees only the warning in logs (Codex
        # round-14 on #2059). The stub keeps ``morning`` discoverable
        # via ``GET /briefings`` so clients can distinguish
        # "known built-in unavailable" from "unknown briefing type"
        # (Codex round-15 on #2059).
        register_briefing_provider(None)
        register_briefing_render_provider(
            MORNING_BRIEFING_TYPE_NAME,
            _UnavailableMorningRenderProvider(),
            description=_MORNING_BRIEFING_DESCRIPTION_FALLBACK,
            is_available=_morning_unavailable,
        )
        logger.warning(
            "briefings_bootstrap: %s unavailable; "
            "Web API will report morning.available=false (type still "
            "discoverable via GET /briefings)",
            MORNING_BRIEFING_PLUGIN_NAME,
            exc_info=True,
        )
        return

    register_briefing_provider(_list_briefings)
    # Pass description AND is_available so the registry mirrors the
    # plugin host's ``_initialize`` registration (Codex round-13 on
    # #2059: without ``is_available`` the registry's
    # ``_default_is_available`` returns True unconditionally, so the
    # per-request ``[plugins].disabled`` gate never runs in production).
    register_briefing_render_provider(
        MORNING_BRIEFING_TYPE_NAME,
        MorningBriefingRenderProvider(),
        description=_MORNING_BRIEFING_DESCRIPTION,
        is_available=_morning_is_available,
    )


__all__ = [
    "MORNING_BRIEFING_PLUGIN_NAME",
    "MORNING_BRIEFING_TYPE_NAME",
    "bootstrap_builtin_briefings",
]
