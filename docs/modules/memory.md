**Last Verified:** 2026-04-22

## Summary

The memory system gives PollyPM agents a stable place to store facts, feedback, project knowledge, patterns, and episodic notes — and to recall them into fresh session prompts. `MemoryBackend` is the protocol; `FileMemoryBackend` is the only shipped implementation, writing markdown-with-frontmatter files under `<project>/.pollypm/memory/` and indexing them in a SQLite FTS5 table on the state DB.

`memory_prompts.py` is the load-bearing renderer. `build_memory_injection(...)` runs keyword + importance-fallback recall against the backend and produces a `## What you should know` markdown section. `build_worker_protocol_injection(...)` prepends the canonical worker guide for `role == "worker"` sessions so every worker boots knowing the task lifecycle before any recall-based context.

Touch this module when adding a new memory type, a new scope tier, or when changing the injection budget. Do not add LLM calls here — memory is deterministic; `memory_curator` is the separate daily pass that uses heuristics (not embeddings) to keep recall sharp.

## Core Contracts

```python
# src/pollypm/memory_backends/base.py
class MemoryType(str, Enum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"
    PATTERN = "pattern"
    EPISODIC = "episodic"

class ScopeTier(str, Enum):
    SESSION = "session"   # auto-purge when session ends
    TASK = "task"         # TTL 30 days after terminal
    PROJECT = "project"   # no auto-expiry
    USER = "user"         # no auto-expiry, cross-project

class MemoryBackend(Protocol):
    def root(self) -> Path: ...
    def exists(self) -> bool: ...
    def ensure_memory(self) -> Path: ...
    def write_entry(self, *args, **kwargs) -> MemoryEntry: ...
    def read_entry(self, entry_id: int) -> MemoryEntry | None: ...
    def list_entries(self, ...) -> list[MemoryEntry]: ...
    def recall(
        self, *, query: str, scope: list[tuple[str, str]], types: list[str],
        importance_min: int = 1, limit: int = 15,
    ) -> list[RecallResult]: ...
    def summarize(self, scope: str, *, limit: int = 20) -> str: ...
    def compact(self, scope: str, *, limit: int = 50) -> MemorySummary: ...

# src/pollypm/memory_prompts.py
BUDGET_TOKENS = 4096
CHARS_PER_TOKEN = 4
BUDGET_CHARS = BUDGET_TOKENS * CHARS_PER_TOKEN  # 16_384
INJECTION_HEADING = "## What you should know"

def compute_task_context_summary(
    *, task_title: str | None = None, task_description: str | None = None,
    session_role: str | None = None, project: str | None = None,
) -> str: ...

def build_memory_injection(
    backend: MemoryBackend,
    *,
    user_id: str,
    project_name: str,
    task_context_summary: str,
    types: Iterable[str] | None = None,
    importance_min: int = 3,
    limit: int = 15,
    budget_chars: int = BUDGET_CHARS,
) -> str: ...

def prepend_memory_injection(prompt: str, injection: str) -> str: ...

def load_worker_guide_text() -> str: ...

def build_worker_protocol_injection(
    *, role: str, task_title: str | None = None, task_id: str | None = None,
) -> str: ...

def prepend_worker_protocol(prompt: str, injection: str) -> str: ...
```

## File Structure

- `src/pollypm/memory_backends/__init__.py` — registry (`get_memory_backend`) and re-exports.
- `src/pollypm/memory_backends/base.py` — `MemoryBackend` protocol, `MemoryType`, `ScopeTier`, per-type dataclasses, `validate_typed_memory`.
- `src/pollypm/memory_backends/file.py` — `FileMemoryBackend` (markdown files + FTS5).
- `src/pollypm/memory_prompts.py` — injection renderer (memory + worker protocol).
- `src/pollypm/memory_cli.py` — `pm memory` commands.
- `src/pollypm/memory_extractors.py` — extractors used by `knowledge_extract` to populate memory from transcripts.
- `src/pollypm/memory_curator.py` — daily TTL sweep / dedup / decay / promotion. See [plugins/memory-curator.md](../plugins/memory-curator.md).
- `src/pollypm/storage/state.py` — owns `memory_entries` + `memory_summaries` FTS tables.

## Implementation Details

- **Deterministic.** For a fixed store and fixed `(user, project, task)` triple, `build_memory_injection` produces byte-identical output. `MemoryBackend.recall` orders by score then by id desc as a tie-breaker; the renderer is pure.
- **Two-pass recall.** Primary pass uses the caller's `task_context_summary` as the FTS5 query. Fallback pass uses an empty query so entries that matter across any task (user preferences, load-bearing project facts) surface even without keyword overlap. Merge preserves primary order, dedupes by `entry_id`.
- **Budget.** Total rendered length is capped at `BUDGET_CHARS = 16384` (≈ 4K tokens at 4 chars/token). When recall output exceeds the budget, the lowest-scored entries drop out first.
- **Four surfacing types.** Only `USER`, `FEEDBACK`, `PROJECT`, `PATTERN` are rendered into the injection section. `REFERENCE` is pointer data; `EPISODIC` is curated separately by the memory curator.
- **Worker protocol injection.** `build_worker_protocol_injection(role="worker", ...)` loads the canonical worker guide (see `_locate_worker_guide` for the search path — `docs/worker-guide.md` archive or the in-repo fallback) and wraps it under a `## Worker Protocol` heading. This sits *above* the memory injection when both are applied.
- **FTS5 safety.** Callers that accept user-provided query strings should pass them through `storage.fts_query.normalize_fts_query` before calling `recall` — unescaped FTS operators are a known crash source.
- **TTL.** Task-scope entries TTL 30 days after the task reaches terminal; session-scope entries purge when the session ends; project and user scope do not expire. `StateStore.sweep_expired_memory_entries()` plus the `memory.ttl_sweep` recurring handler enforce this daily.

## Related Docs

- [plugins/memory-curator.md](../plugins/memory-curator.md) — daily dedup/decay/promotion pass.
- [features/agent-personas.md](../features/agent-personas.md) — consumers of `build_worker_protocol_injection` + `build_memory_injection`.
- [modules/state-store.md](state-store.md) — `memory_entries` + FTS tables live on the state DB.
