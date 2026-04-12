# 0019 GitHub Issue Execution System

## Goal

Implement the GitHub-based issue tracking and execution system as a pluggable backend for PollyPM. Issues live in GitHub Issues with label-based state transitions, and execution follows a three-session pipeline: Opus specs/reviews, Codex implements/tests, Opus does final code review and merge.

## Background

PollyPM v1 supports two issue management backends (doc 06). The file-based tracker is already implemented. This issue builds the GitHub-based tracker and the multi-session execution pipeline that drives it.

## Three-Session Execution Pipeline

Every issue flows through three distinct sessions, ideally on different models, to ensure quality through separation of concerns:

### Session 1: Spec/Review (Opus)
- **Role:** Architect and reviewer
- **Model:** Claude Opus (or strongest available)
- **Responsibilities:**
  - Write the issue spec: goal, scope, acceptance criteria, test instructions, dependencies
  - Break ambiguous requests into well-scoped issues
  - After implementation, perform independent code review
  - Run its own verification tests (separate from the implementer's tests)
  - Approve and merge, or request changes with specific feedback
- **Key principle:** The spec writer and code reviewer is a DIFFERENT session than the implementer. Different model, different context, different perspective.

### Session 2: Implementation (Codex)
- **Role:** Builder and tester
- **Model:** Codex (or implementation-focused model)
- **Responsibilities:**
  - Pick up the next ready issue
  - Implement the solution following the spec
  - Write unit and integration tests
  - Verify the implementation works from a user perspective
  - Commit in small, meaningful pieces
  - Move the issue to needs-review when done
- **Key principle:** The implementer proves their own work works before handing off. But their approval is not sufficient — the reviewer must independently verify.

### Session 3: Code Review and Merge (Opus)
- **Role:** Quality gate
- **Model:** Claude Opus (same role as Session 1, may be same or different session)
- **Responsibilities:**
  - Read the implementation diff
  - Check against the spec's acceptance criteria
  - Run independent verification (not just re-running the implementer's tests)
  - Check for regressions, security issues, code quality
  - Either approve and merge, or move back to in-progress with specific change requests
- **Key principle:** The reviewer does NOT trust the implementer's test results at face value. They verify independently.

## GitHub Issue Lifecycle

### Labels

State is tracked via labels. Each issue has exactly one state label at a time:

| Label | State | Owner |
|-------|-------|-------|
| `polly:not-ready` | Issue exists but not actionable | PM |
| `polly:ready` | Fully specified, ready for implementation | PM |
| `polly:in-progress` | Codex is actively implementing | Codex (Session 2) |
| `polly:needs-review` | Implementation complete, awaiting review | Opus (Session 3) |
| `polly:in-review` | Opus is actively reviewing | Opus (Session 3) |
| `polly:completed` | Approved and merged | PM |

### Workflow Steps

1. **Opus writes the issue.** Creates a GitHub Issue with title, body (goal, scope, acceptance criteria, test instructions, dependencies), and `polly:ready` label.

2. **Codex picks it up.** Queries for oldest issue with `polly:ready`. Relabels to `polly:in-progress`. Creates a feature branch.

3. **Codex implements.** Follows the spec. Writes tests. Commits incrementally. Verifies from user perspective.

4. **Codex hands off.** Relabels to `polly:needs-review`. Adds a comment with:
   - What was done
   - How to test
   - Any deviations from the spec and why
   - Link to the branch/PR

5. **Opus reviews.** Relabels to `polly:in-review`. Reads the diff. Checks acceptance criteria. Runs independent verification.

6. **Opus decides:**
   - **Approve:** Relabels to `polly:completed`. Merges the PR. Closes the issue.
   - **Request changes:** Relabels back to `polly:in-progress`. Adds a comment with specific change requests. Codex picks it back up.

### Issue Template

Every issue must include:

```markdown
## Goal
What this issue accomplishes and why.

## Scope
What is in scope and what is explicitly out of scope.

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2
- [ ] ...

## Test Instructions
How to verify this works. Specific commands, expected outputs, scenarios to test.

## Dependencies
Issues that must be completed before this one can start.
- Depends on #XX (description)
- Depends on #YY (description)
```

## Plugin Interface Implementation

The GitHub backend implements the standard issue management interface (doc 06):

| Method | GitHub Implementation |
|--------|----------------------|
| `list_issues(state_filter)` | `gh issue list --label polly:<state>` |
| `create_issue(title, body, metadata)` | `gh issue create --title --body --label` |
| `move_issue(id, target_state)` | Remove old label, add new label via `gh issue edit` |
| `get_issue(id)` | `gh issue view --json` with comments |
| `append_note(id, note)` | `gh issue comment` |
| `next_available()` | `gh issue list --label polly:ready --sort created --json` → oldest |
| `report_status()` | Count issues per label |

All operations use the `gh` CLI. No GitHub API tokens managed by PollyPM — `gh` handles its own auth.

## Automated Plugin Validation

Before activation, the GitHub backend must pass validation:

1. `gh` CLI is installed and authenticated
2. The target repo exists and is accessible
3. All `polly:*` labels exist (create them if not)
4. `create_issue` → `move_issue` → `get_issue` → `append_note` round-trip succeeds on a test issue
5. Test issue is cleaned up after validation

## Configuration

In `<project>/.pollypm/config/project.toml`:

```toml
[plugins]
issue_backend = "github"

[plugins.github_issues]
repo = "samhotchkiss/pollypm"    # owner/repo
```

## Acceptance Criteria

- [x] GitHub issue backend plugin implements all 7 interface methods
- [x] Labels are auto-created on first use if missing
- [x] Plugin passes automated validation on activation
- [x] Three-session pipeline works end-to-end: Opus creates issue → Codex implements → Opus reviews and merges
- [x] State transitions are reflected in GitHub labels in real-time
- [x] Handoff comments contain structured information (what was done, how to test, deviations)
- [x] Review session runs independent verification, not just re-running implementer's tests
- [x] Change requests cycle back to in-progress correctly
- [x] `report_status()` returns accurate counts across all states
- [x] Works alongside file-based tracker (different projects can use different backends)

## Implementation Status

- Backend and CLI coverage now include GitHub issue CRUD, next-issue selection, status reporting, history, validation, handoff notes, review approve/reject flows, and mixed file/GitHub project coexistence.
- Activation now validates the GitHub backend immediately when a project tracker is enabled.
- End-to-end automated coverage now exercises GitHub issue creation, pickup, handoff with branch/PR metadata, review rejection back to in-progress, rework handoff, approval, completion, and final state reporting.

## Test Instructions

1. Configure a test project to use the GitHub issue backend
2. Create an issue via `create_issue()` — verify it appears in GitHub with correct label
3. Run `next_available()` — verify it returns the oldest ready issue
4. Run `move_issue()` through all states — verify labels update correctly
5. Run `append_note()` — verify comment appears on the issue
6. Run `report_status()` — verify counts match GitHub state
7. Run the full three-session pipeline on a small real issue
8. Run plugin validation and verify it passes
9. Intentionally break auth and verify validation catches it

## Dependencies

- None — the plugin interface (doc 06) is already specified. This is a new backend implementation.
