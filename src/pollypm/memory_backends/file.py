from __future__ import annotations

import math
import re
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

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

        Supersession (M08): pass ``supersedes=<old_id>`` to chain a new
        entry after an existing one. The old row's ``superseded_by`` is
        set to the new entry's id so recall hides the old but audit
        (``read_entry`` + ``pm memory show``) still sees both.
        """
        supersedes = kwargs.pop("supersedes", None)
        if memory is not None and isinstance(memory, _TYPED_MEMORY_CLASSES):
            entry = self._write_typed_entry(memory, **kwargs)
        elif memory is not None:
            raise TypeError(
                "write_entry positional argument must be a typed memory dataclass "
                f"(got {type(memory).__name__})"
            )
        else:
            # Legacy keyword path — emit a DeprecationWarning and dispatch to a
            # ProjectMemory under the hood for back-compat.
            entry = self._write_legacy_entry(**kwargs)

        if supersedes is not None:
            try:
                old_id = int(supersedes)
            except (TypeError, ValueError):
                old_id = None
            if old_id is not None and old_id != entry.entry_id:
                self._state_store.update_memory_entry(
                    old_id, superseded_by=entry.entry_id
                )
        return entry

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
        scope_tier = str(
            overrides.get("scope_tier", memory.scope_tier)
            or ScopeTier.PROJECT.value
        )
        if scope_tier not in VALID_SCOPE_TIERS:
            raise ValueError(
                f"scope_tier must be one of {sorted(VALID_SCOPE_TIERS)} "
                f"(got {scope_tier!r})"
            )

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
            scope_tier=scope_tier,
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
        scope_tier: str = ScopeTier.PROJECT.value,
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
        if scope_tier not in VALID_SCOPE_TIERS:
            raise ValueError(
                f"scope_tier must be one of {sorted(VALID_SCOPE_TIERS)} "
                f"(got {scope_tier!r})"
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
            scope_tier=scope_tier,
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
        scope_tier: str = ScopeTier.PROJECT.value,
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
            "scope_tier": scope_tier,
        }
        result = self._plugins.run_filters(
            "memory.before_write",
            payload,
            metadata={"scope": scope, "kind": kind, "type": type_value, "scope_tier": scope_tier},
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
        scope_tier = str(payload.get("scope_tier", scope_tier))

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
            scope_tier=scope_tier,
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
            scope_tier=scope_tier,
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
            scope_tier=record.scope_tier,
        )
        self._plugins.run_observers(
            "memory.after_write",
            entry,
            metadata={"scope": scope, "kind": kind, "type": type_value, "scope_tier": scope_tier},
        )
        return entry

    def list_entries(
        self,
        *,
        scope: str | None = None,
        kind: str | None = None,
        type: str | None = None,
        scope_tier: str | None = None,
        limit: int = 50,
    ) -> list[MemoryEntry]:
        """List recent memory entries (back-compat wrapper over ``recall``).

        Behavior matches the pre-M02 contract: when callers pass a ``kind``
        filter (used by ``knowledge_extract`` and ``checkpoints`` to fetch
        legacy-kind rows like ``"decision"`` or ``"checkpoint"``), we defer
        to the direct state-store query so those kinds keep working. When
        ``kind`` is absent we route through ``recall(query="")`` — which
        orders by recency only, since there is no keyword to rank by — so
        this call path exercises the same plumbing as the primary read
        path.
        """
        if kind is not None:
            # Legacy-kind path. Recall doesn't take a kind filter (kinds
            # pre-date the typed schema and overlap with types only
            # loosely), so we stay on the direct store query here.
            entries = self._state_store.list_memory_entries(
                scope=scope, kind=kind, type=type, scope_tier=scope_tier, limit=limit
            )
            return [_record_to_entry(item) for item in entries]
        results = self.recall(
            "",
            scope=scope,
            types=[type] if type is not None else None,
            scope_tier=scope_tier,
            limit=limit,
        )
        return [result.entry for result in results]

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

    def recall(
        self,
        query: str,
        *,
        scope: str | list[str] | list[tuple[str, str]] | None = None,
        scope_tier: str | list[str] | None = None,
        types: list[str] | None = None,
        limit: int = 10,
        importance_min: int = 1,
        include_superseded: bool = False,
    ) -> list[RecallResult]:
        """Relevance-ranked retrieval over memory.

        Scoring (v1, see issue #231 / memory-system-review.md §3.3)::

            0.5 * fts_score + 0.3 * importance/5 + 0.2 * recency_decay

        where

        * ``fts_score`` — FTS5 bm25 normalised into ``[0, 1]`` via
          ``1 / (1 + |bm25|)``. bm25 is negative-lower-is-better by
          convention in SQLite, so we take the absolute value; more
          negative (= better) ⇒ larger ``|bm25|`` ⇒ smaller fraction —
          which inverts the "better" direction. We therefore pass the raw
          bm25 rank order and use the transform to squash into ``[0, 1]``
          while *preserving* the ordering given by SQLite.
        * ``importance/5`` — importance is 1..5 so this naturally sits in
          ``[0.2, 1.0]``.
        * ``recency_decay`` — ``exp(-age_days / 90)``; an entry written
          today scores 1.0, one 90 days old scores ~0.37.

        When ``query`` is empty, ``fts_score`` is 0 and results are
        ordered purely by importance + recency.

        ``scope`` accepts one of:

        * ``None`` — don't filter by scope.
        * a single ``str`` — match rows with ``scope == value``.
        * a ``list[str]`` — match rows whose ``scope`` is in the list.
        * a ``list[tuple[str, str]]`` — match rows whose
          ``(scope_tier, scope)`` pair is in the list (M03 tiered
          recall).

        ``scope_tier`` filters to rows whose ``scope_tier`` is in the
        given set (single string or list of strings). Combines with
        ``scope`` when both are passed.
        ``types`` filters to the given MemoryType values (strings).
        Entries marked superseded or past their TTL are never returned.
        """
        scopes, tier_scope_pairs = _coerce_scope_argument(scope)
        scope_tiers = _coerce_scope_tiers(scope_tier)
        raw_results = self._state_store.recall_memory_entries(
            query=query,
            scopes=scopes,
            types=types,
            importance_min=importance_min,
            limit=limit,
            scope_tiers=scope_tiers,
            tier_scope_pairs=tier_scope_pairs,
            include_superseded=include_superseded,
        )
        now = datetime.now(UTC)
        scored: list[RecallResult] = []
        for record, bm25_score in raw_results:
            fts_component = _normalize_bm25(bm25_score)
            importance_component = float(record.importance) / 5.0
            recency_component = _recency_decay(record.created_at, now)
            score = (
                0.5 * fts_component
                + 0.3 * importance_component
                + 0.2 * recency_component
            )
            rationale = (
                f"fts={fts_component:.2f} "
                f"importance={importance_component:.2f} "
                f"recency={recency_component:.2f}"
            )
            entry = _record_to_entry(record)
            scored.append(
                RecallResult(entry=entry, score=score, match_rationale=rationale)
            )
        # Re-rank in Python — SQL ordered by bm25 only; the final score
        # blends in importance + recency so a high-importance-older entry
        # can outrank a low-importance-fresh one (and vice versa). The
        # state store fetched a generous candidate pool (see
        # ``candidate_multiplier`` on recall_memory_entries) so the
        # Python-side top-K is meaningful.
        scored.sort(key=lambda r: r.score, reverse=True)
        top = scored[:limit]
        self._plugins.run_observers(
            "memory.after_recall",
            top,
            metadata={
                "query": query,
                "scope": scopes,
                "scope_tier": scope_tiers,
                "tier_scope_pairs": tier_scope_pairs,
                "types": types,
                "limit": limit,
                "result_count": len(top),
            },
        )
        return top

    def summarize(self, scope: str, *, limit: int = 20) -> str:
        """Summarise the most recent entries in a scope.

        Back-compat wrapper over ``recall``: issues a no-query recall
        (ordered by importance + recency) and renders the top N entries
        as markdown for legacy callers that want a quick paste-ready
        view.
        """
        results = self.recall("", scope=scope, limit=limit)
        entries = [result.entry for result in results]
        summary = _summarize_entries(scope, entries)
        self._plugins.run_observers(
            "memory.after_summarize",
            summary,
            metadata={"scope": scope, "entry_count": len(entries)},
        )
        return summary

    # ------------------------------------------------------------------
    # Tiered-scope lifecycle API (M03 / #232)
    #
    # These thin wrappers expose the state-store helpers via the backend
    # so callers that hold a FileMemoryBackend instance — typically a
    # plugin observer — don't need to reach into the store directly.
    # The after-write observer hook can register these as handlers for
    # ``memory.session.ended`` / ``memory.task.terminal`` and get the
    # spec's per-tier lifecycle for free.
    # ------------------------------------------------------------------

    def purge_session_scope(self, session_id: str) -> int:
        """Remove session-tier memory entries with ``scope == session_id``.

        Returns the count of removed rows. Emits a
        ``memory.after_session_purge`` observer so downstream plugins
        (e.g. analytics, audit) can react. No-op (returns 0) when the
        session has no session-tier memory, so it's safe to call from
        any session-teardown path.
        """
        removed = self._state_store.purge_session_scope(session_id)
        self._plugins.run_observers(
            "memory.after_session_purge",
            {"session_id": session_id, "removed": removed},
            metadata={"session_id": session_id, "removed": removed},
        )
        return removed

    def expire_task_scope(
        self,
        task_id: str,
        *,
        terminal_at: str | None = None,
        ttl_days: int = 30,
    ) -> int:
        """Set TTL ``terminal_at + ttl_days`` on task-tier entries.

        Returns the count of updated rows. Call this from the
        task-terminal transition (``mark_done`` / ``mark_cancelled``)
        wiring — the actual wire-up lives with the task service owner
        (work service) and isn't installed here, but the helper is
        kept backend-side so the same plugin-observer-shape that
        handles session teardown can handle task teardown too.
        """
        updated = self._state_store.expire_task_scope(
            task_id, terminal_at=terminal_at, ttl_days=ttl_days
        )
        self._plugins.run_observers(
            "memory.after_task_expire",
            {"task_id": task_id, "updated": updated, "ttl_days": ttl_days},
            metadata={"task_id": task_id, "updated": updated},
        )
        return updated

    def forget(self, entry_id: int) -> bool:
        """Hard-delete an entry by id. Returns True when a row was removed.

        The on-disk markdown file is left in place for forensic reasons
        (a stray ``.md`` is harmless and makes accidental forgets
        recoverable by re-import). Callers that want aggressive cleanup
        can delete the file themselves via ``entry.file_path`` before
        calling forget.
        """
        existing = self._state_store.get_memory_entry(entry_id)
        if existing is None:
            return False
        removed = self._state_store.delete_memory_entry(entry_id)
        if removed:
            self._plugins.run_observers(
                "memory.after_forget",
                _record_to_entry(existing),
                metadata={"scope": existing.scope, "kind": existing.kind, "type": existing.type},
            )
        return removed

    def update(
        self,
        entry_id: int,
        *,
        body: str | None = None,
        importance: int | None = None,
        tags: list[str] | None = None,
    ) -> MemoryEntry | None:
        """Edit an entry in place. Returns the refreshed MemoryEntry, or None.

        Only ``body``, ``importance``, and ``tags`` are mutable via the
        operator CLI — changing type / scope_tier requires a full
        rewrite because it changes the retrieval-time semantics.
        """
        changed = self._state_store.update_memory_entry(
            entry_id,
            body=body,
            importance=importance,
            tags=tags,
        )
        if not changed:
            return self.read_entry(entry_id)
        refreshed = self._state_store.get_memory_entry(entry_id)
        if refreshed is None:
            return None
        entry = _record_to_entry(refreshed)
        self._plugins.run_observers(
            "memory.after_edit",
            entry,
            metadata={"scope": entry.scope, "kind": entry.kind, "type": entry.type},
        )
        return entry

    def stats(self) -> dict[str, int | dict[str, int]]:
        """Return aggregate counts for the ``pm memory stats`` CLI.

        Shape::

            {
                "total": <int>,
                "by_type": {"user": N, "feedback": N, ...},
                "by_scope": {"pollypm": N, "operator": N, ...},
                "by_importance": {"1": N, "2": N, ...},
                "by_tier": {"project": N, "task": N, ...},
                "superseded": N,
                "expired": N,
            }

        Cheap to compute — a handful of GROUP BY queries over a single
        table.
        """
        store = self._state_store
        total_row = store.execute(
            "SELECT COUNT(*) FROM memory_entries"
        ).fetchone()
        total = int(total_row[0]) if total_row else 0

        def _bucket(col: str) -> dict[str, int]:
            rows = store.execute(
                f"SELECT {col}, COUNT(*) FROM memory_entries GROUP BY {col}"
            ).fetchall()
            return {str(row[0]) if row[0] is not None else "(null)": int(row[1]) for row in rows}

        by_type = _bucket("type")
        by_scope = _bucket("scope")
        by_importance = _bucket("importance")
        by_tier = _bucket("scope_tier")

        superseded = int(
            store.execute(
                "SELECT COUNT(*) FROM memory_entries WHERE superseded_by IS NOT NULL"
            ).fetchone()[0]
        )
        now_iso = datetime.now(UTC).isoformat()
        expired = int(
            store.execute(
                "SELECT COUNT(*) FROM memory_entries WHERE ttl_at IS NOT NULL AND ttl_at <= ?",
                (now_iso,),
            ).fetchone()[0]
        )
        return {
            "total": total,
            "by_type": by_type,
            "by_scope": by_scope,
            "by_importance": by_importance,
            "by_tier": by_tier,
            "superseded": superseded,
            "expired": expired,
        }

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
        scope_tier=getattr(record, "scope_tier", ScopeTier.PROJECT.value),
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
    scope_tier: str = ScopeTier.PROJECT.value,
) -> str:
    tag_line = ", ".join(tags) if tags else "none"
    ttl_line = ttl_at if ttl_at else "none"
    return "\n".join(
        [
            f"# {title}",
            "",
            f"- Scope: `{scope}`",
            f"- Scope tier: `{scope_tier}`",
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


def _coerce_scopes(scope: str | list[str] | None) -> list[str] | None:
    if scope is None:
        return None
    if isinstance(scope, str):
        return [scope]
    return list(scope)


def _coerce_scope_argument(
    scope: str | list[str] | list[tuple[str, str]] | None,
) -> tuple[list[str] | None, list[tuple[str, str]] | None]:
    """Split the polymorphic ``scope`` argument into (scopes, tier_scope_pairs).

    Accepts the same three forms ``recall()`` documents:

    * ``None`` → ``(None, None)``.
    * ``str`` → ``(["s"], None)``.
    * ``list[str]`` → ``(list, None)``.
    * ``list[tuple[str, str]]`` → ``(None, list)`` — tiered recall.

    Mixed lists (some strings, some tuples) raise ``TypeError`` so
    callers catch the bug at the boundary rather than at SQL execution.
    Empty list → ``(None, None)`` so "no scope filter" and "empty scope
    list" share semantics.
    """
    if scope is None:
        return None, None
    if isinstance(scope, str):
        return [scope], None
    items = list(scope)
    if not items:
        return None, None
    # Decide: are all strings, or all (tier, scope) tuples?
    all_strings = all(isinstance(item, str) for item in items)
    if all_strings:
        return [str(item) for item in items], None
    all_pairs = all(
        isinstance(item, tuple) and len(item) == 2 for item in items
    )
    if all_pairs:
        pairs: list[tuple[str, str]] = []
        for tier, scope_id in items:
            if tier not in VALID_SCOPE_TIERS:
                raise ValueError(
                    f"scope tuple tier must be one of "
                    f"{sorted(VALID_SCOPE_TIERS)} (got {tier!r})"
                )
            pairs.append((str(tier), str(scope_id)))
        return None, pairs
    raise TypeError(
        "scope must be None, a str, list[str], or list[tuple[tier, scope]] — "
        "mixed lists are not supported"
    )


def _coerce_scope_tiers(
    scope_tier: str | list[str] | None,
) -> list[str] | None:
    if scope_tier is None:
        return None
    if isinstance(scope_tier, str):
        tiers = [scope_tier]
    else:
        tiers = list(scope_tier)
    for tier in tiers:
        if tier not in VALID_SCOPE_TIERS:
            raise ValueError(
                f"scope_tier must be one of {sorted(VALID_SCOPE_TIERS)} "
                f"(got {tier!r})"
            )
    return tiers


def _normalize_bm25(bm25_score: float | None) -> float:
    """Map SQLite FTS5 bm25 to ``[0, 1]`` with higher = better.

    SQLite's bm25() returns a negative float where *more negative* means
    a better match (it's the negation of the standard BM25 score). The
    transform ``1 / (1 + |bm25|)`` squashes into ``[0, 1]`` while keeping
    the "more negative ⇒ higher score" intuition — with one subtlety:
    because ``|bm25|`` grows with match quality, ``1/(1+|bm25|)`` *shrinks*
    with match quality, which is backwards. So we invert: we want the
    best match to map to a number near 1, the worst to a number near 0.

    The clean form is: ``1 - 1/(1+|bm25|) = |bm25| / (1 + |bm25|)``. As
    ``|bm25|`` grows, the fraction approaches 1 — matching the
    "higher = better" convention callers expect.

    When ``bm25_score`` is None (no FTS query ran), returns 0.0 — the
    recall pipeline then ranks purely on importance + recency.
    """
    if bm25_score is None:
        return 0.0
    magnitude = abs(float(bm25_score))
    return magnitude / (1.0 + magnitude)


def _recency_decay(created_at: str, now: datetime, *, half_life_days: float = 90.0) -> float:
    """Exponential recency decay: ``exp(-age_days / half_life_days)``.

    An entry written ``now`` scores 1.0; one ``half_life_days`` old scores
    ``1/e`` ≈ 0.37. The spec calls this "linear half-life over 90 days"
    but the implementation here is the standard exponential form —
    strictly monotonic and numerically stable for very old entries. If
    the timestamp fails to parse (corruption / legacy rows without
    timezone info), we return 0.5 so the entry isn't silently buried.
    """
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return 0.5
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    age = (now - created).total_seconds() / 86400.0
    if age <= 0:
        return 1.0
    return math.exp(-age / half_life_days)
