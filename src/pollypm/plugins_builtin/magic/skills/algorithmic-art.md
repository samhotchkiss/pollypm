---
name: algorithmic-art
description: Generative art via p5.js with seeded randomness — reproducible, tweakable, shareable as a single HTML file.
when_to_trigger:
  - generative art
  - creative visual
  - algorithmic
  - p5.js
  - procedural
kind: magic_skill
attribution: https://github.com/travisvn/awesome-claude-skills
---

# Algorithmic Art

## When to use

Reach for this when the user wants a visually rich piece whose character comes from a rule system rather than hand drawing — flow fields, particle systems, L-systems, cellular automata, noise landscapes. The output is a self-contained HTML file the user can re-run with different seeds to get variations on the same theme.

## Process

1. Pick the generator family: flow field, noise blob, particle system, grid subdivision, or recursive tree. Do not combine families in one piece — each has a different aesthetic register.
2. Commit to a seed up front. Every random call runs through `random(seed)` from p5. This makes the output reproducible and gives the user a knob to turn.
3. Define the palette before writing code: background + 3 strokes. Hex values, committed. Pull from the user's brand via `brand-guidelines` when one exists.
4. Write p5.js in `instance mode` inside a single HTML file. Inline the p5 script from a CDN version-pinned URL (no unpinned `latest`). Set canvas size to 1200x1200 unless the user specifies.
5. Expose seed, palette, and 2-3 rule parameters as `const` at the top of the sketch. The user should be able to tweak one number and see a clear variation.
6. Add a keyboard handler: `s` saves PNG, `r` re-seeds with a random seed and re-renders. This is the contract for every algorithmic piece.
7. Save the HTML to artifacts. Also run the sketch headlessly via Playwright and capture a PNG snapshot as a preview thumbnail.

## Example invocation

```
User: "Generate a flow-field piece in our brand colors."

Agent:
1. Family: flow field.
2. Seed: 42.
3. Palette: #0d1117 bg, #4a9eff, #ff6b35, #f5f5f5 strokes.
4. Single HTML file, p5 v1.9.0 CDN pinned.
5. Parameters: NOISE_SCALE=0.005, PARTICLES=2000, STEPS=200.
6. Keybinds s/r wired.
7. Save: artifacts/task-47/flowfield.html + flowfield.png.
```

## Outputs

- One self-contained HTML file with inlined p5 CDN reference.
- PNG preview rendered via headless Playwright.
- Seed, palette, and rule params exposed at the top as editable constants.
- Keybinds `s` (save) and `r` (re-seed) wired.

## Common failure modes

- Using `Math.random()` instead of p5's seeded `random()` — output is not reproducible.
- Mixing flow field + particle system in one sketch — visually noisy.
- Leaving CDN URL as `latest`; the piece breaks when p5 ships a breaking change.
- Forgetting the save-key binding so the user cannot export a variation they like.
