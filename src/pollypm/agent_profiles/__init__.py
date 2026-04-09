from pathlib import Path

from pollypm.agent_profiles.base import AgentProfile, AgentProfileContext
from pollypm.plugin_host import extension_host_for_root


def get_agent_profile(name: str, *, root_dir: Path | None = None) -> AgentProfile:
    root = str((root_dir or Path.cwd()).resolve())
    return extension_host_for_root(root).get_agent_profile(name)


__all__ = ["AgentProfile", "AgentProfileContext", "get_agent_profile"]
