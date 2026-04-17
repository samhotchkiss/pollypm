---
name: design-taste-frontend
description: Cultivate aesthetic judgment for UI — hierarchy, contrast, whitespace, microinteractions, type.
when_to_trigger:
  - looks bad
  - design feedback
  - ui critique
  - feels generic
  - needs polish
kind: magic_skill
attribution: https://github.com/madewithclaude/awesome-claude-artifacts
---

# Design Taste for Frontend

## When to use

Use when someone hands you a UI that "works but feels off" and asks what is wrong. Or when reviewing your own output and suspecting it reads generic. This skill names what is missing so the fix is actionable — not "make it prettier," but "the hierarchy collapses because every heading is the same weight."

## Process

1. Squint at the screenshot. Literally: half-close your eyes. Whatever you see at low contrast is the real hierarchy. If nothing stands out, there is no hierarchy — that is problem #1.
2. Check contrast quantitatively. Text against background: 4.5:1 for body, 3:1 for large. Use a contrast checker; do not eyeball. Low contrast reads as "unfinished" even when the user cannot name why.
3. Measure whitespace. For each element, is the gap to its neighbor too tight, too loose, or on the grid? Generic UIs under-space; confident UIs over-space. When in doubt, add.
4. Check the type scale. How many distinct sizes on this page? Three is ideal, four is okay, five+ is drift. How many weights? Two is ideal. Three means you are using weight as decoration.
5. Count the colors. Primary + secondary + accent + 2-3 neutrals = 5-6 total. Seven+ means the palette is running the design, not the other way around.
6. Look for **alignment** failures. Grid-aligned elements feel intentional; off-by-2 elements feel broken. Every edge should line up with another edge.
7. Find the **focal point**. Where does the eye go first? If nowhere, the page has no focal point — add contrast via size, color, or whitespace until one emerges.
8. Ship specific fixes, ranked: "Increase h1 from 32px to 56px" (hierarchy), "bump body from #888 to #d0d0d0" (contrast), "double the padding around the card" (whitespace). Never "make it look better."

## Example invocation

```
User: "This dashboard feels generic. What's wrong?"

Diagnosis:
1. Squint test: all text reads as same weight. No clear entry point.
2. Contrast: body text #888 on #f5f5f5 = 3.2:1, fails AA. Fix: #555.
3. Whitespace: cards have 16px padding; should be 32px at this density.
   Gap between rows is 8px; should be 24px. Everything feels crammed.
4. Type scale: 14, 15, 16, 18, 20, 22, 24 px in use. Collapse to 14/16/20/32.
5. Colors: seven near-identical blues. Pick one primary and one hover state.
6. Alignment: chart y-axis does not align with card padding. Move chart left 8px.
7. Focal point: none. Promote the "most urgent task" card with a 2x height + accent border.

Ship in order of impact: focal point (hierarchy), contrast, whitespace, type scale, palette.
```

## Outputs

- A ranked list of specific, numeric fixes.
- A focal-point decision — which element leads the eye.
- Contrast measurements for the main text-on-bg pairs.
- A before/after screenshot if the fixes are trivial enough to apply in-session.

## Common failure modes

- Saying "make it prettier" without naming what is wrong.
- Fixing symptoms (font change) while the root cause (no hierarchy) remains.
- Chasing trends; last year's aesthetic looks as dated as five years ago's.
- Ignoring contrast because "it's a design choice"; design choices should not violate AA.
