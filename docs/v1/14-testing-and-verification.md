---
## Summary

PollyPM requires every change to be tested and every feature to be verified from a user's perspective. Unit tests are necessary but not sufficient — agents must interact with what they build, launching it, using it, and confirming it works end-to-end. This document defines the testing architecture, the "prove it works" philosophy, and the verification hierarchy that governs all agent-built work on PollyPM.

---

# 14. Testing and Verification

## Testing Philosophy

PollyPM is built by agents supervised by PollyPM. This creates a direct feedback loop: if the testing standards are weak, the system that enforces those standards is itself unreliable. Testing is therefore a first-class concern, not an afterthought.

The core beliefs:

- **Tests are proof, not decoration.** A test exists to prove something works, not to check a coverage box.
- **"It compiles" is not proof.** Code that parses correctly and passes type checks can still be fundamentally broken.
- **"Tests pass" is not proof.** Tests that are too narrow, test the wrong thing, or mock away the real behavior provide false confidence.
- **Interacting with the running system is proof.** The only way to know something works is to use it the way a user would.
- **Agents have tmux.** Unlike CI-only test environments, PollyPM agents can launch processes, interact with UIs, and verify behavior from the outside.


## Test Architecture

PollyPM's test suite is organized into three layers, each serving a different purpose.

### Layer 1: Unit Tests

Unit tests verify individual functions, classes, and modules in isolation.

Scope:

- Config parsing: `pollypm.toml` loading, validation, default handling
- State management: SQLite operations, event recording, query correctness
- Plugin loading: Discovery, registration, lifecycle hooks
- Health classification: State machine transitions, classification logic
- Checkpoint creation: Schema validation, delta computation, serialization
- Provider adapters: Command construction, environment setup, output parsing
- Queue management: Priority ordering, assignment logic, state transitions

Properties:

- Fast: the full unit test suite runs in seconds
- Isolated: no filesystem side effects, no network calls, no tmux dependency
- Deterministic: same inputs always produce same outputs
- Located in: `tests/unit/` within the pollypm repository

Unit tests use standard Python testing tools (pytest) and may use lightweight test doubles for external interfaces (e.g., a fake SQLite database, mock filesystem). Heavy mocking of internal components is discouraged — if a unit test requires extensive mocking, the code under test may need better interfaces.

### Layer 2: Integration Tests

Integration tests verify that multiple components work together correctly.

Scope:

- Session launch flow: config load → account selection → tmux window creation → provider CLI start
- Heartbeat loop: heartbeat fires → pane captured → health classified → event recorded → alert raised
- Failover flow: failure detected → checkpoint created → new account selected → session relaunched → recovery prompt injected
- Inbox routing: issue arrives → project matched → queue updated → worker notified
- Checkpoint lifecycle: Level 0 created → Level 1 triggered → recovery prompt built → session recovered
- Plugin lifecycle: plugin discovered → loaded → hook called → error handled

Properties:

- Slower than unit tests but still automated
- Hit real systems where feasible: real SQLite databases, real filesystem operations, real tmux sessions
- May use real provider CLIs in test mode or with test accounts
- Located in: `tests/integration/` within the pollypm repository

### Layer 3: End-to-End Tests

End-to-end tests verify complete workflows from the user's perspective.

Scope:

- Full session lifecycle: `pm up` → sessions launch → work happens → `pm down` → sessions stop cleanly
- Recovery scenario: session launched → session killed → heartbeat detects → recovery happens → work continues
- TUI interaction: dashboard launches → shows correct state → responds to commands → updates in real-time
- Multi-session coordination: operator assigns work → worker picks up → worker completes → review session starts

Properties:

- Slow: may take minutes per test
- Require a real tmux environment
- Exercise the full stack from CLI commands to provider sessions
- Located in: `tests/e2e/` within the pollypm repository
- May require specific test configuration (test accounts, test projects)

### Test Directory Structure

```
tests/
  unit/
    test_config.py
    test_state.py
    test_plugins.py
    test_health.py
    test_checkpoints.py
    test_providers/
      test_claude_adapter.py
      test_codex_adapter.py
    test_queue.py
  integration/
    test_session_launch.py
    test_failover.py
    test_inbox_routing.py
    test_checkpoint_lifecycle.py
    test_plugin_lifecycle.py
  e2e/
    test_full_lifecycle.py
    test_recovery.py
    test_tui.py
    test_multi_session.py
  conftest.py
  fixtures/
    sample_configs/
    sample_transcripts/
    sample_checkpoints/
```


