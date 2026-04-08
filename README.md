# PollyPM

PollyPM is a tmux-first supervisor for interactive CLI agent sessions.

The core design goal is simple:

- every agent runs natively inside its own tmux window
- PollyPM can monitor and assist those sessions
- the operator can always drop into any window and take over directly
- provider support is adapter-based so Claude, Codex, and future CLIs can coexist

## Current scope

This repository currently provides:

- an architecture and MVP plan in [`docs/architecture.md`](/Users/sam/dev/promptmaster/docs/architecture.md)
- a Python CLI scaffold
- a config model for accounts and sessions
- provider adapters for `claude` and `codex`
- a local runtime adapter that isolates accounts with provider-native profile env vars
- tmux bootstrap commands for PollyPM and worker windows
- SQLite-backed launch/event bookkeeping
- Docker runtime command generation for provider sessions

## Quick start

Create an initial config:

```bash
uv run pm onboard
```

The guided onboarding now asks only for:

- how many Codex accounts you want to connect
- how many Claude accounts you want to connect
- which connected account should run PollyPM
- whether PollyPM should fail over to another connected account if that account is exhausted
- whether it should scan your home directory for git repos and register them as known projects

For each account, PollyPM opens a temporary tmux login window, you complete the real provider login there, and PollyPM detects the connected identity from that isolated account home. The local runtime uses provider-native profile isolation when available, including `CLAUDE_CONFIG_DIR` for Claude and `CODEX_HOME` for Codex. If a provider does not expose enough information for automatic detection, onboarding falls back to asking for the email once.

At the end of onboarding, PollyPM also installs global `promptmaster` and `pm` commands with `uv tool install -e`, so you can launch it from any shell instead of only from the repo directory.

PollyPM defaults new project worktrees under `~/dev`, but it can register repositories from anywhere on disk. During onboarding, it can scan your home directory for git repos and ask which ones should be tracked as known projects.

If onboarding is interrupted, rerunning `pm onboard` will recover already-connected account homes and continue from there instead of forcing a full restart.

If you just want the static example instead of the guided flow:

```bash
uv run pm init
```

Check the environment:

```bash
uv run pm doctor
```

Inspect and manage connected accounts:

```bash
uv run pm accounts
uv run pm account-doctor
uv run pm accounts-ui
uv run pm relogin codex_s_swh_me
uv run pm add-account codex
uv run pm remove-account codex_s_swh_me
```

Inspect and manage known projects:

```bash
uv run pm projects
uv run pm scan-projects
uv run pm add-project ~/dev/wire
```

Start PollyPM:

```bash
promptmaster up
```

`pm up` does two things:

- if tmux session `promptmaster` already exists, it attaches to it
- if it does not exist, it creates the session and then attaches
- it opens or focuses the `pm-control` TUI window so you land in the main control surface

The standard tmux layout is:

- window `0`: `pm-heartbeat`
- window `1`: `pm-operator`
- window `2`: `pm-control` TUI

The `heartbeat` and `operator` windows are launched as real interactive `claude` or `codex` sessions, not `--print` jobs.

Most normal operation should now happen from the `pm-control` TUI. From there you can:

- inspect accounts, sessions, projects, alerts, and recent events
- add, remove, and relogin accounts
- set the controller account and failover accounts
- scan for git repos and register projects
- create, launch, stop, and remove worker sessions
- claim/release leases and send input to sessions

If you want to open the TUI directly from a shell:

```bash
promptmaster ui
```

Operational CLI commands are still available from inside the `promptmaster` tmux session:

```bash
uv run pm plan
uv run pm status
```

If you use the CLI for debugging or scripting, create an extra shell window inside the `promptmaster` tmux session and run `pm` there.

Run a heartbeat sweep:

```bash
uv run pm heartbeat
```

Inspect recent events and alerts:

```bash
uv run pm events
uv run pm alerts
```

Claim or release control of a worker:

```bash
uv run pm claim worker_demo --owner human --note "manual intervention"
uv run pm release worker_demo
```

These worker commands apply after you add actual worker sessions to the config.

Send text into a session pane:

```bash
uv run pm send worker_demo "Continue with the next implementation step."
```

## Notes

- The local runtime is the default path today and uses per-account `CODEX_HOME` / `CLAUDE_CONFIG_DIR` plus isolated home directories.
- Docker runtime wrapping is implemented, but you still need to supply a suitable agent image per account before using it in production.
- PollyPM treats tmux as the operator cockpit, not the only source of truth.
- Recovery is based on logs, git state, and checkpoints, not exact hidden model session state.
- The intended startup path is `pm up`; PollyPM rejects operational commands outside the `promptmaster` tmux session.
