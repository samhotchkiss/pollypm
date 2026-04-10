# V1 Spec Tracking

## Status Legend

| # | Status | Meaning |
|---|--------|---------|
| 1 | Stub | Placeholder — title and rough outline only |
| 2 | Initial Draft | First pass of real content, not yet reviewed |
| 3 | In Process with Sam | Actively being worked on in conversation |
| 4 | Proposed Final Pending | Draft complete, awaiting Sam's review |
| 5 | First Principles Review | Undergoing systematic review for gaps and contradictions |
| 6 | First Principles Review Completed | Review done, all issues resolved |
| 7 | Finished | Final spec — all sections complete, open questions resolved |

## Spec Status

| Doc | Name | Status | Notes |
|-----|------|--------|-------|
| 01 | Architecture and Domain | 6 - FPR Completed | Core principles incl. opinionated-but-pluggable, agent-driven config, system roles, components |
| 02 | Configuration, Accounts, and Isolation | 6 - FPR Completed | Global/project-local config split, account homes for auth only, project-scoped launches |
| 03 | Session Management and Tmux | 6 - FPR Completed | Session lifecycle, lease model, worktrees in .pollypm/worktrees/, multi-session ready |
| 04 | Extensibility and Plugin System | 6 - FPR Completed | Five-layer architecture, transport architecture (CLI→MCP→HTTP), plugin validation, override hierarchy |
| 05 | Provider SDK | 6 - FPR Completed | Provider contract, transcript ingestion pipeline (background thread, no LLM), PollyPM-owned archive |
| 06 | Issue Management | 6 - FPR Completed | File-based + GitHub tracks, plugin interface, agent-driven backend selection, validation |
| 07 | Project History Import | 6 - FPR Completed | JSONL + git reconstruction, output to docs/ (committed), no secrets rule, INSTRUCT.md |
| 08 | Project State, Memory, and Documentation | 6 - FPR Completed | Two-stage pipeline (mechanical ingestion + LLM extraction via Haiku), docs/ path, documentation is a plugin |
| 09 | Inbox and Threads | 6 - FPR Completed | File-based inbox in .pollypm/inbox/, pluggable |
| 10 | Heartbeat and Supervision | 6 - FPR Completed | Core/plugin boundary, stable core APIs, CLI as v1 access layer |
| 11 | Agent Personas and Prompt System | 6 - FPR Completed | Rules + magic systems, manifest injection, built-in packaging, override hierarchy |
| 12 | Checkpoints and Recovery | 6 - FPR Completed | Three-tier model, per-session paths, pluggable strategy |
| 13 | Security, Observability, and Cost | 6 - FPR Completed | Account homes auth-only, docs/INSTRUCT.md security boundary, pluggable policies |
| 14 | Testing and Verification | 6 - FPR Completed | Plugin validation harness, prove-it-works as overridable default, Rules integration |
| 15 | Migration and Stability | 6 - FPR Completed | Override hierarchy as architectural invariant, agent-driven patches safe from upgrades |

## Resolved Cross-Spec Items

| # | Source | Target | Item | Resolution |
|---|--------|--------|------|------------|
| O1 | 06 | 04 | Issue management plugin registration path | Resolved: .pollypm/plugins/ with standard manifest |
| O2 | 07 | 08 | Import output format alignment | Resolved: both write to docs/, same markdown format |
| O3 | 08 | 11 | project-overview.md injection | Resolved: docs/project-overview.md in prompt assembly step 2 |
| O4 | 10 | 12 | Heartbeat Level 0 alignment | Resolved: heartbeat records Level 0 per checkpoint spec |
| O5 | 11 | 04 | Agent profile plugin interface | Resolved: profile backend in plugin families |
| O6 | 02 | 13 | Account capacity vs cost tracking | Resolved: capacity in state store, cost in transcript ledger |

## Future Specs (Post-V1)

| Topic | Notes |
|-------|-------|
| Service API Detail | Full endpoint catalog, error schemas, event types, pagination. Doc 04 has 8 operations — needs expansion. |
| Agent Prompt Versioning | Track what prompt version a session launched with. Detect drift when prompts change. |
| Onboarding Flow | Already built and working. Document the existing flow when it stabilizes. |
| Deployment Modes | Docker runtime, remote SSH, multi-machine. Currently local-only. |
| Data Retention Policies | Tiered retention by data type (transcripts forever, Level 0 checkpoints 24h, etc.). |
| Multi-Human Collaboration | Multiple operators, shared inbox ownership, concurrent human sessions. |
| Notification Routing | Push notifications, email, Slack/Discord beyond TUI alerts. |

## Summary

- **First Principles Review Completed:** 15 specs
- **Finished:** 0 specs (awaiting Sam's final review to promote)
- **Future specs identified:** 7 topics
- **Total:** 15 specs (+ README, TRACKING)
