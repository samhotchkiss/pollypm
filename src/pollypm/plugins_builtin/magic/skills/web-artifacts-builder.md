---
name: web-artifacts-builder
description: Self-contained HTML artifacts using React + Tailwind + shadcn — single file, no build step, works offline.
when_to_trigger:
  - html artifact
  - demo page
  - single file app
  - prototype ui
kind: magic_skill
attribution: https://github.com/madewithclaude/awesome-claude-artifacts
---

# Web Artifacts Builder

## When to use

Use when the user wants a self-contained interactive HTML file — a prototype, a dashboard mock, a shareable demo, a one-off tool. Everything in one file, no build step, runs by opening in a browser. For production apps, this is the wrong shape — pick a proper framework.

## Process

1. Decide "artifact vs app." Artifacts are <2000 lines, single-purpose, disposable. If you are considering multiple files, you want `shadcn-ui` scaffold instead.
2. Use one HTML file with inline `<script type="module">`. Pull React, ReactDOM, and Tailwind from CDNs — version-pinned, never unpinned. Tailwind Play CDN for prototyping, compiled via CLI for final delivery.
3. Structure: `<!doctype html>` head with meta viewport and Tailwind link, body with `<div id="root">`, script at bottom imports React and renders. Keep the skeleton under 15 lines.
4. Import React via `import { useState } from "https://esm.sh/react@18"`. Pin versions. `esm.sh` handles JSX via `?dev` for dev, plain for prod. Alternative: Babel standalone for in-browser JSX, slower but no network dep.
5. For shadcn-style components, inline the ones you need. Do not try to pull the shadcn CLI — it wants a project. Copy `Button`, `Card`, `Input` source directly into the file.
6. State with hooks only. No Redux, no Zustand, no build-time context machinery. If state gets complex, the artifact is becoming an app — stop and scaffold properly.
7. Persist via `localStorage` with a namespaced key: `localStorage.setItem('polly-artifact-task-47', JSON.stringify(state))`. This lets the artifact feel persistent without a backend.
8. Before handing off, open the HTML in a fresh browser with DevTools open. Fix every console warning. An artifact with warnings looks unpolished.

## Example invocation

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Task board prototype</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-zinc-950 text-zinc-100 min-h-screen">
  <div id="root"></div>
  <script type="module">
    import React, { useState } from "https://esm.sh/react@18.3.1";
    import ReactDOM from "https://esm.sh/react-dom@18.3.1/client";

    function App() {
      const [tasks, setTasks] = useState(() => {
        try { return JSON.parse(localStorage.getItem('tasks')) ?? []; }
        catch { return []; }
      });
      const add = (title) => {
        const next = [...tasks, { id: crypto.randomUUID(), title }];
        setTasks(next);
        localStorage.setItem('tasks', JSON.stringify(next));
      };
      return React.createElement('div', { className: 'p-8' },
        React.createElement('h1', { className: 'text-3xl font-semibold' }, 'Tasks'),
        React.createElement('button', {
          className: 'mt-4 bg-orange-500 px-4 py-2 rounded',
          onClick: () => add('New task ' + (tasks.length + 1)),
        }, 'Add'),
        React.createElement('ul', { className: 'mt-6 space-y-2' },
          tasks.map(t => React.createElement('li', { key: t.id, className: 'bg-zinc-900 p-3 rounded' }, t.title))
        ),
      );
    }
    ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App));
  </script>
</body>
</html>
```

## Outputs

- A single `.html` file runnable by double-clicking.
- Version-pinned CDN imports.
- LocalStorage persistence if state matters.
- Zero console warnings in the browser.

## Common failure modes

- Unpinned CDN URLs (`@latest`); artifact breaks when upstream ships breaking change.
- Growing past 2000 lines and refusing to scaffold properly.
- No localStorage; user loses their state on refresh and the artifact feels broken.
- Skipping the DevTools check; console warnings leak into the hand-off.
