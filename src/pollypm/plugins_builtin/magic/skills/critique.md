---
name: critique
description: Produce constructive, actionable critique of someone else's code — scoped to what they can act on.
when_to_trigger:
  - review this
  - feedback on
  - critique
  - roast this code
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Critique

## When to use

Use when another author has shared code and asked for feedback — informally, pre-PR, or during a paired session. This is different from formal PR review in scope: your job is to help the author make the code better before they ask for a merge, not to gatekeep. Keep it specific, keep it scoped, keep it kind.

## Process

1. Ask what they want feedback on. "The architecture, the readability, the performance, or all three?" Without a scope, you will overwhelm them with everything you see.
2. State what is working before what is broken. Not as a sandwich-compliment; be specific. "The split between `Service` and `Repository` is clean — the `Repository` has zero business logic, which is rare." This builds the signal that you actually read the code.
3. Rank issues by severity: **blocker** (ship bug, security hole, wrong invariant), **quality** (will be painful in six months), **taste** (I would have done it differently). Never present taste issues as quality issues.
4. For each issue, name the **symptom**, the **mechanism**, and the **suggested direction**. Not the full fix — leave room for the author. "The cache is keyed on `user_id` but `user_id` can be None for anonymous sessions, so all anonymous sessions collide in one cache bucket — suggest adding a sentinel or using `(user_id, session_id)`."
5. Link to prior art when you can. A link to a pattern doc, a prior PR, or a reference implementation is worth five paragraphs of explanation.
6. End with one specific next action. "If I were you, I'd fix the cache keying first — it's the only blocker. The other three can wait for a follow-up." Authors leave critique sessions wanting to know what to do next.
7. Time-box it. Ten issues is too many. Pick the top five and file the rest as a follow-up note — or keep them in your head and raise them in the next pass.

## Example invocation

```
Author: "Here's my first draft of the rate limiter — what do you think?"

You:
Working: token-bucket shape is right; clock is injected so it's testable.

Blocker (1):
  Symptom: burst limit is applied AFTER the refill, so a client that waits
  one interval can get 2x burst.
  Mechanism: `refill()` is called unconditionally before the check on
  line 42.
  Direction: refill up to the current time but cap at `burst_size`.
  Prior art: see Stripe's idempotency limiter in
  github.com/stripe/stripe-go/internal/ratelimit.

Quality (2):
  - Magic number 60 in four places — extract to `REFILL_WINDOW_SECONDS`.
  - `limit` and `burst` are both passed separately; they should be a single
    `RateLimit` value object.

Taste (2) — feel free to ignore:
  - I'd name `check()` -> `allow()` so the bool is obvious.
  - I'd invert the early-return on line 38; reads better.

Next: fix the burst-after-refill bug first. The rest can land in a follow-up.
```

## Outputs

- A scoped, ranked critique (blocker / quality / taste).
- Each issue names symptom + mechanism + direction.
- Prior-art links where they clarify.
- One specific next action to close the session.

## Common failure modes

- Dumping every observation at once; author shuts down.
- Presenting taste as quality; author loses trust when they disagree.
- Providing the full fix inline; author does not learn, does not own.
- Softening blockers so much they read as taste; author ships the bug.
