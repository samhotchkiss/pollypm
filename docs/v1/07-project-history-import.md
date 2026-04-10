---
## Summary

When PollyPM imports an existing project, it performs a deep chronological analysis of all prior work — JSONL transcripts, git commits, existing documentation — to reconstruct the full story of the project. The result is a structured set of project documents stored in `<project>/docs/` that give any future agent session comprehensive context about what was built, why, and how. These documents are committed to git by default — they are valuable shared knowledge. This is a one-time, token-intensive process that ends with a user interview to confirm and correct the generated understanding.

---

# 07. Project History Import

## Import Process

The import is a five-stage pipeline that transforms raw project artifacts into structured documentation. It is not a shallow scan or a snapshot — it reconstructs the chronological narrative of the project.

### Stage 1: Discover Sources

PollyPM scans the project for all available history sources:

| Source | What is found |
|--------|--------------|
| JSONL transcripts | All `.jsonl` files from two locations: (1) PollyPM's own standardized archive at `<project>/.pollypm/transcripts/` and (2) provider-specific directories (`.claude/`, `.codex/`, etc.) for initial import of pre-PollyPM history. Provider transcripts are copied into `.pollypm/transcripts/` during import so all future reads use the canonical location. |
| Git history | All commits, branches, tags, and their associated diffs |
| Existing documentation | README, CONTRIBUTING, architecture docs, inline comments, doc directories |
| Config files | `package.json`, `pyproject.toml`, `Cargo.toml`, Makefiles, CI configs — anything that reveals project structure and tooling |
| Test suites | Test files and their coverage, which reveal what the project considers important to verify |

### Stage 2: Build Timeline

All discovered sources are merged into a single chronological stream. Each entry in the timeline is timestamped and typed:

- Git commits with their diffs and messages
- JSONL transcript turns (user, assistant, tool calls) with their timestamps
- File creation and modification events inferred from git history
- Documentation changes tracked as discrete events

The timeline is the master data structure for the import. Everything downstream reads from it.

### Stage 3: Extract Understanding

PollyPM walks the timeline from earliest to latest and extracts structured understanding:

| Extraction target | What is captured |
|-------------------|-----------------|
| What was built and when | Feature timeline, component introduction dates, capability evolution |
| Decisions and rationale | Architectural choices, technology selections, tradeoff discussions (primarily from JSONL conversation context) |
| Goals stated or implied | Explicit goal statements from conversations, implied goals from patterns of work |
| Architecture emerged | System structure as it currently stands, inferred from code organization, imports, and explicit architecture discussions |
| Tried and abandoned | Approaches that were started and reverted, libraries that were added and removed, directions that were explored and dropped |
| Bugs found and fixed | Error patterns, debugging sessions, regression fixes |
| Patterns and conventions | Coding style, naming conventions, testing approaches, file organization patterns |

JSONL transcripts are the primary source for understanding intent and rationale. Git commits show what changed; transcripts show why.

### Stage 4: Generate Documentation

The extracted understanding is written into a structured set of project documents:

| Document | Content |
|----------|---------|
| `project-overview.md` | Vision, goals, current state, architecture summary, key conventions, pointers to detailed docs |
| `decisions.md` | Chronological record of key decisions with rationale, context, and alternatives considered |
| `architecture.md` | System design as it currently stands — components, boundaries, data flow, dependencies |
| `history.md` | Narrative of how the project evolved — what was built in what order and why |
| `conventions.md` | Coding patterns, naming conventions, testing approaches, file organization, and tooling preferences observed in the codebase |

Every generated document starts with a `## Summary` paragraph (2-5 sentences) so agents can scan quickly without reading the full document.

Documents are cross-referenced — `architecture.md` links to relevant decisions in `decisions.md`, `history.md` references the features described in `project-overview.md`, and so on.

Each document includes a "last updated" timestamp indicating when it was generated or last modified.

### Stage 5: User Interview

Before locking the documentation, PollyPM presents the draft understanding to the user for confirmation and correction:

