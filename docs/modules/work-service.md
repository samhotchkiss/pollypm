**Last Verified:** 2026-04-22

## Summary

The work service is PollyPM's sealed task-management engine. It owns the eight-state task lifecycle (`draft` → `queued` → `in_progress` / `blocked` / `on_hold` → `review` → `done` / `cancelled`), per-task flow execution (`FlowNodeExecution` rows), gate evaluation at transitions, worker-session bookkeeping, and sync-adapter hooks (file + GitHub). All mutations go through `WorkService` methods — the SQLite connection and schema are owned entirely by `SQLiteWorkService` and are not reachable from callers.

Flow templates are declared as YAML in `src/pollypm/work/flows/` (built-in) or under `<project>/.pollypm/flows/` (project override). Gates are small classes implementing the `Gate` protocol; evaluation is run automatically at `claim` and explicitly via `validate_advance`.

Touch this module when adding a new task field, lifecycle transition, or gate. Do not issue ad-hoc SQL — route through the typed methods, and prefer extending `service_*.py` helper modules over expanding `SQLiteWorkService` directly.

## Core Contracts

```python
# src/pollypm/work/service.py
class WorkService(Protocol):
    # Lifecycle
    def create(self, *, title, description, type, project, flow_template,
               roles, priority, acceptance_criteria=None, constraints=None,
               relevant_files=None, labels=None,
               requires_human_review=False) -> Task: ...
    def get(self, task_id: str) -> Task: ...
    def list_tasks(self, *, work_status=None, owner=None, project=None,
                   assignee=None, blocked=None, type=None,
                   limit=None, offset=None) -> list[Task]: ...
    def queue(self, task_id: str, actor: str) -> Task: ...
    def claim(self, task_id: str, actor: str) -> Task: ...
    def next(self, *, agent=None, project=None) -> Task | None: ...
    def update(self, task_id: str, **fields: object) -> Task: ...
    def cancel(self, task_id: str, actor: str, reason: str) -> Task: ...
    def hold(self, task_id: str, actor: str, reason: str | None = None) -> Task: ...
    def resume(self, task_id: str, actor: str) -> Task: ...

    # Flow transitions
    def node_done(self, task_id: str, actor: str, work_output: WorkOutput) -> Task: ...
    def approve(self, task_id: str, actor: str, reason: str | None = None) -> Task: ...
    def reject(self, task_id: str, actor: str, reason: str) -> Task: ...
    def block(self, task_id: str, actor: str, blocker_task_id: str) -> Task: ...
    def validate_advance(self, task_id: str, actor: str) -> list[GateResult]: ...

    # Execution + context
    def get_execution(self, task_id, node_name=None) -> FlowNodeExecution: ...
    def add_context(self, task_id, actor, text: str) -> ContextEntry: ...
    def get_context(self, task_id) -> list[ContextEntry]: ...

    # Links
    def link(self, from_id: str, to_id: str, kind: str) -> None: ...
    def unlink(self, from_id: str, to_id: str, kind: str) -> None: ...
    def dependents(self, task_id: str) -> list[Task]: ...

    # Flows
    def available_flows(self, project: str | None = None) -> list[FlowTemplate]: ...
    def get_flow(self, name: str, project: str | None = None) -> FlowTemplate: ...

    # Sync (file/GitHub)
    def sync_status(self, task_id: str) -> dict[str, object]: ...
    def trigger_sync(self, ...) -> ...: ...

    # Queries
    def state_counts(self, project: str | None = None) -> dict[str, int]: ...
    def my_tasks(self, agent: str) -> list[Task]: ...
    def blocked_tasks(self, project: str | None = None) -> list[Task]: ...

    # Worker sessions
    def ensure_worker_session_schema(self) -> None: ...
    def upsert_worker_session(self, ...) -> WorkerSessionRecord: ...
    def get_worker_session(self, ...) -> WorkerSessionRecord | None: ...
    def list_worker_sessions(self, ...) -> list[WorkerSessionRecord]: ...
    def end_worker_session(self, ...) -> None: ...
    def update_worker_session_tokens(self, ...) -> None: ...
```

Key models in `src/pollypm/work/models.py`:

- `WorkStatus` (8 values): `DRAFT, QUEUED, IN_PROGRESS, BLOCKED, ON_HOLD, REVIEW, DONE, CANCELLED`. `TERMINAL_STATUSES = {DONE, CANCELLED}`.
- `TaskType`: `EPIC, TASK, SUBTASK, BUG, SPIKE`.
- `Priority`: `CRITICAL, HIGH, NORMAL, LOW`.
- `NodeType`: `WORK, REVIEW, TERMINAL`.
- `ActorType`: `ROLE, AGENT, HUMAN, PROJECT_MANAGER`.
- `ExecutionStatus`: `PENDING, ACTIVE, BLOCKED, COMPLETED`.
- `Decision`: `APPROVED, REJECTED`.
- `Task`, `FlowTemplate`, `FlowNode`, `FlowNodeExecution`, `WorkOutput`, `Artifact`, `ContextEntry`, `GateResult`, `Transition`, `WorkerSessionRecord`, `DigestRollupCandidate`.

