# PollyPM V1 Product Spec

This directory is the complete specification for PollyPM v1.

## Context

- PollyPM is a tmux-first supervisor for interactive CLI agent sessions.
- It orchestrates multiple Claude, Codex, and future CLI agents from one control plane.
- PollyPM is in active daily use. All v1 features are built incrementally without breaking existing functionality.
- The plugin system is the primary extensibility mechanism — every major subsystem is replaceable.

## Design Philosophy

**Opinionated but pluggable.** PollyPM ships strong, opinionated defaults for everything — prompts, rules, issue management, documentation, heartbeat strategy, recovery behavior. But every behavior is replaceable. Users don't fight the system; they swap the parts they disagree with.

- **Core = runtime/API substrate.** PollyPM core owns tmux, sessions, transcripts, accounts, scheduling, state store, and a stable internal API.
- **Plugins = policy engines.** Everything else — issue management, documentation, heartbeat evaluation, agent prompts, rules, magic — is a plugin that calls core APIs.
- **Agent-driven configuration.** When a user disagrees with behavior, the agent asks if they want to change it and patches the system directly. The agent IS the configuration interface.
- **Override hierarchy.** Built-in defaults → user-global overrides (~/.pollypm/) → project-local overrides (<project>/.pollypm/). Patches create overrides, never modify built-ins.

## Project-Local State

Project documentation lives in `<project>/docs/` (committed to git by default):

```
<project>/docs/
  project-overview.md   # Vision, goals, current state, architecture summary
  decisions.md          # Chronological decision log with rationale
  architecture.md       # Living architecture description
  conventions.md        # Coding standards, patterns, naming
  history.md            # Project evolution narrative
  risks.md              # Active risks and open questions
  ideas.md              # Captured ideas not ready for action
```

Docs are valuable shared knowledge and must NEVER contain secrets. On project setup, PollyPM asks whether to commit `docs/`; the default is yes.

All operational state lives in `<project>/.pollypm/` (gitignored by default):

```
<project>/.pollypm/
  config/          # Project settings, plugin selections
  transcripts/     # PollyPM-owned JSONL transcript archive
  rules/           # Situational instruction sets
  magic/           # Capability catalog
  plugins/         # Project-local plugins
  logs/            # Session logs
  artifacts/       # Checkpoints, summaries
  inbox/           # Inbox items and threads
  worktrees/       # Per-session git worktrees
  INSTRUCT.md      # Implementation instructions (may contain sensitive details)
```

## Documents

### Foundation

1. `01-architecture-and-domain.md` — Core principles, system roles, components, technology stack
2. `02-configuration-accounts-and-isolation.md` — TOML config, account homes, capacity model, failover
3. `03-session-management-and-tmux.md` — Session lifecycle, tmux integration, lease model, worktrees

### Extensibility

4. `04-extensibility-and-plugin-system.md` — Five-layer architecture, plugin families, hooks/filters
5. `05-provider-sdk.md` — Stable SDK for adding new CLI providers

### Project Intelligence

6. `06-issue-management.md` — Two-track issue management (file-based + GitHub), plugin interface
7. `07-project-history-import.md` — Chronological project reconstruction from JSONL + git
8. `08-project-state-memory-and-documentation.md` — Async doc maintenance, summary-first pattern, session injection

### Operations

9. `09-inbox-and-threads.md` — Inbox items, thread routing, PM/PA handoff
10. `10-heartbeat-and-supervision.md` — Session monitoring, health classification, intervention
11. `11-agent-personas-and-prompt-system.md` — Named personas, prompt optimization, task-specific instructions

### Reliability

12. `12-checkpoints-and-recovery.md` — Three-tier checkpoints, recovery flow, token discipline
13. `13-security-observability-and-cost.md` — Isolation boundaries, alerting, cost tracking
14. `14-testing-and-verification.md` — Prove-it-works philosophy, three-layer test architecture
15. `15-migration-and-stability.md` — Incremental delivery, backward compatibility, schema migration
