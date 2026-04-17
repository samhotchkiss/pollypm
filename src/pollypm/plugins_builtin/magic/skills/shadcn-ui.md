---
name: shadcn-ui
description: Pattern enforcement for shadcn/ui components — composition, theming, accessibility done correctly.
when_to_trigger:
  - shadcn
  - ui components
  - radix ui
  - component library
kind: magic_skill
attribution: https://github.com/shadcn-ui/ui
---

# shadcn/ui

## When to use

Use when the project is (or should be) on shadcn/ui. This is the right default for new React apps: the components are copied into your repo (not installed as a dep), they sit on Radix for accessibility primitives, and they are Tailwind-native. Reach for it whenever you want a design system where you fully own every component.

## Process

1. Install via the CLI — never hand-copy components. `npx shadcn@latest init`, then `npx shadcn@latest add button dialog form`. The CLI wires paths, Tailwind config, and CSS variables correctly.
2. All components live under `components/ui/`. Treat that directory as owned code, not a vendor blob. Edit freely; upstream does not push updates.
3. Theme via **CSS variables**, not Tailwind overrides. The shadcn convention: `--primary`, `--primary-foreground`, `--muted`, `--accent` in `globals.css`. Change the brand by editing 12 CSS variables, not 200 Tailwind class references.
4. Compose, do not decorate. A card with a form is `<Card><CardHeader><CardTitle>...</CardTitle></CardHeader><CardContent>...</CardContent></Card>`. Do not add arbitrary `className` to reshape primitives; build higher-order components if you need variation.
5. Use `<Form>` + `react-hook-form` + Zod. The form primitives shadcn ships handle labels, errors, and descriptions. Do not hand-roll forms when this stack is available.
6. Accessibility comes from Radix, not from you. But you still need to label. Every `<Input>` wrapped in `<FormItem>` with `<FormLabel>`. Icons inside buttons paired with `sr-only` text.
7. Dark mode via `className="dark"` on `<html>` using `next-themes`. Components respect the CSS variables automatically; do not write `dark:` variants for colors that are already theme tokens.
8. Extend with `class-variance-authority` (already installed) for button/badge variants. `cva` over inline conditions every time.

## Example invocation

```tsx
// Correct shadcn composition — no hand-rolled inputs, no ad-hoc decoration.
'use client';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';

import { Button } from '@/components/ui/button';
import {
  Form, FormControl, FormDescription, FormField,
  FormItem, FormLabel, FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';

const schema = z.object({
  title: z.string().min(1).max(200),
});

export function NewTaskForm({ onSubmit }: { onSubmit: (v: z.infer<typeof schema>) => void }) {
  const form = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema),
    defaultValues: { title: '' },
  });

  return (
    <Form {...form}>
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-6">
        <FormField
          control={form.control}
          name="title"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Title</FormLabel>
              <FormControl><Input placeholder="Ship the thing" {...field} /></FormControl>
              <FormDescription>Keep it under 200 characters.</FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        <Button type="submit">Create task</Button>
      </form>
    </Form>
  );
}
```

## Outputs

- Components added via the CLI, living under `components/ui/`.
- Theme via CSS variables in `globals.css`.
- Forms composed with `<Form>` + RHF + Zod.
- No hand-rolled form primitives.

## Common failure modes

- Hand-copying components so they drift from the CLI's expected file structure.
- Overriding colors with Tailwind classes instead of theme variables; breaks dark mode.
- Decorating primitives with `className` instead of building proper variants.
- Rolling your own form validation when `<Form>` is right there.
