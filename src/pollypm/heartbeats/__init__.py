from pathlib import Path

from pollypm.heartbeats.base import HeartbeatBackend
from pollypm.plugin_host import extension_host_for_root


def get_heartbeat_backend(name: str, *, root_dir: Path | None = None) -> HeartbeatBackend:
    root = str((root_dir or Path.cwd()).resolve())
    return extension_host_for_root(root).get_heartbeat_backend(name)
