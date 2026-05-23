"""Render / regenerate adapter for the briefings core seam.

This is the plugin-side implementation of
:class:`pollypm.briefings_registry.BriefingRenderProvider` (added in
#2059 round-9). The Web API was previously importing
``pollypm.plugins_builtin.morning_briefing`` internals directly to
render / regenerate the morning briefing — that violated the
documented core → ``plugins_builtin`` boundary
(``cli.py:201-207``; ``briefings_registry.py:10-16``).

With this module the boundary is restored:

- The plugin's ``_initialize`` hook constructs a
  :class:`MorningBriefingRenderProvider` and installs it via
  :func:`pollypm.briefings_registry.register_briefing_render_provider`.
- The Web API looks the provider up by type name
  (``"morning"``) through the registry and never imports
  ``plugins_builtin``.
"""
from __future__ import annotations

import logging
from datetime import UTC
from typing import Any

from pollypm.briefings_registry import BriefingArtifact
from pollypm.config import DEFAULT_CONFIG_PATH, resolve_config_path

logger = logging.getLogger(__name__)


class MorningBriefingRenderProvider:
    """Concrete :class:`BriefingRenderProvider` for the morning briefing.

    Encapsulates everything the Web API used to import directly:
    inbox read, settings load, state load, briefing-tick fire. The Web
    API only sees this object through the registry seam.
    """

    type_name = "morning"

    def render_last(self, config: Any) -> BriefingArtifact | None:
        """Read the newest morning briefing off disk (spec §9.1 ``last``).

        Late-bound imports so tests monkeypatching the underlying
        modules (e.g. ``...morning_briefing.inbox.list_briefings``)
        flow through to the call.
        """
        from pollypm.plugins_builtin.morning_briefing import inbox as _inbox

        base_dir = config.project.base_dir
        entries = _inbox.list_briefings(base_dir, status="all", limit=1)
        if not entries:
            return None
        entry = entries[0]
        read = _inbox.read_briefing(base_dir, entry.date_local)
        if read is None:
            # Metadata json existed during list_briefings but the body
            # disappeared between calls (rare — atomic-write replaces
            # both; treat as missing so the caller can regenerate).
            return None
        meta_entry, markdown = read
        return BriefingArtifact(
            date_local=meta_entry.date_local,
            markdown=markdown,
            mode=meta_entry.mode or None,
            generated_at=meta_entry.created_at or None,
            metadata={
                "status": meta_entry.status,
                "pinned": meta_entry.pinned,
                "yesterday": meta_entry.yesterday,
                "priorities": list(meta_entry.priorities),
                "watch": list(meta_entry.watch),
                **dict(meta_entry.meta),
            },
        )

    def regenerate(
        self,
        config: Any,
        project: str | None = None,
    ) -> BriefingArtifact:
        """Force-fire the morning briefing pipeline (spec §9.1 ``regenerate``).

        Mirrors ``pm briefing now`` — runs gather → synthesize → emit
        and writes to the inbox. Morning briefings are whole-workspace
        only today; if ``project`` is set we raise ``ValueError`` and
        the route layer translates that into a typed 400. (Codex
        round-1 on PR #2059: silently ignoring ``project`` let clients
        believe they had narrowed scope when they hadn't.)
        """
        # Late-bound imports — see ``render_last`` rationale.
        from pollypm.plugins_builtin.morning_briefing.handlers import (
            briefing_tick as _tick,
        )
        from pollypm.plugins_builtin.morning_briefing.settings import (
            load_briefing_settings,
        )
        from pollypm.plugins_builtin.morning_briefing.state import load_state

        if project is not None:
            raise ValueError(
                "project-scoped morning briefings are not implemented",
            )

        base_dir = config.project.base_dir
        project_root = config.project.root_dir

        # Prefer the path ``pm serve`` actually loaded (``config.config_path``)
        # so a non-default ``--config`` is honored. Falling back to
        # ``DEFAULT_CONFIG_PATH`` keeps the path working for tests that
        # construct ``PollyPMConfig`` in memory. (Codex round-1 P0 on
        # PR #2059.)
        config_path = getattr(config, "config_path", None) or DEFAULT_CONFIG_PATH
        settings = load_briefing_settings(resolve_config_path(config_path))

        # Mirror the CLI's timezone-resolution precedence
        # (``cli.py:_current_local_now`` -> ``_tick._local_now``):
        #   ``[briefing].timezone`` -> ``[pollypm].timezone`` -> system TZ.
        # The previous implementation only consulted ``[pollypm].timezone``,
        # so a config with ``[pollypm].timezone="UTC"`` plus a
        # ``[briefing].timezone="America/Los_Angeles"`` override would
        # regenerate against the wrong local day / quiet-mode window.
        # (Codex round-10 P0 on PR #2059.)
        fallback_tz = getattr(config.pollypm, "timezone", "") or ""
        now_local = _tick._local_now(settings, fallback_tz)
        state = load_state(base_dir)

        result = _tick.fire_briefing(
            project_root=project_root,
            base_dir=base_dir,
            settings=settings,
            now_local=now_local,
            state=state,
            config=config,
            emit_to_inbox=True,
        )
        if not isinstance(result, dict) or not result.get("fired"):
            reason = (
                str(result.get("reason"))
                if isinstance(result, dict) else "unknown"
            )
            # Surface as a runtime error — the route translates this
            # into a typed 503. We deliberately don't depend on the
            # Web API's error types from a plugin module.
            raise RuntimeError(
                f"briefing regenerate did not fire ({reason})",
            )
        draft = result.get("draft") or {}
        return BriefingArtifact(
            date_local=str(draft.get("date_local") or ""),
            markdown=str(draft.get("markdown") or ""),
            mode=str(draft.get("mode") or "") or None,
            generated_at=now_local.astimezone(UTC).isoformat(),
            metadata={
                "emitted_to_inbox": bool(result.get("emitted", False)),
                "yesterday": draft.get("yesterday"),
                "priorities": list(draft.get("priorities") or []),
                "watch": list(draft.get("watch") or []),
                "quiet_mode": bool(result.get("quiet_mode", False)),
                **dict(draft.get("meta") or {}),
            },
        )


__all__ = ["MorningBriefingRenderProvider"]
