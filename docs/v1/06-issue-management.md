---
## Summary

PollyPM v1 ships two opinionated-default issue management backends — a file-based tracker (default for local projects) and a GitHub-based tracker — but the user can replace either with a custom backend through a standardized interface. All backends share the same logical state machine, the same PA/PM role split, and the same reporting interface, so the rest of the system never needs to know which backend is active. Issue backend selection is project-local, stored in `<project>/.pollypm/config/`.

---

# 06. Issue Management

## Logical State Machine

Every issue backend implements the same six-state pipeline. The states are ordered and directional — issues move forward through the pipeline, with the exception of review rejection which sends an issue back to in-progress.

| State | Meaning |
|-------|---------|
| not-ready | Issue exists but is not actionable — missing acceptance criteria, blocked, or needs refinement |
| ready | Issue is fully specified and available for a PA to pick up |
| in-progress | A PA is actively working on the issue |
| needs-review | PA has finished implementation and is requesting PM review |
| in-review | PM is actively reviewing the issue |
| completed | PM has approved the work and the issue is done |

The PA picks the next issue from `ready`, moves it to `in-progress`, implements it, and moves it to `needs-review`. The PM moves it to `in-review`, reviews the work, and either requests changes (moving it back to `in-progress`) or approves it (moving it to `completed`). This split is universal across all backends.

Issues should be small, testable, and independently shippable. A good issue can be implemented and reviewed in a single session.


## Track 1 — File-Based Issue Tracker

The file-based tracker is the default for local projects. It uses the filesystem as its state store, with folder names encoding issue state.

### Folder Structure

Issues live under `<project>/issues/` with one subdirectory per state:

```
<project>/issues/
  00-not-ready/
  01-ready/
  02-in-progress/
  03-needs-review/
  04-in-review/
  05-completed/
```

Each issue is a single markdown file named `<number>-<slug>.md`. Moving an issue between states means moving the file between directories.

### ID Assignment

The file `.latest_issue_number` at the root of the issues directory is the canonical source for monotonic issue IDs. When a new issue is created, the number in this file is incremented and the new value becomes the issue ID. This file must be treated as an atomic counter — concurrent access is not expected (one active worker per project), but the file is the single source of truth.

### Supporting Files

Each project's issues directory may contain:

| File | Purpose |
|------|---------|
| `instructions.md` | Standing instructions for PAs working issues in this project |
| `notes.md` | Persistent notes about the issue backlog, priorities, or context |
| `progress-log.md` | Append-only log of issue state transitions with timestamps |

### Workflow

1. PM (or human) creates an issue in `00-not-ready/` or `01-ready/`
2. PA calls `next_available()`, which returns the lowest-numbered issue in `01-ready/`
3. PA moves the issue file to `02-in-progress/` and begins work
4. PA completes implementation, moves the issue file to `03-needs-review/`
5. PM picks up the issue, moves it to `04-in-review/`, reviews the work
6. PM either moves the issue back to `02-in-progress/` with review comments appended, or moves it to `05-completed/` and merges the branch


## Track 2 — GitHub-Based Issue Tracker

The GitHub-based tracker uses GitHub Issues as its state store and the `gh` CLI for all operations.

### Label-Based State

GitHub Issues uses labels to represent the same logical states. Each issue carries exactly one state label at a time:

| Label | Maps to State |
|-------|--------------|
| `polly:not-ready` | not-ready |
| `polly:ready` | ready |
| `polly:in-progress` | in-progress |
| `polly:needs-review` | needs-review |
| `polly:in-review` | in-review |
| `polly:completed` | completed |

Moving an issue between states means removing the old state label and adding the new one. PollyPM manages this through `gh issue edit` commands.

### State via Labels, Not Projects

State is tracked through labels, not GitHub Projects board columns. A GitHub Projects board can be configured to visualize the pipeline by grouping on state labels, but the board is a view — labels are the source of truth.

### Workflow

1. PM (or human) creates a GitHub Issue with the `polly:not-ready` or `polly:ready` label
2. PA calls `next_available()`, which queries for the oldest issue with the `polly:ready` label
3. PA relabels the issue to `polly:in-progress` and begins work
4. PA completes implementation, relabels to `polly:needs-review`, and adds a comment with handoff notes
5. PM relabels to `polly:in-review`, reviews the work
6. PM either relabels back to `polly:in-progress` with a review comment, or relabels to `polly:completed` and closes the issue

### Comments as Handoff Mechanism

GitHub Issue comments serve as the communication channel between PA and PM:

