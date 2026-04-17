---
name: frontend-design
description: Bold design decisions for web UIs — no generic aesthetics, React + Tailwind as the default stack.
when_to_trigger:
  - design a UI
  - make it beautiful
  - frontend polish
  - redesign page
kind: magic_skill
attribution: https://github.com/madewithclaude/awesome-claude-artifacts
---

# Frontend Design

## When to use

Use when you are building a new page, redesigning an existing one, or the user says "this looks generic." The default output of AI-generated UI is gray boxes and blue buttons — this skill replaces that with deliberate hierarchy, deliberate type, and one bold choice per page. Reach for it anytime a user cares what the UI looks like, not just what it does.

## Process

1. Name the **one bold move** before you write a line of code. Editorial typography? Asymmetric grid? Dense data viz? Dark mode with a single accent? One move; everything else supports it.
2. Lock typography first. Pair one display font with one text font. Inter for text is fine. Pair with Fraunces, Instrument Serif, or JetBrains Mono display — not another neutral sans. Set a type scale: 12 / 14 / 16 / 20 / 28 / 40 / 64. Never use arbitrary sizes.
3. Define the palette: background, foreground, muted, accent. Four colors. Accent only on CTAs and states that demand attention. A fifth color is not "nice to have" — it is design debt.
4. Spacing scale: 4 / 8 / 12 / 16 / 24 / 32 / 48 / 64 / 96. All padding and margin snap to this. No `p-[13px]`. Tailwind makes this automatic — use it.
5. Establish hierarchy with **size and weight**, not color. Body 16/400, subheads 20/600, headlines 40/700+. Color changes mean state changes (hover, active, error) — do not also make color mean importance.
6. Whitespace is the design. When in doubt, remove a border, add padding, drop a shadow. Generic UIs over-structure; bold UIs breathe.
7. Microinteractions: one subtle motion per page. A fade on load, a spring on the primary CTA, a cursor-following highlight. Not five. One.
8. Inspect at mobile first, then desktop. If the design only works wide, it is a desktop site with a broken mobile view. Build it the other way.

## Example invocation

```tsx
// Landing page — editorial hero, dark canvas, one accent.
export function Hero() {
  return (
    <section className="min-h-screen bg-zinc-950 text-zinc-100 px-6 py-24 md:px-16">
      <p className="font-mono text-sm text-zinc-500 mb-8">01 / overview</p>
      <h1 className="font-serif text-5xl md:text-7xl leading-[0.95] max-w-4xl">
        Ship the work.
        <span className="text-orange-400"> Not the process.</span>
      </h1>
      <p className="mt-8 text-lg text-zinc-400 max-w-xl leading-relaxed">
        Polly manages the whole team — tasks, workers, context, memory —
        so you can stay in flow.
      </p>
      <div className="mt-12 flex gap-4">
        <a href="/start" className="bg-orange-400 text-zinc-950 font-semibold
                                    px-6 py-3 rounded-full hover:bg-orange-300
                                    transition">
          Get started
        </a>
        <a href="/docs" className="text-zinc-300 px-6 py-3 hover:text-zinc-100">
          Read the docs
        </a>
      </div>
    </section>
  );
}
```

## Outputs

- A React + Tailwind page with one bold design choice.
- Typography paired (display + text), type scale pinned.
- Four-color palette, accent reserved for CTAs.
- Mobile-first with desktop enhancements.

## Common failure modes

- No bold move; page looks like every other SaaS landing.
- Arbitrary pixel values; design feels janky at different zooms.
- Color-coded importance; when a user has red-green issues, hierarchy disappears.
- Three microinteractions competing; attention fractures.
