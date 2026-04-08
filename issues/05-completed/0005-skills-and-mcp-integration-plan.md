# 0005 Skills And MCP Integration Plan

## Goal

Define how PollyPM should integrate, expose, and supervise skills and MCPs without turning the operator flow into a bag of hidden magic.

## Proposal

PollyPM should treat skills and MCPs as first-class but explicitly scoped capabilities, not as ambient hidden context.

The operating model is:

- `skills` are declarative bundles of instructions, allowed tools, and optional MCP dependencies.
- `MCPs` are runtime services that expose concrete tools.
- `PollyPM` is the policy and visibility layer that decides which skills and MCPs are available to each session, at what scope, and with what authority.

### 1. Capability model

Split the capability stack into three layers:

1. `global baseline`
   - safe, read-only capabilities that every PollyPM session may inspect
   - examples: local repo metadata, session status, health snapshots, non-destructive project listing

2. `project scope`
   - capabilities attached to one project and inherited only by sessions launched for that project
   - examples: project-specific MCP servers, project-specific skills, issue-tracker or repo automation

3. `session override`
   - ephemeral capabilities granted to one live PM or PA session
   - examples: temporary access to a troubleshooting MCP, a human-approved write action, or a debug-only skill

PollyPM should never let a session discover a capability that is not in its assigned scope. The session prompt can mention only the allowed surface for that role and project.

### 2. Skill integration

Skills should be represented as declarative records with:

- name and version
- source location
- allowed roles: `pm`, `pa`, or both
- required MCPs, if any
- side-effect class: `read-only`, `write`, or `destructive`
- project scope: `global`, `project`, or `session`

PollyPM should load skills at launch time and compile them into a capability manifest for each session. The manifest is what the operator sees in the control room and what the worker session receives as its authoritative tool surface summary.

Worker sessions should not discover installed skills by scanning the filesystem directly. They should only receive the compiled manifest that PollyPM has approved.

### 3. MCP integration

MCP servers should be modeled as runtime resources with explicit ownership and scope:

- `global MCPs` are shared, read-only, and safe to expose to PM/operator workflows
- `project MCPs` are bound to one project and can be reused by all sessions in that project
- `private MCPs` are bound to one live session and are useful for temporary debugging or one-off task tooling

PollyPM should launch MCPs separately from worker sessions and track them as managed runtime units. A worker session can only attach to MCPs that PollyPM has already marked available for that project or session.

### 4. Policy boundaries

Policy should be enforced before launch and at runtime.

Before launch:

- PollyPM validates that a session’s skills only request MCPs allowed by the project policy.
- PollyPM rejects any capability that widens scope silently.
- The control room must show the exact policy delta before a project-scoped MCP is enabled.

At runtime:

- read-only MCPs may be auto-attached
- write-capable MCPs require explicit project policy approval
- destructive MCPs require operator confirmation plus a narrow session lease
- secrets-bearing MCPs must be scoped to the minimum possible project or session boundary

No skill may increase its own authority. A skill can request capabilities, but PollyPM decides whether the request is valid for that project and role.

### 5. Operator visibility

The control room should expose three views:

1. `Capability inventory`
   - installed skills
   - registered MCPs
   - scope and role for each item
   - source path or package identity
   - enabled/disabled state

2. `Policy view`
   - which capabilities are available to PM, PA, and worker sessions
   - which capabilities are blocked by default
   - whether a project has any write or destructive MCPs enabled
   - the last approval or override that expanded scope

3. `Runtime health`
   - MCP process status
   - last heartbeat / last successful handshake
   - restart count
   - auth freshness or token expiry signal when available
   - last error and current degraded state

For session detail pages, PollyPM should render a compact capability banner:

- active skills
- attached MCPs
- project scope
- policy class
- current health

That banner should make it obvious when the session is running with limited or degraded tooling.

### 6. Runtime health model

Each skill or MCP runtime should produce a health record with:

- `state`: `healthy`, `degraded`, `stopped`, or `failed`
- `last_seen_at`
- `last_error`
- `restart_count`
- `active_sessions`
- `policy_scope`

Health is not just process liveness. PollyPM should consider a runtime degraded if it is:

- running but missing required auth
- running but failing handshake
- repeatedly crashing
- attached to a scope that no longer matches its project policy

The control room should surface degraded health as an operator action item, not just a log line.

### 7. PM/PA supervision model

PM and PA should see different slices of the same capability graph:

- PM sees policy, approvals, and runtime health
- PA sees what is available for execution and what is blocked
- worker sessions see only the capabilities they can actually use

This keeps PollyPM aligned with its supervision model:

- PM manages scope and approvals
- PA executes within the approved surface
- workers operate inside a constrained capability contract

## Recommended Config Shape

Use config-level declarations for authoritative scope and policy, not ad hoc prompt text.

Suggested structure:

- `skills`: catalog of named skill definitions
- `mcps`: catalog of named MCP runtime definitions
- `projects.<name>.skills`: enabled skills for the project
- `projects.<name>.mcps`: enabled MCPs for the project
- `sessions.<name>.skills`: session-specific overrides

The config file should remain the source of truth, while the control room becomes the source of visibility and approval workflow.

## Acceptance Criteria

- The design clearly separates discovery, policy, runtime availability, and UI exposure.
- The report ties the plan back to PollyPM’s PM/PA supervision model.
- The proposal makes clear which capabilities are global, project-scoped, and session-scoped.
- The proposal defines how the control room reports capability inventory, policy state, and runtime health.
