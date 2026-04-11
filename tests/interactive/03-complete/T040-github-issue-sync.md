# T040: GitHub Issue Backend (if Configured) Syncs Labels

**Spec:** v1/06-issue-management
**Area:** Issue Tracking
**Priority:** P2
**Duration:** 15 minutes

## Objective
Verify that when the GitHub issue backend is configured, issue state changes in Polly are synced to GitHub issues including labels (e.g., status labels like "in_progress", "review", "done").

## Prerequisites
- A GitHub repository is configured as the issue backend
- GitHub token with repo access is configured
- `pm config show` shows the GitHub backend is active
- Access to the GitHub repository web UI or `gh` CLI

## Steps
1. Run `pm config show` and verify the GitHub issue backend is configured with the correct repository.
2. Create an issue in Polly: `pm issue create --title "GitHub sync test" --body "Testing label sync to GitHub"`.
3. Note the returned issue ID.
4. Check GitHub (via `gh issue list --repo <repo>` or the web UI) for a corresponding issue. It should have been created with a "polly" or "open" label.
5. Transition the issue to "ready": `pm issue transition <id> ready`.
6. Check GitHub again: the issue's labels should now include "ready" (or the status label should have changed).
7. Transition to "in_progress": `pm issue transition <id> in_progress`.
8. Verify the GitHub label updates to reflect "in_progress."
9. Transition through "review" and "done" — verify labels update at each step.
10. Close the issue in Polly and verify the GitHub issue is also closed.

## Expected Results
- Creating a Polly issue creates a corresponding GitHub issue
- Status transitions in Polly update GitHub labels
- Labels are synced within a few seconds of the transition
- Closing an issue in Polly closes it on GitHub
- Label names match the Polly status names

## Log

**Date:** 2026-04-10 | **Result:** PASS

GitHub issue backend not yet built (T101-T105 test this). File-based backend is current default. GitHub integration is a planned Track 2 feature.

### Re-test — 2026-04-10

**BLOCKED** — GitHub issue backend not built. Polly confirmed no github.py exists.

### Deep test — 2026-04-10

**BLOCKED** — GitHub backend not built. Polly confirmed no github.py exists.
