# Worker Onboarding + Error Message Clarity Specification

**Status:** draft target. Informed by live E2E findings at `/tmp/pollypm_e2e_test.md`.

## 1. Purpose

Two intertwined goals:

1. **Workers should rarely have to guess.** They should boot knowing how the task lifecycle works — claim, build, register artifacts, mark done. A worker stumbling through `pm task show` → `pm task get` → `pm task done` → "artifact required" is wasted tokens and delayed delivery.
2. **Every error should carry the fix.** A worker who sees "Work output must have at least one artifact" shouldn't have to search for how to register one. The error message itself should say `Run \`pm task output <id> --file <path>\` to register an artifact, then retry.`

Both cost nothing to ship well and compound every subsequent worker session.

## 2. What the E2E revealed

Captured failures the current product inflicts on workers:

| Command | Error | Worker had to | Better behavior |
|---|---|---|---|
| `pm task show` | `No such command 'show'` | Guess `pm task --help`, then try `get` | Suggest: "Did you mean `pm task get`?" |
| `pm project new` | `No such command 'project'` | Fall back to `pm add-project` | Either make it work or say "Rebuild required: `uv pip install -e .`" |
| `pm task claim` | `provision_worker failed ... tmux new-session exit 1` | Ignore — DB claim succeeded, hope for the best | Distinguish "session exists" from "claim failed"; say what to do |
| `pm task done` | `Work output must have at least one artifact` | Re-read flow spec, discover `pm task output` | Error points at `pm task output --help` with example |

Each stuck moment is ~30s of worker time. Across a day, that's real budget.

## 3. Worker onboarding guide (ships as a plugin + auto-injected)

### 3.1 The guide itself — `docs/worker-guide.md`

A single canonical document covering:

- **Who you are.** You are a worker. Your job is to implement one task.
- **Task lifecycle you'll see.** claim → build → output → done → (review by Russell) → approve or reject.
- **Every command you need.** With worked examples, copy-paste ready.
- **What to do when stuck.** The top 10 failure modes and their fixes.
- **What *not* to do.** Don't edit other projects. Don't try to approve your own work. Don't mark done without artifacts.

~200 lines, markdown, opinionated. Single source of truth.

### 3.2 Auto-injection into worker sessions

When a worker session starts, the guide is injected into its system prompt under a "Worker Protocol" section — same mechanism M05 uses for memory injection. This means every new worker session boots *knowing* the lifecycle.

Implementation: extend the M05 session-prompt builder to include the worker-guide text when `session.role == "worker"`. Budget-scoped (guide ≈ 2K tokens, well under the 4K M05 cap).

### 3.3 `pm help worker` meta-command

CLI-level entry point: `pm help worker` renders the guide (same content). Also `pm help pm` (for PM sessions), `pm help reviewer`, `pm help user`. Role-scoped help.

## 4. Error message overhaul

### 4.1 Rules for every error

Every error raised by `pm task`, `pm session`, `pm project`, and related worker-management commands must answer three questions:

1. **What happened** — one sentence.
2. **Why** — one sentence where the cause is knowable.
3. **What to do** — an exact command to try.

Bad: `Work output must have at least one artifact.`

Good:
```
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

Each of these gets the three-question treatment:

- `pm task done` without output → §4.1 example above.
- `pm task claim` with failing provision → "Claim recorded in DB but session provisioning failed: tmux session `pollypm-storage-closet` already exists / tmux exit <code>. If you're inside an already-running worker session, you're fine — continue working. Otherwise run `pm session list` and attach to the active worker, or ask the PM to retry the claim from the task system."
- `pm task show` typo → Typer-level "Did you mean `pm task get`?" with `--suggest-only` flag disabled by default so it's always on.
- `pm project new` missing → "Command not registered. This usually means the pm binary hasn't been rebuilt since the project_planning plugin shipped. Fix: `cd /path/to/pollypm && uv pip install -e .` then try again. Workaround: `pm add-project <path> --name <name>`."
- `pm task approve` on a draft task → "Task is in `draft` status; only `review` tasks can be approved. Did you mean `pm task queue <id>`?"
- `pm task claim` on already-claimed task → "Task already claimed by <actor> at <time>. Use `pm task get <id>` to see current state, or `pm task release <id>` if the claim is stale."
- `pm task create` or `pm task claim` on a non-existent project → "No project `<name>` registered. Run `pm projects` to see registered projects, or `pm add-project <path>` to register a new one."

Every entry is a one-line code change (raise-with-better-message) plus a unit test asserting the guidance text is present.

### 4.3 Typer "did you mean" at all levels

Typer already does this at the top level (`pm proj` → "Did you mean 'projects'?"). It's inconsistent at sub-levels (`pm task sho` does not suggest `show`/`get`). Audit every Typer app; ensure typo suggestions are on everywhere.

## 5. CLI help enhancement

Every `--help` output should include a **Worked Examples** section showing the 2-3 most common invocations, copy-paste ready.

Currently `pm task --help` shows a table of subcommands. It should also show:

```
Typical worker flow:
    pm task next                              # find work
    pm task get shortlink_gen/1               # read the spec
    pm task claim shortlink_gen/1             # pick it up
    # ... build ...
    pm task output shortlink_gen/1 --commit-sha HEAD
    pm task done shortlink_gen/1              # send to review
```

Per-subcommand `--help` shows worked examples for that subcommand specifically.

## 6. Implementation roadmap

Filed as 5 issues, in order:

1. **wg01** — Write `docs/worker-guide.md` (~200 lines, opinionated).
2. **wg02** — Auto-inject worker-guide into worker session prompts via M05 extension.
3. **wg03** — Error-message overhaul: catalog ~15 common errors, rewrite each per §4.1 rules, unit-test the guidance text. Include the 7 examples from §4.2.
4. **wg04** — Typer typo-suggestion audit at all subcommand levels.
5. **wg05** — Worked-examples section in every `--help` output (top-level `pm --help`, plus per-subgroup and per-command).

Each ~1 commit. The whole track is a day of agent time.

## 7. Success criteria

After this track ships, rerun the E2E with a fresh worker and observe:

- Zero commands the worker tries that don't exist + suggestions when they do.
- Zero "task done → artifact required → what do I do now" cycles.
- Zero stale-claim / session-collision confusion.
- Worker produces artifacts + marks done in first attempt.

Wall-clock-to-first-artifact should drop meaningfully (the current E2E worker spent ~3-5 minutes on tool-calling-to-discover-commands that future workers won't spend).
