# T101: GitHub Issues Display in Cockpit Rail Issues View

**Spec:** v1/06-issue-management
**Area:** Issue Management — GitHub Integration
**Priority:** P0
**Duration:** 15 minutes

## Objective
Verify that selecting "Issues" under a project in the cockpit rail shows GitHub issues (from `gh issue list`) grouped by polly:* state labels, instead of showing the old file-based issue tracker.

## Prerequisites
- pollypm is running (`pm up`)
- The pollypm project is registered and has GitHub issues with polly:* labels
- `gh` CLI is authenticated

## Steps
1. Open the pollypm tmux session
2. In the cockpit rail, navigate to the PollyPM project
3. Select the "Issues" sub-item under the project
4. Observe the right pane — it should show GitHub issues grouped by state
5. Verify issues with `polly:ready` label appear under a "ready" section
6. Verify the issue titles match what `gh issue list --label polly:ready` returns
7. Check that issues with `polly:in-progress` label appear separately
8. Verify the display includes issue numbers (e.g., #1, #7, #12)
9. Verify tier labels are shown if present (tier:0, tier:1, etc.)
10. Refresh by navigating away and back — verify issues update

## Expected Results
- Issues view shows GitHub issues, NOT file-based issues from issues/ directory
- Issues are grouped by polly:* state label
- Issue numbers and titles are displayed
- The view refreshes when navigated to

## Log

**Date:** 2026-04-10 | **Result:** BLOCKED (GitHub backend not built)

GitHub issue backend not yet implemented. Issue 0019 tracks this work. File-based backend is current default.

### Re-test — 2026-04-10

**Result: BLOCKED** — GitHub issue backend not built. Polly confirmed: 'No github.py or any file with github in the name under src/pollypm/. The file-based tracker is the only issue backend currently implemented.'
