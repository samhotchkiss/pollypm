# Memory Management System — Ground-Up Review and Redesign

**Status:** draft proposal for post-v1. Written by Claude Opus 4.7 (1M context) after a fresh read of the current subsystem.

## 1. What's there today

The current memory system (`src/pollypm/memory_backends/`, ~300 LOC across `base.py` + `file.py` + `__init__.py`):

- **Protocol:** `MemoryBackend` with `write_entry`, `list_entries`, `read_entry`, `summarize`, `compact`.
- **Entry shape:** `MemoryEntry(entry_id, scope, kind, title, body, tags, source, file_path, summary_path, created_at, updated_at)`.
- **Storage:** files at `.pollypm/memory/<scope>/<timestamp>-<slug>.md` + SQLite index (`record_memory_entry` in `storage/state.py`).
- **Writers:** `knowledge_extract.py` (post-session knowledge deltas), `checkpoints.py` (session checkpoint summaries).
- **Readers:** Almost nothing reads it back. `summarize` exists; no agent automatically pulls memory into its context at session start.

## 2. Honest assessment — where it's weak

1. **Retrieval is a text dump, not a recall.** `summarize(scope, limit=20)` returns a concatenation of the last 20 entries. An agent asking "what did we decide about testing?" has to read all 20 and filter. No relevance ranking. No query.

2. **No forgetting.** `compact` creates a summary artifact but never *removes* anything. Memory grows linearly forever, which means the "most recent" signal dilutes over time.

3. **No importance weighting.** A one-off observation sits beside a load-bearing decision with equal weight. No signal for "this is the kind of thing to always surface" vs. "this was noise."

4. **Ad-hoc kind taxonomy.** `knowledge_extract.py` writes `kind=goals/architecture_changes/decisions/risks/ideas`. `base.py` documents `kind="note"` as the default. No schema per kind, no validator — any string goes. The kinds drift over time and carry no semantic promise.

5. **Flat scope model.** `scope` is a single string, usually a project name or "operator". There is no notion of session-scoped vs. project-scoped vs. user-scoped memory, which are genuinely different lifecycles.

6. **No automatic context injection.** The most important read-path an agent memory system has is "inject relevant memories into the agent's prompt at session start." We don't do this. Memory is write-only for most consumers.

7. **Single-writer semantics are accidental.** Two agents writing conflicting facts both persist. No merge, no supersession, no versioning.

8. **No freshness / decay.** A memory from 6 months ago ranks equal to yesterday's. Advisors don't distinguish "this used to be true."

9. **No structured fields per kind.** A "decision" should have a title, rationale, alternatives-considered, decision-date, revisit-by. Today they're all freetext markdown.

10. **The observability surface is absent.** `pm memory list` doesn't exist. Memory is invisible to the operator unless they cat files.

## 3. What a good memory system for PollyPM looks like

The design I'd propose draws from the agentic-design-patterns book (Chapter 8) plus the Claude Code auto-memory system (which works well in practice — it's what's injected into my own context today).

### 3.1 Tiered memory

Four layers, each with distinct lifecycle and retrieval semantics:

| Tier | Lifetime | What it holds | Example |
|---|---|---|---|
| **Working** | session-only | Context window + session state | Current conversation |
| **Episodic** | per-task / per-session, retained | Narrative of what happened | "In session #5312, worker built the auth module and shipped on approve" |
| **Semantic** | project- or user-scoped, long-lived | Distilled facts, preferences, decisions | "This project's test strategy is Playwright e2e; user prefers small modules" |
| **Procedural** | project- or user-scoped, curated | Patterns, how-tos, "when X do Y" | "When the cockpit crashes, run `pm down && pm up` to reset" |

Only Semantic and Procedural are long-lived and the heart of cross-session learning. Episodic is retained but rarely re-read. Working is what the session already has.

### 3.2 Typed entries with per-type schemas

Borrowed from the Claude Code auto-memory pattern. Each memory is one of a small set of types:

| Type | When to write | Structure |
|---|---|---|
| **user** | Learning about the operator's role, skill, preferences | one-paragraph body; `description` one-liner |
| **feedback** | Operator corrected or confirmed an approach | rule + `Why:` line + `How to apply:` line |
| **project** | Project-specific fact, state, decision, constraint | fact + `Why:` + `How to apply:` |
| **reference** | Pointer to external system (URL, Linear project, Grafana board) | one-paragraph pointer |
| **pattern** | How-to / when-to pattern for the project | condition + action |
| **episodic** | What happened in a specific session (auto-captured) | narrative summary with timestamps |

Every entry has: `id`, `type`, `name`, `description` (one-liner used for relevance matching), `body`, `scope`, `importance` (1–5), `created_at`, `updated_at`, `superseded_by` (nullable), `ttl` (nullable).

### 3.3 Retrieval API

Replace `list_entries` / `summarize` with a recall-shaped API:

```python
memory.recall(
    query: str,                    # free-text query
    scope: str | list[str] | None, # project / user / cross-scope
    types: list[str] | None,       # filter by type
    limit: int = 10,
    importance_min: int = 1,
) -> list[MemoryEntry]
```

