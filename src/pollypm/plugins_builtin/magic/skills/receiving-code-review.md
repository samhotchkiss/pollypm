---
name: receiving-code-review
description: Triage review feedback, address comments without churn, request re-review cleanly.
when_to_trigger:
  - review came back
  - address feedback
  - reviewer comments
  - requested changes
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Receiving Code Review

## When to use

Use every time a reviewer leaves comments. The goal is a clean second pass — you want the reviewer to see exactly what changed and exactly what you pushed back on. Done poorly, review turns into a three-day text thread. Done well, review wraps in one more round.

## Process

1. Read every comment before responding to any. Do not fire-and-forget reply to the first — you need the full shape of the feedback to avoid contradicting yourself.
2. Categorize comments into: **accept**, **push back**, **clarify**. Budget 60% accept, 20% push back, 20% clarify. If you are accepting <40%, you are capitulating; if >80%, you are not thinking critically.
3. For accepts: make the change, reply with a single emoji or short ack, and resolve the thread. Do not re-explain what the reviewer said.
4. For push-backs: reply with the reasoning, not the conclusion. "Agree in the common case, but here we deliberately trade X for Y because Z. Here's the relevant test." Cite prior art. Then offer a path: "If you still think we should, I'll change it."
5. For clarifies: ask the question back with a specific option. "Did you mean A or B?" is better than "what do you mean?" — do not make the reviewer write more words.
6. Make all changes in one or two commits with clear messages: `review: address feedback on cancel cascade`. Do not squash yet — reviewers appreciate seeing discrete changes during the second pass.
7. After all threads are addressed, post one summary comment tagging the reviewer: "Addressed all except X and Y; see threads for reasoning. Ready for re-review." This is the ping that restarts their queue.
8. When approved and merged, squash if the team squashes; otherwise leave the review iteration commits as-is.

## Example invocation

```
Reviewer leaves 8 comments. You triage:
- 5 accept (typos, missing docstring, simpler helper, rename var, remove dead code)
- 2 push back (removing the retry loop — still want it because flaky upstream; splitting into two files — small enough to leave as one)
- 1 clarify ("not sure what `resolve_parent` returns in the empty case" — ask: "Do you mean the None branch or the [] branch? They're handled on lines 34 and 39.")

For each accept: change + emoji ack + resolve thread.
For push-backs: reply with reasoning and prior-art citation, leave thread open.
For clarify: specific yes/no question back.

Commit: "review: address feedback on cancellation cascade"
Summary comment: "Addressed 6/8 — see 2 open threads for reasoning. Ready for re-review, @alice."
```

## Outputs

- Comments categorized and each thread explicitly handled.
- One or two commits addressing accepted feedback.
- Push-backs with reasoning, not just conclusions.
- A summary ping when ready for re-review.

## Common failure modes

- Accepting every comment to avoid conflict; PRs get weaker, reviewer's taste is not a substitute for yours on questions where you have context.
- Pushing back with "I disagree" and no reasoning; escalates the thread.
- Making 14 tiny fix-up commits; second-round review becomes a mess.
- No summary ping; reviewer does not know it is ready again and the PR sits.
