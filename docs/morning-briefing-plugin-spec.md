# Morning Briefing Plugin Specification

**Status:** v1 shipped. This spec describes the built-in morning briefing plugin that is already live in PollyPM.
**Implementation:** Entry module: `src/pollypm/plugins_builtin/morning_briefing/plugin.py`
**Depends on:** work service, inbox view, roster/jobs APIs, advisor plugin (optional - briefing can include advisor insights if present).

## 1. Purpose

One message per day in your inbox at 6 a.m. local time, summarizing what shipped yesterday and what matters today. No cross-project chatter during the day — just the morning snapshot.

Supersedes #84 (marked obsolete elsewhere). Keep v1 deliberately simple; expand from real use.

## 2. Plugin containment

```
plugins_builtin/morning_briefing/
  pollypm-plugin.toml
  plugin.py
  profiles/
    herald.md                   # concise, organizing, forward-looking persona
  flows/
    morning_briefing.yaml       # single-node flow → emits inbox message
  handlers/
    briefing_tick.py            # @every 1h; gate on "is it 6 a.m. local & unbriefed"
    gather_yesterday.py         # cross-project data gathering
    identify_priorities.py      # today's top priorities
    synthesize.py               # delegates to herald session
```

## 3. Trigger mechanism

Roster entry fires hourly. The handler checks: "Is it briefing time in user's local timezone, and haven't we briefed today?"

```python
api.roster.register_recurring(
    schedule="@every 1h",
    handler_name="briefing.tick",
    payload={},
    dedupe_key="briefing.tick",
)
```

Handler:
1. Read user's timezone from config (fallback to system TZ).
2. Compute `now_local`.
3. If `now_local.hour != config.briefing.hour` (default 6), skip.
4. If `state.last_briefing_date == now_local.date()`, skip (already briefed).
5. Check `[briefing].enabled` (default true). If false, skip.
6. Gather data (§4), synthesize (§5), emit inbox message (§6).
7. Set `state.last_briefing_date = now_local.date()` so we don't re-fire.

Hourly tick + date-gate = exactly-once-per-day, resilient to restarts and missed hours (if the 6 a.m. tick was missed for any reason, the 7 a.m. tick fires the briefing late).

**Current implementation note (2026-04-26):** the roster entry is the only
source of scheduled briefings. Dashboard rendering is read-only with respect to
this plugin: it displays an already-published briefing but does not import the
plugin or opportunistically fire one while rendering.

## 4. Data gathering (cross-project)

Window: yesterday 00:00 to 23:59 local time.

For **each tracked project**:
- `git log --since="yesterday 00:00" --until="yesterday 23:59"` → commit count + headline per commit.
- Work service query: tasks that transitioned state yesterday (completed, approved, cancelled, rejected, started).
- Advisor log (`.pollypm-state/advisor-log.jsonl`): insights emitted yesterday.
- Downtime artifacts: tasks that reached `awaiting_approval` yesterday.

For **today's priorities**:
- Top-N tasks across all projects, sorted by (priority desc, stale-in-current-state desc). Default N=5.
- Blockers: tasks in `blocked` state with their blocker references.
- Awaiting approval: tasks with `kind ∈ {advisor_insight, downtime_result, plan_approval}` currently in the inbox.

**Audit-log signal gate.** Before running the heavier per-project git and SQL
probes, the current gatherer checks the project's audit log. A tracked project
is included when it has any non-`heartbeat.tick` audit event in the briefing
window; heartbeat-only or empty logs are skipped so dormant tracked projects do
not produce empty sections. Operators can bypass the gate with
`POLLYPM_BRIEFING_INCLUDE_ALL=1`, and internal callers/tests can pass
`include_quiet_projects=True`. See [Audit Log](audit-log.md) for the event
schema and the signal-gate details.

## 5. Synthesis

Herald session (short-lived worker, 5-min budget) receives:
- Yesterday's structured data (commit list, task transitions, insights, downtime).
- Today's priority list.
- Last 3 briefings (trajectory — so the herald doesn't repeat yesterday's framing identically).

Herald prompt:
- Concise, scannable, forward-looking. Not a commit log dump — a narrative.
- Target length: 200–400 words. Hard ceiling 600.
- Structure:
  - **Yesterday** (2–4 sentences)
  - **Today's priorities** (bulleted, top 5 max)
  - **Watch** (optional, 0–2 bullets — things trending wrong, blockers that need attention, awaiting approvals that have sat for >24h)

Herald tone: morning-coffee-briefing, not status-meeting. Direct, organized, no filler.

## 6. Inbox emission

Single inbox entry per day, kind=`morning_briefing`. Body is the herald's synthesized text. User actions:
- Read it → auto-close after 24h (next briefing supersedes).
- `pm inbox reply <id> "..."` to note follow-ups against it.

The briefing is a read-only informational message; no approve/reject semantics.

## 7. Settings

`pollypm.toml`:

```toml
[briefing]
enabled = true                 # default
hour = 6                       # 24h local hour
timezone = "America/New_York"  # override system TZ
priorities_count = 5           # top-N tasks to surface
```

CLI:
- `pm briefing now` — force-fire the briefing immediately (for testing / manual trigger).
- `pm briefing disable` / `enable`.
- `pm briefing status` — last briefing date, next scheduled time.
- `pm briefing preview` — run the full gather + synthesize path without writing to inbox. Prints to stdout.

## 8. Failure modes

- **Herald session times out or errors.** Fall back to a structured, non-narrative briefing generated without LLM (just the structured yesterday + priorities lists). Inbox still receives something; user sees "(generated without synthesis — check logs)."
- **No projects tracked.** Briefing says: "No projects yet. Use `pm project new` to get started." Once per install.
- **No activity yesterday.** Briefing still fires with: "Quiet day yesterday. Today's priorities:" — don't skip. Silence is harder to reason about than a "nothing happened" note.
- **User on vacation (no activity for 7+ days).** Briefing enters "quiet mode" — one briefing per week instead of daily, until activity resumes. Configurable via `[briefing].quiet_mode_after_days` (default 7).

## 9. Expanding from here (post-v1, explicitly deferred)

- Voice briefing (audio generation).
- Per-project briefings (opt-in).
- Delivery channels beyond inbox (email, Slack).
- Evening wrap-up briefing.
- User-customizable prompt.
- Mark priorities for the day from within the briefing ("pin these three as today's focus").

## 10. Implementation roadmap (mb01–mb05)

1. **mb01** — Plugin skeleton, herald persona, roster tick with 6-a.m.-local gate, date-dedupe state.
2. **mb02** — Data gathering: yesterday's activity + today's priorities.
3. **mb03** — Synthesis flow + herald session + fallback structured briefing on failure.
4. **mb04** — Inbox emission + quiet-mode for long silences.
5. **mb05** — CLI (`pm briefing now/preview/status/enable/disable`) + `[briefing]` config.
