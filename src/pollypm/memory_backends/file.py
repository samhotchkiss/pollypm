from __future__ import annotations

import re
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pollypm.memory_backends.base import (
    EpisodicMemory,
    FeedbackMemory,
    MemoryBackend,
    MemoryEntry,
    MemorySummary,
    MemoryType,
    PatternMemory,
    ProjectMemory,
    ReferenceMemory,
    TypedMemory,
    UserMemory,
    validate_typed_memory,
)

if TYPE_CHECKING:
    from pollypm.storage.state import StateStore


class _PluginHook(Protocol):
    """Narrow subset of ExtensionHost the file backend consumes.

    Keeping this as a structural protocol (not an import of ExtensionHost)
    is what keeps memory_backends from depending on plugin_host. The caller
    may pass an ExtensionHost, a test stub, or ``None`` (no plugin hooks).
    """

    def run_filters(self, hook_name: str, payload: object, *, metadata: dict | None = ...) -> object: ...

    def run_observers(self, hook_name: str, payload: object, *, metadata: dict | None = ...) -> object: ...


class _NullHook:
    def run_filters(self, hook_name, payload, *, metadata=None):
        # Mirror HookFilterResult shape minimally: allow-through.
        class _AllowResult:
            action = "allow"
            payload = None
            reason = None

        result = _AllowResult()
        result.payload = payload
        return result

    def run_observers(self, hook_name, payload, *, metadata=None):
        return []


_TYPED_MEMORY_CLASSES = (
    UserMemory,
    FeedbackMemory,
    ProjectMemory,
    ReferenceMemory,
    PatternMemory,
    EpisodicMemory,
)


