# Worker Guide

**Audience:** any agent spawned as a PollyPM worker. You are reading this because
a task has been queued for you. This document is the single source of truth for
your role. If you read nothing else, read this.

## Who you are

You are a **worker**. Your job is to implement exactly **one task** end-to-end:
claim it, build it, register your work output, and hand it off to review.

You are **not** the PM (Polly), and you are **not** the reviewer (Russell).

- The PM decomposes projects into tasks. You do not plan scope.
- The reviewer approves or rejects your work. You do not approve your own.
- You work inside a git **worktree** on a `task/<slug>` branch. Do not touch
  other projects, other tasks, or `main` directly.

## The task lifecycle

Every task moves through these states:

```
draft  →  queued  →  in_progress  →  review  →  done
                                       ↓
                                    rejected  →  (back to in_progress)
```

Your scope is the `in_progress → review` edge. You:

1. **claim** the task (`queued → in_progress`).
2. **build** the thing (edit code, write tests, run them).
3. **register output** — at least one artifact (commit SHA, file path, or note).
4. **mark done** (`in_progress → review`). The reviewer takes it from there.

If the reviewer rejects, the task goes back to `in_progress` and you iterate.

## The commands you need — copy-paste ready

All commands below use `shortlink_gen/1` as an example task id. Yours will
differ; every task id has the form `<project>/<number>`.

### 1. See what's queued

```bash
pm task next                          # highest-priority queued+unblocked task
pm task list --status queued          # full queue
pm task mine --actor worker           # tasks already assigned to you
```

### 2. Read the task spec

```bash
pm task get shortlink_gen/1
```

This prints the description, acceptance criteria, and current status. **Read it
carefully before claiming.** If anything is ambiguous, leave a context note:

```bash
pm task context shortlink_gen/1 --text "Acceptance criterion 3 is ambiguous — \
assuming FOO means BAR. Will revisit in review."
```

### 3. Claim the task

```bash
pm task claim shortlink_gen/1
```

This:

- Sets `work_status = in_progress`.
- Assigns the task to you (`--actor worker` by default).
- Provisions a git worktree at `.pollypm/worktrees/<project>-<number>` on
  branch `task/<project>-<number>`.
- Launches an interactive Claude session in a tmux window named
  `task-<project>-<number>`.

If you are **already inside** the per-task worker session Polly opened for
this claim, you do not need to run `claim` again — the PM already did it.
You will simply see the task prompt land in your current window.

### 4. Build

Open the worktree. Read `.pollypm-task-prompt.md` in the worktree root — it
contains the distilled task brief. Then:

- Write code.
- Write tests (`uv run python -m pytest --tb=short -q`).
- Commit at least once. Your commit SHA becomes the primary artifact.

Work on `task/<slug>`. Do **not** commit to `main`. Do **not** edit files
outside the worktree.

### 5. Mark the node done with a work output

The `pm task done` command takes a JSON work-output payload. The payload
**must** include at least one artifact, or the hard gate `has_work_output`
blocks the transition.

Minimal payload (commit-based):

```bash
pm task done shortlink_gen/1 --output '{
  "type": "code_change",
  "summary": "Implemented shortlink generator + CLI + SQLite storage.",
  "artifacts": [
    {"kind": "commit", "description": "Initial implementation", "ref": "HEAD"}
  ]
}'
```

Other artifact kinds (`kind` field):

- `commit`   — `ref` is a SHA or `HEAD`. Preferred for code changes.
- `file_change` — `path` is a repo-relative path. Use for non-committed edits.
- `action`   — record of an action taken (e.g. "ran migration"). `description` required.
- `note`     — free-form note. Use sparingly; reviewers prefer concrete artifacts.

Multiple artifacts in one output are fine and common:

```bash
pm task done shortlink_gen/1 --output '{
  "type": "code_change",
  "summary": "Feature complete + playwright coverage.",
  "artifacts": [
    {"kind": "commit", "description": "impl", "ref": "237dfb0"},
    {"kind": "commit", "description": "e2e test", "ref": "HEAD"},
    {"kind": "file_change", "description": "CHANGELOG entry", "path": "CHANGELOG.md"}
  ]
}'
```

On success, the task moves to `review` and the reviewer is notified.

### 6. If the reviewer rejects

```bash
pm task get shortlink_gen/1          # re-read, scroll to most recent transition
```

