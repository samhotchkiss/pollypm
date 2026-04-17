---
name: architecture-diagram
description: Generate standalone HTML/SVG architecture diagrams with a dark theme, self-contained and shareable.
when_to_trigger:
  - architecture diagram
  - system architecture
  - infrastructure layout
  - deployment diagram
kind: magic_skill
attribution: https://github.com/Cocoon-AI/architecture-diagram-generator
---

# Architecture Diagram

## When to use

Reach for this when the user wants a polished, single-file HTML/SVG diagram they can drop into a doc, wiki, or RFC. These are the "executive-friendly" diagrams — clean shapes, clear layers, readable at thumbnail size. Skip this for informal sketches; use `excalidraw-diagram` instead.

## Process

1. Identify the layers: user tier, edge/CDN, API tier, service tier, data tier, external dependencies. If a layer is empty, omit it.
2. List every component with its technology label ("FastAPI", "Postgres 15", "Redis", "S3"). Do not render generic boxes — name the tech.
3. Emit a single HTML file containing inline SVG. Use `<g>` for layer groups, `<rect>` for components, `<path>` for connections. No external CSS, no external fonts — fully self-contained.
4. Apply the dark theme: page `#0d1117`, layer `#161b22`, component `#21262d`, stroke `#58a6ff`, text `#c9d1d9`. This matches GitHub's dark palette so it composites cleanly.
5. Add a legend if there are more than two edge styles (sync vs async vs event).
6. Save as `architecture.html` in the task artifact directory. Open it once headlessly to confirm it renders without warnings.
7. Offer a PNG export via Playwright screenshot only if the user asks — the HTML is the primary artifact.

## Example invocation

```
User: "Draw our production architecture."

Agent:
1. Layers: User -> Cloudflare -> Polly API (FastAPI on Fly.io) -> [Postgres, Redis, S3].
2. Emit HTML with inline SVG:
   - <g id="user-tier"> with a single user icon
   - <g id="edge"> with Cloudflare rect
   - <g id="api"> with FastAPI rect
   - <g id="data"> with three rects
   - <path> arrows between layers
3. Dark theme applied.
4. Save: .pollypm/artifacts/task-47/architecture.html.
5. Confirm renders.
6. Embed screenshot; link HTML file.
```

## Outputs

- A single self-contained `architecture.html` file with inline SVG.
- Dark-theme palette applied by default.
- All technology names labeled on components.
- Layer groupings visually separated.

## Common failure modes

- Linking to external CSS or fonts; the file must work offline.
- Using generic labels like "Database" instead of "Postgres 15".
- Cramming all components into one layer; always group by tier.
- Forgetting to check it renders — always open the file once before handing it off.
