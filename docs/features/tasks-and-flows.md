**Last Verified:** 2026-04-22

## Summary

Tasks are the unit of work PollyPM routes between a human, Polly, workers, and Russell. They live in the work service's SQLite DB. Each task progresses through an eight-state lifecycle (`draft → queued → in_progress → blocked / on_hold → review → done / cancelled`) and — within `in_progress` / `review` — through the nodes of a **flow template**.

Flow templates are declarative YAML describing a graph of work / review / terminal nodes, each with a role, actor type, and optional budget. `standard`, `bug`, `spike`, `chat`, and `user-review` ship built in; projects can override any of them by dropping a same-named YAML under `<project>/.pollypm/flows/`. **Gates** are small classes that check preconditions at transitions (e.g. "must have at least one commit artifact before claiming review"); hard failures block the advance.

Touch this doc when adding a built-in flow, a new gate, or changing the task lifecycle. Do not document schema details here — that belongs in [modules/work-service.md](../modules/work-service.md).

## Core Contracts

```python
# src/pollypm/work/models.py
class WorkStatus(Enum):
    DRAFT, QUEUED, IN_PROGRESS, BLOCKED, ON_HOLD, REVIEW, DONE, CANCELLED

class NodeType(Enum):
    WORK, REVIEW, TERMINAL

class ActorType(Enum):
    ROLE, AGENT, HUMAN, PROJECT_MANAGER

# src/pollypm/work/flow_engine.py
class FlowValidationError(Exception): ...
def resolve_flow(name: str, project: str | None = None) -> FlowTemplate: ...
def _parse_node(name: str, data: dict) -> FlowNode: ...

# src/pollypm/work/gates.py
@runtime_checkable
class Gate(Protocol):
    name: str
    gate_type: str   # "hard" | "soft"
    def check(self, task: Task, **kwargs) -> GateResult: ...

class GateRegistry:
    def register(self, gate: Gate) -> None: ...
    def get(self, name: str) -> Gate | None: ...

def evaluate_gates(registry: GateRegistry, task: Task, names: list[str], **kwargs) -> list[GateResult]: ...
def has_hard_failure(results: list[GateResult]) -> bool: ...
```

`pm task` commands (see `src/pollypm/work/cli.py`):

```
pm task next [--project ...]          # next claimable task
pm task claim <task_id>               # atomically assign + activate first node
pm task done <task_id> --output ...   # mark the active node done; advance
pm task show <task_id>
pm task list [--status ...] [--project ...] [--json]
pm task create ...
pm task cancel <task_id> --reason ...
pm task hold <task_id> [--reason ...]
pm task resume <task_id>
pm task approve <task_id> [--reason ...]
pm task reject <task_id> --reason ...
pm task block <task_id> --on <blocker_id>
pm task context <task_id> --add "..."
pm task release <task_id>
```

```
pm flow list
pm flow validate <name>
pm flow show <name>
```

## File Structure

- `src/pollypm/work/` — full work service; see [modules/work-service.md](../modules/work-service.md) for the module map.
- `src/pollypm/work/flows/standard.yaml` — default flow: draft → plan → implement → review → done.
- `src/pollypm/work/flows/bug.yaml` — bug-fix flow.
- `src/pollypm/work/flows/spike.yaml` — time-boxed investigation.
- `src/pollypm/work/flows/chat.yaml` — chat-task flow (light).
- `src/pollypm/work/flows/user-review.yaml` — flow whose review gate is a human inbox approval.
- `src/pollypm/work/gates.py` — gate protocol, registry, evaluation helpers.
- `src/pollypm/plugins_builtin/project_planning/gates/` — planner-specific gates.
- `src/pollypm/plugins_builtin/downtime/gates/` — downtime-specific gates.
- `src/pollypm/plugins_builtin/advisor/flows/advisor_review.yaml` — advisor review flow.
- `src/pollypm/plugins_builtin/downtime/flows/downtime_explore.yaml` — exploration flow.
- `src/pollypm/plugins_builtin/morning_briefing/flows/morning_briefing.yaml` — briefing flow.

## Implementation Details

- **Id format.** `<project>/<number>` e.g. `pollypm/42`. Parsed by `_parse_task_id` in `work/service_support.py`.
- **Gate evaluation.** `claim` auto-evaluates gates referenced by the target node. The call fails with `InvalidTransitionError` when a *hard* gate fails. Soft-gate failures are surfaced in `GateResult` but do not block.
- **Flow override chain.** `resolve_flow(name, project)` walks built-in < user-global (`~/.pollypm/flows/`) < project-local (`<project>/.pollypm/flows/`). Same file name in a higher tier wins entirely — flows are not merged.
- **`requires_human_review`.** Set on a task, validated at `queue()`. The human must approve via the inbox before the task can move past draft.
- **Per-task worker sessions.** `claim` triggers `SessionManager.ensure_worker_session` — new git worktree, new tmux window, worker prompt rendered (see [features/agent-personas.md](agent-personas.md)), transcript archived on teardown. Fails loud with `ProvisionError` if worktree or pane creation fails.
- **Review loop.** A rejected review returns the task to `in_progress` at the review node's upstream work node, with the rejection reason appended as a `ContextEntry`. `reviewer.rejection_feedback` helpers format the feedback for the worker.

## Related Docs

- [modules/work-service.md](../modules/work-service.md) — schema + implementation.
- [features/agent-personas.md](agent-personas.md) — worker prompt assembly per claim.
- [features/inbox-and-notify.md](inbox-and-notify.md) — `requires_human_review` approval path.
- [plugins/project-planning.md](../plugins/project-planning.md) — architect + critic flow templates.
