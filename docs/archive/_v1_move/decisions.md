# Decisions

## Summary
- PollyPM is a tmux-first modular monolith: one local system with explicit internal boundaries instead of microservices.
- SQLite-backed state and the work service are the operational source of truth; the cockpit and CLI are views and control surfaces over that state.
- Providers, runtimes, schedulers, heartbeat strategies, and agent profiles stay replaceable through plugin contracts.
- Raw extracted knowledge now belongs under `.pollypm/knowledge/`; this document stays intentionally short and human-readable.

## Decisions
- Use `tmux` as the native execution surface so operators can watch, take over, and recover real sessions instead of opaque background jobs.
- Keep the system as a modular monolith rather than splitting into microservices. Core modules stay in one process, but provider/runtime/storage/session boundaries are explicit and replaceable.
- Treat SQLite-backed state as the shared authority for session health, task flow, inbox/messages, and cached account usage.
- Route implementation through the work service and per-task worker sessions instead of long-lived generic worker shells.
- Keep provider integrations behind adapter contracts so Claude Code, Codex CLI, and future providers can ship independently.
- Keep runtime execution behind runtime adapters so local shell and containerized execution can evolve independently.
- Cache provider usage out of band and read that cache from the UI rather than probing providers inline during cockpit rendering.
- Keep project documentation human-scale. Raw extracted knowledge and noisy machine output belong in `.pollypm/knowledge/`, not in committed front-door docs.

## Notes
This is the compact decision log for contributors and operators. For deeper architecture context, start with [README.md](../README.md), [docs/architecture.md](architecture.md), and [docs/getting-started.md](getting-started.md).
