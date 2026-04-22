# Ideas

This page captures worthwhile product, workflow, and documentation ideas that
have come up repeatedly while building PollyPM. It is a curated shortlist, not
an event ledger. For the current system shape, see [Project Overview](project-overview.md),
[Decisions](decisions.md), and [History](history.md).

## Summary

- Keep polishing the operator golden path so first-run, recovery, and handoff
  flows match the real behavior of the tool.
- Reuse persisted state wherever possible instead of repeating expensive tmux,
  Git, or shell work on the foreground path.
- Make the docs and UI lead with user outcomes first and internal machinery
  second.

## Near-Term Ideas

- **Sharper startup and recovery guidance:** Keep tightening the README, CLI
  help, and operator-facing docs so the expected session flow is obvious before
  users hit `pm send`, `pm up`, or recovery commands.
- **Background refresh over synchronous reads:** Continue moving UI refresh work
  off the main thread and prefer cached heartbeat snapshots before live tmux
  capture or shell probes.
- **Clearer trust model for extensions:** Keep plugin and provider discovery
  explicit with docs and runtime warnings so users understand when PollyPM is
  loading external code.
- **Help that matches the happy path:** Standardize examples and command help
  around the most common workflows so subcommands remain copy-paste friendly as
  the CLI grows.
- **Outcome-first product language:** Continue simplifying public-facing docs,
  website copy, and cockpit wording so PollyPM is described in terms of what it
  helps teams ship, not just the internal mechanics it uses.
- **Better doc hygiene around generated content:** Add more guardrails around
  generated documentation so extracted knowledge stays readable, source-backed,
  and clearly separated from raw event storage.

## Longer-Term Ideas

- Expand the operator dashboard so it can explain why PollyPM thinks a worker is
  healthy, idle, blocked, or stale instead of only showing the classification.
- Keep improving the boundary between provider adapters and runtimes so new
  providers can be added without leaking provider-specific assumptions into the
  control plane.
- Add stronger validation loops for deployment and production-health checks
  where PollyPM relies on external systems that local tests cannot fully cover.

*Last updated: 2026-04-21*
