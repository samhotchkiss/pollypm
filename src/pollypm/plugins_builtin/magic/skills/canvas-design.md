---
name: canvas-design
description: Produce PNG or PDF visual designs with an explicit design philosophy — hierarchy, rhythm, restraint.
when_to_trigger:
  - marketing image
  - design asset
  - visual design
  - social graphic
kind: magic_skill
attribution: https://github.com/madewithclaude/awesome-claude-artifacts
---

# Canvas Design

## When to use

Use when the user needs a finished raster or PDF asset — social graphic, blog hero, one-pager, pitch slide — where typography, spacing, and hierarchy decide success. This skill forces you to commit to a design philosophy before opening the canvas. For editable vector work, pick `svg-design`.

## Process

1. State the design philosophy in one line before touching tools. Examples: "Swiss editorial — bold sans headline, generous whitespace, one accent color." Commit. Do not shop for styles mid-render.
2. Define the grid: 12-col for horizontal media, 6-col for square, 4-col for vertical. Set margin = 2 gutters. All elements align to the grid.
3. Pick exactly two typefaces: one display, one text. Three weights total across them. Never more. Default to `Inter` + `Fraunces` unless the user has brand fonts.
4. Limit the palette to three colors plus background. Use `brand-guidelines` to pull colors if the project has them.
5. Render via HTML+CSS at the target dimensions, screenshot with Playwright at 2x DPI for PNG. For PDF output, use `@page` rules and export via headless Chrome print.
6. Inspect the output at thumbnail size. If the hierarchy collapses at 200px wide, the design is wrong — bigger headline, less text.
7. Save to artifacts. Export both `.png` (2x) and `.pdf` if the user mentioned print.

## Example invocation

```
User: "Design a launch graphic for our v1 release."

Agent:
1. Philosophy: "Swiss editorial. Display serif headline 'Polly v1'. One accent: #ff6b35. Lots of whitespace."
2. Grid: 1200x630 (OG image), 12-col, 48px margin.
3. Fonts: Fraunces 900 display, Inter 400 text.
4. Palette: #0d0d0d bg, #f5f5f5 text, #ff6b35 accent.
5. Render HTML, Playwright screenshot @ 2x.
6. Output: .pollypm/artifacts/task-47/launch.png (2400x1260).
```

## Outputs

- A 2x-resolution PNG at the target dimensions.
- Optional PDF export if the user specified print.
- A one-line design philosophy recorded with the asset (so future edits can match).
- All assets saved to the task's artifact directory.

## Common failure modes

- Shopping for design styles mid-render — commit upfront or the output looks muddled.
- Using four typefaces because "one more wouldn't hurt."
- Exporting at 1x so the asset looks soft on retina displays.
- Forgetting to inspect at thumbnail size; designs that only work at full size fail on feeds.
