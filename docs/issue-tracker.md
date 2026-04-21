# PollyPM Issue Tracker

This document describes the GitHub-backed issue flow used by PollyPM projects
that manage work through `gh issue ...` and `polly:` labels.

## Source of Truth

GitHub Issues is the tracker. A task's state is encoded by exactly one PollyPM
label at a time:

| GitHub label | State |
|--------------|-------|
| `polly:not-ready` | `00-not-ready` |
| `polly:ready` | `01-ready` |
| `polly:in-progress` | `02-in-progress` |
| `polly:needs-review` | `03-needs-review` |
| `polly:in-review` | `04-in-review` |
| `polly:completed` | `05-completed` |

GitHub Projects boards are optional views only. The label on the issue is the
state. Comments carry the handoff notes between implementation and review.

## Working The Queue

1. Create issues with `polly:not-ready` when the work still needs scoping, or
   `polly:ready` when it is fully specified.
2. Pick the oldest issue with `polly:ready`.
3. Move it to `polly:in-progress` before implementation starts.
4. Finish the work, then move it to `polly:needs-review` and leave a comment
   with what changed and how to verify it.
5. Reviewers move it to `polly:in-review`, inspect the diff, and either:
   - move it back to `polly:in-progress` with a corrective comment, or
   - move it to `polly:completed` and close the issue.

## Handoff Notes

Use GitHub comments for the handoff trail:

- Implementation handoff: what was changed, what was tested, and any known
  follow-up.
- Review feedback: specific fixes needed before the issue can complete.
- Completion note: confirm the issue is done and reference the PR if there is
  one.

## Practical `gh` Commands

```bash
gh issue list --label polly:ready --state all --repo owner/repo
gh issue view 123 --repo owner/repo
gh issue edit 123 --remove-label polly:ready --add-label polly:in-progress --repo owner/repo
gh issue comment 123 --body "Ready for review: ..."
gh issue close 123 --repo owner/repo
```

## When To Use This

Use this flow for GitHub-hosted projects that want issue tracking to stay in
the repository and visible to the team. If a project is still on the local
filesystem tracker, use the project-local issue instructions instead.
