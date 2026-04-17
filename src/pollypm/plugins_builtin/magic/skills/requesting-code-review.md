---
name: requesting-code-review
description: Structure a PR description so reviewers can make a decision in under five minutes.
when_to_trigger:
  - ready for review
  - open a PR
  - needs review
  - request review
  - draft pr
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Requesting Code Review

## When to use

Use every time you open a PR. A good PR description is the single highest-leverage investment in reviewer time; it is the difference between "approved in 10 minutes" and "stuck for two days." Do not treat the description as an afterthought.

## Process

1. Title: imperative, under 70 chars, scoped. `feat(work): cascade cancellation to children`. Never "WIP" on a PR you expect merged today.
2. Start the body with one sentence on **why**. Not what — reviewers can read the diff. Why did this need to happen? What problem does it close?
3. Include a **Summary** section: 2-4 bullets on what changed. Each bullet names a file or module so reviewers know where to look.
4. Include a **Test plan** section: bulleted checklist of what you verified. This is what reviewers can re-run themselves. Include commands: `uv run pytest tests/test_work_service.py`.
5. Include a **Risk** or **Out of scope** section when relevant. If you touched a shared module, name the downstream consumers you verified. If you intentionally did not handle case X, say so.
6. Screenshots or recordings for any UI change. Always. Reviewers should not have to pull the branch to see what you mean.
7. Link the issue with `Closes #247` (or `Refs #247` for partial fixes). GitHub's auto-close depends on exact phrasing; `Closes`, `Fixes`, `Resolves` all work.
8. Pick reviewers deliberately. Name the person who knows the module. Do not @-team and hope someone grabs it.

## Example invocation

```markdown
## Summary

Cascade cancellation from a parent task to all its descendants. Closes
the gap where cancelled parents left children running indefinitely.

- `src/pollypm/work/service.py` — `cancel` now recurses via `children_of`.
- `tests/test_work_service.py` — new test exercises two-level cascade.

## Test plan

- [x] `uv run pytest tests/test_work_service.py` — all 34 green
- [x] Manual: create parent+child via `pm task create`, cancel parent,
      confirm child status flips in `pm task list`.
- [x] No regressions in `tests/integration/test_work_flow.py`.

## Risk

Touches the WorkService public API. Advisor plugin and planner plugin
both consume `cancel` — I grep'd both, neither branches on child
behavior. No changes required there.

Closes #311
```

## Outputs

- A PR with title, why-sentence, Summary, Test plan, Risk, and issue link.
- Screenshots attached for UI changes.
- Specific reviewers requested by name.
- A `Closes #N` trailer so the issue auto-closes on merge.

## Common failure modes

- Title = "WIP" or "fixes." Reviewer cannot triage without opening it.
- Body = "see diff." Reviewer cannot decide if the change is the right shape.
- No test plan; reviewer has to reconstruct your validation.
- Requesting review from @everyone; no one feels ownership, review stalls.
