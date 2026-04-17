---
name: typescript-advanced-types
description: Generics, conditional types, mapped types, template literals — type-level programming that stays readable.
when_to_trigger:
  - typescript types
  - type-level programming
  - generics
  - conditional types
kind: magic_skill
attribution: https://github.com/microsoft/TypeScript
---

# TypeScript Advanced Types

## When to use

Reach for advanced types when you have a real type-safety need: an API client that should reject invalid query parameters at compile time, a form library that types fields from a schema, a database query builder. Do not reach for them as a puzzle — clever types that nobody can read are worse than `any`.

## Process

1. **Generics as parameters, not as puzzles.** `function map<T, U>(xs: T[], f: (t: T) => U): U[]` is a generic that earns its complexity. `type Flatten<T> = T extends (infer U)[] ? U : T` is a tool; use it in one place and consider a comment.
2. **Conditional types with `infer` extract structure.** `ReturnType<T>`, `Parameters<T>`, `Awaited<T>` are the built-ins you will reach for most. Write custom ones when library authors' needs exceed what is built in.
3. **Mapped types transform shape.** `type Partial<T> = { [K in keyof T]?: T[K] }`. Combine with `as` for key remapping: `type Getters<T> = { [K in keyof T as \`get${Capitalize<string & K>}\`]: () => T[K] }`.
4. **Template literal types for structured strings.** `type HttpMethod = 'GET' | 'POST'; type Endpoint = \`${HttpMethod} /api/${string}\``. Use for API routes, event names, CSS class prefixes.
5. **`satisfies` over type annotation for literal objects.** `const config = { ... } satisfies Config` gives you type checking without widening. `const config: Config = { ... }` loses the literal types of values.
6. **`never` for impossible branches.** Exhaustive switches: `default: const _: never = value;` compiles only if every case is handled; fails when someone adds a new variant.
7. **`unknown` over `any`.** `unknown` requires narrowing before use; `any` is a silent escape hatch. Runtime-parsed data always starts as `unknown`.
8. **Comment every conditional type with an example.** When the reviewer reads it, they should not reverse-engineer the intent.

## Example invocation

```typescript
// Type-level API — the compiler rejects wrong query shapes
type Route = 'GET /v1/tasks' | 'POST /v1/tasks' | 'GET /v1/tasks/:id';

type Params<R extends Route> =
  R extends `${string} /${string}/:${infer P}` ? { [K in P]: string } :
  R extends `${string} /${string}/:${infer P}/${string}` ? { [K in P]: string } :
  never;

type Body<R extends Route> = R extends 'POST /v1/tasks'
  ? { title: string; project_id: string }
  : never;

type Response<R extends Route> =
  R extends `GET /v1/tasks/:id` ? Task :
  R extends `GET /v1/tasks` ? Task[] :
  R extends `POST /v1/tasks` ? Task :
  never;

async function call<R extends Route>(
  route: R,
  opts: (R extends `${'POST' | 'PUT' | 'PATCH'} ${string}` ? { body: Body<R> } : {})
       & (Params<R> extends never ? {} : { params: Params<R> })
): Promise<Response<R>> {
  // ... build URL, fetch, return typed
  return {} as Response<R>;
}

// Usage — all type-checked
const tasks = await call('GET /v1/tasks', {});               // Task[]
const task  = await call('GET /v1/tasks/:id', { params: { id: 'abc' } }); // Task
const made  = await call('POST /v1/tasks', {
  body: { title: 'Ship magic', project_id: 'p1' },           // { title, project_id } required
});

// Exhaustive switch
function statusColor(s: Task['status']): string {
  switch (s) {
    case 'pending':   return 'gray';
    case 'running':   return 'blue';
    case 'succeeded': return 'green';
    case 'failed':    return 'red';
    case 'cancelled': return 'yellow';
    default:
      const _: never = s; // compile error if a new status is added and not handled
      throw new Error(`unexpected status: ${s satisfies never}`);
  }
}
```

## Outputs

- Types that catch incorrect usage at compile time.
- Exhaustiveness via `never` in switches and discriminated unions.
- `unknown` for parsed input, narrowed via guards.
- Advanced types commented with one example.

## Common failure modes

- Clever types nobody can read; `any` would have been more honest.
- `any` sneaking through; one escape hatch poisons a whole call graph.
- Type annotations on object literals widening to the declared type and losing literal inference.
- No exhaustiveness check; adding a new variant silently misses cases.
