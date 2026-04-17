---
name: svg-design
description: Hand-crafted SVG icons and illustrations produced from a precise spec — no raster fallbacks.
when_to_trigger:
  - icon
  - logo
  - vector illustration
  - svg asset
kind: magic_skill
attribution: https://github.com/madewithclaude/awesome-claude-artifacts
---

# SVG Design

## When to use

Use when the user needs a scalable vector asset — icon, logo, tiny illustration — that will live in a UI or a marketing page. SVG is the right call whenever crispness at any size matters. For photorealistic imagery or painterly art, use `visual-explainer` instead.

## Process

1. Nail down the spec before drawing: dimensions (default 24x24 for icons, 512x512 for logos), stroke vs fill style, one color or full palette, purpose (toolbar icon, favicon, hero illustration).
2. Draft the path mentally as a grid walk. For icons, stay on integer coordinates in a 24x24 viewBox so edges sit on pixel boundaries.
3. Write the SVG by hand — no `<foreignObject>`, no embedded raster. Use `<path>` with the minimum `d` string, not `<polyline>` chains. Stroke-based icons: `stroke-width="2"`, `stroke-linecap="round"`, `stroke-linejoin="round"`. Fill-based: `fill="currentColor"` so CSS can recolor.
4. Normalize the `viewBox` to `0 0 W H` and set `width="W"` + `height="H"` on the root. Never hard-code absolute pixel sizes inside children.
5. Strip editor cruft — no `<metadata>`, no `sodipodi`/`inkscape` namespaces, no XML comment headers. The file ships clean.
6. Run the output through SVGO mentally (flatten transforms, remove empty groups, round to 2 decimals). If a path is >500 chars and it is an icon, you are overdoing it.
7. Emit the SVG inside a code fence plus save to the artifact directory as `<slug>.svg`.

## Example invocation

```
User: "Design a minimal toolbar icon for 'sync'."

Agent response:
```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M21 12a9 9 0 0 1-15 6.7L3 16" />
  <path d="M3 12a9 9 0 0 1 15-6.7L21 8" />
  <path d="M21 3v5h-5" />
  <path d="M3 21v-5h5" />
</svg>
```
Saved to .pollypm/artifacts/task-47/sync-icon.svg.
```

## Outputs

- A single `.svg` file saved to the task artifact directory.
- Inline preview in the chat response.
- `currentColor` for fill/stroke so the consumer controls theming.
- No editor cruft, no raster embeds.

## Common failure modes

- Hard-coding a single color instead of using `currentColor`; breaks theming.
- Leaving Inkscape/Sodipodi namespaces in the output.
- Drawing a logo at 24x24; start bigger and let the consumer downscale.
- Using `<polyline>` with 200 points where a single cubic `<path>` would do.