class FileMemoryBackend(MemoryBackend):
    """File-backed memory store.

    Dependencies are injected via the constructor:

    * ``project_path`` — resolved project root used for file placement.
    * ``state_store`` — already-constructed StateStore handle.
    * ``plugins`` — filter/observer dispatcher (ExtensionHost-shaped);
      defaults to a no-op if omitted, so tests don't need a plugin host.
    * ``memory_root`` / ``artifacts_root`` — optional pre-resolved output
      directories. When omitted, the backend uses ``<project_path>/.pollypm``
      conventions so the happy path stays the same.

    The backend no longer imports ``plugin_host``, ``projects``, or
    constructs its own ``StateStore`` — those are the caller's concern (see
    :func:`pollypm.memory_backends.get_memory_backend`).
    """

    def __init__(
        self,
        project_path: Path,
        *,
        state_store: "StateStore | None" = None,
        plugins: _PluginHook | None = None,
        memory_root: Path | None = None,
        artifacts_root: Path | None = None,
        state_db: Path | None = None,
    ) -> None:
        self._project_path = project_path.expanduser().resolve()
        # Back-compat: if callers pass state_db/no state_store, construct one.
        # This keeps FileMemoryBackend(tmp_path) working for direct users,
        # but new code is expected to inject via get_memory_backend().
        if state_store is None:
            from pollypm.storage.state import StateStore

            self._state_db = state_db or (self._project_path / ".pollypm-state" / "state.db")
            self._state_store = StateStore(self._state_db)
        else:
            self._state_db = state_db
            self._state_store = state_store
        self._plugins: _PluginHook = plugins if plugins is not None else _NullHook()
        # Conventional layout under .pollypm/ — callers may override.
        dossier_root = self._project_path / ".pollypm"
        self._memory_root = memory_root or (dossier_root / "memory")
        self._artifacts_root = artifacts_root or (dossier_root / "artifacts")

    # Exposed read-only accessors for callers that need the same handles.
    # Tests and a few callers need the store and project path; exposing
    # them as properties keeps the ivars private while preserving the
    # existing read surface (no direct writes from outside).
    @property
    def project_path(self) -> Path:
        return self._project_path

    @property
    def store(self) -> "StateStore":
        return self._state_store

    def root(self) -> Path:
        return self._project_path

    def exists(self) -> bool:
        # Historically tied to the dossier dir; the backend can only answer
        # whether its own memory root exists without reaching into projects.
        return self._memory_root.parent.exists()

    def ensure_memory(self) -> Path:
        self._memory_root.mkdir(parents=True, exist_ok=True)
        (self._artifacts_root / "memory").mkdir(parents=True, exist_ok=True)
        return self._memory_root

    def write_entry(self, memory: TypedMemory | None = None, /, **kwargs: Any) -> MemoryEntry:
        """Persist a memory entry.

        Preferred form — pass a typed memory dataclass as the first positional
        argument::

            backend.write_entry(ProjectMemory(fact=..., why=..., how_to_apply=...))

        Legacy form (deprecated) — pass ``scope/title/body/kind/tags/source``
        as keyword arguments. This path stays supported for one release and
        maps to ``type=PROJECT`` for back-compat so existing writers
        (``knowledge_extract``, ``checkpoints``) keep working unchanged.
        """
        if memory is not None and isinstance(memory, _TYPED_MEMORY_CLASSES):
            return self._write_typed_entry(memory, **kwargs)
        if memory is not None:
            raise TypeError(
                "write_entry positional argument must be a typed memory dataclass "
                f"(got {type(memory).__name__})"
            )
        # Legacy keyword path — emit a DeprecationWarning and dispatch to a
        # ProjectMemory under the hood for back-compat.
        return self._write_legacy_entry(**kwargs)

    # ------------------------------------------------------------------
    # Typed write path
    # ------------------------------------------------------------------

    def _write_typed_entry(self, memory: TypedMemory, **overrides: Any) -> MemoryEntry:
        validate_typed_memory(memory)

        scope = str(overrides.get("scope") or memory.scope or "project")
        importance = int(overrides.get("importance", memory.importance))
        tags_value = overrides.get("tags", memory.tags)
        tags = [str(tag) for tag in (tags_value or [])]
        source = str(overrides.get("source", memory.source))
        ttl_at = overrides.get("ttl_at", memory.ttl_at)
        superseded_by = overrides.get("superseded_by", memory.superseded_by)

        title, body = _render_typed_title_body(memory)
        return self._persist_entry(
            scope=scope,
            title=title,
            body=body,
            type_value=memory.TYPE.value,
            # Legacy ``kind`` column mirrors the type for back-compat with
            # readers that still filter by ``kind``. New readers should use
            # the ``type`` column.
            kind=memory.TYPE.value,
            tags=tags,
            source=source,
            importance=importance,
            ttl_at=ttl_at,
            superseded_by=superseded_by,
        )

    # ------------------------------------------------------------------
    # Legacy keyword path
    # ------------------------------------------------------------------

    def _write_legacy_entry(
        self,
        *,
        scope: str,
        title: str,
        body: str,
        kind: str = "note",
        tags: list[str] | None = None,
        source: str = "manual",
        importance: int = 3,
        ttl_at: str | None = None,
        superseded_by: int | None = None,
    ) -> MemoryEntry:
        warnings.warn(
            "FileMemoryBackend.write_entry(scope=..., title=..., body=..., kind=...) "
            "is deprecated; pass a typed memory dataclass "
            "(UserMemory, FeedbackMemory, ProjectMemory, ReferenceMemory, "
            "PatternMemory, EpisodicMemory) instead. Legacy calls are persisted "
            "with type='project'.",
            DeprecationWarning,
            stacklevel=3,
        )
        return self._persist_entry(
            scope=scope,
            title=title,
            body=body,
            type_value=MemoryType.PROJECT.value,
            kind=kind,
            tags=[str(tag) for tag in (tags or [])],
            source=source,
            importance=int(importance),
            ttl_at=ttl_at,
            superseded_by=superseded_by,
        )

    # ------------------------------------------------------------------
    # Shared persistence — runs plugin filters, writes to disk + SQLite
    # ------------------------------------------------------------------

    def _persist_entry(
        self,
        *,
        scope: str,
        title: str,
        body: str,
        type_value: str,
        kind: str,
        tags: list[str],
        source: str,
        importance: int,
        ttl_at: str | None,
        superseded_by: int | None,
    ) -> MemoryEntry:
        payload = {
            "scope": scope,
            "title": title,
            "body": body,
            "kind": kind,
            "type": type_value,
            "tags": list(tags),
            "source": source,
            "importance": importance,
            "ttl_at": ttl_at,
            "superseded_by": superseded_by,
        }
        result = self._plugins.run_filters(
            "memory.before_write",
            payload,
            metadata={"scope": scope, "kind": kind, "type": type_value},
        )
        action = getattr(result, "action", "allow")
        if action == "deny":
            raise PermissionError(getattr(result, "reason", None) or "Memory write denied by plugin")
        mutated = getattr(result, "payload", None)
        payload = mutated if isinstance(mutated, dict) else payload

        scope = str(payload.get("scope", scope))
        title = str(payload.get("title", title))
        body = str(payload.get("body", body))
        kind = str(payload.get("kind", kind))
        type_value = str(payload.get("type", type_value))
        tags = [str(tag) for tag in payload.get("tags", tags)]
        source = str(payload.get("source", source))
        importance = int(payload.get("importance", importance))
        ttl_at = payload.get("ttl_at", ttl_at)
        superseded_by = payload.get("superseded_by", superseded_by)

        self.ensure_memory()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        slug = _slugify(title)[:80]  # Truncate to avoid "File name too long" errors
        scope_dir = self._memory_root / scope
        scope_dir.mkdir(parents=True, exist_ok=True)
        file_path = scope_dir / f"{stamp}-{slug}.md"
        summary_path = file_path
        content = _render_entry(
            scope=scope,
            title=title,
            body=body,
            kind=kind,
            type_value=type_value,
            tags=tags,
            source=source,
            importance=importance,
            ttl_at=ttl_at,
        )
        file_path.write_text(content)

        record = self._state_store.record_memory_entry(
            scope=scope,
            kind=kind,
            title=title,
            body=body,
            tags=tags,
            source=source,
            file_path=str(file_path),
            summary_path=str(summary_path),
            type=type_value,
            importance=importance,
            superseded_by=superseded_by,
            ttl_at=ttl_at,
        )
        entry = MemoryEntry(
            entry_id=record.entry_id,
            scope=record.scope,
            kind=record.kind,
            title=record.title,
            body=record.body,
            tags=record.tags,
            source=record.source,
            file_path=Path(record.file_path),
            summary_path=Path(record.summary_path),
            created_at=record.created_at,
            updated_at=record.updated_at,
            type=record.type,
            importance=record.importance,
            superseded_by=record.superseded_by,
            ttl_at=record.ttl_at,
        )
        self._plugins.run_observers(
            "memory.after_write",
            entry,
            metadata={"scope": scope, "kind": kind, "type": type_value},
        )
        return entry

    def list_entries(
        self,
        *,
        scope: str | None = None,
        kind: str | None = None,
        type: str | None = None,
        limit: int = 50,
    ) -> list[MemoryEntry]:
        entries = self._state_store.list_memory_entries(
            scope=scope, kind=kind, type=type, limit=limit
        )
        return [_record_to_entry(item) for item in entries]

    def read_entry(self, entry_id: int) -> MemoryEntry | None:
        entry = self._state_store.get_memory_entry(entry_id)
        if entry is None:
            return None
        memory_entry = _record_to_entry(entry)
        self._plugins.run_observers(
            "memory.after_read",
            memory_entry,
            metadata={"scope": entry.scope, "kind": entry.kind, "type": entry.type},
        )
        return memory_entry

    def summarize(self, scope: str, *, limit: int = 20) -> str:
        entries = self.list_entries(scope=scope, limit=limit)
        summary = _summarize_entries(scope, entries)
        self._plugins.run_observers("memory.after_summarize", summary, metadata={"scope": scope, "entry_count": len(entries)})
        return summary

    def compact(self, scope: str, *, limit: int = 50) -> MemorySummary:
        entries = self.list_entries(scope=scope, limit=limit)
        summary_text = _summarize_entries(scope, entries)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        summary_dir = self._artifacts_root / "memory" / scope
        summary_dir.mkdir(parents=True, exist_ok=True)
        summary_path = summary_dir / f"{stamp}.md"
        summary_path.write_text(summary_text)
        record = self._state_store.record_memory_summary(
            scope=scope,
            summary_text=summary_text,
            summary_path=str(summary_path),
            entry_count=len(entries),
        )
        summary = MemorySummary(
            summary_id=record.summary_id,
            scope=record.scope,
            summary_text=record.summary_text,
            summary_path=Path(record.summary_path),
            entry_count=record.entry_count,
            created_at=record.created_at,
        )
        self._plugins.run_observers("memory.after_compact", summary, metadata={"scope": scope, "entry_count": len(entries)})
        return summary


