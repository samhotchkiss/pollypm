# Worker Onboarding

**Audience:** contributors and maintainers who need to understand the worker
onboarding flow that ships today.

This page documents current behavior only. It is not a roadmap. For planned
follow-up ideas, see
[`docs/future/worker-onboarding-future.md`](future/worker-onboarding-future.md).

## Canonical worker-facing docs

The worker-facing guide is
[`docs/worker-guide.md`](worker-guide.md). That is the document a worker should
read for lifecycle, commands, and troubleshooting.

The same guide is also available through:

- `pm help worker`
- the injected `## Worker Protocol` section for worker sessions

## What ships today

Worker onboarding currently happens through three surfaces:

1. **Role-scoped guide content**
   The worker guide is the long-form, copy-paste-ready document for task
   workers. It explains the task lifecycle, expected commands, output payload
   shape, and common failure modes.

2. **Automatic prompt injection**
   Worker sessions include the guide under a `## Worker Protocol` heading during
   prompt assembly. This means a fresh worker session starts with the same
   operating instructions the CLI exposes via `pm help worker`.

3. **Per-task prompt inside the worktree**
   When `pm task claim <project>/<number>` provisions a worker, the session
   manager creates a real git worktree at
   `.pollypm/worktrees/<project>-<number>` on branch
   `task/<project>-<number>`, writes `.pollypm-task-prompt.md` into that
   worktree, and launches the worker with a kickoff message telling it to read
   that file.

## Current worker flow

At a high level, the shipped worker experience is:

1. A task is claimed with `pm task claim <project>/<number>`.
2. PollyPM provisions the task worktree and worker session.
3. The worker starts in that worktree, with task details written to
   `.pollypm-task-prompt.md`.
4. The worker uses the worker guide plus the task prompt to implement the work,
   commit changes, and submit `pm task done ... --output '{...}'`.

The two documents serve different purposes:

- `docs/worker-guide.md` is the human-readable operating manual.
- `.pollypm-task-prompt.md` is the task-specific brief generated for one worker
  on one task.

## Current error guidance

The current build already gives workers guidance in a few places:

- `docs/worker-guide.md` has a dedicated troubleshooting section for common
  worker-path failures.
- `pm help worker` gives workers a stable way to re-read that guidance from the
  CLI.
- worker provisioning writes a task-specific prompt with an explicit
  `pm task done ... --output` example instead of leaving the completion format
  implicit.

This page intentionally does **not** promise that every CLI error in PollyPM is
fully standardized. It only documents the onboarding and guidance surfaces that
exist now.

## Code paths

The current onboarding flow is implemented in these areas:

- `src/pollypm/cli.py`
  role-scoped `pm help worker`
- `src/pollypm/memory_prompts.py`
  worker-guide prompt injection
- `src/pollypm/work/session_manager.py`
  task worktree creation, `.pollypm-task-prompt.md`, and worker launch kickoff

## Non-goals for this page

This page is not:

- a worker manual
- a future-spec document
- a backlog for error-message improvements

Those concerns now live separately:

- worker instructions: [`docs/worker-guide.md`](worker-guide.md)
- future ideas: [`docs/future/worker-onboarding-future.md`](future/worker-onboarding-future.md)
