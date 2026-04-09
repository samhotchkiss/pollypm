from pathlib import Path

from pollypm.memory_backends.base import MemoryBackend, MemoryEntry, MemorySummary
from pollypm.memory_backends.file import FileMemoryBackend


def get_memory_backend(project_path: Path, backend_name: str = "file") -> MemoryBackend:
    if backend_name == "file":
        return FileMemoryBackend(project_path)
    raise ValueError(f"Unsupported memory backend: {backend_name}")


__all__ = [
    "MemoryBackend",
    "MemoryEntry",
    "MemorySummary",
    "FileMemoryBackend",
    "get_memory_backend",
]