## The "Prove It Works" Requirement

Every feature, bug fix, and refactor must be verified from a user's perspective. This goes beyond running automated tests.

### What "Prove It Works" Means

For a bug fix:

1. Write a failing test that reproduces the bug
2. Fix the bug
3. Verify the test now passes
4. Verify the fix from the user's perspective (launch the system, trigger the scenario, confirm the bug is gone)
5. Run the full test suite to check for regressions

For a new feature:

1. Write unit tests as you build each component
2. Write an integration test that exercises the feature's key workflow
3. Launch the feature in a real environment (tmux session, real config, real provider if applicable)
4. Interact with the feature the way a user would
5. Confirm the output, behavior, and error handling are correct
6. Run the full test suite

For a refactor:

1. Verify all existing tests pass before starting
2. Perform the refactor
3. Verify all existing tests still pass
4. Verify no behavioral regression from the user's perspective
5. If the refactor changes interfaces, update integration tests to cover the new interface

### Why Automated Tests Are Not Sufficient

Automated tests verify what the test author thought to test. They miss:

- UX problems: the feature works but is confusing or awkward to use
- Integration gaps: each component works alone but they do not compose correctly
- Environmental issues: works in the test environment but not in the real one
- Edge cases the test author did not anticipate

Human-perspective verification (or agent-perspective verification via tmux) catches these gaps.

### Agents and tmux Verification

PollyPM agents have a unique advantage over traditional CI: they can interact with running systems through tmux.

This means an agent can:

- Launch a PollyPM instance with `pm up`
- Observe the TUI dashboard
- Watch session behavior in real time
- Send commands and verify responses
- Check logs and state after operations complete
- Kill sessions and verify recovery behavior

Agents are expected to use this capability. "I ran the tests and they passed" is a necessary step but not the final one. The final step is "I launched it, used it, and it works."


## Testing Requirements by Change Type

### Bug Fixes

| Step | Required | Details |
|------|----------|---------|
| Failing test first | Yes | Write a test that fails without the fix and passes with it |
| Fix implementation | Yes | Make the minimal change to fix the bug |
| Unit test passes | Yes | The new test and all existing unit tests pass |
| Integration test | If applicable | If the bug spans components, add or update an integration test |
| User-perspective verification | Yes | Interact with the system to confirm the bug is fixed |
| Full test suite | Yes | All tests pass, no regressions |

### New Features

| Step | Required | Details |
|------|----------|---------|
| Unit tests | Yes | Cover core logic, edge cases, error handling |
| Integration test | Yes | At least one test exercising the feature's primary workflow |
| tmux verification | Yes | Launch and interact with the feature in a real environment |
| Documentation | If user-facing | Update relevant spec docs or help text |
| Full test suite | Yes | All tests pass, no regressions |

### Refactors

| Step | Required | Details |
|------|----------|---------|
| Pre-refactor test pass | Yes | All tests pass before any changes |
| Post-refactor test pass | Yes | All existing tests still pass |
| Behavioral verification | Yes | No user-visible behavior changes |
| Interface test updates | If applicable | Update tests if interfaces changed |
| Full test suite | Yes | All tests pass, no regressions |

### Configuration Changes

| Step | Required | Details |
|------|----------|---------|
| Default behavior preserved | Yes | Existing configs continue to work without modification |
| New field validation | Yes | New fields are validated with clear error messages |
| Migration test | If applicable | If old configs need migration, test the migration path |
| Documentation | Yes | Config changes are documented in the spec |


## Verification Hierarchy

When verifying work, the layers build on each other. Each layer adds confidence that the previous layer cannot provide alone.

### Level 1: Unit Tests

- **What it proves**: Individual components behave correctly given specific inputs
- **What it misses**: Component interactions, environmental factors, UX issues
- **Speed**: Seconds
- **Automation**: Fully automated, runs in CI

### Level 2: Integration Tests