v1: scores by keyword match + importance + recency. v1.1: adds vector-embedding similarity (book Ch 8's long-term memory model — the Memory Bank / MemoryService pattern).

### 3.4 Write discipline — memory is what was *learned*, not what was *done*

Not every event becomes a memory. Writing is intentional, like extracting signal from noise. The knowledge_extract.py flow is the right *direction* (distill deltas from session events) but needs sharper rules:

- **User memory** extracted when user reveals role/skill/preference.
- **Feedback memory** extracted on correction or notable confirmation.
- **Project memory** extracted on decision, constraint, or non-derivable fact.
- **Pattern memory** extracted when a how-to was established.
- **Episodic memory** auto-written at session-end with a summary template.

The extractor agent (a lightweight Haiku pass, as today) produces candidate memories. Rule-based filter or reviewer-agent decides which graduate.

### 3.5 Automatic context injection at session start

When a new session starts (any persona: Polly, Russell, worker, planner, advisor, herald), the SessionService queries memory:

```python
relevant = memory.recall(
    query=session.task_context_summary,
    scope=[session.project, session.user],
    types=["user", "feedback", "project", "pattern"],
    importance_min=3,
    limit=15,
)
```

The top-N memories get packed into the session's system prompt under a "What you should know" section. This is how "Polly remembers you" becomes real.

### 3.6 Forgetting and consolidation

A curator handler (roster-registered, runs daily):
- Merges duplicate / near-duplicate memories.
- Marks low-importance old memories for pruning (TTL).
- Promotes episodic patterns that recur into procedural memory.
- Logs every action so the user can audit.

### 3.7 Update and supersession

When a new memory contradicts an existing one, the writer flags it. The old memory is marked `superseded_by=<new_id>` rather than deleted. `recall` defaults to only-active, but auditing can see the history.

### 3.8 Observability

- `pm memory list [--scope X] [--type Y] [--importance N]` — browse.
- `pm memory show <id>` — read full entry.
- `pm memory edit <id>` — correct or refine.
- `pm memory forget <id>` — explicit delete.
- `pm memory stats` — counts by scope/type/importance.
- Cockpit panel (post-rail extensibility lands).

### 3.9 Plugin surface

`MemoryBackend` stays a plugin kind. Existing `FileMemoryBackend` evolves. Future backends:
- `SQLiteMemoryBackend` — one-file store, no separate markdown (simpler for some users).
- `VectorMemoryBackend` — v1.1, uses a vector store for semantic retrieval.

Write paths go through an `observer` chain so plugins can intercept (e.g. a "team sync" plugin mirrors memories to a shared store).

## 4. Phased roadmap

**Phase 1 — Schema + retrieval (the load-bearing changes)**

- **#M01** Typed memory schema — enum of types with per-type structure validation. Migration from today's free-form entries (default everyone to `project` type until manually re-categorized).
- **#M02** Recall API — `memory.recall(query, scope, types, limit, importance_min)`. Scoring = keyword + importance + recency. Replaces `summarize`/`list_entries` as the primary read path.
- **#M03** Tiered scope model — session / task / project / user. Session-scoped entries auto-expire; project and user are the durable tiers.

**Phase 2 — Write discipline + injection**

- **#M04** Extraction refactor — rework `knowledge_extract.py` into type-aware extractors. Distinct prompts per type produce distinct entries.
- **#M05** Context injection at session start — SessionService.create() calls memory.recall and packs top-N into the persona prompt under a structured section.

**Phase 3 — Curation + observability**

- **#M06** Curator handler — roster-registered daily job that dedupes, prunes by TTL, promotes episodic patterns.
- **#M07** `pm memory` CLI — list/show/edit/forget/stats with `--json`.
- **#M08** Supersession semantics — writers flag contradictions; superseded entries preserved for audit but not returned by default.

**Phase 4 — Semantic retrieval (v1.2+)**

- **#M09** Vector-embedding support — new `VectorMemoryBackend` plugin using a lightweight local store (e.g., chromadb or sqlite-vec).
- **#M10** Cross-project memory for operator — user-tier memories surface across all projects the operator works in.

## 5. What this buys us

- **Polly remembers you.** When a session boots, it sees "you're a senior engineer, you hate mocked tests, last week we decided on Playwright, the project is a shortlink generator using microservices." That's injected automatically, not manually.
- **Advisors get sharper.** Advisor persona already has access to `docs/project-plan.md`; adding the memory recall gives it the full "what has this user historically cared about?" context.
- **Learning compounds.** Patterns repeated across projects get promoted to procedural memory; future projects inherit.
- **Forgetting is explicit, not accidental.** Memory doesn't rot — old entries get consolidated or TTL'd on a schedule.

## 6. What stays out of scope

- Memory for agent self-reflection during a session (that's working memory / context window).
- Shared/team memory across multiple users (possible but not v1).
- Structured knowledge graphs (triples, ontologies) — interesting but heavy.

## 7. Migration risk

The existing memory directory has real content. The schema migration:
1. Adds new columns (`type`, `importance`, `superseded_by`, `ttl`) with defaults.
2. Existing entries get `type="project"` and `importance=3` as defaults.
3. Users re-categorize over time via the `pm memory edit` CLI.
4. Extractors produce new-shape entries going forward.

No data loss. Compatible with the current file layout.
