---
name: visual-explainer
description: Turn any concept into an AI-generated visual — whiteboard, infographic, diagram, or mockup — using OpenAI or Gemini image generation.
when_to_trigger:
  - explain visually
  - diagram this
  - create a visual
  - make a whiteboard
  - illustrate this concept
kind: magic_skill
attribution: https://github.com/ericblue/visual-explainer-skill
---

# Visual Explainer

## When to use

Reach for this skill when a user says "explain this visually" or when a concept is inherently spatial, hierarchical, or too tangled for prose. Words are cheap; a generated image forces you to commit to one clean metaphor. Use it for pitches, onboarding, and any moment where a collaborator will scroll past a wall of text.

## Process

1. Ask one clarifying question: "whiteboard sketch, polished infographic, system diagram, or UI mockup?" Pick one style — do not deliver four.
2. Draft a tight prompt: subject + viewpoint + style + labels. Keep it under 80 words. Name every text element you want rendered so the model does not hallucinate labels.
3. Call the image model. Default to `gpt-image-1` for whiteboards, `imagen-3` for infographics, `gemini-2.5-flash-image` for mockups. Request 1024x1024 unless the user specifies aspect ratio.
4. Inspect the output. If any label is garbled, regenerate with the label quoted verbatim in the prompt ("the label reads exactly: `Throughput`").
5. Save the image under the task's artifact directory and emit a markdown embed plus a one-paragraph caption explaining the diagram.
6. If the user wants variations, generate three at most and ask them to pick one. Do not iterate past round two without a concrete change request.

## Example invocation

```
User: "Explain our job-queue architecture visually."

Agent:
1. Whiteboard sketch, infographic, or system diagram? -> system diagram.
2. Prompt: "Dark-theme system diagram, isometric perspective. Top layer: FastAPI service labeled 'Polly API'. Middle: Redis-backed RQ queue labeled 'Work Queue'. Bottom: three worker boxes labeled 'Worker 1', 'Worker 2', 'Worker 3'. Arrows: API -> queue -> workers. Clean typography, muted slate palette."
3. Generate via gpt-image-1 @ 1024x1024.
4. Save to .pollypm/artifacts/task-47/architecture.png.
5. Emit: ![architecture](artifacts/task-47/architecture.png) + caption.
```

## Outputs

- One image file saved under the task's artifact directory.
- A markdown embed pointing at the file.
- A two-sentence caption naming the parts and the flow.
- (Optional) One regeneration if the first attempt garbled labels.

## Common failure modes

- Generating four stylistic variants when the user asked for one explanation — pick a style, commit.
- Letting the model hallucinate labels; always enumerate exact text in the prompt.
- Producing a "beautiful" image that does not teach; caption must state what the viewer should learn.
- Skipping the save-to-artifacts step; an unsaved image is useless five minutes later.
