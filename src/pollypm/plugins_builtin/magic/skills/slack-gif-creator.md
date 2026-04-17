---
name: slack-gif-creator
description: Animated GIFs sized and palette-tuned for Slack — emoji, reactions, small celebrations.
when_to_trigger:
  - gif
  - animated emoji
  - slack reaction
  - celebration image
kind: magic_skill
attribution: https://github.com/travisvn/awesome-claude-skills
---

# Slack GIF Creator

## When to use

Use when the user wants a small animated GIF for Slack — a reaction, a custom emoji, a tiny celebration clip. Slack has hard constraints (128x128 for emoji, 1MB file size cap) and this skill bakes those in so you do not ship a GIF Slack rejects.

## Process

1. Confirm the use: custom emoji (128x128, <128KB strongly preferred), inline reaction image (up to 480x480, <1MB), or larger channel embed (up to 720x720, <2MB). Defaults: emoji.
2. Pick the animation approach: SVG-to-GIF via a headless Chrome render loop, or p5.js canvas frames assembled via `gifenc`. For text-heavy emoji, use SVG; for organic motion, use p5.
3. Keep the frame count tight — 12-24 frames, 8-12 fps. A 1.5s loop at 12fps is 18 frames. More frames = bigger file = Slack rejects.
4. Reduce to a 64-color palette using median-cut quantization. Solid backgrounds compress best; avoid gradients in emoji.
5. Assemble the GIF with `gifenc` (browser-friendly) or `gifski` (native). Set `loop=0` for infinite, which is what Slack expects.
6. Verify the file size before delivering. If over budget, drop frames first, then reduce palette, then shrink dimensions. Never compress with lossy JPEG-style artifacts — they look awful in emoji.
7. Save the GIF to the artifact directory and embed a preview in the response.

## Example invocation

```
User: "Make a Slack emoji for 'shipped it' — something that pulses green."

Agent:
1. Use: custom emoji 128x128.
2. Approach: SVG-to-GIF. Letters 'SHIPPED IT' with pulsing green fill.
3. 16 frames @ 12fps = 1.33s loop.
4. Palette: green ramp + white, 16 colors total.
5. Assemble via gifenc, loop=0.
6. File size check: 42KB. Under budget.
7. Save: artifacts/task-47/shipped-it.gif. Embed preview.
```

## Outputs

- A `.gif` file sized for the target Slack context.
- File size confirmed under Slack's cap before delivery.
- Loop mode set to infinite (loop=0).
- Preview embedded in the response.

## Common failure modes

- Animating at 30fps because "it looks smoother" and blowing past the size cap.
- Using gradients in an emoji that gets quantized to 16 colors — produces banding.
- Forgetting to set `loop=0`; single-play GIFs feel broken in Slack.
- Shipping at 256x256 when the target is an emoji — Slack scales it down and destroys detail.
