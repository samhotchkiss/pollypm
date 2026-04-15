# Task Management

All work is managed through the **work service** via `pm task` and `pm flow` CLI commands.

## Task Lifecycle

```
draft → queued → claimed (in_progress) → node_done → review → approve/reject → done
```

## Creating and Dispatching Work

```bash
# Create a task with description and role assignments
pm task create "Title" -p <project> -d "Description with acceptance criteria" \
  -f standard --priority normal -r worker=worker -r reviewer=russell

# Queue it — makes it available for worker pickup
pm task queue <project>/<number>
```

The heartbeat automatically nudges idle workers when queued tasks are available.

## Monitoring Tasks

```bash
pm task list -p <project>          # all tasks for a project
pm task counts -p <project>        # counts by status
pm task status <project>/<number>  # detailed task with flow state
pm task next -p <project>          # highest-priority queued task
pm task blocked -p <project>       # tasks with unresolved blockers
```

## Reviewing Work

When a task reaches a review node:

```bash
pm task approve <id> --actor polly --reason "looks good"
pm task reject <id> --actor polly --reason "specific feedback"
```

## Other Operations

```bash
pm task hold <id>                  # pause a task
pm task resume <id>                # unpause
pm task cancel <id> --reason "..." # cancel
pm task context <id> "note"        # add progress note
pm task link <from> <to> -k blocks # create dependency
```

## Flows

```bash
pm flow list                       # show available flows
```

- **standard**: implement → code_review → done
- **bug**: reproduce → fix → code_review → done
- **spike**: research → done (no review)
- **user-review**: implement → human_review → done (user must approve)

## File Projection

The `issues/` folder is a read-only mirror of work service state. Task files
appear automatically in state-named subdirectories. Do not move files manually.
