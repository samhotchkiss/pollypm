from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, Union


class MemoryType(str, Enum):
    """Typed memory kinds (M01 schema).

    Values are stable strings persisted in the ``memory_entries.type`` column
    and in file frontmatter. Adding a new type requires an enum extension +
    a schema migration (see #230 acceptance notes).
    """

    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"
    PATTERN = "pattern"
    EPISODIC = "episodic"


# ---------------------------------------------------------------------------
# Per-type dataclasses. Each TypedMemory subclass describes the required
# structure for a given MemoryType. Validation is performed in
# ``_validate_typed_memory`` below (called by ``FileMemoryBackend.write_entry``)
# so backends can share a single validator.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _TypedMemoryBase:
    """Shared fields carried by every typed memory payload.

    These are optional hints the backend may apply when persisting a typed
    memory: ``scope`` defaults to ``"project"`` at the backend when unset,
    ``importance`` defaults to 3, and ``tags``/``source`` follow the existing
    FileMemoryBackend conventions. ``ttl_at`` is an ISO-8601 timestamp string
    (null = no expiry), and ``superseded_by`` is reserved for future writers
    that flag contradictions (set lazily; no FK enforcement in v1).
    """

    scope: str | None = None
    importance: int = 3
    tags: tuple[str, ...] = ()
    source: str = "manual"
    ttl_at: str | None = None
    superseded_by: int | None = None

    # Subclasses override this with their MemoryType.
    TYPE: MemoryType = MemoryType.PROJECT


@dataclass(slots=True)
class UserMemory(_TypedMemoryBase):
    name: str = ""
    description: str = ""
    body: str = ""

    TYPE: MemoryType = MemoryType.USER


@dataclass(slots=True)
class FeedbackMemory(_TypedMemoryBase):
    rule: str = ""
    why: str = ""
    how_to_apply: str = ""

    TYPE: MemoryType = MemoryType.FEEDBACK


@dataclass(slots=True)
class ProjectMemory(_TypedMemoryBase):
    fact: str = ""
    why: str = ""
    how_to_apply: str = ""

    TYPE: MemoryType = MemoryType.PROJECT


@dataclass(slots=True)
class ReferenceMemory(_TypedMemoryBase):
    pointer: str = ""
    description: str = ""

    TYPE: MemoryType = MemoryType.REFERENCE


@dataclass(slots=True)
class PatternMemory(_TypedMemoryBase):
    when: str = ""
    then: str = ""

    TYPE: MemoryType = MemoryType.PATTERN


@dataclass(slots=True)
class EpisodicMemory(_TypedMemoryBase):
    summary: str = ""
    session_id: str = ""
    started_at: str = ""
    ended_at: str = ""

    TYPE: MemoryType = MemoryType.EPISODIC


TypedMemory = Union[
    UserMemory,
    FeedbackMemory,
    ProjectMemory,
    ReferenceMemory,
    PatternMemory,
    EpisodicMemory,
]


# Required fields per type — validator uses this map to emit precise errors.
_REQUIRED_FIELDS: dict[MemoryType, tuple[str, ...]] = {
    MemoryType.USER: ("name", "description", "body"),
    MemoryType.FEEDBACK: ("rule", "why", "how_to_apply"),
    MemoryType.PROJECT: ("fact", "why", "how_to_apply"),
    MemoryType.REFERENCE: ("pointer", "description"),
    MemoryType.PATTERN: ("when", "then"),
    MemoryType.EPISODIC: ("summary", "session_id", "started_at", "ended_at"),
}


def validate_typed_memory(memory: TypedMemory) -> None:
    """Raise ``ValueError`` if any required field on ``memory`` is missing.

    "Missing" means the attribute is absent or evaluates to an empty string /
    whitespace. The message names the type and the offending field so callers
    (e.g. extractors) can correct upstream without a stack trace read.
    """
    required = _REQUIRED_FIELDS.get(memory.TYPE, ())
    missing: list[str] = []
    for field_name in required:
        value = getattr(memory, field_name, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field_name)
    if missing:
        raise ValueError(
            f"{memory.TYPE.value} memory missing required field(s): "
            f"{', '.join(missing)}"
        )
    if not (1 <= int(memory.importance) <= 5):
        raise ValueError(
            f"{memory.TYPE.value} memory importance must be between 1 and 5 "
            f"(got {memory.importance})"
        )


@dataclass(slots=True)
class MemoryEntry:
    entry_id: int
    scope: str
    kind: str
    title: str
    body: str
    tags: tuple[str, ...]
    source: str
    file_path: Path
    summary_path: Path
    created_at: str
    updated_at: str
    # M01 typed-schema fields. Defaults make this dataclass back-compatible
    # with any external constructor calls that predate typed memory.
    type: str = MemoryType.PROJECT.value
    importance: int = 3
    superseded_by: int | None = None
    ttl_at: str | None = None


@dataclass(slots=True)
class MemorySummary:
    summary_id: int
    scope: str
    summary_text: str
    summary_path: Path
    entry_count: int
    created_at: str


class MemoryBackend(Protocol):
    def root(self) -> Path: ...

    def exists(self) -> bool: ...

    def ensure_memory(self) -> Path: ...

    def write_entry(self, *args, **kwargs) -> MemoryEntry: ...

    def list_entries(
        self,
        *,
        scope: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[MemoryEntry]: ...

    def read_entry(self, entry_id: int) -> MemoryEntry | None: ...

    def summarize(self, scope: str, *, limit: int = 20) -> str: ...

    def compact(self, scope: str, *, limit: int = 50) -> MemorySummary: ...
