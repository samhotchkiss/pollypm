# PollyPM Issue Tracker

> **Note**: This document describes the legacy file-based issue tracker.
> The **work service** (`pm task`, `pm flow`) is now the source of truth
> for all task management. The `issues/` folder is maintained as a
> **read-only projection** by the file sync adapter — it mirrors work
> service state for human inspection but is never the authority. See
> `docs/work-service-spec.md` for the current system.

## Source of Truth

The work service SQLite database (`.pollypm/state.db` per project) owns
all task state: status, flow position, execution history, context log,
dependencies, and roles. Agents interact with it via `pm task` CLI
commands, not by moving files.

The `issues/` folder structure is kept in sync automatically so that
`ls issues/01-ready/` still works for quick inspection, but:

- **Never move files manually** — the sync adapter will overwrite your
  changes on the next transition.
- **Never treat file presence/absence as authoritative** — if the sync
  is stale, the work service is correct.

## Folder Mapping (projection only)

The file sync adapter maps work service status to folders:

| Work Status   | Folder             |
|---------------|--------------------|
| draft         | `00-not-ready`     |
| queued        | `01-ready`         |
| in_progress   | `02-in-progress`   |
| blocked       | `02-in-progress`   |
| on_hold       | `00-not-ready`     |
| review        | `03-needs-review`  |
| done          | `05-completed`     |
| cancelled     | `05-completed`     |

## Legacy Role Split

For projects still using the old file-based system (no `.pollypm/state.db`):

- PA owns implementation.
- PM owns review and merge.

PA responsibilities:
- pick the next small issue
- move it to `02-in-progress`
- implement and test it
- move it to `03-needs-review`
- notify PM that review is needed

PM responsibilities:
- move issues to `04-in-review`
- review and validate the work
- request changes or move to `05-completed`
- merge when approval criteria are satisfied

## Guidance

- Keep issues small, testable, and independently shippable.
- Prefer many small issues over a few large ones.
- Keep the project north star visible while executing issue-level work.
- Use PM to review drift, scope creep, and low-value loops.