- **What it proves**: Components compose correctly, data flows through the system
- **What it misses**: Full-stack behavior, real-world conditions, UX issues
- **Speed**: Seconds to minutes
- **Automation**: Fully automated, may require test infrastructure

### Level 3: User-Perspective Verification

- **What it proves**: The system works as a user would experience it
- **What it misses**: Edge cases not exercised during verification
- **Speed**: Minutes
- **Automation**: Semi-automated (agent interacts via tmux, following a verification plan)

### Level 4: PM Review

- **What it proves**: The work meets the project's quality standards and design intent
- **What it misses**: Nothing — this is the final gate
- **Speed**: Variable (depends on reviewer availability)
- **Automation**: Not automated (human or PM agent reviews the work product)

All four levels are required before work is considered complete. Levels 1-3 are the implementer's responsibility. Level 4 is the reviewer's responsibility.


## Stability Contract

PollyPM is a live system in daily use. The testing requirements exist to protect that stability.

### The Contract

1. **PollyPM works today.** This is the baseline. Nothing we build should break what already works.

2. **Every change is tested before merge.** No exceptions. If a change cannot be tested, it is not ready to merge.

3. **Incremental delivery.** Features are broken into small pieces. Each piece is merged and verified independently. No big-bang integrations.

4. **Tests run before every merge.** The full test suite is the gate. If tests fail, the merge does not happen.

5. **Rollback is always possible.** Every change has a rollback plan. If something breaks in production use, we can revert to the previous working state.

### What This Means in Practice

- Do not accumulate large uncommitted changes. Commit and test frequently.
- Do not skip tests "just this once." The one time you skip is the time it breaks.
- Do not merge with known test failures. Fix the test or fix the code, but do not proceed with red tests.
- Do not refactor and add features in the same change. Separate them so each can be tested and reverted independently.
- Do not remove tests without understanding why they exist. A test that seems unnecessary may be catching a subtle regression.


## Test Infrastructure

### Test Configuration

Tests use a separate configuration that does not interfere with production PollyPM state:

- Test SQLite database in a temporary directory
- Test tmux session with a unique name (avoids collision with production sessions)
- Test account homes in temporary directories
- Test configs that reference test accounts and test projects

### Test Fixtures

Common test data is maintained in `tests/fixtures/`:

- Sample `pollypm.toml` files for various configurations
- Sample JSONL transcripts for testing token extraction
- Sample checkpoint JSON files for testing recovery prompt construction
- Sample pane output for testing health classification

### Continuous Integration

When CI is configured:

- Unit tests run on every push
- Integration tests run on every pull request
- E2E tests run on merge to main (or on explicit request)
- Test results are reported in PR checks


## Testing Prompts and Agent Instructions

Testing requirements are communicated to worker agents through the task-specific prompt system (doc 11).

Each task prompt includes:

- The verification steps required for the type of change being made
- Specific test files to create or update
- Instructions for tmux-based verification
- The full test suite command to run before marking work complete

This ensures agents do not need to remember the testing policy — it is injected into every task context.

### Testing Rules as Part of the Rules System

Testing requirements are part of PollyPM's Rules system (doc 11). The opinionated defaults are loaded from rules files such as `rules/bugfix.md`, `rules/build.md`, `rules/refactor.md`, etc. Each rules file defines the testing expectations for that type of change.

Users can modify these rules files per-project. A project that does not need tmux verification for every change can relax that requirement in its project-local rules. A project that needs stricter verification (e.g., security-critical code) can add additional requirements. The built-in rules files are defaults, not mandates.


## Automated Plugin Validation

Every plugin must pass a validation harness before activation. This ensures that plugins conform to their declared interface and do not introduce runtime errors into the system.

### Validation Harness

When a plugin is loaded, PollyPM runs it through a validation harness that:

1. **Exercises all interface methods** defined by the plugin type (e.g., a checkpoint strategy plugin must implement `create_checkpoint`, `load_checkpoint`, `prune`, etc.)
2. **Provides test inputs** appropriate for each method — synthetic but structurally valid data that exercises the method's expected input/output contract
3. **Validates return types and structure** — the harness checks that return values match the expected schema
4. **Tests error handling** — the harness passes known-invalid inputs and verifies the plugin raises appropriate errors rather than crashing or returning garbage

### Validation Outcome