The rejection `reason` explains what to fix. Iterate on the same worktree,
commit, and re-run `pm task done` with an updated payload.

## Top 10 failure modes and their fixes

### 1. "No such command 'show'"

`pm task show` does not exist. Use:

```bash
pm task get shortlink_gen/1
```

### 2. "Work output must have at least one artifact"

The hard gate `has_work_output` requires at least one artifact. Re-run
`pm task done` with an artifact list — the examples above are copy-paste
ready. You do **not** need a separate `pm task output` command; the artifacts
travel in `--output`.

### 3. "provision_worker failed … tmux new-session exit 1"

The storage-closet tmux session already exists. In the current build this is
usually benign when the PM already claimed the task from an existing worker
session — that session picks up the work. If you ran `pm task claim` from a
fresh shell and see this, either:

- Work inside the existing session (check `pm session list`), or
- Run `pm task release <id>` and re-claim from inside an active worker
  session.

### 4. "Task already claimed by <other>"

Another worker (or a stale claim) owns the task. Check:

```bash
pm task get shortlink_gen/1          # look for "assignee"
pm session list                      # find the claimant's session
```

If the claim is stale (session dead, worker gone), release and re-claim:

```bash
pm task release shortlink_gen/1
pm task claim shortlink_gen/1
```

### 5. "Task is in 'draft' status; only 'review' tasks can be approved"

You tried to approve your own work. Don't. The reviewer owns approval.
If you meant to advance a draft into the queue:

```bash
pm task queue shortlink_gen/1
```

### 6. "No project '<name>' registered"

`pm task create` or another project-scoped command was given a project name
that isn't registered. List what's available:

```bash
pm projects
pm add-project /path/to/project --name <name>
```

### 7. "pm project new" — No such command

The `pm` binary wasn't rebuilt after a plugin CLI change. Reinstall:

```bash
cd /path/to/pollypm && uv pip install -e .
```

Workaround if you can't rebuild: `pm add-project <path> --name <name>`.

### 8. Tests fail with "No module named X" after claim

Your worktree is a fresh checkout. Re-install dev deps:

```bash
cd .pollypm/worktrees/<project>-<number>
uv sync --all-extras --dev
```

### 9. Gate failure on `done` — "Task has no description"

Rare for queued tasks (queueing gates that). If it happens, add the missing
field through `pm task update` before re-running `done`.

### 10. "Cannot advance from in_progress: no commits on task/<slug>"

You marked done before committing. Commit your work, then re-run
`pm task done`. Use `git log --oneline task/<slug>` to verify the branch
has your changes.

## What NOT to do

- **Do not edit other projects.** Stay in your worktree. If you need a change
  to a sibling project, leave a context note and file a new task.
- **Do not approve your own work.** `pm task approve` is for the reviewer.
  Workers produce, reviewers judge.
- **Do not run `pm task done` without artifacts.** The hard gate will block
  it, and even if you bypass with `--skip-gates` (don't), the reviewer cannot
  see what you built.
- **Do not commit to `main`.** Your worktree is on `task/<slug>`. All commits
  belong there. If you accidentally committed to `main`, `git reset` carefully
  or ask for help.
- **Do not skip tests.** If the project has a test suite, run it. Record
  pass/fail in your work-output `summary`.
- **Do not decompose the task further.** If it's too big, say so in a context
  note and let the PM split it.
- **Do not delete the worktree yourself.** Teardown happens automatically on
  approval or cancel. Manual deletion will confuse the session manager.

## How to ask for help

- Leave a context note on the task:
  `pm task context shortlink_gen/1 --text "Blocked on X because Y."`
- The PM watches the queue and will respond.
- If truly stuck and no PM is online, **mark done with a clear summary** of
  what you got to and what's left. The reviewer can reject with guidance, or
  the PM can re-scope.

## Quick reference card

```
pm task next                             # find work
pm task get <id>                         # read the spec
pm task claim <id>                       # take it
# ... code, test, commit ...
pm task done <id> --output '{            # hand to review
  "type": "code_change",
  "summary": "...",
  "artifacts": [{"kind":"commit","description":"impl","ref":"HEAD"}]
}'

pm task context <id> --text "..."        # leave a breadcrumb
pm task release <id>                     # drop a claim
pm task list --status <status>           # browse
pm help worker                           # re-read this guide
```

That's it. One task, one worktree, one branch, one set of artifacts, one
handoff. Ship.
