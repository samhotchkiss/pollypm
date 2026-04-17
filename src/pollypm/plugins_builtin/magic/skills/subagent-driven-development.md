---
name: subagent-driven-development
description: Coordinate multiple AI agents on a single goal — split scope, integrate outputs, resolve conflicts.
when_to_trigger:
  - split this work
  - parallel agents
  - subagent
  - multi-agent work
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Subagent-Driven Development

## When to use

Use when a task has enough independent parallelism that forking multiple subagents beats serial execution. PollyPM is built around this model — workers fan out per task, the supervisor integrates. Apply this skill when you are the coordinator or when you need to decide between parallel and sequential.

## Process

1. **Identify independent seams.** Work can be split across seams that do not share mutable state: different files, different modules, different sub-features. If two subagents would edit the same file, the seam is wrong — find a new one or serialize.
2. **Write each subagent's brief in writing.** Title, scope, inputs, deliverables, acceptance criteria, files they may touch, files they must not touch. This brief is the contract; vagueness leads to integration hell.
3. **Fan out with a shared plan.** Each subagent gets a copy of the overall plan plus their specific slice. They should understand how their piece integrates with the whole, not just their local scope.
4. **Isolate working directories.** One worktree per subagent (see `using-git-worktrees`). Separate branches, separate build outputs, no shared mutable state on disk.
5. **Commit per subagent, integrate in one pass.** Each subagent pushes to their own branch. The coordinator rebases and merges them in a deterministic order. Do not let subagents push to the same branch — history becomes an interleaved mess.
6. **Check for cross-cutting assumptions.** After the first subagent ships, re-read the others' briefs to see if their assumptions held. If subagent A changed the error shape, subagent B may need to update.
7. **Integrate with a single commit** when conflicts appear. The coordinator owns the integration commit — do not pass merge conflicts down to subagents. They finish their work; you stitch it together.
8. **Post-integration verification.** Full test suite, smoke test of the combined behavior, manual check of any cross-subagent flow. A green test per subagent is not sufficient.

## Example invocation

```
Coordinator plan: ship magic skills v1 (71 skills, 10 categories).

Seams:
- Each of the 10 categories is a clean seam (no shared files, no shared state).
- Smoke test is the only cross-cutting artifact — coordinator owns it, subagents only update the expected count.

Subagent briefs (brief per subagent):
  1. "Architecture & Visualization (10 skills) — create 10 .md files per the
      catalog. Update smoke test EXPECTED_COUNT to 10. Do not touch other
      categories or the manifest."
  2. "Documents (8 skills) — ..."
  ... etc

Workflow:
- Coordinator creates 10 worktrees, one per subagent.
- Subagents execute in parallel.
- Coordinator rebases in order (1, 2, 3, ..., 10), resolves any smoke-test
  conflicts by taking the later commit's EXPECTED_COUNT + adding prior categories.
- Integration: full pytest pass + count == 71.
```

## Outputs

- A written brief per subagent with scope + do-not-touch list.
- One worktree and branch per subagent.
- Integration commit owned by the coordinator.
- Cross-cutting tests verified after integration.

## Common failure modes

- Seams that share mutable state; subagents race, work stomps work.
- Vague briefs; subagents improvise, deliverables do not fit together.
- Subagents pushing to the same branch; history tangles.
- Skipping post-integration verification; each slice works in isolation but the whole is broken.
