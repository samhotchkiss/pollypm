---
## Summary

After import (doc 07), PollyPM maintains project documentation on an ongoing basis through asynchronous JSONL transcript analysis — the project agent is never burdened with documentation duties. The documentation system is a pluggable backend, not hard-coded core behavior; the default plugin writes markdown files to `<project>/docs/`, but users can replace it with Notion, wiki, or custom backends. Project documentation in `docs/` is committed to git by default — it is valuable shared knowledge. Docs must NEVER contain secrets; sensitive operational details go in `<project>/.pollypm/INSTRUCT.md` (gitignored). All documents follow a summary-first pattern for token-efficient context injection, and the project overview is injected into every new worker session. Memory is organized into five hierarchical scopes — global, project, issue, session, and inbox thread — with a standardized backend interface for storage and retrieval. Transcripts are read from `<project>/.pollypm/transcripts/`, PollyPM's own standardized archive, which is continuously written by PollyPM during active sessions.

---

# 08. Project State, Memory, and Documentation

## Ongoing Documentation Maintenance

After the initial import (doc 07) produces the project documentation in `docs/`, PollyPM keeps it current through continuous, asynchronous ingestion of new project activity.

### Two-Stage Pipeline

Ongoing documentation maintenance uses a two-stage pipeline that separates mechanical ingestion from LLM-powered extraction.

#### Stage 1: Transcript Ingestion (no LLM, background thread)

This stage runs continuously in the PollyPM process as a background thread. It is described in detail in [05-provider-sdk.md](05-provider-sdk.md).

- Tails provider JSONL files for all active sessions, normalizes events into PollyPM's standardized JSONL format, and writes them to `<project>/.pollypm/transcripts/<session-id>/`.
- Also captures Polly operator conversations about each project, so that decisions and context from the operator chat are part of the transcript record.
- Handles file rotation (new files appearing, old files renamed by the provider) by re-evaluating glob patterns.
- This stage involves no LLM — it is pure file tailing and format normalization, and is free in terms of inference cost.

#### Stage 2: Knowledge Extraction (LLM, scheduled)

This is the documentation plugin's core job. It reads the transcript archive produced by Stage 1 and extracts structured knowledge into the project's `docs/` directory.

1. New transcript entries since the last extraction checkpoint are read from `.pollypm/transcripts/`.
2. Entries are batched and fed to a cheap model (Haiku or similar) for analysis.
3. The model extracts: decisions, architecture changes, convention shifts, goal changes, risks, and ideas.
4. Affected documents in `docs/` are identified.
5. Delta-based updates are applied — only changed sections are rewritten, preserving the rest of the document.
6. The `## Summary` paragraph of each updated document is regenerated to reflect the new state.
7. The extraction checkpoint is advanced.

Stage 2 runs on a schedule (e.g., every 15 minutes, or triggered by the scheduler plugin) — NOT per-event. This keeps extraction economical by batching transcript entries rather than reacting to each individual event.

**Key point:** The documentation plugin is an LLM-powered extraction pipeline, not just a file writer. It uses a cheap model to understand what happened in transcripts and update project documentation accordingly.

### What Triggers Updates

| Signal | Affected Documents |
|--------|-------------------|
| New architectural decision discussed in transcript | `decisions.md`, `architecture.md` |
| Coding pattern established or changed | `conventions.md` |
| Project goal stated, changed, or completed | `project-overview.md` |
| New risk identified or existing risk resolved | `risks.md` |
| Significant feature completed | `project-overview.md`, `history.md` |
| Idea discussed but not acted on | `ideas.md` |

### Key Constraint

The project agent is never asked to write or maintain documentation. Documentation maintenance is entirely a background concern handled by PollyPM infrastructure. Worker sessions focus exclusively on implementation and review work.


## Document Structure

All project documents live in `<project>/docs/` (when using the default documentation plugin) and follow a consistent structure. These documents are committed to git by default. On project setup, PollyPM asks the user whether `docs/` should be committed; the default is yes.

**No secrets in docs/.** Documentation in `docs/` must NEVER contain secrets, credentials, API keys, or other sensitive information. These files are shared via version control. Sensitive operational details — how to deploy, test environment setup, credentials references — belong in `<project>/.pollypm/INSTRUCT.md`, which is gitignored and may contain sensitive implementation instructions.

### Core Documents

