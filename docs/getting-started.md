# Getting Started with PollyPM

PollyPM is a tmux-first control plane for running multiple AI coding agents in parallel. You talk to a PM agent named Polly; she creates tasks, spawns workers in isolated git worktrees, and routes their output through a reviewer before handing it back to you.

This guide gets you from zero to a first completed task in under 15 minutes.

Use the docs in this order:

- [README.md](../README.md) for the high-level module map and architecture.
- `docs/getting-started.md` for the install and first-run walkthrough.
- [docs/project-overview.md](project-overview.md) only when you want the deeper generated background and historical context.

## Install

PollyPM needs Python 3.13+, `tmux`, `git`, and at least one of the `claude` or `codex` CLIs on your PATH. Install those first if you don't have them.

```bash
# Clone the repo wherever you keep code
git clone https://github.com/samhotchkiss/pollypm ~/dev/pollypm
cd ~/dev/pollypm

# Editable install so `git pull` upgrades you in place
uv pip install -e .
```

After this, `pm` and `pollypm` are on your PATH. They're the same command.

## First run

Verify your environment:

```bash
pm doctor
```

You should see JSON reporting that `tmux`, `claude` (or `codex`), and optionally `docker` are found. Run `pm doctor` — it reports tmux, git, provider auth, and storage health. Fix anything that comes back red before continuing.

Now launch PollyPM:

```bash
pm
```

On a fresh machine, `pm` runs onboarding: it writes a default config to `~/.pollypm/config.toml`, walks you through your first account login, and creates the PollyPM tmux session. When it finishes, you're attached to the cockpit — a tmux layout with Polly on one side and a project/session rail on the other.

If you're already inside tmux when you run `pm`, PollyPM prints the attach command instead of grabbing your client:

```
PollyPM is running. Attach with: tmux switch-client -t pollypm
```

## Add your first account

Accounts are how PollyPM authenticates to Claude Code or Codex CLI. Onboarding adds one for you; to add more later:

```bash
pm add-account claude       # Claude Code — anthropic.com auth
pm add-account codex        # Codex CLI — openai.com auth
```

Each runs the provider's native login flow (browser OAuth for Claude, API key or OAuth for Codex) in an isolated profile directory so accounts don't step on each other.

**Which to pick?** If you have a Claude subscription, start with `claude` — PollyPM's default profiles are tuned for it. Add `codex` later if you want a second provider for failover or cost-splitting.

To check what's configured:

```bash
pm accounts
```

## Add your first project

A "project" in PollyPM is a registered git repository with a `.pollypm/` directory for per-project state (task DB, worktrees, logs). Register one two ways:

```bash
# Register an existing repo without planning
pm add-project ~/dev/my-app

# Or register + kick off the architecture planner in one go
pm project new ~/dev/my-app
```

`pm add-project` is the lightweight path — it scaffolds `.pollypm/`, imports git and transcript history, and returns. `pm project new` does the same and then prompts to run the planner, which produces `docs/project-plan.md` and a prioritized task backlog.

List registered projects:

```bash
pm projects
```

## Ask Polly to do something

Polly lives in the cockpit's operator pane. Attach to PollyPM (`pm` or `tmux attach -t pollypm`), focus Polly's pane, and type your request. For this walkthrough, use a small, real task:

> Build a markdown-to-HTML CLI tool in the `my-app` project. Single `md2html` entrypoint that reads a `.md` file and writes a `.html` file. Include one Playwright E2E test that renders a sample file and checks the output HTML in a headless browser.

Here's what should happen, in order:

1. **Polly responds with a plan.** She'll summarize the work, confirm the project, and create a task via `pm task create`. You'll see something like: *"Created task my_app/1 — Build md2html CLI. Queueing now."*

2. **A worker session appears in the rail.** PollyPM spawns a dedicated worker in a git worktree on a `task/<slug>` branch. The new session shows up in the cockpit rail on the left.

3. **The worker claims the task and starts working.** Press `enter` on the worker row in the rail to open it in a full pane. You'll see the worker reading the spec (`pm task get my_app/1`), claiming it (`pm task claim my_app/1`), then editing files.

4. **The worker signals done.** When the code compiles and the Playwright test passes, the worker runs `pm task done my_app/1 --output '...'` and the task transitions to `review`.

5. **The reviewer (Russell) takes over.** Russell runs tests, checks git, and either approves or rejects. Approval moves the task to `done`; rejection sends it back to the worker with feedback.

6. **Polly notifies you.** When the task lands in `done`, Polly posts a summary to your inbox with file paths, commit SHA, and how to verify.

Watch it all happen from outside the cockpit:

```bash
pm activity --follow         # tail the live event stream
pm task list --project my_app
pm task status my_app/1
```

Inside the cockpit, press `r` to refresh the rail, `enter` to drill into a row, and `ctrl+w` to detach.

## Respond to inbox messages

When Polly needs you — approval on a plan, a judgment call, a completed task — she writes to your inbox. Check it:

```bash
pm inbox
```

This lists tasks waiting on you. View one:

```bash
pm inbox show my_app/1
```

Inside the cockpit, the inbox has its own pane; press `enter` on an inbox row to open the full thread. To approve or reject a task under review from the CLI:

```bash
pm task approve my_app/1 --actor user
pm task reject my_app/1 --actor user --reason "Need integration test for empty input"
```

Rejection sends the task back to `in_progress` with your feedback attached — the worker picks it up again automatically.

## Where to go next

- **[README.md](../README.md)** — architecture map, module boundaries, and the current system shape.
- **[docs/worker-guide.md](worker-guide.md)** — the worker lifecycle, `pm task` commands, and the output format workers use to hand off.
- **[docs/planner-plugin-spec.md](planner-plugin-spec.md)** — how `pm project plan` decomposes work through an architect + 5-critic pipeline.
- **[docs/downtime-plugin-spec.md](downtime-plugin-spec.md)** — the downtime plugin for scheduled background work.
- **[docs/advisor-plugin-spec.md](advisor-plugin-spec.md)** — the advisor plugin that reviews in-flight tasks against your goals.
- **[docs/morning-briefing-plugin-spec.md](morning-briefing-plugin-spec.md)** — the daily delta-since-last-look report.
- **`pm activity --help`** — filtering and following the live event stream.
- **`pm memory --help`** — the memory plugin for long-term context.
- **[docs/plugin-authoring.md](plugin-authoring.md)** and **[docs/plugin-discovery-spec.md](plugin-discovery-spec.md)** — author your own provider, runtime, or profile plugin.
- **[docs/magic-skills-catalog.md](magic-skills-catalog.md)** — the starter catalog of reusable agent skills.
- **[docs/project-overview.md](project-overview.md)** — the long-form generated project context after you already know your way around.

If something doesn't match what you're seeing, run `pm debug` for a one-screen snapshot of sessions, alerts, and recent events — that's the right thing to paste into a bug report.
