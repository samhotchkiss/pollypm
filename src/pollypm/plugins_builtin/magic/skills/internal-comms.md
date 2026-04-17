---
name: internal-comms
description: Status reports, newsletters, and FAQs for stakeholders — tuned for scan-and-move-on reading.
when_to_trigger:
  - status update
  - newsletter
  - stakeholder comms
  - weekly update
  - faq
kind: magic_skill
attribution: https://github.com/travisvn/awesome-claude-skills
---

# Internal Comms

## When to use

Use when writing something multiple people will skim — a weekly update, a launch announcement, an FAQ, a stakeholder report. The audience is busy; the job is to let them get the point in 20 seconds and decide if they need more. This is not the skill for deep technical docs (use `markdown-document`) or sales material (use `canvas-design` + copy).

## Process

1. Open with the summary line. One sentence, maximum 20 words, answering "what happened." If a stakeholder reads only this, they should not be surprised next week.
2. Use the TL;DR + details pattern. TL;DR is three bullet points. Details sits under an H2 for each bullet. Readers who care drill down; readers who do not move on.
3. Order sections by "what the reader needs to know" not "chronological order of work." If something is blocked, blockers go above ships — readers need to know what is stuck.
4. Use concrete numbers. "Shipped 12 PRs this week" beats "productive week." "23% latency reduction" beats "perf improvements."
5. Call out decisions needed from the reader. Bold them. "Decision needed: approve budget for Postgres by Friday." If you bury the ask, it does not happen.
6. End with a "next week" or "next steps" section — 2-3 items. Never end with "that's all"; always plant the next beat.
7. Format for the channel: email gets markdown rendered to HTML, Slack gets Slack-flavored markdown (no tables), Notion gets native blocks. Do not send markdown as plain text to Slack — it reads as noise.

## Example invocation

```markdown
# Polly weekly — 2026-04-15

**TL;DR**
- Shipped plugin discovery (#173) and work service (#198).
- Blocked: Postgres migration needs approval — decision needed by Friday.
- Up next: magic skills starter pack (#247).

## Details

### Shipped

- **Plugin discovery (#173)** — plugins now auto-register from `content_paths`. Docs at `docs/plugin-discovery-spec.md`.
- **Work service (#198)** — SQLite-backed task lifecycle. 34 tests, all green.

### Blocked

- **Postgres migration** — need budget approval for managed Postgres ($80/mo). Decision needed by Fri Apr 18.

## Next week

- Ship magic skills v1 (71 skills).
- Start cockpit v2 design.
```

## Outputs

- A single-file document formatted for the target channel.
- TL;DR at top, details below, decisions bolded.
- Concrete numbers, not vague adjectives.
- A "next week" or "next steps" section closing the piece.

## Common failure modes

- Chronological recap instead of importance order; readers miss the blocker.
- Vague language ("good progress") that tells the reader nothing.
- Burying decisions in the middle; they get ignored.
- Sending markdown source to a channel that does not render it.
