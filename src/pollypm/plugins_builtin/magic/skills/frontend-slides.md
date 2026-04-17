---
name: frontend-slides
description: Animation-rich HTML slide decks using Reveal.js or Slidev, or convert .pptx into a web-native deck.
when_to_trigger:
  - html slides
  - animated deck
  - reveal.js
  - slidev
  - web presentation
kind: magic_skill
attribution: https://github.com/madewithclaude/awesome-claude-artifacts
---

# Frontend Slides

## When to use

Use when the deck should live on the web — a conference talk link, a landing page, an embedded demo — and can take advantage of live iframes, video, animated transitions, and syntax-highlighted code. If the audience will edit in PowerPoint, pick `pptx-create` instead.

## Process

1. Pick the framework: **Slidev** (Vue + markdown-authored, best DX for code-heavy decks), **Reveal.js** (pure HTML, most customizable), or **impress.js** (2.5D spatial transitions). Default: Slidev.
2. Author slides in one markdown file separated by `---`. Use front-matter per slide for layout (`cover`, `two-cols`, `image-right`, `center`).
3. Constrain typography: two font sizes for titles, one for body, one for code. Slidev default (Inter + Fira Code) is fine; override only for brand.
4. Embed live code with `<<< ./snippets/demo.ts` so snippets live in files and stay linted. Do not hand-paste code into slides — it goes stale.
5. Transitions: `fade` default, `slide-left` for progression, `zoom` for emphasis. Three transition types max in one deck; more feels amateur.
6. Add speaker notes via `<!-- -->` below each slide. These show in presenter mode.
7. Build and save the output: Slidev exports to SPA (`slidev build`), PDF (`slidev export`), or PNG (`slidev export --format png`). Ship all three.

## Example invocation

```markdown
---
theme: default
title: Polly v1 — what shipped
---

# Polly v1

Shipped 2026-04-15.

<!-- Welcome! 20 minutes. -->

---
layout: two-cols
---

# Scope

- Work service
- Plugin discovery
- Memory system v1

::right::

# Cut

- Live activity feed (moved to v1.1)

---

# Code example

```ts
const work = new WorkService(db);
await work.create({ project: 'polly', flow: 'implement_module' });
```

<<< ./snippets/work-create.ts

---
layout: center
---

# Questions?
```

## Outputs

- A source markdown file (authoritative) + built SPA + PDF + PNG exports.
- Speaker notes embedded in HTML comments.
- Live code loaded from snippet files, not hand-pasted.
- All outputs saved to the task artifact directory.

## Common failure modes

- Using five transition types; reads as "I discovered CSS animations today."
- Pasting code directly into slides; goes stale within a sprint.
- Skipping speaker notes; presenter mode becomes useless.
- Building HTML-only and forgetting that offline PDF is the audience's backup.
