"""Compatibility magic plugin."""

from __future__ import annotations

from pollypm.agent_profiles.defaults import StaticPromptProfile
from pollypm.itsalive import build_deploy_instructions
from pollypm.plugin_api.v1 import Capability, PollyPMPlugin


plugin = PollyPMPlugin(
    name="magic",
    version="0.2.0",
    description="Compatibility alias for the dedicated itsalive plugin instructions.",
    capabilities=(Capability(kind="agent_profile", name="magic"),),
    agent_profiles={
        "magic": lambda: StaticPromptProfile(name="magic", prompt=build_deploy_instructions()),
    },
)
