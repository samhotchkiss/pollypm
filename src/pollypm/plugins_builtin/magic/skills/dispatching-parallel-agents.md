---
name: dispatching-parallel-agents
description: Decide when to fan out concurrent agents vs. when sequential work is correct — file scopes, shared state, latency.
when_to_trigger:
  - should we parallelize
  - agent fan-out
  - parallelize work
  - fork agents
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Dispatching Parallel Agents

## When to use

Use before firing off a second (or tenth) agent. Parallelism is not free: coordination cost, integration cost, and subtle races. This skill tells you when to parallelize and when sequential is faster end-to-end despite looking slower.

## Process

1. **Parallelize when file scopes are disjoint.** Each agent touches its own set of files. No shared mutable state on disk. This is the cleanest case; fan out without hesitation.
2. **Serialize when agents will contend on the same file.** Two agents editing `src/core.py` will produce merge hell. The "faster" parallelism is slower than one agent doing both edits in sequence.
3. **Parallelize for read-heavy analysis.** Three agents each analyzing a different subdirectory and returning structured reports is a classic good fan-out. Integration is easy — concatenate the reports.
4. **Serialize when an earlier agent's output is the input to a later one.** A research phase that feeds an implementation phase is sequential by nature. Forking the implementation before the research lands is cargo-cult parallelism.
5. **Bound the fan-out.** Two is the first step up from one; four is a jump; ten is a different regime with dedicated coordination. Do not fan out to N just because you can. Each additional agent adds integration overhead that compounds with the square of N.
6. **Write the integration plan before you fork.** "How will I combine the outputs?" If the answer is "I'll figure it out," you will hit integration hell. If the answer is "each agent produces a file in a subdirectory; I concat them with a manifest", you are fine.
7. **Time-box the subagent.** Parallel work without a timeout means one stuck agent holds the whole integration. Every subagent has a deadline and a graceful-failure plan.
8. **Measure after.** Did the fan-out actually save wall-clock time? Include your coordination time. If coordination ate the gains, that is a signal for the next decision.

## Example invocation

```
Scenario A: "Generate 71 skill files across 10 categories."
  Scopes: 10 disjoint directories? No — all in one `skills/` dir.
          But: file names are disjoint per agent, content is independent.
  Verdict: Parallelizable. Each agent owns its category's filenames.
  Integration: coordinator runs smoke test; conflicts only on EXPECTED_COUNT in
  test file. Serialize test-file updates or hand the whole test file to the
  coordinator.

Scenario B: "Refactor WorkService to take a backend protocol, then update all
             callers."
  Phase 1 (refactor core) is upstream of phase 2 (update callers).
  Verdict: Serial. Forking phase 2 before phase 1 lands creates contention
  on the signature of WorkService.

Scenario C: "Analyze three large directories for security issues."
  Read-only. Outputs are independent per-directory reports.
  Verdict: Parallelize. Three agents, concat reports, dedupe findings.

Scenario D: "Add 15 small fixes across the codebase."
  Each fix touches 1-2 files, disjoint from the others.
  Verdict: Parallelize modestly (3-5 agents). Integration is per-fix PR review.
  Do not fan out to 15; the coordinator becomes the bottleneck.
```

## Outputs

- A written call: parallelize (with count) or serialize, with the reason.
- If parallelizing: disjoint file scopes per agent, an integration plan, a time-box.
- If serializing: a reason captured so the next similar decision is easy.

## Common failure modes

- Parallelizing contended work; merge conflicts eat the speedup.
- Serializing clearly independent work because "it's simpler"; leaves hours on the table.
- Fanning out to N because N agents were available; coordination cost dominates.
- No integration plan upfront; subagents finish, coordinator is stuck.
