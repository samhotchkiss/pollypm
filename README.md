<p align="center">
  <img src="https://img.shields.io/badge/tmux-native-blue?style=flat-square" alt="tmux-native"/>
  <img src="https://img.shields.io/badge/Claude%20%2B%20Codex-multi--provider-purple?style=flat-square" alt="multi-provider"/>
  <img src="https://img.shields.io/badge/can't%20be%20blocked-green?style=flat-square" alt="unblockable"/>
  <img src="https://img.shields.io/badge/tests-404-brightgreen?style=flat-square" alt="404 tests"/>
</p>

<h1 align="center">PollyPM</h1>

<p align="center">
  <strong>An AI project manager that runs your coding agents, keeps them moving, and stays out of the way.</strong>
</p>

---

PollyPM is a management and orchestration layer on top of Claude Code and Codex CLI. It uses the native CLI apps through tmux — interacting no differently from a live human operator. That means it can't be blocked, can't be rate-limited differently than you, and every session is a real terminal you can attach to and take over at any time.

Give it your projects and your subscriptions. It gives you **Polly**, your AI project manager, who watches all your sessions, nudges them forward when there's a clear next step, automatically documents your projects, and manages your usage across multiple Claude and Codex accounts — rotating between subscriptions when you run out of quota and doing extra work when you have quota to spare.

## What You Get

**A project manager that actually manages**
- Polly creates issues, assigns them to workers, reviews the results, and sends work back if it's not good enough
- Workers get nudged when they stall: *"state the remaining task in one sentence, execute the next step now"*
- Your projects get automatically documented — decisions, architecture, risks, ideas — extracted from your agent transcripts

**Supervision that doesn't sleep**
- Heartbeat runs every 60 seconds via cron — works even when you close your laptop lid and come back
- Detects stuck, looping, and crashed sessions. Recovers them automatically with provider failover
- Monitors your interactions and can teach you better ways to instruct your agents

**Multi-provider, multi-account, multi-project**
- Claude and Codex running side by side, each in their own tmux window
- Stack multiple subscriptions — Polly rotates between them based on quota and health
- Manage as many projects as you want, each with isolated workers and git worktrees

**A cockpit, not a cage**
- Textual TUI: navigate with `j`/`k`, mount any session, type directly into Claude or Codex
- You're never locked out. Every agent is a real tmux window. Attach anytime, take the wheel
- The heartbeat backs off when you're working — auto-claims a lease so it won't interfere

## The Architecture Idea

PollyPM is **highly opinionated but highly pluggable**.

It works the way we think is best out of the box. But everything is a plugin. Tell Polly *"I don't want markdown docs anymore, build me a wiki for each project"* and she knows how to create a plugin that still lets the core update while using your functionality on top.

The core primitives are **memory**, **tasks**, and **sessions**. Whether you store your task list in SQLite, GitHub Issues, or files on disk, the task plugin transforms them into a format the TUI can display and the heartbeat can keep moving forward.

| Layer | Built-in | Pluggable |
|-------|----------|-----------|
| **Task tracking** | File-based (6 states) + GitHub Issues (`polly:*` labels) | Any backend that implements 7 methods |
| **Documentation** | Markdown in `docs/` with Haiku extraction | Custom doc backends (wiki, Notion, etc.) |
| **Heartbeat** | Local snapshot + classify + recover | Custom health checks and interventions |
| **Providers** | Claude Code + OpenAI Codex | Any CLI agent that runs in a terminal |
| **Runtime** | Local tmux + Docker | Custom execution environments |
| **Messaging** | File-based inbox with thread state machine | Discord, Telegram, Slack (coming soon) |

## Quick Start

```bash
# Install
uv tool install pollypm

# Onboard your Claude and Codex accounts
pm onboard

# Launch everything — heartbeat, operator, workers
pm up

# Install the heartbeat so it runs even when the cockpit is closed
pm heartbeat install

# Attach to the cockpit
tmux attach -t pollypm
```

Navigate with `j`/`k`. Press `Enter` to mount a session. Press `n` to launch a new worker. Press `s` for settings. Press `Ctrl-W` to detach.

## How the Heartbeat Works

Every 60 seconds, `pm heartbeat` runs a sweep across all your sessions:

```
Capture pane snapshots
    → Classify health (healthy / stuck / needs_followup / blocked / dead)
    → Detect problems (3 identical snapshots = stuck, pane_dead = crashed)
    → Alert (raise/clear in SQLite, notify the operator)
    → Recover (relaunch dead sessions, failover to backup accounts)
    → Nudge (after 5 idle cycles, tell the worker to get moving)
    → Checkpoint (save state for recovery prompts)
```

The heartbeat runs via cron — it doesn't care if your cockpit is open or closed, if you're asleep, or if your terminal crashed. As long as the machine is on, Polly is watching.

## Coming Soon

- **Discord and Telegram integrations** — get notifications, send commands from your phone
- **Web interface** — manage sessions from a browser
- **Apple Voice Notes plugin** — record a voice note on your phone or Apple Watch, Polly figures out what you want done
- **Agent coaching** — Polly watches your interactions and suggests better prompting techniques

## Status

In active daily use managing 5 projects. 16,000+ heartbeat cycles. Dozens of auto-recovered crashes. 404 tests.

## License

MIT
