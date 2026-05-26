# Agent Handbook

**Audience:** any autonomous agent driving PollyPM from the outside — Polly
(the operator PM), an architect session, a per-task worker, Russell the
reviewer, an advisor, or a one-shot maintenance script. If you are reading
this because a session just started and you need to drive the CLI without
trial-and-error, you are in the right place.

This page is the discoverable reference for the agent-facing surface. It
exists because earlier agents trial-and-errored through `--help` and fell
through to raw SQL on the workspace DB to inspect tables (see issue
#1629). The fix is: every common verb has a CLI; this page lists them.

The full Typer command tree is also machine-readable — grep
`pm cli-reference --json` into your working context if you need flags this
page does not cover.

## 1. Task lifecycle

A task is a row in the work service. Its `work_status` moves through these
states (from `pollypm.work.models.WorkStatus`):

```
draft  ──►  queued  ──►  in_progress  ──►  review  ──►  done
                            ▲                │
                            └─ rework ◄──────┘  (reviewer rejected)
                            │
                          on_hold  / blocked      (orthogonal pauses)
                            │
                          cancelled                (terminal)
```

| State         | Meaning                                                                                   | What causes the transition into it                                                       |
| ------------- | ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `draft`       | Task exists but is not yet ready for a worker. Acceptance criteria can still be edited.    | `pm task create` (default initial state).                                                |
| `queued`      | Eligible to be claimed by a worker. Roles, flow, and acceptance criteria are frozen here.  | `pm task queue`. If the task has `requires_human_review`, `pm task approve-human-review` is the gate. |
| `in_progress` | A worker has claimed the task; an isolated worktree + tmux session exist for it.           | `pm task claim` (auto-fired by Polly's queue→claim handoff).                              |
| `review`      | The worker has marked the work output complete; awaiting reviewer decision.                | `pm task done --output '{…}'`.                                                            |
| `rework`      | Reviewer rejected the last version. Task is non-terminal — the worker is expected to iterate. | `pm task reject`. Next `pm task claim` transitions it back to `in_progress`.             |
| `blocked`     | Has an unresolved blocker link. Auto-unblocks when the blocker reaches `done`.             | `pm task block` / auto-set when a `blocks` link is added against a non-`done` task.       |
| `on_hold`     | Operator-paused. Keeps state; resumable.                                                  | `pm task hold --reason "…"`. Resume with `pm task resume`.                                |
| `done`        | Terminal. The reviewer approved.                                                          | `pm task approve` (reviewer) after a `review` node.                                       |
| `cancelled`   | Terminal. Operator killed the task.                                                       | `pm task cancel`.                                                                         |

**Terminal states** are `done` and `cancelled` (see
`pollypm.work.models.TERMINAL_STATUSES`). Everything else is mutable.

## 2. Roles

Roles are canonical snake_case keys stored in the `actor` columns of the
work DB. Full spec: `docs/role-contract-spec.md` (`ROLE_REGISTRY`).

| Role key       | Persona  | What it does                                                                                                                      | When to use                                                                          |
| -------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `operator_pm`  | Polly    | The PM. Decomposes goals into tasks, queues work, routes review, escalates to the user. Long-lived session.                        | Anything cross-task or user-facing. Polly is the only role that talks to the human. |
| `architect`    | Archie   | The planner. Owns project structure (the `architect.md` guide), big-picture decomposition, and per-project session memory.         | Use `pm worker-start --role architect <project>` when a project needs deep planning. |
| `worker`       | Worker   | A per-task agent. Provisioned automatically by `pm task claim` inside an isolated git worktree. Tears down on `done`/`cancel`.     | You **do not** start workers manually — let Polly queue+claim, or use `pm task claim`. The `--role worker` flag on `worker-start` is **deprecated** (see anti-patterns). |
| `reviewer`     | Russell  | Approves or rejects worker output. Owns the review-node side of the flow. Runs alongside Polly.                                    | Reviewer transitions: `pm task approve` and `pm task reject`. Don't impersonate.    |
| `advisor`      | (varies) | Real-time alignment coach. Watches recent activity and emits inbox messages only when worth saying. See `docs/advisor-plugin-spec.md`. | Advisor is a plugin, not a queueable task role. Don't queue work to advisor.        |
| `polly`        | Polly    | Alias for the `operator_pm` agent name when assigning roles (e.g. `--role operator_pm=polly`).                                     | Same as `operator_pm`.                                                              |

The valid roles for `--role <role>=<agent>` on `pm task create` (from the
error message body, kept here for grep-ability):
`architect`, `reviewer`, `worker`, `polly`, `russell`, `triage`. **Note**:
`user` is **not** a role — the human is not an autonomous agent.

Architect sessions run from persistent project worktrees. When PollyPM
launches or relaunches an architect, it fast-forwards a clean architect
worktree to local `main`/`master` when possible. If local changes or a
divergent branch make that unsafe, PollyPM leaves the worktree untouched and
writes `.pollypm/architect-worktree-status.md` inside it for the architect to
surface before relying on repository state.

## 3. Canonical CLI commands

All commands are invoked as `pm <verb>` (or `pollypm <verb>` — both
entry points resolve to the same Typer app).

### Inspect (read-only, safe in any loop)

| Command                                 | What it gives you                                                                            |
| --------------------------------------- | -------------------------------------------------------------------------------------------- |
| `pm task get <id>`                      | Full task record: title, status, owner, executions, recent context. The default detail view. |
| `pm task status <id>`                   | Compact pretty-printed summary (node, status, owner, executions, latest context).             |
| `pm task context <id>`                  | The task's `work_context_entries` log, chronological. Replaces `SELECT … FROM work_context_entries`. Add `--limit N`, `--entry-type note`, `--show-internal` as needed. |
| `pm task transitions <id>`              | The task's `work_transitions` log (state changes, who, when). Replaces `SELECT … FROM work_transitions`. |
| `pm task list --status queued`          | Filter tasks by status (queued/in_progress/done/etc). Add `--project <name>` to scope.       |
| `pm task counts [--project <name>]`     | Histogram by status. Quick health check for a project.                                       |
| `pm task next -p <project>`             | The next-claimable task for a project (priority-ordered).                                    |
| `pm inbox --awaits-user`                | Inbox items that need the human. The default lens (per #1573).                               |
| `pm cli-reference --json`               | Full Typer command tree as JSON. Grep this when this page is silent.                         |

Every read command accepts `--json` for machine-readable output. Use it
when piping into other tools or your own parser.

### Create / move (state-changing)

| Command                                                          | Effect                                                                            |
| ---------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| `pm task create "<title>" -p <project>`                          | Creates a `standard`-flow task in `draft`. Auto-adds `worker`+`reviewer` roles (#1637). |
| `pm task create … --flow <name>`                                 | Same, with a non-default flow template. Pass explicit `--role key=value` flags.   |
| `pm task queue <id>`                                             | `draft → queued`. The task is now claimable.                                       |
| `pm task approve-human-review <id> --reason "<text>"`            | For tasks with `requires_human_review`, the human-gate transition into `queued`.   |
| `pm task claim <id>`                                             | `queued → in_progress`. Provisions a per-task worker session. Polly fires this automatically. |
| `pm task done <id> --output '{"type":"code_change","summary":"…","artifacts":[…]}'` | `in_progress → review`. The `--output` JSON is the work-output payload. |
| `pm task approve <id> --actor reviewer`                          | Reviewer side: `review → done`.                                                    |
| `pm task reject <id> --actor reviewer --reason "<text>"`         | Reviewer side: `review → rework`.                                                  |
| `pm task hold <id> --reason "<text>"`                            | Pause without losing state.                                                       |
| `pm task resume <id>`                                            | Un-pause.                                                                         |
| `pm task cancel <id>`                                            | Terminal kill.                                                                    |
| `pm task context <id> "<text>" [--actor <role>]`                 | Append a context entry (when given positional text). With no text, lists instead — see Inspect. |

### Worker / session management

| Command                                            | Effect                                                                                |
| -------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `pm worker-start <project> --role architect`       | Start a long-lived architect session for a project. (For per-task `worker` sessions, just `pm task claim` — see anti-patterns.) |
| `pm worker-stop <session>`                         | Stop a managed session and disable heartbeat recovery for it.                          |
| `pm reset --force`                                 | Kill all PollyPM tmux sessions (cockpit + storage closet). Restart with `pm up`.       |
| `pm up`                                            | Boot or attach to the PollyPM tmux session and the cockpit.                            |
| `pm status`                                        | Session and health overview.                                                          |
| `pm doctor`                                        | Diagnose missing deps / config trouble.                                               |

### Discovery

| Command                       | Effect                                                                                          |
| ----------------------------- | ----------------------------------------------------------------------------------------------- |
| `pm --help`                   | Top-level command list. Sub-help via `pm task --help`, `pm session --help`, etc.                 |
| `pm cli-reference --json`     | The complete machine-readable command tree (commands, subcommands, flags, types, help text). Grep this at session start to populate your working context with every available verb. |
| `pm help worker`              | Role-specific guide (renders `docs/worker-guide.md`).                                            |

## 4. Common errors and fixes

The "did you mean" hints from Click are Levenshtein-distance based and
will occasionally suggest the wrong flag. Use this table as the
authoritative reality.

| You ran / saw                                                                            | What's actually true                                                                                              | Fix                                                                                                                                                |
| ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Error: Required task roles are missing. The 'standard' requires worker, reviewer.`      | Pre-#1637 behavior. The `standard` flow now auto-adds `worker`+`reviewer` when no `--role` is passed.             | Just rerun without the role flags: `pm task create "<title>" -p <project>`. Or explicitly: `--role worker=worker --role reviewer=reviewer`.       |
| `Error: No such option: '--reason'. Did you mean '--json'?` on `pm task queue`           | `task queue` does **not** accept `--reason`. The `--json` suggestion is wrong (distance match, not semantic).      | Queue first, then record context as a separate call: `pm task queue <id>` followed by `pm task context <id> "<reason text>" --actor <your-role>`. |
| `ERROR: column "source" does not exist` (from a raw psql query on `work_context_entries`) | The schema doesn't have a `source` column — the columns are `entry_type`, `actor`, `text`, `task_id`, `timestamp`, etc. | Don't shell into the workspace DB. Use `pm task context <id>` (lists entries) and `pm task transitions <id>` (lists state changes).                          |
| `Required: --role`                                                                       | The non-`standard` flow you picked has required role keys you didn't pass.                                         | Either switch back to `--flow standard` (auto-fills) or pass `--role <key>=<agent>` for each required role. See `pm flow list`.                    |
| `worker-start --role worker` prints DEPRECATED                                           | The managed-worker pattern is gone (memory-leak hazard). Per-task workers are spawned by `pm task claim`.          | Use `pm task next -p <project>` then `pm task claim <id>`. For a long-running planner, use `--role architect`.                                     |
| `WARNING: task claim recorded, but worker session provisioning failed`                   | The DB row claim succeeded; the tmux session did not.                                                              | Either continue inside an existing session, or `pm task hold <id> --reason "provision failed"` and `pm task resume <id>` after fixing.             |
| `pm task done` rejects your JSON                                                         | `--output` must be a JSON object with at least `{"type": …, "summary": …, "artifacts": […]}`.                      | Quote properly. Example: `pm task done foo/1 --output '{"type":"code_change","summary":"shipped X","artifacts":[]}'`.                              |

## 5. Anti-patterns

These are things autonomous agents have repeatedly tried that are wrong.
Avoid them.

- **Don't shell out to `psql` against the workspace DB.** The schema is
  internal and has shifted (column renames, table splits) several times.
  The CLI is the contract; `pm task context`, `pm task transitions`,
  `pm task get --json`, and `pm cli-reference --json` cover the read paths.
- **Don't `pm task approve` from a hot loop.** Reviewer approval is a
  one-shot decision per `review` node. Repeated calls either no-op or
  raise; if you're polling for "is this done yet," read `pm task status`
  or `pm task get --json` instead.
- **Don't bypass `pm reset --force`** by killing tmux sessions directly.
  `pm reset` also stops the rail daemon and reaps orphan cockpit panes
  (#1590) — `tmux kill-session` alone leaves zombies that pile up.
- **Don't impersonate roles.** Pass `--actor` explicitly when you act on
  behalf of a role (`--actor reviewer` for `pm task reject`, `--actor
  worker` for `pm task done`). The work service validates actor strings
  against `ROLE_REGISTRY`; mismatches show up later as drift in the
  audit log.
- **Don't `pm worker-start --role worker`.** Deprecated and rejected at
  the CLI. Workers are per-task and provisioned by `pm task claim`.
- **Don't queue without acceptance criteria on a standard task.** The
  `readiness_warnings` line that `pm task queue` prints isn't decorative
  — it's the worker's brief. A task queued with empty AC will produce
  vague work output and a likely reject.
- **Don't manually claim tasks or dispatch handoffs because the
  heartbeat looks stuck.** When the recovery loop fails, file and fix
  the loop. Manual claims hide the bug. (See the heartbeat-cascade
  model.)

## Quick start: my first turn as an autonomous agent

If you've just been spawned and need to drive PollyPM, run this exact
sequence to populate your working context:

```bash
# 1. Snapshot the full CLI surface — grep this when you need a flag.
pm cli-reference --json > /tmp/pm-cli.json

# 2. See system health and what's waiting on a human.
pm status
pm inbox --awaits-user

# 3. For each project you're touching, see queue depth.
pm task counts --project <project>
pm task list --status in_progress --project <project>

# 4. Before creating a task, decide: standard flow or custom?
#    Standard auto-fills worker+reviewer. Anything else needs explicit roles.
pm flow list
```

Then, for the actual work:

```bash
# Create a task (standard flow — auto-roles).
pm task create "Revive booktalk end-to-end" -p booktalk \
    --acceptance-criteria "First run completes" \
    --acceptance-criteria "Cron is scheduled and visible in pm status"

# Queue it. Record the operator-side reason as a context entry (not a flag).
pm task queue booktalk/40
pm task context booktalk/40 "Operator-created revival; activate when worker available" --actor operator_pm

# Inspect at any time.
pm task status booktalk/40
pm task context booktalk/40
pm task transitions booktalk/40
```

If any command surprises you, **check `pm cli-reference --json` before
guessing**. The contract is the CLI, not the SQL schema, not `--help`
output you remember from a previous session.

---

*Last updated to track #1629 closeout. Wedges that landed:
auto-role default (#1637), `pm task context`/`pm task transitions`
(#1669), `pm cli-reference --json` (#1684), and this page.*
