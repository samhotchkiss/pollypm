---
name: extract-design-system
description: Extract design tokens — colors, spacing, type scale — from existing code or screenshots into a structured token file.
when_to_trigger:
  - extract design system
  - analyze design tokens
  - reverse engineer styles
  - audit styles
kind: magic_skill
attribution: https://github.com/madewithclaude/awesome-claude-artifacts
---

# Extract Design System

## When to use

Use when a project has grown organically and visual tokens are scattered across stylesheets, inline styles, or design screenshots. The skill produces a canonical token file so future work stops drifting. Run this before you introduce any new design system — you cannot replace what you have not measured.

## Process

1. Scope the input. Either (a) a directory of stylesheets and component files, or (b) a set of screenshots. Never mix — run the skill twice if you have both.
2. For code input: scan every `.css`, `.scss`, `.tsx`, `.jsx` for color literals (hex, rgb, hsl, oklch), px/rem values, font-family declarations, border-radius, box-shadow. Use AST walks, not naive regex.
3. For screenshots: sample a 10x10 grid of pixels per frame, cluster with k-means (k=16), drop near-neutrals, filter by frequency. The dominant 6-8 colors are the palette.
4. Cluster tokens: colors by perceptual distance (deltaE <5 = same token), spacing by ratio (4/8/12/16/24 = same scale), fonts by family.
5. Emit a `design-tokens.json` file following the W3C Design Tokens draft schema: `color`, `dimension`, `fontFamily`, `fontWeight`, `lineHeight`, `shadow`, `border`.
6. Report the findings with counts: "34 distinct color literals collapsed into 7 tokens; 12 spacing values collapsed into a 4/8/16/24/32/48 scale." This tells the user how much drift there was.
7. Offer a follow-up: "Want a migration patch that replaces raw literals with token references?" — only if requested.

## Example invocation

```
User: "Extract our design system from src/components/."

Agent:
1. Scope: src/components/**/*.{tsx,css}.
2. Scan finds 41 color literals, 23 spacing values, 4 font-families.
3. Cluster: 8 color tokens (primary, primary-hover, surface, text, text-muted, accent, success, danger). 5 spacing (4/8/16/24/48). 2 fonts.
4. Emit docs/design-tokens.json.
5. Report: "41 -> 8 colors (80% reduction). 23 -> 5 spacing (78% reduction)."
```

## Outputs

- A `design-tokens.json` file in W3C Design Tokens format.
- A report: raw count -> token count per category, with reduction %.
- A flag list of outliers (tokens that appear only once — candidates for removal).
- Optional migration patch if requested.

## Common failure modes

- Using regex on CSS instead of an AST; misses inline-styled React components entirely.
- Clustering colors by raw hex distance instead of perceptual deltaE; produces redundant tokens that look identical.
- Treating every unique px value as a token; a scale has 5-8 entries, not 50.
- Skipping the outlier report; design debt hides in one-off values.