## File Structure

- `src/pollypm/work/service.py` — the `WorkService` protocol.
- `src/pollypm/work/sqlite_service.py` — `SQLiteWorkService` implementation.
- `src/pollypm/work/schema.py` — `WORK_SCHEMA` (all `work_*` tables).
- `src/pollypm/work/models.py` — dataclasses and enums.
- `src/pollypm/work/flow_engine.py` — YAML flow loading, validation, override chain.
- `src/pollypm/work/gates.py` — `Gate` protocol, `GateRegistry`, `evaluate_gates`, `has_hard_failure`.
- `src/pollypm/work/flows/*.yaml` — built-in flows: `standard.yaml`, `bug.yaml`, `spike.yaml`, `chat.yaml`, `user-review.yaml`.
- `src/pollypm/work/service_queries.py` — pure read helpers (task CRUD, state counts).
- `src/pollypm/work/service_support.py` — error types (`TaskNotFoundError`, `InvalidTransitionError`, `ValidationError`, `WorkServiceError`), id parsing, timestamps.
- `src/pollypm/work/service_transitions.py` / `service_transition_manager.py` — advance / done / transition logic.
- `src/pollypm/work/service_notifications.py` — staged notifications + digest rollup.
- `src/pollypm/work/service_sync.py` — `record_sync_state`, `read_sync_status`, `run_trigger_sync`.
- `src/pollypm/work/service_dependencies.py` / `service_dependency_manager.py` — link kinds, blocked-by cycles.
- `src/pollypm/work/service_worker_sessions.py` / `service_worker_session_manager.py` — per-task worker rows.
- `src/pollypm/work/session_manager.py` — **load-bearing** per-task worker bootstrap (worktree + tmux + prompt + transcript).
- `src/pollypm/work/cli.py` — `pm task` and `pm flow` commands.
- `src/pollypm/work/inbox_cli.py` / `inbox_view.py` — `pm inbox` surface.
- `src/pollypm/work/dashboard.py` — Textual widgets for task list / detail / sessions.
- `src/pollypm/work/sync_file.py` / `sync_github.py` / `sync.py` — sync adapter plumbing.
- `src/pollypm/work/task_assignment.py` — in-process event bus feeding `task_assignment_notify` and `human_notify`.
- `src/pollypm/work/mock_service.py` — in-memory `WorkService` for tests.

## Implementation Details

- **Ids.** Task ids are `<project>/<number>` (e.g. `pollypm/42`). `_parse_task_id` in `service_support.py` is the only sanctioned parser.
- **Transitions.** Lifecycle methods (`queue`, `claim`, `node_done`, `approve`, `reject`, `block`, `cancel`, `hold`, `resume`) are the only way to move `work_status`. `update(**fields)` explicitly cannot change `work_status`.
- **Gate evaluation.** `claim` evaluates gates automatically; the call fails if any *hard* gate fails. Soft gates are advisory. `validate_advance` exposes the same evaluation for callers that want to check before attempting an advance.
- **Flow resolution.** `flow_engine.resolve_flow(name, project)` walks the override chain (`built-in < user-global < project-local`). A project-local YAML with the same name wins.
- **Per-task worker sessions.** `SessionManager` (`work/session_manager.py`) binds one tmux window to one task. On `claim`, it creates a git worktree under `<project>/.pollypm/worktrees/`, opens a tmux window, renders the worker prompt (see [modules/memory.md](memory.md) and [features/agent-personas.md](../features/agent-personas.md)), and records a `WorkerSessionRecord`. On terminal transitions, it tears down the window and archives transcripts. `ProvisionError` is raised and surfaced to `pm task claim` when provisioning truly fails so the user sees an actionable message.
- **Event bus.** `_sync_transition` in `sqlite_service.py` emits `TaskAssignmentEvent` into `pollypm.work.task_assignment`. `plugins_builtin/task_assignment_notify` and `plugins_builtin/human_notify` subscribe.
- **Writer serialization.** All mutations happen on a single-writer connection — concurrency is reader-side only.

## Related Docs

- [features/tasks-and-flows.md](../features/tasks-and-flows.md) — higher-level flow + gates UX.
- [features/inbox-and-notify.md](../features/inbox-and-notify.md) — inbox items bridged from work-service tasks.
- [features/agent-personas.md](../features/agent-personas.md) — per-task worker prompt assembly.
- [plugins/task-assignment-notify.md](../plugins/task-assignment-notify.md) — consumer of the event bus.
- [modules/memory.md](memory.md) — recall injection into worker prompts.
