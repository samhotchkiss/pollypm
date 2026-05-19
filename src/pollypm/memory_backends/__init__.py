from pathlib import Path

from pollypm.memory_backends.base import (
    VALID_SCOPE_TIERS,
    EpisodicMemory,
    FeedbackMemory,
    MemoryBackend,
    MemoryEntry,
    MemorySummary,
    MemoryType,
    PatternMemory,
    ProjectMemory,
    RecallResult,
    ReferenceMemory,
    ScopeTier,
    TypedMemory,
    UserMemory,
    validate_typed_memory,
)
from pollypm.memory_backends.file import FileMemoryBackend


def get_memory_backend(project_path: Path, backend_name: str = "file") -> MemoryBackend:
    """Resolve a memory backend for ``project_path``.

    This is the composition root for memory backends: it pulls in the
    plugin host, scaffolds the project layout, constructs the StateStore,
    and hands those resolved handles to the backend. Backends themselves
    should not reach into ``plugin_host`` / ``projects`` / ``storage.state``
    — they receive what they need here (#115).
    """
    if backend_name == "file":
        # Local imports keep the package boundary narrow and avoid circular
        # imports at module load time (projects imports memory_backends in
        # a few paths).
        from pollypm.plugin_host import extension_host_for_root
        from pollypm.projects import ensure_project_scaffold, project_artifacts_dir, project_dossier_dir
        from pollypm.storage._backend_dispatch import is_pg_backend
        resolved = project_path.expanduser().resolve()
        # Scaffold the project up-front so the backend can assume memory
        # roots exist (it used to call ensure_project_scaffold itself).
        ensure_project_scaffold(resolved)
        state_db = resolved / ".pollypm" / "state.db"
        # Slice K-state-port phase 2d (#1737): on the pg backend wire
        # FileMemoryBackend to a stateless PgMemoryStore adapter that
        # routes every memory_* call through pollypm.storage.pg_memory.
        # On sqlite we keep the legacy StateStore handle.
        state_store: object
        try:
            from pollypm.config import load_config

            config = load_config()
        except Exception:  # noqa: BLE001
            config = None
        if is_pg_backend(config):
            from pollypm.storage.pg_memory import PgMemoryStore

            state_store = PgMemoryStore()
        else:
            from pollypm.storage.state import StateStore

            state_store = StateStore(state_db)
        return FileMemoryBackend(
            resolved,
            state_store=state_store,
            plugins=extension_host_for_root(str(resolved)),
            memory_root=project_dossier_dir(resolved) / "memory",
            artifacts_root=project_artifacts_dir(resolved),
            state_db=state_db,
        )
    raise ValueError(f"Unsupported memory backend: {backend_name}")


__all__ = [
    "MemoryBackend",
    "MemoryEntry",
    "MemorySummary",
    "MemoryType",
    "RecallResult",
    "ScopeTier",
    "VALID_SCOPE_TIERS",
    "TypedMemory",
    "UserMemory",
    "FeedbackMemory",
    "ProjectMemory",
    "ReferenceMemory",
    "PatternMemory",
    "EpisodicMemory",
    "validate_typed_memory",
    "FileMemoryBackend",
    "get_memory_backend",
]
