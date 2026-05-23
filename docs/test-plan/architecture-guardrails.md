# Architecture Guardrails

**Goal:** keep PollyPM modular, plugin-based, and easy to extend while multiple agents are changing code in parallel.

This document is a release-readiness gate. A PR can pass behavior tests and still be rejected if it erodes the architecture.

---

## Core principle

PollyPM should grow by adding narrow modules, plugins, adapters, and explicit contracts — not by piling more conditional logic into central files.

If a change makes the core know about a specific feature, project, UI affordance, or provider that could have lived behind an interface, stop and redesign.

---

## Modularity rules

- Keep plugin behavior in plugin-owned modules, not in global dispatch code.
- Keep Web UI presentation code separate from API service logic.
- Keep API route handlers thin; put reusable behavior behind service/facade functions.
- Keep storage access behind the existing storage/work-service facades.
- Do not reintroduce direct SQLite paths or bypass the configured storage backend.
- Do not make UI code depend on internal DB shapes; use public REST contracts.
- Do not make task lifecycle code depend on Web UI assumptions.
- Do not add broad `if project == ...`, `if provider == ...`, or `if session_name == ...` branches in core paths unless the existing architecture already defines that branch point.
- Prefer small files with single responsibilities over expanding already-large modules.
- Preserve existing plugin discovery, registration, and capability boundaries.

## Test / harness boundary rules

The test plan adds new code categories (perf harness, evals harness, Playwright suite, smoke automation). They have their own boundaries:

- **Test code lives under `tests/`.** Pytest, Playwright specs, evals cases. Production code does not import from `tests/`.
- **Harness scripts live under `scripts/`.** Perf, evals, smoke. They use public CLI/API only; they do not import from `src/pollypm/` internals except via documented public modules.
- **Test fixtures live under `tests/fixtures/`** (or `tests/playwright/fixtures/`). Production code does not read fixtures.
- **Journal entries live under `docs/test-plan/journals/`.** They are documentation, not code; they don't get imported.
- **Evals cases live under `tests/evals/cases/`** as YAML. The runner consumes them; product code never does.
- **Perf scripts use the public chat API + CLI.** They don't reach into PG, events.jsonl, or tmux state directly.
- **Harness tools must NOT add test-only switches to production modules** unless those switches are explicit documented extension points (e.g., a public dry-run flag).

If a test or harness needs to read product internals to verify behavior, the right answer is usually: expose a public introspection API on the product, then have the harness use it. Reaching past the boundary makes the test brittle and the product less modular.

---

## When adding functionality

Ask:

1. **Where is the extension point?** Plugin, service, route, UI component, CLI command, or test harness?
2. **What contract does it consume?** REST model, work-service method, storage facade, config object, or plugin capability?
3. **Can this be tested without booting the whole product?** If not, the boundary may be too blurry.
4. **What owns this state?** Avoid duplicating state across UI, API, PG, audit logs, and tmux transcripts.
5. **What breaks if another plugin adds a similar feature?** If answer is "central switch statement," redesign.

---

## PR review checklist

Every code PR must answer:

- What module/plugin owns the new behavior?
- Which public contract does it use?
- Did the PR avoid adding feature-specific logic to unrelated core paths?
- Did the PR keep route handlers, UI components, and storage access separated?
- Did the PR add or update tests at the module boundary?
- Did the PR preserve performance budgets from §06?

For Web UI PRs:

- UI reads public API models only.
- UI components are separated by concern: session rail, task panel, detail drawer, status badge, perf instrumentation.
- No frontend workaround hides a backend contract bug. File the backend gap instead.

For backend PRs:

- Route layer remains thin.
- Storage goes through facades.
- Plugin-owned behavior stays plugin-owned.
- New API fields are explicit model changes with tests and docs.

---

## Stop conditions

Stop and ask the operator before merging if:

- A fix requires editing a large central file because no extension point exists.
- A feature needs a new plugin hook or capability contract.
- Two plugins need the same core behavior but would implement it differently.
- A UI requirement pressures the backend into a one-off endpoint shape.
- The fastest implementation would couple Web UI, task lifecycle, and storage in one change.

These are architecture decisions, not just implementation details.
