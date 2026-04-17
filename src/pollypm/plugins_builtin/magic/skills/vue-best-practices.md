---
name: vue-best-practices
description: Vue 3 Composition API, reactivity rules, component patterns — the idioms that scale past 50 components.
when_to_trigger:
  - vue
  - vue 3
  - composition api
  - pinia
kind: magic_skill
attribution: https://github.com/vuejs/core
---

# Vue Best Practices

## When to use

Use when working in a Vue 3 codebase — whether scaffolding a new app, reviewing a teammate's component, or refactoring an Options API relic. This skill encodes the idioms that survive as the codebase grows: Composition API with `<script setup>`, Pinia for stores, proper reactivity, SFC conventions.

## Process

1. **`<script setup>` always.** The Options API still ships in Vue 3, but new components go Composition. SFCs with `<script setup lang="ts">` are half the boilerplate and better typed.
2. **`ref` for primitives, `reactive` for objects, never both for the same state.** Pick one per piece of state. Mixing leads to subtle reactivity bugs where `.value` goes missing.
3. **`computed` for derived state.** If you catch yourself updating state in a `watch` to match another state, use `computed` instead. Watches are for side effects, not for deriving.
4. **Pinia, not Vuex, not global refs.** Stores are defined with `defineStore('name', () => { ... })` returning `{ state, getters, actions }` in a setup-style callback. Import where needed; do not register globally unless truly global.
5. **Composables for reusable logic.** Name with `use` prefix: `useTaskList`, `useClipboard`. Return refs and functions; do not return reactives (they lose reactivity on destructure).
6. **Single Root per `<template>`** is no longer required in Vue 3 — but still prefer one root for predictable CSS and transitions. Fragments are fine for small list-y pieces.
7. **Scoped styles by default.** `<style scoped>` in every SFC. For design-system primitives, use CSS variables or Tailwind — not global stylesheets that leak.
8. **TypeScript props via `defineProps<T>()`** with a generic. This gives you real inferred types instead of string-based runtime declarations.

## Example invocation

```vue
<!-- TaskList.vue -->
<script setup lang="ts">
import { computed } from 'vue';
import { useTaskStore } from '@/stores/task';

const props = defineProps<{ projectId: string }>();
const emit = defineEmits<{ select: [taskId: string] }>();

const store = useTaskStore();
const tasks = computed(() => store.byProject(props.projectId));

const activeCount = computed(() =>
  tasks.value.filter(t => t.status === 'running').length
);
</script>

<template>
  <section class="space-y-2">
    <header class="flex justify-between">
      <h2 class="text-xl font-semibold">Tasks</h2>
      <span class="text-sm text-zinc-500">{{ activeCount }} active</span>
    </header>
    <ul class="space-y-1">
      <li
        v-for="task in tasks"
        :key="task.id"
        class="p-3 rounded bg-zinc-900 cursor-pointer"
        @click="emit('select', task.id)"
      >
        {{ task.title }}
      </li>
    </ul>
  </section>
</template>

<style scoped>
section { container-type: inline-size; }
</style>
```

```ts
// stores/task.ts — Pinia setup-style
import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import type { Task } from '@/types';

export const useTaskStore = defineStore('task', () => {
  const tasks = ref<Task[]>([]);

  const byProject = (projectId: string) =>
    computed(() => tasks.value.filter(t => t.projectId === projectId)).value;

  async function load() {
    tasks.value = await fetch('/api/tasks').then(r => r.json());
  }

  return { tasks, byProject, load };
});
```

## Outputs

- `<script setup lang="ts">` SFCs.
- Pinia stores in `stores/`, setup-style.
- Composables prefixed `use`, colocated in `composables/`.
- Scoped styles per SFC; design tokens via CSS variables.

## Common failure modes

- Mixing Options API and Composition API in the same component.
- Destructuring a `reactive` and losing reactivity.
- Reaching for `watch` to derive state — use `computed`.
- Global refs as stores; quickly become unmaintainable.
