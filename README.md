<p align="center">
  <img src="https://img.shields.io/badge/tmux-first-blue?style=flat-square" alt="tmux-first"/>
  <img src="https://img.shields.io/badge/agents-Claude%20%2B%20Codex-purple?style=flat-square" alt="Claude + Codex"/>
  <img src="https://img.shields.io/badge/tests-404%20passing-brightgreen?style=flat-square" alt="404 tests"/>
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT"/>
</p>

<h1 align="center">PollyPM</h1>

<p align="center">
  <strong>A tmux-native supervisor for AI coding agents.</strong>
  <br>
  Run Claude and Codex in parallel. Watch every session. Take the wheel anytime.
</p>

---

## What It Does

PollyPM manages multiple AI coding sessions from one control plane. Each agent runs in its own tmux window — a real `claude` or `codex` process, not an abstraction. You get a cockpit UI, a heartbeat supervisor, and a project manager agent that coordinates work across sessions.

```
     You (human)
       │
  ┌────┴────┐
  │ Cockpit │  ← Textual TUI: navigate, mount, inspect
  └────┬────┘
       │
  ┌────┴────────────────────────────────┐
  │         tmux storage closet         │
  │                                     │
  │  heartbeat   operator   worker-1    │
  │  (Claude)    (Polly)    (Codex)     │
  │  monitors    manages    implements  │
  │  recovers    triages    executes    │
  └─────────────────────────────────────┘
```

**Every session is a real terminal.** You can attach to any window and type. The heartbeat watches for stuck, looping, or crashed sessions and recovers them automatically. The operator (Polly) creates issues, assigns work, and reviews results. Workers execute.

## Key Features

**Supervision that actually works**
- Heartbeat runs via cron every 60 seconds — independent of the UI
- Detects stuck sessions (3 identical snapshots), dead panes, auth failures
- Auto-recovers crashed sessions with provider failover
- Hard limit prevents infinite crash loops (20 attempts, then stops)
- Nudges stalled workers directly: *"state the remaining task, execute the next step"*

**Multi-provider, multi-project**
- Claude (Opus, Sonnet, Haiku) and OpenAI Codex (GPT-5.4) side by side
- Isolated account homes with 700 permissions and Keychain auth
- Failover from Claude to Codex when one provider is down
- Git worktrees per worker — no file conflicts between parallel agents

**Project management built in**
- File-based issue tracker (6 states: not-ready → ready → in-progress → review → completed)
- GitHub issue backend (`polly:*` labels, auto-close on completion)
- Inbox with thread state machine (triage → route → resolve → close → reopen)
- Knowledge extraction: Haiku scans transcripts and updates `docs/decisions.md`

**A cockpit you can actually use**
- Textual TUI rail with `j`/`k` navigation, session spinners, alert indicators
- Mount any session in the right pane — type directly into Claude or Codex
- Settings view with account management, relogin, failover config
- Lease system: cockpit auto-claims leases so the heartbeat won't interfere while you're typing

## Quick Start

```bash
# Install
uv tool install pollypm

# Onboard accounts
pm onboard

# Launch everything
pm up

# Install the heartbeat cron (runs even when cockpit is closed)
pm heartbeat install
```

Attach to the cockpit:
```bash
tmux attach -t pollypm
```

Navigate with `j`/`k`, press `Enter` to mount a session, `n` to launch a new worker, `s` for settings.

## Architecture

```
src/pollypm/
├── supervisor.py        # Session lifecycle, recovery, failover
├── cockpit.py           # Pane routing, session mounting
├── cockpit_ui.py        # Textual TUI rail
├── heartbeats/          # Health classification, alerts, nudges
├── schedulers/          # Cron-driven recurring jobs
├── providers/           # Claude + Codex adapters
├── runtimes/            # Local + Docker execution
├── task_backends/       # File-based + GitHub issue trackers
├── knowledge_extract.py # LLM-powered doc extraction
├── recovery_prompt.py   # Checkpoint-based session recovery
├── messaging.py         # Inbox threads + state machine
└── storage/state.py     # SQLite: events, heartbeats, checkpoints, alerts
```

## How the Heartbeat Works

Every 60 seconds (via cron), `pm heartbeat` runs a sweep:

1. **Capture** — snapshot every session's tmux pane
2. **Classify** — healthy, needs_followup, blocked, done (with transcript snippet)
3. **Detect** — stuck (3 identical snapshots), dead panes, auth failures, loops
4. **Alert** — raise/clear alerts in SQLite with severity levels
5. **Recover** — relaunch dead sessions, failover to backup accounts
6. **Nudge** — after 5 idle cycles, send a direct message to stalled workers
7. **Checkpoint** — save state for recovery prompts

```
$ pm heartbeat
Heartbeat completed. Open alerts: 2
- warn worker_pollypm/suspected_loop: same snapshot for 3 heartbeats
- warn worker_website/needs_followup: Additional work remains
```

## Status

PollyPM is in active daily use managing 5 projects with 3 Codex workers, a Claude heartbeat, and a Claude operator. It has survived extended unattended operation, recovered from dozens of session crashes, and processed over 16,000 heartbeat cycles.

**404 tests.** Every fix ships with a regression test.

## License

MIT
