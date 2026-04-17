---
name: tailwind-design-system
description: Build a coherent Tailwind design system with tokens, variants, and constraints that keep the UI from drifting.
when_to_trigger:
  - tailwind setup
  - design system
  - tailwind tokens
  - theme config
kind: magic_skill
attribution: https://github.com/tailwindlabs/tailwindcss
---

# Tailwind Design System

## When to use

Use when starting a new Tailwind project or when an existing one is drifting — every component has its own spacing values, five teal variants appear, dark mode is patchy. This skill sets up the token layer so utility classes compose into a system instead of a soup.

## Process

1. Decide Tailwind version first. **v4+** uses CSS-first configuration (`@theme` in CSS). **v3** uses `tailwind.config.js`. Pick based on framework support; v4 is the default for new projects on Next 14+.
2. Define tokens in one place. For v4: a single `@theme` block in `app/globals.css`. For v3: the `theme.extend` in `tailwind.config.js`. Nowhere else — not in component files, not in sub-configs.
3. Palette: define semantic names (`background`, `foreground`, `muted`, `accent`, `destructive`) backed by raw colors. Components reference `bg-background`, not `bg-zinc-950`. Rebranding becomes a 5-line diff.
4. Spacing: use Tailwind's default 4px scale. Do not redefine. Override only to extend (e.g. add `128` = 32rem). Never shrink — you will hit missing values later.
5. Typography: define a `fontFamily` and a `fontSize` scale. Match sizes to a coherent ratio (1.125 or 1.25 perfect fourth). Name them semantically: `text-display-lg`, `text-body`, `text-caption`.
6. Dark mode via `prefers-color-scheme` or a class toggle. Use CSS variables so a single flip updates everything. Do not ship both `bg-zinc-50 dark:bg-zinc-900` across 200 components — token once, use everywhere.
7. Enforce with a linter: `eslint-plugin-tailwindcss` catches invalid class names, duplicate utilities, and wrong order. Add to CI.
8. Constrain what designers/devs can reach for: disable the default color palette in the preset if you do not want devs writing `bg-orange-500` unintentionally. Make the semantic tokens the only path of least resistance.

## Example invocation

```css
/* app/globals.css (Tailwind v4) */
@import "tailwindcss";

@theme {
  --color-background: oklch(0.12 0 0);
  --color-foreground: oklch(0.96 0 0);
  --color-muted: oklch(0.68 0 0);
  --color-accent: oklch(0.72 0.18 50);
  --color-destructive: oklch(0.62 0.22 25);

  --font-display: "Fraunces", serif;
  --font-sans: "Inter", sans-serif;
  --font-mono: "JetBrains Mono", monospace;

  --text-display-lg: 3.5rem;
  --text-display-md: 2.25rem;
  --text-body: 1rem;
  --text-caption: 0.875rem;

  --radius-sm: 0.25rem;
  --radius-md: 0.5rem;
  --radius-lg: 1rem;
}

[data-theme="light"] {
  --color-background: oklch(0.99 0 0);
  --color-foreground: oklch(0.15 0 0);
}
```

```tsx
// Components reference tokens, not primitives.
<div className="bg-background text-foreground font-sans">
  <h1 className="font-display text-display-lg">Polly</h1>
  <p className="text-body text-muted">Ship the work.</p>
</div>
```

## Outputs

- A single source of tokens in `globals.css` or `tailwind.config.js`.
- Semantic color names referenced by components.
- Type and spacing scales anchored to a ratio.
- ESLint rule enforcing the system in CI.

## Common failure modes

- Raw color classes scattered across components; rebrand is 200 files.
- Arbitrary `p-[17px]` values that break the rhythm.
- Dark mode as a whack-a-mole `dark:` sprinkle instead of token-based.
- No linter; system entropy wins in a month.
