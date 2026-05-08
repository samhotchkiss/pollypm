"""Morning Briefing plugin.

Delivers a single inbox message per local day at the configured hour
(default 6 a.m.) summarizing yesterday's cross-project activity and
today's priorities. See ``docs/morning-briefing-plugin-spec.md``.

mb01 scope:
    - Plugin skeleton + capability manifest (herald agent profile +
      briefing.tick job handler + roster entry).
    - Herald persona registered as an agent profile.
    - Roster entry: ``@every 1h`` → ``briefing.tick``.
    - Briefing-tick handler: reads ``[briefing]`` settings + persisted
      state, gates on hour + already-briefed-today, and (for now) stubs
      the actual fire via ``handlers.briefing_tick.fire_briefing``.

mb02 adds data gathering, mb03 adds synthesis, mb04 adds inbox emission,
mb05 adds the CLI wrappers. The gather/synthesize/emit code lives behind
the ``fire_briefing`` hook so each of those issues can swap it in without
touching the tick logic.
"""
from __future__ import annotations

from pathlib import Path

from pollypm.plugin_api.v1 import (
    Capability,
    JobHandlerAPI,
    PollyPMPlugin,
    RosterAPI,
)
from pollypm.agent_profiles.defaults import StaticPromptProfile
from pollypm.plugins_builtin.morning_briefing.handlers.briefing_tick import (
    briefing_tick_handler,
)
from pollypm.plugins_builtin.morning_briefing.inbox import briefing_sweep_handler


_HERALD_PROFILE_PATH = Path(__file__).parent / "profiles" / "herald.md"


def _herald_prompt() -> str:
    """Read the herald persona from disk.

    Kept as a function (not a module-level constant) so edits to
    ``profiles/herald.md`` during development show up without restarting
    the plugin host. The file is tiny, so the re-read cost is negligible.
    """
    try:
        return _HERALD_PROFILE_PATH.read_text().strip()
    except OSError:
        # Defensive fallback — if the file ever goes missing the plugin
        # must still load so the rail doesn't lose its roster entry.
        return (
            "<identity>\nYou are the PollyPM Herald.\n</identity>\n"
            "<system>\nBriefing persona file missing; using fallback.\n</system>\n"
        )


def _register_handlers(api: JobHandlerAPI) -> None:
    api.register_handler(
        "briefing.tick",
        briefing_tick_handler,
        max_attempts=1,
        timeout_seconds=60.0,
    )
    api.register_handler(
        "briefing.sweep",
        briefing_sweep_handler,
        max_attempts=1,
        timeout_seconds=30.0,
    )


def _register_roster(api: RosterAPI) -> None:
    # Hourly tick — the handler internally gates on configured briefing
    # hour + date dedupe, so we get exactly-once-per-day with resilience
    # to restarts and missed ticks (spec §3).
    api.register_recurring(
        "@every 1h",
        "briefing.tick",
        {},
        dedupe_key="briefing.tick",
    )
    # Sweep auto-closes un-pinned briefings older than 24h. Runs every
    # 6 hours — the briefing itself fires once a day, so anything more
    # frequent is wasted work.
    api.register_recurring(
        "@every 6h",
        "briefing.sweep",
        {},
        dedupe_key="briefing.sweep",
    )


plugin = PollyPMPlugin(
    name="morning_briefing",
    version="0.1.0",
    description=(
        "Morning briefing: one inbox message per local day summarizing "
        "yesterday's activity and today's priorities."
    ),
    capabilities=(
        Capability(kind="agent_profile", name="herald"),
        Capability(kind="job_handler", name="briefing.tick"),
        Capability(kind="job_handler", name="briefing.sweep"),
        Capability(kind="roster_entry", name="briefing.tick"),
        Capability(kind="roster_entry", name="briefing.sweep"),
    ),
    agent_profiles={
        "herald": lambda: StaticPromptProfile(name="herald", prompt=_herald_prompt()),
    },
    register_handlers=_register_handlers,
    register_roster=_register_roster,
)