- The generated `project-overview.md` is shown first as the high-level summary
- The user is asked to confirm, correct, or expand on key points
- Missing context that could not be inferred from artifacts is explicitly asked about (e.g., "I found no documentation of why X was chosen over Y — can you clarify?")
- Corrections are incorporated into the documents
- The user signs off on the final set

The user interview is required. The import does not lock documents without human confirmation.

### Stage 6: Lock into Docs

Finalized documents are written to `<project>/docs/` and become the project's living documentation. From this point forward, ongoing maintenance (doc 08) keeps them up to date.

**Committed by default.** Project documentation in `docs/` is committed to git. On project setup, PollyPM asks whether `docs/` should be committed; the default is yes.

**No secrets in docs/.** Documentation in `docs/` must NEVER contain secrets, credentials, API keys, or other sensitive information. These files are intended to be shared via version control. Sensitive operational details — deployment procedures, environment setup, test credentials — belong in `<project>/.pollypm/INSTRUCT.md`, which is gitignored and may contain sensitive implementation instructions.


## Source Handling Details

### JSONL Transcripts

JSONL transcript files contain the richest source of intent and decision-making context:

- **User turns** reveal goals, priorities, and corrections
- **Assistant turns** reveal reasoning, proposed approaches, and explanations
- **Tool calls** reveal what was actually done — files read, files written, commands executed
- Transcripts are parsed turn by turn, with each turn placed on the timeline at its timestamp
- Multi-session projects may have many transcript files spanning weeks or months

### Git Commits

Git history provides the objective record of what changed:

- Each commit is placed on the timeline with its diff, message, author, and timestamp
- File renames and deletions are tracked to understand refactoring
- Branch and merge patterns reveal the development workflow
- Commit messages provide terse but useful context about intent

### Existing Documentation

Pre-existing documentation is incorporated rather than replaced:

- README files contribute to project-overview.md
- Architecture docs contribute to architecture.md
- CONTRIBUTING guides contribute to conventions.md
- Inline code comments are scanned for rationale and decision context


## Token Budget

The import is token-intensive by design. Walking the full timeline of a mature project may consume significant tokens. This is acceptable because:

1. The import runs once per project
2. The output (structured docs/) saves far more tokens over the project's lifetime by giving agents precise context instead of requiring them to rediscover it
3. The alternative — shallow scanning — produces documentation that misses rationale, decisions, and evolution, which are the most valuable parts


## Output Format

The output format is designed for both human and agent consumption:

- Every document starts with `## Summary` (2-5 sentences) — this is the agent-scannable entry point
- Documents use markdown with consistent heading structure
- Cross-references use relative links between docs/ documents
- Each document has a "last updated" timestamp
- The `project-overview.md` is the primary injection document — it is the first thing a new agent session reads (doc 08)


## Resolved Decisions

1. **Chronological reconstruction, not snapshot.** The import walks the full history rather than analyzing only the current state. This captures rationale, evolution, and abandoned approaches that a snapshot misses.

2. **JSONL transcripts are the primary source.** Git shows what changed; transcripts show why. When both are available, transcripts provide the richer signal for understanding intent and decisions.

3. **User interview required before locking.** Automated analysis will miss context and may misinterpret intent. The user interview catches errors and fills gaps before documentation becomes the project's institutional memory.

4. **One-time intensive import is acceptable.** The import is expensive in tokens but runs once. The ongoing savings from having precise project context in every future session far outweigh the one-time cost.

5. **Output format matches ongoing doc maintenance format.** The documents generated by import use the same structure, conventions, and summary-first pattern as documents maintained by ongoing updates (doc 08). There is no format migration between import and maintenance.


## Cross-Doc References

- Ongoing documentation maintenance after import: [08-project-state-memory-and-documentation.md](08-project-state-memory-and-documentation.md)
- Plugin system for import source adapters: [04-extensibility-and-plugin-system.md](04-extensibility-and-plugin-system.md)
- Project overview injection into sessions: [08-project-state-memory-and-documentation.md](08-project-state-memory-and-documentation.md)
