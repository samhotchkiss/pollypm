# Session Management

PollyPM runs agents in tmux windows inside a "storage closet" session. The cockpit UI mounts one session at a time into its right pane for direct interaction.

## Session Types

- **heartbeat** (Claude) — runs every 60s via cron, monitors all sessions, detects stuck/dead/looping sessions, auto-recovers crashes, nudges stalled workers
- **operator / Polly** (Claude) — project manager. Triages inbox, creates issues, assigns work to workers, reviews completed work
- **workers** (Claude or Codex) — one per project. Execute assigned tasks: read code, write code, run tests, commit

## Starting and Stopping

```bash
pm up                           # launch everything
pm reset --force                # kill everything, clean state
pm worker-start <project_key>   # launch/relaunch one worker
```

## Assigning Work

Use the task system to assign work to workers:

```bash
pm task create "Title" -p <project> -d "Description" -f standard -r worker=worker -r reviewer=russell
pm task queue <project>/<number>
```

The heartbeat nudges idle workers to claim queued tasks automatically.

## Recovery Pipeline

When the heartbeat detects a dead or stuck session:
1. Raise an alert (warn or error severity)
2. Check if a human holds a lease (defer if so)
3. Rate limit (max 5 attempts per 30-minute window, hard stop at 20 total)
4. Build candidate accounts (same provider first, then cross-provider failover)
5. Restart the session with the best available account
6. Clear alerts on success

## Leases

Leases prevent the heartbeat from interfering while a human is typing:
- The cockpit auto-claims a lease when mounting a session
- Leases auto-expire after 30 minutes
- `pm claim` / `pm release` for manual control
