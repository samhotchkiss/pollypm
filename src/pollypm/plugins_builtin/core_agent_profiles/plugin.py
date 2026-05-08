from pollypm.plugin_api.v1 import Capability, PollyPMPlugin
from pollypm.agent_profiles.defaults import (
    StaticPromptProfile,
    heartbeat_prompt,
    polly_prompt,
    reviewer_prompt,
    triage_prompt,
    worker_prompt,
)

plugin = PollyPMPlugin(
    name="core_agent_profiles",
    capabilities=(
        Capability(kind="agent_profile", name="polly"),
        Capability(kind="agent_profile", name="russell"),
        Capability(kind="agent_profile", name="heartbeat"),
        Capability(kind="agent_profile", name="worker"),
        Capability(kind="agent_profile", name="triage"),
    ),
    agent_profiles={
        "polly": lambda: StaticPromptProfile(name="polly", prompt=polly_prompt()),
        "russell": lambda: StaticPromptProfile(name="russell", prompt=reviewer_prompt()),
        "heartbeat": lambda: StaticPromptProfile(name="heartbeat", prompt=heartbeat_prompt()),
        "worker": lambda: StaticPromptProfile(name="worker", prompt=worker_prompt()),
        "triage": lambda: StaticPromptProfile(name="triage", prompt=triage_prompt()),
    },
)
