---
name: executing-plans
description: Work through a plan systematically — pick next item, ship it, verify, update, move on.
when_to_trigger:
  - execute plan
  - next step
  - work through plan
  - project execution
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Executing Plans

## When to use

Use when a plan exists and the work is mid-flight. Execution discipline is what makes plans pay off; without it, a plan is a document that accumulates in a drawer. Pair this skill with `writing-plans` — the author mode and the executor mode.

## Process

1. **Open the plan at the start of each session.** Not mentally; literally. The plan is the source of truth for "what do I work on?" — do not freelance.
2. **Pick the **next** item, not the most interesting item.** Dependencies matter; the plan has an order for a reason. If the order is wrong, fix the plan first (one commit), then execute.
3. **Scope the pick to a commit.** "Add visual-explainer skill" is commit-sized. "Implement 71 skills" is not. Break larger items into commits as you pick them up.
4. **Ship the commit.** Write the code, run the tests, review the diff, commit, push. Do this in one block; do not context-switch to planning in the middle.
5. **Verify against acceptance criteria.** Re-read the plan's acceptance criteria for this milestone. Does the commit move you closer? Do not check "done" until every criterion is genuinely met.
6. **Update the plan.** Strike through completed items. Note any scope changes with a rationale: "Cut #5 — Vue support deferred to v1.1 because Pinia store shape needs design." The plan stays honest.
7. **Identify the next item** before ending the session. Write it down. Do not rely on tomorrow-you remembering where you were; future-you will want the 10-second handoff note.
8. **Recognize drift.** If the plan has not been updated in two days of work, you are either ahead of plan (update to celebrate) or freelancing (reconcile — is the plan wrong, or is the work wrong?).

## Example invocation

```markdown
# Magic Skills Execution Log — Day 3

## Session start
Plan: docs/plans/magic-skills-v1.md
Current milestone: M2 "All 10 categories shipped".
Done: Architecture (10), Documents (8), Code Quality (10), Testing (6), Frontend (8).
Next: Backend & Database (8 skills).

## Work
- supabase-postgres.md — drafted, RLS examples included, pushed 2c28d89
- neon-postgres.md — drafted, branching workflow included, pushed ...
- (ran smoke test after each, all green)
...

## Session end
Shipped: 8 skills, category 6 complete.
Next session: Deploy / Infrastructure, start with deploy-to-vercel.md.
Plan status: on track; 4 categories remaining, ~2 days of work left.
Handoff note: no blockers; continue.
```

## Outputs

- One commit per pick (or a clear rationale for larger batches).
- Tests green after each commit.
- Plan updated with strikethroughs and notes.
- A handoff note at session end naming the next item.

## Common failure modes

- Picking the interesting item; dependency order breaks and later work stalls.
- Shipping without verifying acceptance; "done" accumulates technical debt.
- Plan never updated; nobody knows the real state, including you in a week.
- No handoff note; resuming costs 15-30min of re-orientation per session.