def _record_to_entry(record) -> MemoryEntry:
    return MemoryEntry(
        entry_id=record.entry_id,
        scope=record.scope,
        kind=record.kind,
        title=record.title,
        body=record.body,
        tags=record.tags,
        source=record.source,
        file_path=Path(record.file_path),
        summary_path=Path(record.summary_path),
        created_at=record.created_at,
        updated_at=record.updated_at,
        type=getattr(record, "type", "project"),
        importance=getattr(record, "importance", 3),
        superseded_by=getattr(record, "superseded_by", None),
        ttl_at=getattr(record, "ttl_at", None),
    )


def _render_typed_title_body(memory: TypedMemory) -> tuple[str, str]:
    """Convert a typed memory into a (title, body) pair for on-disk storage.

    The body uses a simple key/value markdown format — readable by humans and
    trivially parseable by future extractors. Title is the first salient
    field of each type so the file-system name is informative.
    """
    if isinstance(memory, UserMemory):
        return memory.name or memory.description[:80] or "user memory", "\n".join(
            [
                f"**Description:** {memory.description}",
                "",
                memory.body,
            ]
        )
    if isinstance(memory, FeedbackMemory):
        return memory.rule[:120] or "feedback", "\n".join(
            [
                f"**Rule:** {memory.rule}",
                f"**Why:** {memory.why}",
                f"**How to apply:** {memory.how_to_apply}",
            ]
        )
    if isinstance(memory, ProjectMemory):
        return memory.fact[:120] or "project fact", "\n".join(
            [
                f"**Fact:** {memory.fact}",
                f"**Why:** {memory.why}",
                f"**How to apply:** {memory.how_to_apply}",
            ]
        )
    if isinstance(memory, ReferenceMemory):
        return memory.description[:120] or memory.pointer[:120] or "reference", "\n".join(
            [
                f"**Pointer:** {memory.pointer}",
                f"**Description:** {memory.description}",
            ]
        )
    if isinstance(memory, PatternMemory):
        return f"When: {memory.when}"[:120] or "pattern", "\n".join(
            [
                f"**When:** {memory.when}",
                f"**Then:** {memory.then}",
            ]
        )
    if isinstance(memory, EpisodicMemory):
        title = memory.summary[:120] or f"session {memory.session_id}" or "episodic"
        return title, "\n".join(
            [
                f"**Session:** {memory.session_id}",
                f"**Started:** {memory.started_at}",
                f"**Ended:** {memory.ended_at}",
                "",
                memory.summary,
            ]
        )
    # Unreachable: validate_typed_memory has already rejected unknown types.
    return "memory", ""


