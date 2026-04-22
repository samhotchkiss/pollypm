# Risks

This page tracks the recurring risks that matter to day-to-day PollyPM work.
It is a curated project document, not a transcript dump. For background on the
system itself, start with [Project Overview](project-overview.md),
[Decisions](decisions.md), and [History](history.md).

## Summary

- Worker health and queue state can still be misread when PollyPM has to infer
  status from pane output, snapshots, or partial runtime evidence.
- Large orchestration changes can cross multiple subsystems, so targeted local
  validation still matters before declaring work complete.
- Transcript-derived documentation needs human review; generated output can
  drift from the verified repository state if raw events are noisy or stale.

## Current Risks

- **State detection drift:** Heartbeat, cockpit, and inbox views rely on a mix
  of pane content, persisted snapshots, and heuristics. That can create false
  "needs follow-up" states or delay recognition that a worker is truly idle or
  done.
- **Environment-sensitive operations:** PollyPM depends on external tools and
  local runtime conditions such as provider CLIs, tmux, Git worktrees, model
  availability, and filesystem permissions. Missing or misconfigured pieces can
  block validation, recovery, or commit workflows at the moment they are
  needed.
- **Wide blast radius for workflow refactors:** Changes in routing, supervision,
  turn handling, or account management tend to span multiple command surfaces at
  once. A fix in one lane can regress operator workflow, escalation behavior, or
  worker lifecycle handling somewhere else.
- **Documentation quality depends on curation:** Knowledge extraction is useful
  for surfacing patterns, but raw session events and low-signal snapshots can
  leak implementation noise into user-facing docs unless the output is cleaned
  up and cross-checked.
- **Third-party code is a trust boundary:** Plugins, provider adapters, and
  other discovered extensions execute in the operator environment. That keeps
  PollyPM flexible, but it also means safety depends on clear trust assumptions
  and explicit user awareness.

## Working Rules

- Treat generated documentation as a draft until the underlying code paths or
  tests have been verified.
- Prefer focused validation on the touched subsystem when a change affects
  session state, routing, recovery, or supervision.
- Keep raw event records in transcript storage and issue-specific handoff
  artifacts, not in top-level product docs.

*Last updated: 2026-04-21*
