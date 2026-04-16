# Live Activity Feed Plugin Specification

**Status:** v1 target. Depends on: work service, session service, state store (events), extensible cockpit rail (#ER01–#ER04).

## 1. Purpose

A real-time view of what all sessions and tasks are doing *right now*, visible as its own panel in the cockpit. Supersedes #85.

## 2. What "activity" means

Every interesting thing that happens in the system emits an event. The activity feed is the projection of that event stream, filtered for human relevance and rendered in reverse-chronological order with live updates.

Event categories:
- **Session** — started, idle, working, stuck, ended.
- **Task** — state transitions (queued → in_progress → review → done), claims, rejections.
- **Worker** — pane created, pane killed, build/test runs, commits produced.
- **Inbox** — message emitted (advisor, downtime, briefing, planner approval).
- **Heartbeat** — notable alerts and recoveries.
- **Plugin** — plugin loaded, plugin errored.

Filtered OUT of the feed (visible only in raw logs):
- Ticks with no decision.
- Health polls that showed nothing changed.
- Internal plumbing noise.

## 3. Plugin containment

```
plugins_builtin/activity_feed/
  pollypm-plugin.toml
  plugin.py
  cockpit/
    feed_panel.py            # Textual widget for the feed
  handlers/
    event_projector.py       # reads events table, projects to feed entries
  templates/
    entries/                 # one markdown template per event-type for rendering
```

## 4. Data source

The state store already tracks events (alerts, transitions). Extend the `work_events` table (or introduce `activity_events` if cleaner) with fields sufficient for rendering:

```
id, timestamp, project, kind, actor, subject, verb, summary, severity, payload_json
```

The existing event-emission points (`record_event` in the supervisor API, work-service transitions, session lifecycle) get a structured-summary field added. Back-compat: existing events without summary render as "kind + actor" fallbacks.

## 5. Feed rendering

Cockpit gets a new left-rail entry "Activity" (via the extensible-rail API, see Extensible Rail spec). Selecting it shows the feed panel:

- Reverse chronological, paginated, 50 per page.
- Each entry: timestamp (relative + hover for absolute), project chip, actor chip, verb, summary.
- Severity drives visual weight (critical red, recommendation yellow, routine neutral).
- Filters in the panel: by project, by kind, by actor, by time window (last hour / last day / all).
- Click an entry → detail view with full payload + links to related task/session.

## 6. Update mechanism

- The feed panel subscribes to new events via the state-epoch monotonic counter (already used by cockpit).
- On tick (cockpit refresh cadence), panel queries events with `id > last_seen_id`.
- New events slide in at the top with a brief highlight.

No polling of raw tmux or git; all activity is event-backed.

## 7. `pm activity` CLI

- `pm activity` — print last N activity entries.
- `pm activity --follow` — stream new entries as they arrive (like `tail -f`).
- `pm activity --project X` / `--kind Y` / `--since 1h` — filter.
- `pm activity --json` — machine-readable.

## 8. Implementation roadmap (lf01–lf05)

1. **lf01** — Plugin skeleton, event-projector handler, `activity_events` schema (or extend `work_events`).
2. **lf02** — Event-emission sites across work service / session service / inbox / heartbeat get the structured-summary field filled in.
3. **lf03** — Cockpit panel Textual widget + rail entry registration (depends on extensible-rail API).
4. **lf04** — Filter UI + detail view in panel.
5. **lf05** — `pm activity` CLI with follow-mode.
