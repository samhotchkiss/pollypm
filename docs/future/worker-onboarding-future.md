# Future Worker Onboarding Ideas

**Status:** future work only. This page is not a description of shipped
behavior.

This document preserves the forward-looking material that used to live in
`docs/worker-onboarding-and-errors-spec.md`, while keeping the current shipped
behavior documented separately in
[`../worker-onboarding.md`](../worker-onboarding.md).

## 1. Purpose

Two intertwined goals:

1. **Workers should rarely have to guess.** They should boot knowing how the
   task lifecycle works — claim, build, register artifacts, mark done. A worker
   stumbling through `pm task show` → `pm task get` → `pm task done` →
   "artifact required" is wasted tokens and delayed delivery.
2. **Every error should carry the fix.** A worker who sees "Work output must
   have at least one artifact" shouldn't have to search for how to register
   one. The error message itself should say
   `Run \`pm task output <id> --file <path>\` to register an artifact, then retry.`

Both cost little to ship well and compound every subsequent worker session.

## 2. What the E2E revealed

Captured failures the current product inflicts on workers:

| Command | Error | Worker had to | Better behavior |
|---|---|---|---|
| `pm task show` | `No such command 'show'` | Guess `pm task --help`, then try `get` | Suggest: "Did you mean `pm task get`?" |
| `pm project new` | `No such command 'project'` | Fall back to `pm add-project` | Either make it work or say "Rebuild required: `uv pip install -e .`" |
| `pm task claim` | `provision_worker failed ... tmux new-session exit 1` | Ignore — DB claim succeeded, hope for the best | Distinguish "session exists" from "claim failed"; say what to do |
| `pm task done` | `Work output must have at least one artifact` | Re-read flow spec, discover `pm task output` | Error points at `pm task output --help` with example |

Each stuck moment is ~30s of worker time. Across a day, that's real budget.

## 3. Worker onboarding guide enhancements

### 3.1 The guide itself — `docs/worker-guide.md`

A single canonical document covering:

- **Who you are.** You are a worker. Your job is to implement one task.
- **Task lifecycle you'll see.** claim → build → output → done → (review by Russell) → approve or reject.
- **Every command you need.** With worked examples, copy-paste ready.
- **What to do when stuck.** The top 10 failure modes and their fixes.
- **What *not* to do.** Don't edit other projects. Don't try to approve your own work. Don't mark done without artifacts.

~200 lines, markdown, opinionated. Single source of truth.

### 3.2 Auto-injection into worker sessions

When a worker session starts, the guide is injected into its system prompt
under a "Worker Protocol" section — same mechanism M05 uses for memory
injection. This means every new worker session boots *knowing* the lifecycle.

Implementation idea: extend the session-prompt builder to include the
worker-guide text when `session.role == "worker"`.

### 3.3 `pm help worker` meta-command

CLI-level entry point: `pm help worker` renders the guide (same content). Also
`pm help pm` (for PM sessions), `pm help reviewer`, `pm help user`. Role-scoped
help.

## 4. Error message overhaul

### 4.1 Rules for every error

Every error raised by `pm task`, `pm session`, `pm project`, and related
worker-management commands should answer three questions:

1. **What happened** — one sentence.
2. **Why** — one sentence where the cause is knowable.
3. **What to do** — an exact command to try.

Bad: `Work output must have at least one artifact.`

Good:

```text
Task cannot advance from in_progress to review: no artifacts have been recorded.

Register your work with:
    pm task output shortlink_gen/1 --file path/to/output.md --kind note

Or for committed code, use --commit-sha:
    pm task output shortlink_gen/1 --commit-sha HEAD

Then retry:
    pm task done shortlink_gen/1

See `pm task output --help` for other kinds (pr, file_change, action).
```

### 4.2 The target catalog

Examples worth standardizing:

- `pm task done` without output
- `pm task claim` with failing provision
- `pm task show` typo
- `pm project new` missing
- `pm task approve` on a draft task
- `pm task claim` on already-claimed task
- `pm task create` or `pm task claim` on a non-existent project

Each entry is a one-line raise-with-better-message change plus a unit test
asserting the guidance text is present.

### 4.3 Typer "did you mean" at all levels

Typer already does this at the top level (`pm proj` → "Did you mean
'projects'?"). A future pass could audit every Typer app and ensure typo
suggestions are on everywhere.

## 5. CLI help enhancement

Every `--help` output should include a **Worked Examples** section showing the
2-3 most common invocations, copy-paste ready.

For example, `pm task --help` should show a typical worker flow:

```text
pm task next                              # find work
pm task get shortlink_gen/1               # read the spec
pm task claim shortlink_gen/1             # pick it up
# ... build ...
pm task output shortlink_gen/1 --commit-sha HEAD
pm task done shortlink_gen/1              # send to review
```

Per-subcommand `--help` can then show worked examples for that subcommand
specifically.

## 6. Implementation roadmap

Potential issue sequence:

1. **wg01** — Write `docs/worker-guide.md`.
2. **wg02** — Auto-inject worker-guide into worker session prompts.
3. **wg03** — Error-message overhaul with guidance-rich tests.
4. **wg04** — Typer typo-suggestion audit at all subcommand levels.
5. **wg05** — Worked-examples section in every relevant `--help` output.

## 7. Success criteria

If this future track ships well, a fresh worker E2E should show:

- fewer nonexistent commands tried before recovery
- fewer "task done → artifact required → what now" loops
- less stale-claim / session-collision confusion
- more first-attempt success from implementation to review handoff
