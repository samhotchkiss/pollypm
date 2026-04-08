from pathlib import Path

from promptmaster.plugin_host import extension_host_for_root
from promptmaster.schedulers.base import ScheduledJob, SchedulerBackend


def get_scheduler_backend(name: str, *, root_dir: Path | None = None) -> SchedulerBackend:
    root = str((root_dir or Path.cwd()).resolve())
    return extension_host_for_root(root).get_scheduler_backend(name)


__all__ = ["ScheduledJob", "SchedulerBackend", "get_scheduler_backend"]