| Document | Purpose | Update frequency |
|----------|---------|-----------------|
| `project-overview.md` | Vision, goals, current state, architecture summary, conventions, pointers to detailed docs | Updated whenever goals, state, or architecture change |
| `decisions.md` | Append-only chronological decision log with rationale | Appended when new decisions are detected |
| `architecture.md` | Living architecture description — components, boundaries, data flow | Updated when architecture changes |
| `conventions.md` | Coding standards, patterns, naming, testing approach | Updated when conventions evolve |
| `history.md` | Project evolution narrative | Appended as significant work is completed |
| `risks.md` | Active risks, drift concerns, open questions | Updated as risks emerge or resolve |
| `ideas.md` | Captured ideas not ready for action | Appended when ideas are discussed but deferred |

### Update Semantics

- `decisions.md` is append-only — decisions are never rewritten, only added
- `history.md` is append-only — new narrative entries are added chronologically
- `ideas.md` is append-only — ideas are added; acted-on ideas are marked as promoted to issues
- All other documents are delta-updated — sections are rewritten when their content changes
- Every document maintains a "last updated" timestamp


## Summary-First Pattern

Every document in `docs/` starts with a `## Summary` section containing 2-5 sentences that capture the essential content.

### Purpose

The summary-first pattern exists for token efficiency:

- Agents read ONLY summaries to get orientation when starting a session
- Agents read the full document only when they need specific details for the task at hand
- This keeps context injection small — injecting all summaries costs a fraction of injecting all documents

### Rules

1. The `## Summary` section is always the first section after the frontmatter separator
2. Summaries are 2-5 sentences — enough to orient, short enough to inject cheaply
3. Summaries are regenerated every time the document is updated
4. Summaries never reference other documents — they are self-contained


## Project Overview Injection

When a new worker session launches, `project-overview.md` is injected as part of the system prompt or initial context.

### What the Overview Contains

The project overview tells the agent:

