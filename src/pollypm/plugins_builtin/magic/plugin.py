"""Compatibility magic plugin."""

from __future__ import annotations

from pollypm.plugins_builtin.core_agent_profiles.profiles import StaticPromptProfile
from pollypm.itsalive import build_deploy_instructions
from pollypm.plugin_api.v1 import PollyPMPlugin


plugin = PollyPMPlugin(
    name="magic",
    version="0.2.0",
    description="Compatibility alias for the dedicated itsalive plugin instructions.",
    capabilities=("agent_profile",),
    agent_profiles={
        "magic": lambda: StaticPromptProfile(name="magic", prompt=build_deploy_instructions()),
    },
)