def _render_entry(
    *,
    scope: str,
    title: str,
    body: str,
    kind: str,
    type_value: str,
    tags: list[str],
    source: str,
    importance: int,
    ttl_at: str | None,
) -> str:
    tag_line = ", ".join(tags) if tags else "none"
    ttl_line = ttl_at if ttl_at else "none"
    return "\n".join(
        [
            f"# {title}",
            "",
            f"- Scope: `{scope}`",
            f"- Type: `{type_value}`",
            f"- Kind: `{kind}`",
            f"- Importance: {importance}",
            f"- Source: `{source}`",
            f"- Tags: {tag_line}",
            f"- TTL: {ttl_line}",
            "",
            body.rstrip(),
            "",
        ]
    )


def _summarize_entries(scope: str, entries: list[MemoryEntry]) -> str:
    lines = [
        f"# Memory Summary: {scope}",
        "",
        f"- Entries: {len(entries)}",
        "",
    ]
    if not entries:
        lines.append("No memory entries recorded yet.")
        lines.append("")
        return "\n".join(lines)

    lines.append("## Recent Entries")
    lines.append("")
    for entry in entries[:10]:
        snippet = " ".join(entry.body.split())[:140]
        lines.append(f"- {entry.title}: {snippet}")
    lines.append("")
    return "\n".join(lines)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "memory"