- What this project is (vision and purpose)
- What matters right now (current priorities and goals)
- What conventions to follow (key coding standards and patterns)
- Where to find detailed docs (pointers to specific documents in `docs/`)
- What the current state is (what's built, what's in progress, what's planned)

### Directed Consultation

The injection includes instructions for the agent to consult specific documents when performing specific types of work:

| Task type | Consult |
|-----------|---------|
| Writing new code | `docs/conventions.md` for patterns and standards |
| Making architectural choices | `docs/decisions.md` for prior decisions and rationale |
| Understanding system structure | `docs/architecture.md` for component boundaries and data flow |
| Understanding project history | `docs/history.md` for evolution narrative |
| Assessing risk | `docs/risks.md` for active risks and open questions |
| Deployment or env setup | `.pollypm/INSTRUCT.md` for sensitive operational details |

The agent is not expected to read all documents — it reads only what is relevant to its current task.


## Memory Scopes

PollyPM organizes memory into five hierarchical scopes, from broadest to narrowest.

### Scope Definitions

| Scope | Lifetime | Content |
|-------|----------|---------|
| Global | Cross-project, persistent | User preferences, cross-project knowledge, tool configurations, learned patterns that apply everywhere |
| Project | Per-project, persistent | Documentation in `docs/`, project-specific conventions, architecture, decisions, history |
| Issue | Per-issue, persistent until issue completes | Issue-specific context, decisions made during implementation, review feedback, approach notes |
| Session | Per-session, ephemeral | Working state, in-progress reasoning, temporary notes, session-local context that does not survive restart |
| Inbox thread | Per-conversation, ephemeral | Conversation-specific context from the inbox/chat interface, tied to a specific human interaction |

### Scope Hierarchy

Scopes are hierarchical — narrower scopes inherit and can reference broader scopes:

- A session has access to its issue scope, which has access to the project scope, which has access to the global scope
- Writing to a scope affects only that scope — there is no automatic propagation upward
- The documentation maintenance process is what promotes important session-level and issue-level information to the project scope (via `docs/` updates)


## Memory Backend Interface

The memory system exposes a standardized interface (from doc 04) that any storage backend can implement.

### Required Methods

| Method | Signature | Purpose |
|--------|-----------|---------|
| `remember` | `remember(scope: str, item: MemoryItem) -> str` | Store an item in the given scope, return its ID |
| `recall` | `recall(scope: str, query: str, limit: int) -> list[MemoryItem]` | Retrieve items matching a query from the given scope |
| `summarize` | `summarize(scope: str) -> str` | Generate a summary of everything in the scope |
| `compact` | `compact(scope: str) -> None` | Compress and deduplicate items in the scope to save space |
| `delete` | `delete(scope: str, id: str) -> None` | Remove a specific item from the scope |

### Default Backend

The default memory backend is a file + SQLite hybrid:

- **File storage** for `docs/` documents and large text content
- **SQLite** for indexed metadata, scope tracking, timestamps, and queries
- This requires no external services and works immediately on any system

### Future Backends

The plugin interface (doc 04) allows alternative memory backends:

- Vector store for semantic search over memory items
- Hosted memory service for team-shared project memory
- Custom backends for domain-specific storage requirements


## Documentation Backend Plugin Interface

The entire documentation system is a plugin, not core. Core provides the API — transcript access via `.pollypm/transcripts/`, state store, and injection hooks — and the documentation plugin consumes these to produce and maintain project documentation in `docs/`.

### Required Methods

| Method | Signature | Purpose |
|--------|-----------|---------|
| `write_document` | `write_document(doc_type: str, content: str, metadata: dict) -> str` | Write or update a document in `docs/`, return its identifier |
| `read_document` | `read_document(doc_type: str) -> Document` | Retrieve a document by type |
| `read_summary` | `read_summary(doc_type: str) -> str` | Retrieve only the summary of a document (for cheap injection) |
| `list_documents` | `list_documents() -> list[Document]` | List all documents in `docs/` |
| `append_entry` | `append_entry(doc_type: str, entry: str) -> None` | Append an entry to an append-only document (decisions, history, ideas) |
| `get_injection_context` | `get_injection_context() -> str` | Return the context string to inject into new worker sessions |

### Default Plugin

The default documentation plugin writes markdown files to `<project>/docs/`. It requires no external services and works immediately.

### Alternative Backends

Users can replace the default with any backend that implements the interface above:

- Notion database backend
- Wiki (Confluence, MediaWiki, etc.)
- Custom CMS or knowledge base
- In-memory backend for testing

### Override Hierarchy

Documentation backend selection follows the standard override hierarchy:

1. **Built-in defaults** — markdown-to-`docs/` ships as the baseline
2. **User-global config** (`~/.pollypm/config/`) — user can set a preferred documentation backend for all new projects
3. **Project-local config** (`<project>/.pollypm/config/`) — each project independently chooses its documentation backend, overriding the global default


## Resolved Decisions

1. **Async documentation, not agent-burdened.** Worker agents never write or maintain documentation. Documentation is maintained by a background process that analyzes transcripts after the fact. This keeps worker sessions focused on implementation.

2. **JSONL from PollyPM's own archive is the source.** Transcript files in `.pollypm/transcripts/` are the primary input for ongoing documentation maintenance. PollyPM owns its own transcripts — each session writes to `.pollypm/transcripts/<session-id>/`, and the ingestion process reads exclusively from this canonical location. This is the richest signal for detecting decisions, convention changes, and goal shifts.

7. **Documentation is a pluggable backend.** The documentation system is a plugin, not core. Core provides transcript access, state store, and injection hooks. The default plugin writes markdown to `docs/`. Users can replace it with Notion, wiki, or custom backends.

8. **Project-local config for all pluggable behavior.** Documentation backend, memory backend, and other pluggable selections live in `<project>/.pollypm/config/`. Override hierarchy: built-in defaults -> user-global -> project-local.

9. **Multi-session transcripts.** Transcripts are per-session (`<session-id>/`), not per-project. This supports concurrent sessions and preserves session identity for analysis.

10. **Two-stage pipeline separates ingestion from extraction.** Ingestion is mechanical and free (no LLM). Extraction uses a cheap model and runs on a schedule. This keeps ingestion fast and extraction economical.

3. **Summary-first pattern for all docs.** Every document in `docs/` starts with a 2-5 sentence summary. This enables cheap orientation — agents can read all summaries for a fraction of the token cost of reading all documents.

4. **project-overview.md is the injection document.** One document is injected into every session. It provides orientation and pointers. Agents read detailed documents on demand, not by default.

5. **Delta-based updates, not full rewrites.** Only changed sections of documents are rewritten. This preserves document stability, reduces token cost of updates, and makes diffs meaningful.

6. **Memory scopes are hierarchical.** Five scopes from global to inbox thread, each with clear lifetime and content semantics. Narrower scopes inherit access to broader scopes but do not propagate changes upward automatically.


## Cross-Doc References

- Initial project import that produces project docs: [07-project-history-import.md](07-project-history-import.md)
- Plugin system for memory backends: [04-extensibility-and-plugin-system.md](04-extensibility-and-plugin-system.md)
- Session launch and context injection: [03-session-management-and-tmux.md](03-session-management-and-tmux.md)
- Architecture and system roles: [01-architecture-and-domain.md](01-architecture-and-domain.md)
