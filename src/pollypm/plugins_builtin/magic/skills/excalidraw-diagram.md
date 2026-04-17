---
name: excalidraw-diagram
description: Generate Excalidraw diagrams from natural language and render them to PNG via Playwright for sharing.
when_to_trigger:
  - excalidraw
  - sketch
  - system flow
  - hand-drawn diagram
  - whiteboard diagram
kind: magic_skill
attribution: https://github.com/coleam00/excalidraw-diagram-skill
---

# Excalidraw Diagram

## When to use

Use when the user wants a hand-drawn-feel diagram they can re-edit later. Excalidraw strikes the right register for whiteboarding sessions, RFCs, and anything where a too-polished diagram would feel overbearing. Do not reach for this when the user wants a final marketing asset — use `canvas-design` for that.

## Process

1. Confirm the diagram type: flow, sequence, class hierarchy, ER, or freeform. If unsure, assume flow and proceed.
2. Sketch the graph on paper first: nodes + edges + labels. Keep it under 15 nodes; split larger graphs into two diagrams.
3. Emit Excalidraw JSON directly. Use `rectangle` for services, `ellipse` for data stores, `diamond` for decisions, `arrow` for flow. Set `strokeStyle: "solid"` for primary paths, `"dashed"` for optional.
4. Apply the dark-theme palette: background `#1e1e1e`, stroke `#e0e0e0`, accent `#4a9eff`. Override only when the user specifies brand colors.
5. Save the `.excalidraw` file under the task's artifact directory. Then render a PNG via headless Playwright hitting `https://excalidraw.com` with the file imported.
6. Emit both artifacts: the JSON (for re-editing) and the PNG (for embedding).
7. If the user wants changes, edit the JSON — do not regenerate from scratch. Preserve node IDs so their mental model of "that box" survives.

## Example invocation

```
User: "Sketch the auth flow: browser -> Polly API -> OAuth provider -> callback -> session."

Agent:
1. Type: sequence diagram.
2. Nodes: Browser, Polly API, GitHub OAuth, Session Store.
3. Edges: Browser->API (login click), API->GitHub (redirect), GitHub->API (callback+code), API->Session (write), API->Browser (cookie).
4. Emit excalidraw JSON with 4 rectangles + 5 arrows + labels.
5. Save: .pollypm/artifacts/task-47/auth-flow.excalidraw + .png.
6. Embed PNG in response, link JSON for re-edit.
```

## Outputs

- A `.excalidraw` JSON file (re-editable at excalidraw.com).
- A rendered PNG (embeddable in markdown).
- Both saved to the task's artifact directory.
- Embed the PNG in the chat response with a link to the JSON.

## Common failure modes

- Cramming 30 nodes into one diagram; always split at the seams.
- Forgetting to render the PNG — users cannot embed JSON in GitHub issues.
- Using default colors on a dark-theme doc, producing invisible labels.
- Regenerating from scratch on every edit and destroying node IDs the user referenced.
