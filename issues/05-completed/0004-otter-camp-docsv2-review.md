# 0004 Otter Camp Docsv2 Review

## Goal

Read `~/dev/otter-camp/docsv2`, extract the patterns that are actually useful for PollyPM, and write a concrete report instead of vague inspiration.

## Summary

Otter Camp V2 is useful to PollyPM where it turns supervisor behavior into explicit state, explicit handoff, and explicit recovery. The strongest transferable patterns are not the full product surface; they are the discipline around a single source of truth, durable event logging, action-required queues, structured recovery checkpoints, and a clear separation between ordinary status updates and policy-gated operations.

PollyPM should borrow those patterns selectively. Its core job is narrower than Otter Camp's: supervise native CLI sessions in tmux, preserve operator control, and keep launches, leases, and recoveries explainable. That means we should adopt the operational model and the UI affordances that support it, but avoid pulling in Otter Camp's full agent, memory, and workflow machinery unless PollyPM starts needing it.

## Adopt Now

- Single authority for state. PollyPM should keep its state store as the source of truth for session lifecycle, lease ownership, launch metadata, and recovery status. That matches Otter Camp's "API/state is canonical" pattern without needing the rest of the platform.
- Durable event log. Otter Camp's event bus pattern maps well to PollyPM's supervisor lifecycle. Every launch, heartbeat, intervention, recovery, and exit should be recorded as an append-only event so the operator can reconstruct what happened after the fact.
- Explicit recovery contracts. PollyPM already has checkpoints; the Otter Camp pattern suggests making them structured and authoritative instead of treating them like best-effort notes. A restart should rehydrate from a known checkpoint plus workspace state, not transcript scraping.
- Action-required queue. Otter Camp's inbox is a good fit for PollyPM's stuck-session and human-review cases. Anything that needs a decision, not just a notification, should land in a persistent queue.
- Scope-aware operator surface. The TUI pattern of keeping one primary cockpit with a clear focus model is directly transferable to `control_tui.py`. The operator needs a fast way to see the active project, session, lease holder, and next action without context switching.

## Adapt Later

- Project/task flow. Otter Camp's work -> review -> done model is useful, but PollyPM does not need the full flow-template/DAG machinery yet. What it does need is a lighter version: launch -> monitor -> intervene -> recover -> complete.
- Binary policy gating. Otter Camp's allow/deny policy is worth borrowing for destructive commands like kill, restart, detach, or cross-session takeover. For normal supervisor actions, the policy layer can stay simple until PollyPM exposes more automation surface.
- Memory handling. Otter Camp's durable memory system is a strong reference for checkpoint summaries and provenance, but PollyPM should stop at structured recovery notes for now. Full taxonomy, extraction, and retrieval are overkill unless we want cross-session knowledge recall.
- Rich realtime UI. The TUI/web patterns around persistent panes, command palette behavior, and live event updates are useful. PollyPM should adopt the interaction model, but not the full three-panel product shape.

## Avoid For Now

- Full Otter Camp control plane. PollyPM is not brokering model invocations, tool execution, or agent policy at that scale. Copying the full execution stack would add complexity without improving the core supervisor loop.
- Massive domain schema. Otter Camp's many-table domain model is appropriate for a product platform, not a session supervisor. PollyPM should keep the schema compact and operational.
- Multi-tenant and SSE-heavy architecture. PollyPM is single-operator and tmux-centered. We do not need Otter Camp's org-level realtime distribution or tenant isolation model to solve current problems.
- Browser/MCP/tool catalog expansion. Those are useful only if PollyPM becomes a general agent runtime. Today they would blur the boundary between supervision and agent execution.

## Concrete Implementation Candidates

- Add a `session_event` ledger in `src/pollypm/storage/state.py` for launches, heartbeats, leases, takeovers, recoveries, and exits.
- Add structured checkpoint records in `src/pollypm/checkpoints.py` that capture last-good workspace state, operator intent, and recovery reason.
- Add a persistent operator inbox in `src/pollypm/messaging.py` for actions that need approval or follow-up, such as stuck sessions and manual restarts.
- Extend `src/pollypm/control_tui.py` with a command-palette flow for `pause`, `resume`, `takeover`, `restart`, and `show history`.
- Tighten `src/pollypm/supervisor.py` so destructive actions and cross-session interventions are policy-gated and leave explicit audit records.
- Use `src/pollypm/worktrees.py` and `src/pollypm/workers.py` to keep recovery anchored to the live workspace and the active session lease, not stale metadata.

## Result

The main takeaway is that PollyPM should adopt Otter Camp's operational rigor, not its entire product surface. The highest-value transfer is a small set of explicit contracts: canonical state, durable events, structured recovery, persistent review queues, and operator-first controls.
