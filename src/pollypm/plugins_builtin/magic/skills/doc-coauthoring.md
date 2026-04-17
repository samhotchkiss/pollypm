---
name: doc-coauthoring
description: Coordinate multiple contributors on a single document — merge styles, reconcile conflicts, preserve voices.
when_to_trigger:
  - co-author
  - merge contributions
  - combine drafts
  - multi-author doc
kind: magic_skill
attribution: https://github.com/travisvn/awesome-claude-skills
---

# Doc Coauthoring

## When to use

Use when two or more people have submitted drafts — or notes — for the same document and you need to produce one coherent version. Naive concatenation fails: voices clash, points duplicate, structure breaks. This skill produces one document that reads as if written by a single author, while preserving each contributor's best bits.

## Process

1. Collect all inputs. Label each by contributor: `input-alice.md`, `input-bob.md`. Do not edit inputs — keep them as provenance.
2. Build a union outline. Go through each input, extract H2/H3 headings, and merge into one outline. Duplicate headings collapse; unique ones preserve. Order by importance, not contributor.
3. For each heading, collect the candidate paragraphs from all inputs. Pick the strongest and note where you borrowed from others. If two inputs disagree, flag with `TODO: resolve — Alice says X, Bob says Y` inline.
4. Normalize voice. Pick one target: first-person plural ("we shipped") for team docs, third-person ("the team shipped") for external. Rewrite every paragraph in the target voice.
5. Preserve distinctive insights verbatim when they are the contributor's signature. Attribute with a footnote or inline citation: "per Alice's analysis..." Do not wash out every voice.
6. Check for duplication. If two sections say the same thing differently, merge them or cut the weaker one.
7. End with an attribution block: "Contributors: Alice, Bob, Claude." Makes authorship legible without cluttering the body.

## Example invocation

```
User: "I have three drafts of the Q1 review. Merge them."

Agent:
1. Load input-alice.md, input-bob.md, input-carol.md.
2. Outline union: Shipped, Blocked, Metrics, Next Quarter. (Carol's "People" section preserved; Alice's "Retro" absorbed into Next Quarter.)
3. Per section: pick strongest paragraph, flag contradictions.
4. Voice: first-person plural, present tense for metrics.
5. Carol's team-health insight preserved verbatim with footnote.
6. Duplicated "shipped X" lines merged.
7. Attribution block at end.

Output: q1-review.md with inline TODO markers for 2 contradictions.
```

## Outputs

- A merged document in the target voice.
- Source inputs preserved unchanged for provenance.
- Inline TODO markers for every factual contradiction.
- Attribution block naming all contributors.

## Common failure modes

- Naive concatenation; voices clash paragraph to paragraph.
- Washing out every voice so no one feels heard; keep distinctive phrases.
- Silently resolving contradictions; always flag them for human decision.
- Losing source inputs; provenance matters when contributors disagree later.
