---
name: pre-mortem
description: Imagine the project failed — reverse-engineer why — surface risks before they bite.
when_to_trigger:
  - what could go wrong
  - risk audit
  - pre-mortem
  - risk analysis
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Pre-Mortem

## When to use

Use before committing to a multi-week plan, before a high-stakes launch, or any time the team is overconfident. A pre-mortem is a post-mortem run in advance: assume the worst has happened, list the causes, prioritize which ones to defend against. It takes 30 minutes and saves weeks.

## Process

1. **Set the frame.** "It is six months from now. The project failed. Not 'underwhelmed' — failed. What happened?" This framing unlocks honest critique that "what could go wrong?" does not.
2. **Generate failure modes broadly.** 15-20 minutes of listing. Technical failures, team failures, market failures, scope failures, integration failures. Aim for quantity; do not filter yet.
3. **Cluster the list.** Similar failures group together — "we lost key engineers" and "handoffs dropped balls" cluster as "team continuity." Clustering reveals the real shape.
4. **Score each cluster on likelihood and impact.** 1-5 scale each, multiplied for severity. Do not skip the number — words ("probable", "high") compress too fast and lose the comparison.
5. **Pick the top three clusters.** That is your actual risk surface. The others are real but not worth a dedicated response; keep the list for awareness.
6. **For each top-three cluster, name a mitigation.** A mitigation is a concrete action: "Assign a shadow owner for each module so knowledge does not single-point." Not "improve communication."
7. **Identify the canary signal.** For each risk, what early sign would tell you it is materializing? "If two consecutive sprints miss the milestone, the estimate was wrong." The canary is what you actually monitor.
8. **Time-box the mitigations.** Either in the plan as explicit work items, or on the calendar as review points. A mitigation that is not on the plan is not happening.

## Example invocation

```markdown
# Pre-mortem: Magic Skills v1 Starter Pack

Imagine it's July. We shipped v1 four months ago. It flopped. Why?

## Raw list (10 minutes)

- Skills are too long; agents do not read them.
- Skills are too short; no useful content.
- Surfacing logic (#234) landed late, so nobody sees skills at the right moment.
- Skills contradict PollyPM's own conventions.
- External attribution is wrong, we get a DMCA or a grumpy Twitter post.
- Smoke test was green but the loader does not actually parse frontmatter.
- We shipped 71 generic skills and nobody added new ones — no community effect.
- Skills ended up in the wrong paths (`user_paths` vs `content_paths`).

## Cluster + score (likelihood × impact, 1-5 each)

1. Surfacing not in place: 4 × 5 = 20
2. Content quality (too long / too short / contradictory): 3 × 5 = 15
3. Loader/path mismatch: 2 × 5 = 10
4. Attribution mistakes: 2 × 3 = 6
5. No community contributions: 4 × 2 = 8

## Top three mitigations

1. **Surfacing**: confirm #234 ships before we publicize #247. If slipping,
   demote this to "internal library" scope.
2. **Content quality**: smoke test enforces 30+ lines; reviewer reads 5
   random skills end-to-end before merge; each body includes Example +
   Failure Modes.
3. **Loader/path mismatch**: add a runtime test that loads every skill
   via the magic plugin's loader (not raw markdown parsing).

## Canary signals

1. Surfacing PR #234 slips past April 25 -> re-scope.
2. Reviewer reads first 5 skills; if any fails the "would I follow this?"
   test, pause and fix before continuing.
3. Test fixture doesn't find any skill via the loader -> stop.
```

## Outputs

- A ranked list of failure clusters with scores.
- Top 3 with named mitigations.
- Canary signals that would tell you a risk is materializing.
- Mitigations added to the plan or calendar.

## Common failure modes

- Politeness: refusing to name real failure modes because they are uncomfortable.
- Listing without scoring; every risk feels equal, nothing gets prioritized.
- Mitigations that are verbs without objects ("improve communication"); not actionable.
- No canary; the risk materializes and nobody noticed until it was too late.
