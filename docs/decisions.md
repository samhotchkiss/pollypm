# Decisions

## Summary

Key decisions made during the project, with rationale and context.

## Decisions

- Implement memory system integration with heartbeat sweep mechanism (issue 0033) — COMPLETED
- Use Level 1 checkpoints triggered on issue completion (issue 0032) — COMPLETED
- Enforce role-based tool restrictions on heartbeat and other workers (0034) — COMPLETED
- Lease timeout handling for website worker with auto-release (0035) — COMPLETED
- Enforce full issue state machine: issues must pass through 03-needs-review → 04-in-review before completion (0036) — **IN PROGRESS with 7 failing tests**; uncommitted changes exist
- Implement reopen and request-change flow for issues (issue 0037) — **IN PROGRESS with 7 failing tests**; uncommitted changes exist
- Update system state documentation for architecture visibility (issue 0038) — Status uncertain; doc last updated April 11
- Use Haiku model instead of Opus for extraction/summarization work (cost optimization)
- Worker idle detection: flag standby workers idle >5 heartbeat cycles for manual intervention — IMPLEMENTED AND OPERATIONAL
- Shift to multi-project analysis: After PollyPM core completion, initiate analysis of 'news' project — IN PROGRESS (PollyPM chunks 25-28, 'news' chunks 13-14 processed as of 00:56:58 UTC)
- Enable concurrent multi-project chunk analysis with <60-second inter-chunk latency — CONFIRMED OPERATIONAL: sustained across project boundary transitions

*Last updated: 2026-04-13T01:22:08.957292Z*
