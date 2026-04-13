# Architecture

## Summary

System design, components, boundaries, data flow, and dependencies.

## Architecture

- Worker-based async pattern with FOUR workers: worker_pollypm, worker_pollypm_website, worker_otter_camp, operator (continuously monitored via heartbeat)
- Role-based access control system restricting tool availability per role (Heartbeat: Bash only; Codex: read-only sandbox; Operator: Bash allowed)
- Heartbeat sweep system for snapshots and memory extraction with idle detection — OPERATIONAL; alerts fire every ~50–61 seconds per worker
- Issue state machine with mandatory review gate: 03-needs-review → 04-in-review → 05-completed (0036/0037 blocked at test failures; actual status IN PROGRESS)
- Tmux-first control plane with live cockpit visibility and automated multi-project analysis pipeline
- Multi-project support FULLY OPERATIONAL: PollyPM chunks 25-28 and 'news'/Extemp chunks 13-14 processed concurrently with <60-second latency
- **Response filter operates in SUSTAINED BYPASS MODE**: 25+ minutes concurrent operation; tool execution unblocked across project boundaries

*Last updated: 2026-04-13T01:22:08.957292Z*
