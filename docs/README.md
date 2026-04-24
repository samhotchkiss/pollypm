# PollyPM Docs

This directory mixes front-door guides, architecture notes, plugin references,
and working design docs. If you are not sure where to start, start here:

- [Getting Started](getting-started.md) — install PollyPM, register a project,
  and run your first task.
- [Architecture](architecture.md) — high-level system map and core boundaries.
- [Worker Guide](worker-guide.md) — the operating manual for spawned worker
  sessions.
- [Plugin Authoring](plugin-authoring.md) — the shortest path to building and
  testing a PollyPM plugin.
- [Plugin Trust Model](plugin-trust.md) — what installing a third-party
  plugin or provider means in v1.

## User

- [Getting Started](getting-started.md) — first-run setup, onboarding, and the
  first end-to-end workflow.
- [Work Service Specification](work-service-spec.md) — the current task,
  workflow, and governance model behind `pm task`.
- [Issue Tracker](issue-tracker.md) — explains the legacy file-based tracker
  and why the work service is now the source of truth.
- Plugin feature specs such as
  [Planner](planner-plugin-spec.md),
  [Advisor](advisor-plugin-spec.md),
  [Downtime](downtime-plugin-spec.md), and
  [Morning Briefing](morning-briefing-plugin-spec.md) cover specific operator
  workflows.

## Plugin Author

- [Plugin Authoring](plugin-authoring.md) — end-to-end walkthrough for writing,
  testing, and installing a plugin.
- [Plugin Trust Model](plugin-trust.md) — the security boundary for external
  plugins and provider packages.
- [Provider Plugin SDK](provider-plugin-sdk.md) — stable adapter surface for
  adding a new CLI provider.
- [Plugin Discovery Spec](plugin-discovery-spec.md) — manifests, capabilities,
  discovery paths, and override rules.
- [Extensibility Architecture](extensibility-architecture.md) and
  [Plugin Boundaries](plugin-boundaries.md) — where plugins fit and what should
  stay in core.

## Contributor

- [Architecture](architecture.md) — current system design and service boundary
  notes.
- [Conventions](conventions.md) — coding, testing, and import-boundary rules.
- [Worker Guide](worker-guide.md) — task lifecycle and handoff expectations for
  implementation work.
- [Release-Fix Packaging](release-fix-packaging.md) — review and rollback
  boundaries for release-candidate fixes.
- [V1 Spec](v1/README.md) — the full chapterized product spec when you need the
  complete model, not just the quick path.
- [Visuals](visuals/index.html) plus the `docs/project-specs/` examples are
  supporting material, not the primary entry point.

## Stop Here Unless You Need It

- When content gets moved into `docs/archive/`, treat it as retired snapshots,
  bulky artifacts, and superseded notes. Do not start there unless you are
  chasing history.
- When content gets moved into `docs/internals/`, treat it as
  implementation-detail material for maintainers, not front-door docs. Do not
  start there unless you are tracing a subsystem.
- `docs/v1/` is valuable, but it is a deep spec set. Reach for it when the
  front-door docs are not enough.
