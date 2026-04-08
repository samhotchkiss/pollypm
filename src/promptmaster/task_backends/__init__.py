from pathlib import Path

from promptmaster.task_backends.base import TaskBackend, TaskRecord
from promptmaster.task_backends.file import FileTaskBackend


def get_task_backend(project_path: Path, backend_name: str = "file") -> TaskBackend:
    if backend_name == "file":
        return FileTaskBackend(project_path)
    raise ValueError(f"Unsupported task backend: {backend_name}")


__all__ = ["TaskBackend", "TaskRecord", "FileTaskBackend", "get_task_backend"]