- PA adds a comment when moving to `needs-review` explaining what was done and how to test
- PM adds a comment when requesting changes explaining what needs to change
- PM adds a comment when completing review confirming approval
- All comments are retrievable through `get_issue()` as part of the issue history

Closing an issue is equivalent to moving it to the `completed` state.


## Plugin Interface

All issue management backends implement the same interface. This is the contract that the rest of PollyPM depends on — the system never calls backend-specific methods.

### Required Methods

| Method | Signature | Returns |
|--------|-----------|---------|
| `list_issues` | `list_issues(state_filter: str \| None) -> list[Issue]` | All issues, optionally filtered by state |
| `create_issue` | `create_issue(title: str, body: str, metadata: dict) -> Issue` | Newly created issue |
| `move_issue` | `move_issue(id: str, target_state: str) -> Issue` | Issue after state transition |
| `get_issue` | `get_issue(id: str) -> Issue` | Issue with full history, notes, and state transitions |
| `append_note` | `append_note(id: str, note: str) -> Issue` | Issue with note appended |
| `next_available` | `next_available() -> Issue \| None` | Next issue in ready state (lowest ID / oldest), or None |
| `report_status` | `report_status() -> StatusReport` | Summary of all states with issue counts |

### Key Requirements

Every backend must be able to answer two fundamental questions:

1. **"What needs to be done next?"** — answered by `next_available()`
2. **"What's the current state?"** — answered by `report_status()`

These two methods are the minimum viable interface for PollyPM's operator and heartbeat sessions to function. A backend that implements only these two methods (plus `move_issue` for state transitions) is sufficient for basic operation.

### Custom Backends

Users can write their own issue management plugins by implementing this interface. Any backend that implements the required methods can be registered through the plugin system (doc 04). Custom issue backends must pass automated validation before activation — PollyPM runs the backend through a standard test suite (create, transition, query, report) and only enables it if all checks pass. Examples of custom backends that users might build:

- Linear issue tracker integration
- Jira adapter
- Notion database backend
- Plain SQLite tracker
- Trello board adapter

The plugin system handles discovery and registration — the issue management interface handles the contract.

### Override Hierarchy

Issue backend selection follows the standard override hierarchy:

1. **Built-in defaults** — file-based tracker ships as the baseline
2. **User-global config** (`~/.pollypm/config/`) — user can set a preferred backend for all new projects
3. **Project-local config** (`<project>/.pollypm/config/`) — each project independently chooses its issue backend, overriding the global default

### Agent-Driven Configuration

If a user expresses dissatisfaction with how issues work (e.g., "I don't like how issues work"), the agent should offer to change the issue backend or patch the project-local rules. The agent can list available backends, explain tradeoffs, and apply the configuration change on the user's behalf — issue management is pluggable precisely so that the user never has to live with a workflow that does not fit.


## Resolved Decisions

1. **Opinionated but pluggable.** File-based and GitHub-based are opinionated defaults covering the two most common development contexts (local solo and GitHub-hosted), but the user can replace them. Additional backends are left to the plugin system rather than shipping more built-in options.

2. **Same logical states across all backends.** The six-state pipeline is universal. Every backend maps to the same states, so the operator, heartbeat, and reporting layers never need backend-specific logic.

3. **Standardized reporting interface.** `report_status()` and `next_available()` are the minimum contract. Any backend that answers "what's next?" and "what's the state?" integrates with PollyPM.

4. **PA/PM role split is universal.** Regardless of backend, the PA works issues and the PM reviews. This split is enforced by the state machine, not by the backend implementation.

5. **File-based is the default.** When no backend is configured, PollyPM uses the file-based tracker. This requires no external services and works immediately. The backend can be changed at any time via project-local config.

7. **Project-local config.** Issue backend selection lives in `<project>/.pollypm/config/`, not in a global config file. Each project independently chooses its backend.

8. **Plugin validation required.** Custom issue backends must pass automated validation (create, transition, query, report) before activation. This prevents broken backends from corrupting project state.

9. **Agent-driven reconfiguration.** When a user expresses dissatisfaction with issue workflow, the agent actively offers to switch backends or adjust rules rather than requiring manual configuration.

6. **GitHub track uses labels, not Projects, for state.** Labels are the source of truth because they are directly queryable via `gh`, require no additional GitHub configuration, and are simpler to manage programmatically. Projects boards are an optional visualization layer.


## Cross-Doc References

- Plugin system for backend registration: [04-extensibility-and-plugin-system.md](04-extensibility-and-plugin-system.md)
- PA and PM role definitions: [01-architecture-and-domain.md](01-architecture-and-domain.md)
- Session lifecycle for worker and operator sessions: [03-session-management-and-tmux.md](03-session-management-and-tmux.md)
