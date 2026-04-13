"""Compatibility magic plugin."""

from __future__ import annotations

from pollypm.agent_profiles.builtin import StaticPromptProfile
from pollypm.plugin_api.v1 import PollyPMPlugin
from pollypm.plugins_builtin.itsalive.plugin import build_deploy_instructions, read_deploy_token, read_owner_token


plugin = PollyPMPlugin(
    name="magic",
    version="0.2.0",
    description="Compatibility alias for the dedicated itsalive plugin instructions.",
    capabilities=("agent_profile",),
    agent_profiles={
        "magic": lambda: StaticPromptProfile(name="magic", prompt=build_deploy_instructions()),
    },
)
