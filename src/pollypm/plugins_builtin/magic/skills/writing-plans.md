---
name: writing-plans
description: Decompose a goal into a written plan with milestones, acceptance criteria, and explicit non-goals.
when_to_trigger:
  - plan a project
  - design phase
  - write a plan
  - project plan
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Writing Plans

## When to use

Use at the start of any multi-day effort. The plan is not the work; the plan is the artifact that lets you — and your future self, and your collaborators — decide what matters when things drift. Skip planning only for tasks under half a day, and only if you are alone.

## Process

1. **State the goal in one sentence.** If you cannot, the goal is not ready. "Ship a magic-skills starter pack" beats "improve the magic plugin experience."
2. **Name 2-5 milestones that carve the goal.** Each milestone is a demonstrable step — something you can show, not just a phase name. "10 skills with smoke test passing" beats "build skills."
3. **For each milestone, write acceptance criteria.** Bulleted, testable. "Acceptance: 71 skill files exist, smoke test is green, each body is 30+ lines." A criterion you cannot check is not acceptance.
4. **List non-goals explicitly.** "Non-goals: rewrite the magic plugin's loader; design a second-tier skill library; wire surfacing logic (covered in #234)." Non-goals kill scope creep before it starts.
5. **Identify the riskiest assumption** and plan to resolve it first. "We assume skills can be surfaced via plain markdown frontmatter — if not, scope changes." The first milestone tests this assumption.
6. **Estimate coarsely.** "Milestone 1: 1 day. Milestone 2: 2 days. Buffer: 1 day." Estimates in half-day or day granularity; finer is false precision.
7. **Capture open questions at the end of the plan.** "Open: should skills support YAML or TOML frontmatter? — defer to owner." Do not block planning on these; surface them.
8. **Review with one collaborator before starting.** A plan that survives ten minutes of skeptical questioning is a plan worth executing. A plan that does not is a plan worth fixing now.

## Example invocation

```markdown
# Magic Skills v1 Plan

## Goal
Ship a curated 71-skill starter pack installed as content in the `magic` plugin.

## Milestones

1. **Category 1 complete (Architecture & Visualization, 10 skills)** — 4h
   Acceptance: 10 .md files exist under `plugins_builtin/magic/skills/`,
   smoke test (count=10) is green, each body >= 30 lines.

2. **All 10 categories shipped** — 4 days
   Acceptance: 71 files, smoke test (count=71) green, zero name collisions.

3. **Documentation + issue closed** — 0.5d
   Acceptance: `docs/magic-skills-catalog.md` linked from magic plugin
   README; issue #247 closed with summary.

## Non-goals
- Rewriting the magic plugin's loader (covered in #173).
- Wiring surfacing logic to task context (covered in #234).
- Designing a second-tier skill library (out of scope).

## Riskiest assumption
Skills can be surfaced via plain markdown + YAML frontmatter through the
existing plugin-discovery content mechanism — if not, we need to build a loader.

## Estimate
4 days content + 0.5 day docs. Buffer: 1 day.

## Open questions
- Should skills support TOML frontmatter in addition to YAML? — defer to maintainer.
- What's the cap for "common failure modes" per skill? — 2-4 sentences.
```

## Outputs

- A markdown plan committed to the repo (or issue body).
- Goal, milestones, acceptance criteria, non-goals, risks, estimate, open questions.
- One collaborator review before starting.

## Common failure modes

- Vague goals ("improve X"); no way to know when done.
- Milestones as phase names; no demonstrable artifact per milestone.
- Skipping non-goals; scope creeps by the end of week one.
- No risk analysis; the "aha, this doesn't work" moment arrives after spending the budget.
