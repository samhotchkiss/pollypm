# Project: Team Standup

An async daily standup app for remote teams.

## What I Want

My team is remote and we hate synchronous standups. I want an app where
each person posts their update (yesterday, today, blockers) whenever they
want during the day. Everyone can see each other's updates in real-time
as they come in.

At a configured time each day, the app emails a formatted digest to the
whole team with everyone's updates, who's blocked, and who hasn't posted yet.

The live feed should update without page refresh — when someone posts their
standup, it should just appear for everyone else who has the page open.
Show a "waiting for..." section for people who haven't posted yet.

Members join a team via invite link (no passwords, just link-based access
with session cookies). Each member has a name, email, and timezone.

I want HTMX for the interactive bits, not a full JavaScript framework.

Seed it with a fake team of 5 people and 30 days of realistic standup
history so the analytics page has data.

## Stack
- FastAPI with WebSocket support
- Jinja2 + HTMX for interactive UI
- SQLite with aiosqlite (async)
- SMTP for email digests
- APScheduler for the daily job

## Directory
`/Users/sam/dev/team-standup`

## Challenge
Real-time WebSocket communication, HTMX interactive patterns, scheduled
background jobs, email sending via SMTP, timezone-aware date handling.
Async-first architecture throughout.
