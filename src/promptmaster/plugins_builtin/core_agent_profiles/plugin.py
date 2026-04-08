from promptmaster.agent_profiles.builtin import StaticPromptProfile, heartbeat_prompt, polly_prompt, worker_prompt
from promptmaster.plugin_api.v1 import PromptMasterPlugin

plugin = PromptMasterPlugin(
    name="core_agent_profiles",
    capabilities=("agent_profile",),
    agent_profiles={
        "polly": lambda: StaticPromptProfile(name="polly", prompt=polly_prompt()),
        "heartbeat": lambda: StaticPromptProfile(name="heartbeat", prompt=heartbeat_prompt()),
        "worker": lambda: StaticPromptProfile(name="worker", prompt=worker_prompt()),
    },
)