- **Pass**: The plugin is activated and available for use. Activation is logged.
- **Fail**: The plugin is **not activated**. PollyPM logs a clear error describing which interface method failed validation and why. The system continues operating with the built-in default (or the previously active plugin) for that capability.

Failed validation never causes PollyPM to crash. It produces a clear, actionable error message and the system degrades gracefully to its defaults.

### Validation Triggers

- Plugin validation runs on PollyPM startup for all configured plugins
- Validation re-runs when a plugin file is modified and PollyPM detects the change
- Users can manually trigger validation via `pm plugin validate <plugin-name>`


## Anti-Patterns

These testing approaches are explicitly discouraged:

| Anti-Pattern | Why It Is Wrong | What To Do Instead |
|-------------|----------------|-------------------|
| Mocking everything | Tests prove the mocks work, not the system | Use real components where feasible |
| Testing only the happy path | Misses error handling, edge cases, failure modes | Test errors, boundaries, and invalid inputs |
| Writing tests after the feature is "done" | Tests become afterthought documentation, not design drivers | Write tests as you build, starting with failing tests for bug fixes |
| Skipping tmux verification | Misses UX issues and integration gaps | Always interact with the running system |
| Big-bang integration testing | Failures are hard to diagnose, fixes are risky | Test at each integration step |
| Snapshot tests for complex output | Brittle, hard to review, hide real assertions | Assert on specific properties, not entire output blobs |
| Disabling flaky tests | Hides real failures behind noise | Fix the flakiness or delete the test |


## Opinionated but Pluggable

The testing philosophy and "prove it works" requirement described in this document are PollyPM's opinionated defaults. They represent what we believe produces the best outcomes, but they are not sacred cows.

- The **prove-it-works philosophy** is an opinionated default, not an absolute mandate. Users can override it per-project. A project with a mature CI pipeline and comprehensive automated test coverage may choose to relax the tmux verification requirement. A rapid-prototyping project may choose lighter testing standards during exploration phases.
- **Testing requirements by change type** (bug fix, new feature, refactor) are defaults loaded from the Rules system (doc 11). Users can modify the rules files to match their project's needs.
- The **verification hierarchy** (unit, integration, user-perspective, PM review) is a recommended layering. Projects can adjust which levels are mandatory for which change types.

This pattern — strong defaults that are fully replaceable — applies throughout PollyPM. Checkpoint strategy, security policies, testing requirements, and migration approach are all configurable and overridable. PollyPM ships opinionated defaults so the system works out of the box, but every project is different.


## Resolved Decisions

1. **Three-layer test architecture.** Unit, integration, and end-to-end tests serve complementary purposes. All three layers are required for complete coverage. No single layer is sufficient.

2. **"Prove it works" is the opinionated default.** Prove-it-works is the opinionated default. It ships as a strong recommendation and is active unless overridden per-project through the Rules system.

3. **Agents must use tmux for verification.** PollyPM agents have the ability to launch and interact with running systems. They are expected to use this ability for every feature and bug fix, not just rely on test output.

4. **Integration tests use real systems, not mocks.** Where feasible, integration tests hit real SQLite databases, real filesystems, and real tmux sessions. Mocking is reserved for external services that cannot be used in tests.

5. **PM review is the final gate.** Automated tests and agent verification are necessary but the work is not done until a human or PM agent reviews it. This catches design issues, UX problems, and strategic misalignment that automated testing cannot.

6. **Incremental delivery over big-bang.** Features are merged in small, independently tested pieces. Each piece works on its own. Large branches with many untested changes are not acceptable.


## Cross-Doc References

- Plugin system and extensibility testing: [04-extensibility-and-plugin-system.md](04-extensibility-and-plugin-system.md)
- Heartbeat monitoring (tested via integration tests): [10-heartbeat-and-supervision.md](10-heartbeat-and-supervision.md)
- Task-specific prompt system (delivers testing instructions): [11-agent-personas-and-prompt-system.md](11-agent-personas-and-prompt-system.md)
- Checkpoint testing and recovery verification: [12-checkpoints-and-recovery.md](12-checkpoints-and-recovery.md)
- Security and observability testing: [13-security-observability-and-cost.md](13-security-observability-and-cost.md)
- Migration stability requirements: [15-migration-and-stability.md](15-migration-and-stability.md)
