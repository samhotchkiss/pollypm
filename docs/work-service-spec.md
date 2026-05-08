# Work Service Specification

**Status**: Draft
**Date**: 2026-04-14
**Supersedes**: `docs/v1/06-issue-management.md` (file-based and GitHub issue backends)
**Inspired by**: OtterCamp v2 task flows (`docsv2/03-projects-and-task-flow.md`), [gastownhall/beads](https://github.com/gastownhall/beads)

---

## 1. Problem Statement

PollyPM's current architecture conflates two fundamentally different concerns:

1. **Messaging** (informational) — conversations between agents, surfacing status to the user
2. **Work governance** (operational) — enforcing process, validating transitions, ensuring quality

The inbox system carries both. Quality gates are injected into message replies. Process enforcement happens through tier classification of messages. Agent coordination depends on message delivery state. This makes the messaging system load-bearing for governance, when messaging is fundamentally an informational primitive.

Additionally, the current file-based task backend has race conditions in multi-agent scenarios:

- `create_task`: read-modify-write race on `.latest_issue_number` and directory scans
- `move_task`: concurrent `shutil.move` on shared files
- `append_note` / `_record_transition`: concurrent read-then-write on shared files like `progress-log.md`

These races exist because agents directly read and write the filesystem. There is no serialization.

## 2. Design Principles

1. **Work is the organizing primitive, not messages.** Communication about work is context attached to work, not a separate system to correlate.

2. **Governance lives on transitions, not in conversations.** State changes are validated operations with explicit gates. Process enforcement is code, not text injected into messages.

3. **One writer, many readers.** The work service is the single process that mutates work state. Agents interact through a defined API. No agent touches the underlying storage directly.

4. **The inbox remains, scoped to its proper role.** The inbox is the communication channel between Polly and the human operator. It surfaces decisions, escalations, and status. It does not carry agent-to-agent coordination or process enforcement.

5. **Pluggable at every layer.** The work service itself is a replaceable plugin implementing a known protocol. Storage backends, flow definitions, gate functions, and sync adapters are all swappable.

6. **The override chain applies.** Flows, gates, and configuration follow the same precedence model as the rest of PollyPM: built-in defaults < user-global (`~/.pollypm/`) < project-local (`<project>/.pollypm/`).

7. **Existing issue systems are downstream projections.** File-based issues and GitHub issues continue to exist but are synced from the work service, not the other way around (with the exception of two-way GitHub sync for inbound changes).

8. **Sealed and testable.** The work service has a strict API boundary. Every operation is a pure function of its inputs plus current state. The entire system is testable without a filesystem, tmux, or any infrastructure.

## 3. Architecture

```
                              ┌──────────┐
                              │   User   │
                              │  (human) │
                              └────┬─────┘
                                   │ reads / responds
                              ┌────▼─────┐
                              │  Inbox   │
                              │ (user ↔  │
                              │  polly)  │
                              └────┬─────┘
                                   │ polly surfaces decisions,
                                   │ escalations, status
                              ┌────▼──────────────────┐
                              │    Polly (operator)    │
                              └────┬──────────────────┘
                                   │ manages work via API
                              ┌────▼──────────────────┐
                              │    Work Service        │
                              │  (sealed, one writer)  │
                              │                        │
                              │  ┌──────────────────┐  │
                              │  │ Flow Engine       │  │
                              │  │ (validates all    │  │
                              │  │  transitions)     │  │
                              │  └──────────────────┘  │
                              │  ┌──────────────────┐  │
                              │  │ Storage Backend   │  │
                              │  │ (swappable)       │  │
                              │  └──────────────────┘  │
                              └────┬──────────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
                Workers      File Issues    GitHub Issues
            (claim/advance    (sync out)     (sync out)
               via API)
```

### Relationship to the Five-Layer Architecture

The work service is a **Layer 1 (Core)** component. It provides durable domain logic for work management. The `pm task` CLI commands are a Layer 4 frontend — thin adapters that translate CLI input into work service API calls. Flow definitions, gate functions, and sync adapters are Layer 5 plugins.

The work service exposes its operations through the Layer 3 Service API, meaning future transports (MCP, HTTP, Discord) get work management for free.

### Process Boundary

V1 ships the work service as an in-process library. The `pm` CLI and other
local callers obtain a `SQLiteWorkService` against the shared `state.db`
through the `pollypm.work.create_work_service` factory; there is no
separate daemon or Unix socket in the current build.

```python
from pollypm.work import create_work_service

with create_work_service(project_path=project.path) as svc:
    ...
```

Direct construction of `SQLiteWorkService` from outside the `pollypm.work`
package is discouraged: it forces every callsite to know the on-disk
layout (workspace vs. per-project paths, dual-DB confusion, etc.) and
bypasses future resolver enhancements (env overrides, fallbacks,
telemetry). The factory resolves the canonical DB path via
`pollypm.work.db_resolver.resolve_work_db_path` and yields a
context-managed service so the SQLite handle closes deterministically.

Callers that genuinely need a non-canonical path (legacy migration,
tests, explicit `--db` overrides) may pass `db_path=...` explicitly so the
deviation is visible at the callsite.

### Audit Hooks

The current work-service implementation emits audit-log events for task
creation, status transitions, task deletion/pruning, and every SQLite work DB
open. The `work_db.opened` event is the forensic breadcrumb for the workspace
vs. messages-side DB confusion: its metadata records whether the opened file
already had a `messages` table and whether work tables had to be created.

See [Audit Log](audit-log.md) for the JSONL schema, on-disk paths, watchdog
rules that consume these events, and the contributor contract for adding new
event types.

- **Startup**: no dedicated service bootstrap. Each caller resolves config and
  builds the service object when needed.
- **Failure mode**: a caller-side failure aborts only that operation. The next
  CLI command or supervisor action can reconstruct the service cleanly.
- **Future transport**: the API boundary is intentionally transport-agnostic,
  so a Unix-socket daemon can still be introduced later without redesigning
  flows, gates, or storage.

SQLite WAL mode handles concurrent reads while writes stay serialized through
the work-service boundary.

## 4. Task Schema

A task is the atomic unit of work tracked by the service.

### Identity

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique identifier. Format TBD — see Open Questions. |
| `title` | string | yes | Human-readable summary |
| `type` | enum | yes | `epic`, `task`, `subtask`, `bug`, `spike` |
| `project` | string | yes | Project key this task belongs to |
| `labels` | list[string] | no | Freeform tags |

### State

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `work_status` | enum | yes | `draft`, `queued`, `in_progress`, `blocked`, `on_hold`, `review`, `done`, `cancelled` |
| `flow_template_id` | string | yes | The flow template governing this task's lifecycle |
| `current_node_id` | string? | no | The flow node currently active (null when in `draft` or `queued`) |
| `owner` | string | derived | Who owes the next action — computed from current node's actor + roles |
| `assignee` | string | no | Who is doing the work (stable across state changes) |
| `priority` | enum | yes | `critical`, `high`, `normal`, `low` |
| `blocked` | bool | derived | True if any `blocked_by` task is not in a terminal state |
| `requires_human_review` | bool | no | If true, task cannot move from `draft` to `queued` without human sign-off via inbox |

#### Work Status Definitions

- `draft`: Idea captured but not yet scoped. Not available for agents. Polly scopes it with description, acceptance criteria, flow template.
- `queued`: Planned and ready for pickup. Has sufficient context for execution.
- `in_progress`: An agent is actively working at a work node in the flow.
- `blocked`: Waiting on a dependency (another task) to resolve.
- `on_hold`: Paused intentionally — not blocked, just not being worked on.
- `review`: Task is at a review node in its flow, awaiting reviewer decision.
- `done`: Completed and approved. Terminal.
- `cancelled`: Abandoned. Terminal.

#### Valid Transitions

```
draft ──→ queued ──→ in_progress ──→ review ──→ done
            │         ↑    │          │
            │         │    │          │ (rejected — back to work)
            │         │    ▼          ▼
            │         │  blocked    in_progress
            │         │    │
            │         │    ▼ (dependency resolved)
            │         └── queued
            │
            ├── on_hold ◄── in_progress
            │     │
            │     ├── queued (resumed — was queued)
            │     │
            │     └── in_progress (resumed mid-work — was in_progress)
            │
            └── on_hold (queued → on_hold)

Any non-terminal state ──→ cancelled
```

#### Hold / Resume Semantics

`hold()` can pause a task from either `queued` or `in_progress`. `resume()` returns the task to the state it was in before the hold:

- A task held from `queued` resumes to `queued`.
- A task held from `in_progress` resumes to `in_progress`. Its `current_node_id` and the associated `FlowNodeExecution` record survive the hold — the worker picks up at the same node on the same visit. This preserves mid-work progress across intentional pauses (e.g., waiting out a dependency that isn't formally blocking, deferring to higher priority work).

The resume path is determined by the active flow node: if a node is active, resume transitions to `in_progress`; otherwise it transitions to `queued`.

### Content

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `description` | text | yes | What needs to be done and why — see Task Context Model below |
| `acceptance_criteria` | text | no | Specific, testable conditions for "done" |
| `constraints` | text | no | What the worker must NOT do, boundaries, limitations |
| `relevant_files` | list[string] | no | Explicit file paths or patterns the worker should touch or reference |
| `context` | list[ContextEntry] | no | Append-only log of decisions, conversation, and observations accumulated during the task's life |

A `ContextEntry`:

| Field | Type | Description |
|-------|------|-------------|
| `actor` | string | Who added this entry (agent name, "user", "system") |
| `timestamp` | datetime | When the entry was added |
| `text` | string | The content |

#### Task Context Model

Context flows into a task at two levels:

**Task-level context (stored in the work service, written by the creator):**

The agent creating the task (usually Polly) front-loads everything the worker needs to execute without guessing:

- **What** needs to be done — specific, actionable description
- **Why** it needs to be done — motivation, what problem it solves, user request that spawned it
- **Where** — explicit file paths, modules, or areas that need to change
- **Acceptance criteria** — how to know it's done
- **Constraints** — what not to do, API boundaries, backward compatibility requirements

A task is not ready to leave `draft` until it has sufficient context for a worker to execute. Polly is responsible for this — she doesn't queue half-baked tasks.

**Project-level context (external to the work service, referenced at claim time):**

When a worker is assigned a task, the supervisor/operator assembles a prompt that includes:

1. The task's full description, acceptance criteria, constraints, and relevant files (from the work service)
2. Pointers to project-level docs: project summary, architecture, conventions (these live in the project repo, not in the work service)
3. The task's context log (accumulated conversation and decisions)

The work service does not own or manage project-level docs. It stores task-specific context. The supervisor knows where the project docs live and is responsible for including them in the worker's prompt. This keeps the work service sealed — it doesn't need to know about project directory structures or documentation conventions.

### Relationships

| Field | Type | Description |
|-------|------|-------------|
| `parent` | id? | The epic or task this is a child of |
| `children` | list[id] | Derived: inverse of `parent` |
| `blocks` | list[id] | Tasks that cannot proceed until this task is done |
| `blocked_by` | list[id] | Tasks that must complete before this task can proceed |
| `relates_to` | list[id] | Associative links (informational, no enforcement) |
| `supersedes` | id? | This task replaces an older one |
| `superseded_by` | id? | Derived: inverse of `supersedes` |

### Roles

| Field | Type | Description |
|-------|------|-------------|
| `roles` | map[string, string] | Maps role names (defined by the flow) to agent/user names |

Example: `{"worker": "pete", "reviewer": "polly", "requester": "user"}`

### Sync

| Field | Type | Description |
|-------|------|-------------|
| `external_refs` | map[string, string] | Maps sync adapter names to external IDs |

Example: `{"github": "myorg/myrepo#42", "file": "issues/01-ready/0042-fix-auth.md"}`

### Audit

| Field | Type | Description |
|-------|------|-------------|
| `created_at` | datetime | When the task was created |
| `created_by` | string | Who created it |
| `updated_at` | datetime | Last modification time |
| `transitions` | list[Transition] | Full transition history |

A `Transition`:

| Field | Type | Description |
|-------|------|-------------|
| `from_state` | string | Previous state |
| `to_state` | string | New state |
| `actor` | string | Who triggered the transition |
| `timestamp` | datetime | When it happened |
| `reason` | string? | Optional explanation (required for rejections) |

## 5. API Surface

The work service exposes these operations. In the current build, all mutations
are intended to flow through the in-process `SQLiteWorkService` boundary. All
operations return structured results (not raw file contents).

**Current implementation note (2026-04-26):** `WorkService`,
`SQLiteWorkService`, and `MockWorkService` all expose the shared parameters
callers rely on, including `created_by`, `skip_gates`, and `entry_type`.

### Task Lifecycle

| Operation | Parameters | Returns | Description |
|-----------|-----------|---------|-------------|
| `create` | title, description, type, project, flow_template, roles, priority, acceptance_criteria?, constraints?, relevant_files?, labels?, requires_human_review?, created_by? | Task | Create a task in `draft` state. Validates that all required roles for the flow are filled. |
| `get` | task_id | Task | Read a task with all fields including current flow node and execution state. |
| `list_tasks` | work_status?, owner?, project?, assignee?, blocked?, type?, limit?, offset? | list[Task] | Query tasks with filters. |
| `queue` | task_id, actor, skip_gates? | Task | Move from `draft` to `queued`. If `requires_human_review`, validates human has approved via inbox unless gates are explicitly skipped. |
| `claim` | task_id, actor, skip_gates? | Task | Atomic: set assignee + activate first flow node + set `work_status=in_progress`. Task must be `queued`. |
| `next` | agent?, project? | Task? | Return the highest-priority queued+unblocked task, optionally filtered by project. Does not claim it. |
| `update` | task_id, fields... | Task | Update mutable fields (title, description, priority, labels, roles). Cannot change work_status directly. |
| `cancel` | task_id, actor, reason | Task | Move any non-terminal task to `cancelled`. |
| `hold` | task_id, actor, reason? | Task | Move `in_progress` or `queued` task to `on_hold`. |
| `resume` | task_id, actor | Task | Move `on_hold` task back to `queued`. |

### Flow Progression

| Operation | Parameters | Returns | Description |
|-----------|-----------|---------|-------------|
| `node_done` | task_id, actor, work_output, skip_gates? | Task | Signal that the current work node is complete. Validates work output is present unless gates are explicitly skipped. Advances flow to `next_node`. Updates `work_status` based on next node type. |
| `approve` | task_id, actor, reason?, skip_gates? | Task | Approve at a review node. Advances to `next_node`. If terminal, task becomes `done`. |
| `reject` | task_id, actor, reason | Task | Reject at a review node. Moves to `reject_node`. Reason is required. Creates new execution record (visit N+1) at the target node. |
| `block` | task_id, actor, blocker_task_id | Task | Mark task as blocked by another task. Sets `work_status=blocked`. Flow stays at current node. |
| `get_execution` | task_id, node_id?, visit? | list[FlowNodeExecution] | Read execution records. Filter by node and/or visit. |

### Context

| Operation | Parameters | Returns | Description |
|-----------|-----------|---------|-------------|
| `add_context` | task_id, actor, text, entry_type? | ContextEntry | Append to the task's context log. |
| `get_context` | task_id, limit?, since?, entry_type? | list[ContextEntry] | Read context entries, most recent first. |

### Relationships

| Operation | Parameters | Returns | Description |
|-----------|-----------|---------|-------------|
| `link` | from_id, to_id, kind | void | Create a relationship. Kind: `blocks`, `relates_to`, `supersedes`, `parent`. Validates both tasks exist. For `blocks`, checks for circular dependencies. |
| `unlink` | from_id, to_id, kind | void | Remove a relationship. |
| `dependents` | task_id | list[Task] | All tasks blocked by this task (transitively). |

### Flows

| Operation | Parameters | Returns | Description |
|-----------|-----------|---------|-------------|
| `available_flows` | project? | list[FlowTemplate] | List all flows after override resolution. If project is specified, includes project-local flows. |
| `get_flow` | name, project? | FlowTemplate | Resolve a flow by name through the override chain. |
| `validate_advance` | task_id, actor | list[GateResult] | Dry-run: can this actor advance the current node? Returns one gate result per evaluated gate. |

### Sync

| Operation | Parameters | Returns | Description |
|-----------|-----------|---------|-------------|
| `sync_status` | task_id | map[string, SyncState] | Current sync state per adapter. |
| `trigger_sync` | task_id?, adapter? | SyncResult | Force a sync cycle. Optional filters. |

### Queries

| Operation | Parameters | Returns | Description |
|-----------|-----------|---------|-------------|
| `state_counts` | project? | map[string, int] | Task counts by state. For dashboards. |
| `my_tasks` | agent | list[Task] | All tasks where this agent fills a role that owns the current state. "What's waiting on me?" |
| `blocked_tasks` | project? | list[Task] | All tasks in a non-terminal state with unresolved blockers. |

## 6. Flows

A flow is a **directed graph of nodes**, not a linear state pipeline. Each node is either a `work` node (an agent does something) or a `review` node (someone evaluates what was done). Nodes connect via `next_node` (success path) and `reject_node` (rejection path, review nodes only). The task's `work_status` is derived from the current node type: work nodes map to `in_progress`, review nodes map to `review`.

### Node Types

| Type | Purpose | Outputs | Edges |
|------|---------|---------|-------|
| `work` | Agent performs work | Work output required (see Section 6a) | `next_node` |
| `review` | Reviewer evaluates work from the preceding node(s) | Approve or reject decision | `next_node` (approve), `reject_node` (reject) |

A flow always starts at a designated start node and ends at a terminal node (a node where `next_node` is null). When a task completes the terminal node, it transitions to `done`.

### Actor Types on Nodes

Each node specifies who does the work:

| Actor Type | Resolution |
|------------|------------|
| `role` | Resolved from the task's `roles` map (e.g., `worker`, `reviewer`) |
| `agent` | A specific named agent |
| `human` | Requires human action — creates an inbox item |
| `project_manager` | The project's PM (Polly, typically) |

### Flow Definition Format

```yaml
name: standard
description: Default work flow — worker implements, reviewer approves

roles:
  worker:
    description: Implements the work
  reviewer:
    description: Reviews and approves
  requester:
    description: Who asked for this
    optional: true

nodes:
  implement:
    type: work
    actor_type: role
    actor_role: worker
    next_node: code_review
    gates: [has_assignee]

  code_review:
    type: review
    actor_type: role
    actor_role: reviewer
    next_node: done
    reject_node: implement
    gates: [has_work_output]

  done:
    type: terminal

start_node: implement
```

### Flow Node Execution

When a task moves through a flow, each visit to a node creates a **flow node execution** record:

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique execution ID |
| `task_id` | string | The task |
| `node_id` | string | Which flow node |
| `visit` | int | Visit counter (1 = first pass, 2 = after first rejection, etc.) |
| `status` | enum | `pending`, `active`, `blocked`, `completed` |
| `work_output` | WorkOutput? | What the agent produced (work nodes only, see Section 6a) |
| `decision` | enum? | `approved` or `rejected` (review nodes only) |
| `decision_reason` | string? | Reviewer's explanation (required on rejection) |
| `started_at` | datetime? | When work began |
| `completed_at` | datetime? | When the node was completed |

Each visit is a separate record. When a reviewer rejects work and the flow loops back, a new execution record (visit N+1) is created. The old records remain for audit. This gives full traceability: what was done on each attempt, what the reviewer said, how many iterations it took.

The task's `current_node_id` always points to the active node. Combined with the execution record, this gives the full picture: "Task is at the Code Review step, visit 2."

### Flow Progression

Flow advancement is **always explicit** — never automatic.

- An agent must signal "node done" to advance. A successful run does NOT automatically advance the flow. The agent may need multiple sessions at a single node.
- Work nodes cannot advance without a work output (see Section 6a).
- Review nodes require an explicit approve or reject decision. Approval advances to `next_node`. Rejection advances to `reject_node`.
- If an agent encounters a blocker, it files a blocking task with a dependency link. The flow stays at the current node until the dependency resolves.

### Built-in Flows

| Flow | Description | Nodes | Roles |
|------|-------------|-------|-------|
| `standard` | Default. Worker implements, reviewer approves. | implement → code_review → done | worker, reviewer, requester? |
| `spike` | Research/investigation. No review. | research → done | worker, requester? |
| `user-review` | Like standard, but human must review. | implement → human_review → done | worker, requester? (human_review node has `actor_type: human`) |
| `bug` | Like standard, with reproduction gate. | reproduce → fix → code_review → done | worker, reviewer, requester? |
| `chat` | Lightweight user ↔ agent conversation persisted as a task. | user_message → agent_response → done | operator?, requester? (user_message node has `actor_type: human`; `operator` defaults to Polly and is overridable via `--role operator=<name>`) |

### Immutability

Flow templates are **immutable once a task is using them**. If the flow needs to change, create a new version of the template. Tasks already in progress keep their original flow. New tasks pick up the updated template. This resolves OQ-6 definitively.

### Override Chain

```
Built-in flows (pollypm/flows/):     lowest precedence
User-global flows (~/.pollypm/flows/):    overrides built-in
Project-local flows (<project>/.pollypm/flows/):  highest precedence
```

A flow at a higher level with the same `name` fully replaces the lower-level flow. New names at any level add new flow options. This matches the existing override semantics for rules, magic, and agent profiles.

### Flow Resolution

When a task is created with `flow: "standard"`:

1. Check `<project>/.pollypm/flows/standard.yaml` — if found, use it
2. Check `~/.pollypm/flows/standard.yaml` — if found, use it
3. Check built-in `pollypm/flows/standard.yaml` — if found, use it
4. Error: unknown flow

### Flow Validation

At load time, the work service validates:
- Every node's `next_node` and `reject_node` reference existing nodes in the same flow
- Only `review` nodes have a `reject_node`
- `start_node` exists
- At least one terminal node exists (a node where `next_node` is null or type is `terminal`)
- No orphan nodes (every non-start node is reachable from the start)
- Every `role`-typed node specifies an `actor_role` that exists in the flow's `roles` definition

## 6a. Work Output

Every work node must produce a **work output** before it can advance. This is the proof of what was done. The work service validates that a work output exists before allowing a `node_done` signal.

### Work Output Schema

```json
{
  "type": "code_change",
  "summary": "Fixed SameSite cookie attribute for mobile webkit",
  "artifacts": [
    {
      "kind": "commit",
      "ref": "abc123def456",
      "description": "Set SameSite=None; Secure on auth cookies"
    },
    {
      "kind": "file_change",
      "path": "src/auth/cookies.py",
      "description": "Added SameSite attribute to cookie builder"
    }
  ]
}
```

### Output Types

| Type | When | Example artifacts |
|------|------|-------------------|
| `code_change` | Work that produces commits or file changes | `commit` refs, `file_change` paths with summaries |
| `action` | Work that produces no local artifacts — an external action was taken | `action` with description ("queued movie X for download", "published blog post", "sent email to Y") |
| `document` | Research, design, or content work | `file_change` paths, `note` with findings |
| `mixed` | Combination of the above | Any artifact kinds |

### Artifact Kinds

| Kind | Fields | Description |
|------|--------|-------------|
| `commit` | `ref`, `description` | A git commit hash |
| `file_change` | `path`, `description` | A file that was created or modified |
| `action` | `description`, `external_ref`? | An action taken outside the local filesystem |
| `note` | `description` | A textual finding or decision |

### Why This Matters

1. **Accountability**: Every work node has a record of what was produced. Reviewers know exactly what to evaluate.
2. **Traceability**: The chain from task → flow node → work output → artifacts gives full provenance.
3. **Flexibility**: Milo queuing a movie download is as valid as Pete committing code. The schema handles both.
4. **Review context**: Review nodes receive the preceding work node's output as context. The reviewer sees what was done, not just that the state changed.

## 7. Gates

A gate is a precondition checked before a transition is allowed. Gates are functions: they take a task and return pass/fail with a reason.

### Built-in Gates

| Gate | Checks | Used by |
|------|--------|---------|
| `has_description` | Task has a non-empty description | All flows: `queue` gate (built-in, always applied) |
| `has_assignee` | Task has a non-empty assignee | standard: claim gate |
| `has_work_output` | Current node execution has a non-empty work output | All work nodes: `node_done` gate (built-in, always applied) |
| `has_commits` | Git commits exist on the task's branch since the task was claimed | Code-oriented work nodes |
| `acceptance_criteria` | All acceptance criteria in the description are addressed | Review nodes |
| `has_reproduction` | Bug has reproduction steps documented | bug flow: reproduce node |
| `all_children_done` | All child tasks are in a terminal state | Any epic transition to done |
| `no_blocked_tasks` | No tasks are blocked by this one (or they're all done) | Informational warning, not blocking |

### Gate Types

- **Hard gates**: Must pass for the transition to proceed. Transition fails with an error listing which gates failed and why.
- **Soft gates**: Log a warning but do not block the transition. Appear in the transition record.

### Custom Gates

Users can register custom gates by placing Python modules in the gates plugin directory:

```
~/.pollypm/gates/my_gate.py          # user-global
<project>/.pollypm/gates/my_gate.py  # project-local
```

A gate module exports a function matching the gate protocol:

```python
def check(task: Task) -> GateResult:
    """Return GateResult(passed=True/False, reason='...')"""
```

Gates are referenced by name in flow definitions. The work service discovers and loads them through the extension host.

## 8. Sync Adapters

Sync adapters project work service state to external systems.

### Adapter Interface

```python
class SyncAdapter(Protocol):
    name: str  # e.g., "github", "file"

    def on_create(self, task: Task) -> None: ...
    def on_transition(self, task: Task, old_status: str, new_status: str) -> None: ...
    def on_update(self, task: Task, changed_fields: list[str]) -> None: ...
```

### Built-in Adapters

**File Adapter**: Maintains the `issues/` folder structure as a read-only projection.

- `on_create` → writes a markdown file to the appropriate state directory
- `on_transition` → moves the file between state directories
- `on_update` → rewrites the markdown content

The adapter projects every `work_status` onto one of five physical folders. Tooling (UIs, scripts, heartbeat checks) may rely on this mapping being stable:

| `work_status` | Folder |
|---------------|--------|
| `draft` | `00-not-ready` |
| `queued` | `01-ready` |
| `in_progress` | `02-in-progress` |
| `blocked` | `02-in-progress` |
| `review` | `03-needs-review` |
| `on_hold` | `00-not-ready` |
| `done` | `05-completed` |
| `cancelled` | `05-completed` |

`blocked` and `on_hold` do not have dedicated folders. `blocked` maps to `02-in-progress` because a blocked task is still in an active work session (the worker is waiting on a dependency, not paused). `on_hold` maps to `00-not-ready` because a held task is intentionally not being worked on and behaves like a scoped-but-parked task for folder-listing purposes. Consumers that need to distinguish these states should read `work_status` from the task record rather than inferring state from folder position.

**GitHub Adapter**: Syncs with GitHub Issues via labels.

- `on_create` → creates a GitHub issue with the appropriate state label
- `on_transition` → swaps labels (e.g., remove `polly:in-progress`, add `polly:needs-review`)
- `on_update` → updates issue title/body

### Inbound GitHub Sync

Inbound GitHub sync is **not shipped in the current build**. The live adapter is
one-way push only (`work service -> GitHub`) via `gh`.

The deferred inbound design is still the right shape: inbound changes should be
treated as requests, not commands, and should pass through the same transition
gates as local actions. But that conflict policy is future work, not a v1
behavioral guarantee.

**Sync state tracking**: Each adapter maintains a cursor/timestamp per task to detect changes since the last sync cycle.

## 9. Worker Task Assignment

### How a Worker Gets Work

The shipped flow is **per-task, auto-claimed**. There is no long-running
generic worker shell, no self-service `pm task next` polling loop, and no
manual `tmux send-keys` poke from the supervisor. Every queued worker-role
task gets its own session, claimed by Polly, provisioned automatically, and
kicked off by the heartbeat sweep.

**Polly auto-claims her own queued tasks.** Per the operator delegation
contract, after `pm task queue <project>/<n>` on a worker-role task that
Polly owns, she immediately runs:

```
pm task claim <project>/<n> --actor worker
```

This is not optional follow-up — it is the contract. Skipping `claim`
leaves the task stranded in `queued`. Non-worker roles (review, plan, etc.)
are the only ones that stop at `queued`.

**`pm task claim` provisions the per-task tmux window automatically.**
The single `claim` call:

- Marks the task `assignee=worker` and `work_status=in_progress`.
- Activates the first flow node.
- Creates a real git worktree at `.pollypm/worktrees/<project>-<number>`
  on branch `task/<project>-<number>`.
- Writes `.pollypm-task-prompt.md` into the worktree root.
- Opens a per-task tmux window named `task-<project>-<number>` whose CWD
  is the worktree, and launches the provider CLI inside it.

**The heartbeat sweep delivers and force-pushes the kickoff.** The kickoff
prompt is sent through the same tmux session-recognition path the
heartbeat uses for every worker. If the pane was still racing the provider
bootstrap when `claim` ran, the heartbeat sweep retries the kickoff send
on its next cycle and stamps `kickoff_sent_at` only after the pane is
ready. Workers do not need to be poked by hand; the supervisor does not
run `tmux send-keys` directly.

```
polly                → pm task queue <id>            (queued)
polly (auto, by rule)→ pm task claim <id> --actor worker
work service         → provisions worktree + per-task tmux window
heartbeat sweep      → recognizes the per-task session, force-pushes the
                       kickoff prompt, stamps kickoff_sent_at when the
                       pane is ready
worker session       → reads .pollypm-task-prompt.md and starts work
```

### Idle Workers

The per-task model means there is no generic "idle worker" waiting for
work. A task gets a worker session when it is claimed; the session ends
on approval, cancel, or teardown. If no tasks are queued for a project,
no worker windows exist for that project. When new work appears, Polly
queues + claims it, and the per-task lifecycle above provisions the
session.

### Priority Resolution

`pm task next` returns tasks ordered by:

1. `critical` > `high` > `normal` > `low`
2. Within the same priority: oldest `created_at` first (FIFO)
3. Only tasks where `work_status` is `queued` and `blocked` is false

## 10. What This Replaces

| Current Module | Disposition | Replaced By |
|----------------|-------------|-------------|
| `task_backends/base.py` | Superseded | Work service protocol |
| `task_backends/file.py` | Superseded | Work service + file sync adapter |
| `inbox_v2.py` (agent-to-agent coordination) | Removed | Work service transitions + per-task worker sessions auto-kicked off by the heartbeat sweep |
| `inbox_v2.py` (user-facing communication) | **Retained** | Still the Polly↔user channel |
| `inbox_processor.py` (quality gate injection) | Removed | Transition gates |
| `inbox_processor.py` (tier classification for user) | **Retained** | Still relevant for user inbox |
| `inbox_delivery.py` (agent poke system) | Removed | Per-task worker sessions provisioned on `pm task claim` and kicked off by the heartbeat sweep |
| `progress-log.md` (shared transition log) | Superseded | Per-task `transitions` in the work service |
| `.latest_issue_number` (atomic counter) | Superseded | Work service ID generation (serialized) |

## 11. What This Keeps

| Thing | Why |
|-------|-----|
| Inbox (user ↔ Polly) | The inbox is the right tool for surfacing decisions, escalations, and status to the human. Tier classification (silent/flag/escalate) still makes sense here. |
| Work status state machine | Eight states (`draft` through `cancelled`) derived from OtterCamp v2. `work_status` is a projection of flow node state, not an independent controller. |
| File-first philosophy | The file sync adapter maintains `issues/` as a human-inspectable projection. `ls issues/01-ready/` still works. |
| Override chain | Built-in < user-global < project-local. Same precedence model as rules, magic, and agent profiles. |
| Heartbeat supervision | Still monitors agent health. Work-state mutations remain safe because the service is in-process and SQLite-backed. |

## 12. Open Questions

### ~~OQ-1: Task ID Format~~ — PARTIALLY RESOLVED, DOT-NOTATION DEFERRED (post-v1)

Sequential numeric, scoped per project. Each project starts at `#0001`. Cross-project references prepend the project slug: `otter-camp#42`. Within a project, the slug is optional — `#42` is sufficient. This mirrors GitHub's `owner/repo#123` convention.

The work service stores `(project, task_number)` as the compound key. Display format is assembled by the caller based on context.

**Dot-notation subtasks (`#42.1`, `#42.2`) are deferred post-v1.** Implementing dot-notation would require extending the primary key to `(project, parent_number, subtask_number)` or parsing subtask IDs into a nested structure — a non-trivial data model change that is not worth blocking v1. For v1, subtasks live as top-level tasks linked to their parent via the `parent` relationship (see §4 Relationships). This preserves the parent/child semantics and the `all_children_done` gate; what it loses is the display-level identity (a subtask of `#42` is `#43`, not `#42.1`).

Future work tracker: see GitHub issue #141.

### ~~OQ-2: Context Log Compaction~~ — DEFERRED (post-v1)

Not a v1 concern. Tasks should be well-scoped enough that context logs stay manageable. If a task is accumulating a massive context log, it's too big and should be split. For v1, `get_context` with `limit` and `since` parameters is sufficient. Revisit if real-world usage reveals a problem.

### ~~OQ-3: Storage Backend for v1~~ — RESOLVED

SQLite, using the existing PollyPM state database (`state.db`). Work service tables use a `work_` prefix (`work_tasks`, `work_flow_templates`, `work_node_executions`, etc.) so they coexist cleanly with existing tables (`sessions`, `heartbeats`, `alerts`, etc.). This lets work service tables reference existing tables via foreign keys (e.g., project references) without cross-database joins or sync. Plugins follow the same pattern — prefixed tables in the shared database.

### ~~OQ-4: Daemon vs. In-Process Service~~ — RESOLVED

In-process for v1. The `pm` CLI calls the work service library directly. SQLite WAL mode handles concurrent reads. Graduate to a Unix socket daemon in Phase 3 once the API surface is stable. The API boundary is designed to be transport-agnostic, so the upgrade is a wiring change, not a redesign.

### ~~OQ-5: Two-Way GitHub Sync Conflict Policy~~ — DEFERRED

Two-way sync is not a v1 concern. V1 ships one-way push only (work service → GitHub). Two-way inbound sync and conflict policy will be designed when needed.

### ~~OQ-6: Flow Versioning~~ — RESOLVED

Flow templates are **immutable once a task is using them**. If the flow needs to change, create a new version of the template. Tasks in progress keep their original flow. New tasks pick up the updated template. See Section 6, Immutability.

### ~~OQ-7: What "Terminal State" Means for Dependencies~~ — RESOLVED

`done` and `cancelled` are both terminal states. However, they resolve dependencies differently:

- **Blocker reaches `done`**: Dependency is satisfied. Blocked task automatically unblocks (returns to `queued`).
- **Blocker is `cancelled`**: Dependency is NOT automatically satisfied. The work service flags this to the PM/operator, who must decide: should the blocked tasks also be cancelled, or can they be unblocked now? The blocked task stays `blocked` until the PM explicitly removes the dependency or cancels it.

This prevents a cancelled blocker from silently unblocking work that may no longer make sense.

### ~~OQ-8: Cross-Project Dependencies~~ — RESOLVED

Yes, cross-project dependencies are allowed. A task in project A can be blocked by `otter-camp#42` in project B. This is valuable for coordinating integrations between projects. The `link` operation validates both tasks exist. Cross-project references use the `project-slug#number` format. Coordination across different flows, PMs, and review cadences is a PM problem, not a system problem — the work service just tracks the dependency.

## 13. Potential Pitfalls

### P-1: In-Process Boundary Erosion

Because v1 is in-process, callers could be tempted to reach around the service
and mutate tables directly. That would bypass flows, gates, sync hooks, and
transition logging. Mitigations:
- Keep CLI and supervisor mutations routed through `SQLiteWorkService`
- Maintain import-boundary tests around service helpers and storage access
- Reserve a future transport boundary for the point where stronger process
  isolation is actually needed

Recent boundary cleanup moved job queue repair/stat operations behind public
`JobQueue` methods and work DB path resolution behind
`pollypm.work.db_resolver`; new callers should use those surfaces instead of
CLI-private helpers or queue internals.

### P-2: Flow Definition Errors

A malformed flow YAML could break task routing for an entire project. Mitigations:
- Validate flow definitions at load time (not at transition time)
- Schema validation: required fields, valid state references, no orphan states
- Built-in flows are tested in CI; user/project flows are validated on first use with clear error messages
- `pm flow validate <path>` command for pre-commit checking

### P-3: Sync Adapter Failures

A GitHub API outage shouldn't block the work service. Mitigations:
- Sync adapters are async, best-effort, eventually consistent
- Failed syncs are queued for retry with backoff
- The work service state is always authoritative — sync failure means the projection is stale, not that work is blocked
- Operators can force a resync with `pm task sync`; per-adapter sync state
  lives in `work_sync_state` and is queryable via the service's
  `sync_status(task_id)` method, but the CLI does not yet expose a dedicated
  per-task sync-status command.

### P-4: Gate Brittleness

Gates that depend on external state (git branches, test results) can fail for reasons unrelated to the task. Mitigations:
- Hard vs. soft distinction: soft gates warn but don't block
- `--skip-gates` escape hatch for operators (logged prominently in the transition record)
- Gate timeout: if a gate can't determine pass/fail within N seconds, it soft-fails with a warning

### P-5: Context Log Bloat

Long-running tasks accumulate large context logs that blow up agent context windows. Mitigations:
- `get_context` supports `limit` and `since` parameters
- Compaction (see OQ-2) summarizes old entries
- Agents are given a summary + last N entries, not the full log

### P-6: Migration from Current System

Existing projects have active issues in `issues/` directories. The transition must not lose in-flight work. Mitigations:
- One-time migration: scan existing `issues/` directories, ingest into the work service
- Map folder position to state, file name to ID+title, file content to description
- The file sync adapter then keeps `issues/` in sync going forward
- Reversibility: the file projection means the old workflow still "works" for read-only inspection even if the work service is the authority

### P-7: Circular Dependencies

`link(A, B, kind="blocks")` followed by `link(B, A, kind="blocks")` creates a deadlock. Mitigations:
- The `link` operation checks for cycles in the `blocks`/`blocked_by` graph before committing
- Cycle detection is a DFS from the target back to the source — O(n) in the size of the dependency chain

### P-8: Flow Graph Complexity

Directed graph flows are more powerful than linear pipelines but harder for agents to reason about. A flow with multiple review stages, parallel paths, or complex rejection loops could confuse agents. Mitigations:
- Built-in flows are simple and well-tested (standard is just: implement → review → done)
- `pm task status <id>` always shows: current node, what's expected, who owns it
- Flow validation at load time catches structural errors (orphan nodes, missing reject edges)
- Start simple — most projects should use built-in flows. Complex flows are an escape hatch, not the default.

### P-9: Role Reassignment Mid-Flight

If `pete` is the worker on a task in `in-progress` and gets reassigned, the new worker needs context. Mitigations:
- Role changes are recorded in the context log: `"system: worker reassigned from pete to nora"`
- The context log carries the full history — the new worker reads it via `pm task get <id>`

## 14. Testing Strategy

The sealed architecture enables comprehensive testing at every layer.

### Unit Tests: Flow Engine

Pure logic, no I/O. Test the flow engine against flow definitions.

```
test_standard_flow_graph_structure
  Given the standard flow template
  Then start_node is "implement"
  And implement.next_node is "code_review"
  And code_review.next_node is "done" (terminal)
  And code_review.reject_node is "implement"

test_flow_advance_from_work_node
  Given a task at the "implement" work node
  When the worker signals node_done with a valid work output
  Then the task advances to "code_review"
  And work_status changes from "in_progress" to "review"

test_flow_advance_without_work_output_rejected
  Given a task at the "implement" work node
  When the worker signals node_done without a work output
  Then the advance is rejected: "work output required"

test_review_approve_advances
  Given a task at "code_review" review node
  When the reviewer approves
  Then the task advances to the terminal node and work_status becomes "done"

test_review_reject_loops_back
  Given a task at "code_review" review node, visit 1
  When the reviewer rejects with reason "needs error handling"
  Then the task moves back to "implement" node
  And a new flow_node_execution record is created with visit=2
  And work_status changes from "review" to "in_progress"

test_rejection_requires_reason
  Given a task at a review node
  When the reviewer rejects without a reason
  Then the rejection is rejected: "reason required"

test_wrong_actor_cannot_advance
  Given a task at "code_review" with reviewer=polly
  When pete (the worker) attempts to approve
  Then the action is rejected: pete is not the reviewer for this node

test_spike_flow_has_no_review_nodes
  Given the spike flow template
  Then all nodes are either work or terminal
  And the path is: research → done

test_owner_derived_from_current_node
  Given a task at "code_review" with actor_role=reviewer, roles={reviewer: polly}
  Then current_owner is "polly"

test_owner_null_at_terminal
  Given a task at the terminal node
  Then current_owner is null

test_custom_flow_overrides_builtin
  Given a project-local standard.yaml with different nodes
  When resolving "standard" for that project
  Then the project-local version is used

test_flow_validation_catches_orphan_nodes
  Given a flow with a node not reachable from start_node
  Then flow validation fails with "orphan node" error

test_flow_validation_review_must_have_reject
  Given a flow with a review node that has no reject_node
  Then flow validation fails with "review node must have reject_node"

test_flow_immutability
  Given a flow template used by an in-progress task
  When attempting to modify the template
  Then modification is rejected: "template is immutable, create a new version"
```

### Unit Tests: Work Output

```
test_code_change_output_valid
  Given a work output with type="code_change" and artifacts=[{kind: "commit", ref: "abc123"}]
  Then validation passes

test_action_output_valid
  Given a work output with type="action" and artifacts=[{kind: "action", description: "queued movie for download"}]
  Then validation passes

test_empty_artifacts_rejected
  Given a work output with type="code_change" and artifacts=[]
  Then validation fails: "work output must include at least one artifact"

test_work_output_stored_on_execution
  When a worker signals node_done with a work output
  Then the flow_node_execution record stores the work output
  And subsequent get_execution calls return it
```

### Unit Tests: Flow Node Execution

```
test_execution_created_on_claim
  When a worker claims a task
  Then a flow_node_execution record is created for the start node, visit=1, status=active

test_execution_completed_on_advance
  When the flow advances past a node
  Then the execution record status becomes "completed" with a completed_at timestamp

test_rejection_creates_new_visit
  Given a task at visit 1 of "implement"
  When the reviewer rejects at "code_review"
  Then a new execution record for "implement" is created with visit=2

test_execution_audit_trail
  After a full lifecycle (implement v1 → review → reject → implement v2 → review → approve)
  Then there are 4 execution records:
    implement visit=1 (completed), code_review visit=1 (completed, rejected),
    implement visit=2 (completed), code_review visit=2 (completed, approved)
```

### Unit Tests: Gate Evaluation

Test each gate function in isolation with mock task data.

```
test_has_assignee_passes
  Given a task with assignee="pete"
  Then has_assignee gate passes

test_has_assignee_fails
  Given a task with assignee=""
  Then has_assignee gate fails with reason "no assignee"

test_has_commits_passes
  Given a mock git interface reporting 3 commits since claim time
  Then has_commits gate passes

test_has_commits_fails_no_commits
  Given a mock git interface reporting 0 commits since claim time
  Then has_commits gate fails with reason "no commits since task was claimed"

test_all_children_done_passes
  Given a task with children [A, B] both in "done" state
  Then all_children_done gate passes

test_all_children_done_fails
  Given a task with children [A, B] where B is in "in-progress"
  Then all_children_done gate fails with reason "child B is still in-progress"

test_soft_gate_warns_but_allows
  Given a soft gate that fails
  When evaluating transition
  Then the transition is allowed but the result includes a warning
```

### Unit Tests: Dependency Graph

```
test_blocked_detection
  Given task A blocked_by task B where B is in "in-progress"
  Then A.blocked is true

test_unblocked_when_blocker_done
  Given task A blocked_by task B where B is in "done"
  Then A.blocked is false

test_circular_dependency_rejected
  Given task A blocks task B
  When attempting to link B blocks A
  Then the link is rejected with "circular dependency detected"

test_transitive_blocking
  Given A blocked_by B, B blocked_by C, C in "in-progress"
  Then A.blocked is true
  And dependents(C) includes both B and A

test_next_skips_blocked_tasks
  Given task A (priority=high, blocked) and task B (priority=normal, unblocked)
  When calling next()
  Then B is returned (not A)
```

### Unit Tests: Task Lifecycle

```
test_create_assigns_id
  When creating a task
  Then the returned task has a non-empty id

test_create_validates_flow_exists
  When creating a task with flow="nonexistent"
  Then creation fails with "unknown flow"

test_create_validates_required_roles
  When creating a task on "standard" flow without filling "worker" role
  Then creation fails with "missing required role: worker"

test_claim_is_atomic
  Given a task in "ready" state
  When pete claims it
  Then assignee is "pete" AND state is "in-progress" (both or neither)

test_claim_fails_if_not_ready
  Given a task in "in-progress" state
  When pete attempts to claim it
  Then claim fails with "task is not in a claimable state"

test_move_records_transition
  When moving a task from "ready" to "in-progress"
  Then the task's transitions list includes the new entry with actor and timestamp

test_update_cannot_change_state
  When calling update with state="done"
  Then update fails with "use lifecycle operations to change state"

test_add_context_appends
  Given a task with 3 context entries
  When adding a new context entry
  Then the task has 4 context entries and the new one is last
```

### Integration Tests: Work Service API

Test the full service through its API boundary with a real storage backend.

```
test_full_standard_lifecycle
  Create a task (draft) → queue → claim (in_progress, implement node) →
  node_done with work output (review, code_review node) →
  approve (done)
  Verify: all execution records, owner flips, work output stored

test_full_standard_lifecycle_with_rejection
  Create → queue → claim → node_done → reject with reason →
  (back to implement, visit 2) → node_done → approve → done
  Verify: rejection reason recorded, visit counter incremented, two execution records per node

test_concurrent_claim_race
  Create and queue a task
  Simulate two workers calling claim simultaneously
  Verify exactly one succeeds and one fails with "already claimed"

test_draft_to_queued_with_human_review
  Create a task with requires_human_review=true
  Attempt to queue without human approval → rejected
  Simulate human approval via inbox → queue succeeds

test_next_ordering
  Create and queue 3 tasks: high priority (oldest), normal priority, high priority (newest)
  Verify next() returns high-oldest first, then high-newest, then normal

test_blocked_task_not_returned_by_next
  Create and queue task A (blocked by B) and task C (unblocked)
  Verify next() returns C

test_blocker_resolution_unblocks
  Create task A blocked by B. Move B to done.
  Verify A.blocked is now false and A appears in next()

test_my_tasks_returns_owned
  Create tasks assigned to different agents in various states
  Verify my_tasks("polly") returns only tasks where polly owns the current state

test_state_counts
  Create tasks in various states
  Verify state_counts() returns correct counts per state
```

### Integration Tests: Flow Override Chain

```
test_builtin_flow_used_when_no_overrides
  With no user or project flow files
  Verify the built-in standard flow is used

test_user_flow_overrides_builtin
  Place a custom standard.yaml in ~/.pollypm/flows/
  Verify the user version is used instead of built-in

test_project_flow_overrides_user
  Place a custom standard.yaml in <project>/.pollypm/flows/
  Verify the project version is used even when user version exists

test_project_adds_new_flow
  Place a deploy.yaml in <project>/.pollypm/flows/
  Verify deploy is available AND built-in flows are still available

test_invalid_flow_file_rejected_at_load
  Place a malformed YAML file in the flows directory
  Verify clear error message at load time, not at transition time
```

### Integration Tests: Sync Adapters

```
test_file_adapter_creates_issue_file
  Create a task via work service
  Verify a markdown file exists in the correct state directory under issues/

test_file_adapter_moves_on_transition
  Move a task from ready to in-progress
  Verify the file moved from issues/01-ready/ to issues/02-in-progress/

test_github_adapter_creates_issue
  Create a task with a GitHub-synced project
  Verify gh issue was created with correct labels

test_github_adapter_swaps_labels_on_transition
  Move a task
  Verify old state label removed, new state label added

test_github_inbound_valid_change
  Simulate a GitHub label change that is a valid transition
  Verify work service state updates accordingly

test_github_inbound_invalid_change
  Simulate a GitHub label change that skips states
  Verify work service state is unchanged and conflict is logged

test_sync_failure_does_not_block_work_service
  Configure a sync adapter that throws on every call
  Verify task mutations still succeed and sync failures are logged

test_sync_retry_on_failure
  Configure a sync adapter that fails once then succeeds
  Verify the sync eventually completes on retry
```

### Integration Tests: Migration

```
test_migrate_existing_issues
  Set up an issues/ directory with files in various state folders
  Run migration
  Verify all issues exist in the work service with correct states

test_migrate_preserves_ids
  Migrate issue 0042-fix-auth.md
  Verify it becomes task 42 in the work service

test_migrate_preserves_content
  Migrate an issue with a markdown body
  Verify the description field contains the body content

test_migrate_idempotent
  Run migration twice
  Verify no duplicates
```

### End-to-End Tests

```
test_e2e_worker_claims_and_completes
  Build a real SQLiteWorkService against a temp DB
  Polly calls: create → queue
  Worker calls: next → claim → add_context → node_done with work output
  Polly calls: approve
  Verify full execution history, work output stored, file adapter projection, context log

test_e2e_work_output_required
  Start work service
  Worker claims a task, attempts node_done without work output
  Verify advance is rejected with "work output required"

test_e2e_gate_blocks_advance
  Start work service with has_commits gate on a work node
  Worker claims a task, attempts node_done without committing
  Verify advance is rejected with gate failure message

test_e2e_supervisor_assigns_work
  Build a real SQLiteWorkService
  Polly creates and assigns a task
  Verify the task shows up in my_tasks for the assigned worker

test_e2e_service_reopen_recovery
  Build a SQLiteWorkService, create some tasks, then drop it
  Re-open a fresh SQLiteWorkService against the same DB
  Verify all state is preserved and operations resume
```

### Performance / Stress Tests

```
test_1000_tasks_list_performance
  Create 1000 tasks
  Verify list_tasks() completes in under 500ms

test_100_concurrent_reads
  Create tasks, then issue 100 simultaneous list/get requests
  Verify all return correct results

test_rapid_transitions
  Move 100 tasks through their full lifecycle sequentially
  Verify all transitions are recorded correctly with no data loss

test_large_context_log
  Add 1000 context entries to a single task
  Verify get_context with limit=10 returns in under 100ms
```

## 15. Worker Session Lifecycle

Worker sessions are the runtime that executes tasks. Their lifecycle is deterministic — no LLM involved in provisioning or teardown. The work service (or event hooks on state transitions) handles this as infrastructure.

### Session Binding

Each task, when claimed, gets a bound worker session:

| Event | Action | Owner |
|-------|--------|-------|
| Task claimed (`claim()`) | Create worktree on task branch, start tmux pane, inject task prompt, bind session to task | Work service / session manager (deterministic code) |
| Work node complete (`node_done()`) | Session idles while review happens. No cost — idle tmux pane burns no tokens. | — |
| Review rejects (`reject()`) | Poke the existing session with rejection feedback. Same session, same worktree, same branch. | Work service sends tmux input |
| Review approves / task done | Archive JSONL, record token usage, tear down session, clean up worktree | Work service / session manager |
| Task blocked (`block()`) | Session idles. Resumes when unblocked. No teardown — there's no cost to an idle session. | — |
| Task cancelled (`cancel()`) | Archive JSONL, record token usage, tear down session, clean up worktree | Work service / session manager |

Key principle: **a worker session lives for the life of its task.** One session per task, from claim to terminal state. This preserves the agent's context window across rejection loops and blocking/unblocking cycles.

### Worktree Management

Each worker session gets an isolated git worktree:

```
<project>/.pollypm/worktrees/<task-id>/
```

- Created on `claim()`: `git worktree add .pollypm/worktrees/<task-id> -b task/<task-slug>`
- Worker operates exclusively in this worktree — never touches the main working directory
- Multiple workers on the same project get separate worktrees on separate branches
- On task completion: approval attempts an auto-merge of the task branch into
  the project's canonical branch, then teardown archives JSONL and removes the
  worktree
- On task cancellation: worktree is removed, branch optionally preserved for forensics

### JSONL Archival

When a worker session is torn down (task reaches terminal state), the Claude JSONL transcript is archived:

```
<project>/.pollypm/transcripts/tasks/<task-id>/
  session.jsonl          # full Claude JSONL transcript
  metadata.json          # task_id, session duration, token summary
```

This provides:
- Full audit trail of every tool call, prompt, and response
- Ability to replay or analyze agent behavior after the fact
- Input for per-task cost accounting

The archival is deterministic — copy files, write metadata, done. No LLM involved.

### Per-Task Token Tracking

Each task accumulates token usage from its worker session(s):

| Field | Type | Description |
|-------|------|-------------|
| `total_input_tokens` | int | Total input tokens across all turns |
| `total_output_tokens` | int | Total output tokens across all turns |
| `session_count` | int | Number of sessions (usually 1, but could be >1 if session crashed and was recovered) |

These fields are derived from `work_sessions`, not stored directly on
`work_tasks`. `pm task get <id>` shows the token totals; aggregate cost views
currently live elsewhere (for example `pm costs` at the project/account level)
rather than as a per-task cost field.

### Parallel Workers

Multiple tasks in the same project can have active workers simultaneously:

- Each gets its own worktree and branch
- The work service tracks which sessions are bound to which tasks
- The PM ensures independent tasks are queued and claimed — tasks that touch overlapping files should be sequenced via dependencies, not parallelized. (Polly auto-claims her queued worker-role tasks; she does not poll `pm task next`.)

### Tmux Session Hardening (Prerequisite)

The tmux session layer is the foundation for all of the above. It must be bulletproof:

- **Auto-recovery**: If a tmux pane dies (process crash, OOM, etc.), the session manager detects it and restarts the agent in the same worktree with recovery context from the JSONL archive and the task's context log.
- **Health monitoring**: Periodic checks that the agent process is alive and responsive. Detect stuck/looping agents (existing heartbeat infrastructure).
- **Clean teardown**: Sessions are always torn down gracefully — JSONL archived, worktree cleaned, tmux pane removed. No orphaned panes or worktrees.
- **Idempotent operations**: Creating a session for a task that already has one is a no-op (or returns the existing session). Tearing down an already-torn-down session is a no-op.
- **Isolation**: Workers cannot access each other's worktrees or tmux panes. A misbehaving worker cannot affect other sessions.

This is a prerequisite workstream — the current tmux handling needs a hardening pass before the work service session lifecycle can be fully reliable.

## 16. Implementation Phases

### Phase 0: Tmux Hardening (Prerequisite)

Audit and harden the tmux session layer. This is foundational — everything else depends on reliable session management.

Deliverables:
- Auto-recovery on pane/process death
- Idempotent create/teardown operations
- Health monitoring integration with heartbeat
- Clean isolation between worker sessions
- Comprehensive test coverage for failure scenarios

### Phase 1: Core Service (No Daemon)

Build the work service as an in-process library. This gets the API surface, flow engine, gate evaluation, and task lifecycle working and testable.

Deliverables:
- `WorkService` protocol definition
- Default implementation with SQLite backend
- Flow engine with YAML loading and override resolution
- Built-in gates
- Full unit and integration test suite
- `pm task` CLI commands as thin wrappers

### Phase 2: Worker Session Lifecycle

Wire the work service to the tmux/worktree layer. Task state transitions trigger deterministic session management.

Deliverables:
- Worktree creation/teardown on claim/completion
- Session binding (task ↔ tmux pane)
- JSONL archival on session teardown
- Per-task token tracking from archived JSONL
- Rejection loop handling (poke existing session)
- Parallel worker support (multiple worktrees per project)

### Phase 3: Sync Adapters

Add the file adapter and GitHub adapter. One-way push for v1.

Deliverables:
- `SyncAdapter` protocol
- File adapter (maintains `issues/` projection)
- GitHub adapter (label-based sync, one-way push)
- Migration tool for existing `issues/` directories

### Phase 4: Daemon Process

Graduate the in-process service to a Unix socket daemon.

Deliverables:
- Daemon with Unix socket transport
- `pm` CLI updated to connect via socket
- Health checking and auto-restart
- Graceful degradation on daemon failure

### Phase 5: Cleanup

Remove superseded modules and update documentation.

Deliverables:
- Remove `task_backends/file.py` and `task_backends/base.py`
- Remove agent-to-agent coordination from inbox modules
- Update v1 spec docs
- Update CLAUDE.md and agent instructions
