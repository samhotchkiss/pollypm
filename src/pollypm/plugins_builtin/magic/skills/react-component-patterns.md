---
name: react-component-patterns
description: React composition, hooks, context, Suspense, error boundaries — the patterns that age well.
when_to_trigger:
  - react component
  - react hook
  - react architecture
  - component pattern
kind: magic_skill
attribution: https://github.com/facebook/react
---

# React Component Patterns

## When to use

Use when designing a new component, refactoring one that grew unwieldy, or debating shape with a collaborator. React has a dozen ways to solve any problem; this skill picks the ones that remain correct as the app grows.

## Process

1. **Compose, do not configure.** A component with 20 props is configuration; a component that accepts `children` or slots is composition. Prefer `<Card><CardHeader>...</CardHeader></Card>` over `<Card title="..." description="..." actions={[...]}>`.
2. **Colocate state** with the component that owns the behavior. Do not lift state for "flexibility" you do not need. Lift only when two siblings need the same state.
3. **Custom hooks isolate side effects.** Every `useEffect` with a non-trivial body should move into a named hook. `useTaskList(projectId)` is testable; inline `useEffect` in your page is not.
4. **Context for cross-cutting, not for sharing.** Auth, theme, locale — context. Props drilling a value through three levels — props. Do not reach for context to avoid passing a prop.
5. **Error boundaries** wrap every route and every lazy-loaded section. Without them, one render error blanks the page. `react-error-boundary` is the default library.
6. **Suspense for async**, not manual loading states, when React can do it. `<Suspense fallback={<Spinner />}>` around a data-fetching subtree beats 10 `isLoading` booleans.
7. **Memoize when the profiler says so.** `useMemo` and `useCallback` have their own cost. Only add them when the React DevTools profiler shows a real re-render problem.
8. **Key lists by identity**, never by index. `key={task.id}` survives reorders; `key={i}` breaks every animation and input focus.

## Example invocation

```tsx
// Custom hook — isolates the side effect + loading state
function useTaskList(projectId: string) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchTasks(projectId)
      .then(ts => { if (!cancelled) { setTasks(ts); setLoading(false); } })
      .catch(e => { if (!cancelled) { setError(e); setLoading(false); } });
    return () => { cancelled = true; };
  }, [projectId]);

  return { tasks, loading, error };
}

// Composed component — children slot for body, explicit key by id
export function TaskList({ projectId }: { projectId: string }) {
  const { tasks, loading, error } = useTaskList(projectId);

  if (loading) return <Spinner />;
  if (error) throw error; // caught by error boundary above

  return (
    <ul className="space-y-2">
      {tasks.map(t => <TaskRow key={t.id} task={t} />)}
    </ul>
  );
}

// Error boundary at the route
import { ErrorBoundary } from 'react-error-boundary';

export function ProjectRoute({ id }: { id: string }) {
  return (
    <ErrorBoundary FallbackComponent={ErrorPanel}>
      <Suspense fallback={<PageSkeleton />}>
        <TaskList projectId={id} />
      </Suspense>
    </ErrorBoundary>
  );
}
```

## Outputs

- Components that compose via children/slots.
- State colocated where it is used; custom hooks for side effects.
- Error boundaries wrapping each route.
- Lists keyed by stable IDs.

## Common failure modes

- Twenty-prop components; configuration creep is a smell.
- Context everywhere; unrelated subtrees re-render on every change.
- Index keys; typing in row 3 teleports to row 4 after a reorder.
- Memoizing speculatively; measurable perf gain is rare, code complexity always costs.
