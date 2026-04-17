---
name: brand-guidelines
description: Apply consistent brand colors and typography to any artifact so every deliverable matches the same visual system.
when_to_trigger:
  - brand colors
  - match our style
  - brand guidelines
  - apply branding
kind: magic_skill
attribution: https://github.com/madewithclaude/awesome-claude-artifacts
---

# Brand Guidelines

## When to use

Use whenever you produce a visual artifact (diagram, slide, doc, GIF, icon) and the project has an established brand. This skill loads the brand's tokens once and threads them through whatever you render next. Without this, every artifact drifts toward generic defaults.

## Process

1. Locate the brand file. Default search order: `docs/brand.md`, `brand/tokens.json`, `.pollypm/brand.toml`, root `BRAND.md`. If none exists, prompt the user for: primary color, secondary color, accent color, display font, body font.
2. Parse into a flat token map: `color.primary`, `color.secondary`, `color.accent`, `color.bg`, `color.text`, `font.display`, `font.body`, `radius.sm/md/lg`, `space.unit`.
3. Record the tokens to `.pollypm/session/brand-tokens.json` for this session so subsequent artifacts reuse them without re-parsing.
4. When any other skill (canvas-design, svg-design, frontend-design, pptx-create) produces an artifact, look up colors and fonts from the token map — never inline hex strings that duplicate brand colors.
5. For typography: pair display + body only. Never introduce a third face under the brand umbrella. Weights from the brand's approved list only.
6. For colors: primary is brand identity, secondary is support, accent is call-to-action. Do not use accent for non-actions. Backgrounds stay brand-neutral (black, white, or one brand-specified surface).
7. Render a one-page style reference when the user asks "what are our brand colors?" — swatches + hex + font samples, saved to `artifacts/brand-reference.html`.

## Example invocation

```
User: "Make the next diagram match our brand."

Agent:
1. Find docs/brand.md. Parse: primary #4a9eff, accent #ff6b35, display Fraunces, body Inter.
2. Cache to .pollypm/session/brand-tokens.json.
3. Next architecture-diagram call: stroke uses color.primary, headers use font.display, body uses font.body.
4. No inline hex literals in the generated HTML; every value pulls from the token map.
```

## Outputs

- A parsed brand token map cached for the session.
- A style reference page (HTML) when requested.
- All subsequent artifacts automatically reference the tokens.
- A warning if the user-requested color falls outside the brand's approved palette.

## Common failure modes

- Inlining hex codes in the generated artifact and breaking token traceability.
- Introducing a third font because "it looked nice."
- Using accent color for decoration instead of calls-to-action.
- Forgetting to re-load brand tokens across session restarts — artifacts drift back to defaults.
