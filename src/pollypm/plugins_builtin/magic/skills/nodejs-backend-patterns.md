---
name: nodejs-backend-patterns
description: Layered architecture, error handling, async patterns, observability — Node.js backends that stay maintainable past 10k LOC.
when_to_trigger:
  - node backend
  - express server
  - fastify
  - nestjs
  - node architecture
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Node.js Backend Patterns

## When to use

Use when scaffolding a new Node backend or when an existing one has outgrown "single index.js." Node services degrade gracefully in three shapes: typed layers, error propagation, and observability. This skill fixes all three.

## Process

1. **Pick the framework by needs, not preference.** Fastify (default — fast, typed, plugin system), Hono (edge runtimes), NestJS (enterprise with heavy conventions), Express only for legacy. Do not mix two.
2. **Layer the code:** `routes/` (HTTP) -> `services/` (business logic) -> `repositories/` (data access). Routes never talk to the DB directly; services never construct HTTP responses. This is the line between a scalable codebase and a mess.
3. **Typed everywhere.** TypeScript with `strict: true`. Validate HTTP input with Zod or `@fastify/type-provider-typebox` — Schema becomes both runtime validation and TypeScript type.
4. **Centralize error handling.** Custom error classes (`TaskNotFoundError extends HttpError`) thrown from services. A single error handler in the framework converts them to HTTP responses. Do not `try/catch/res.status(400).json()` in every route.
5. **Async everywhere, no callbacks.** `async/await`. Promise chains only when needed. Every async function that you call must be awaited — the linter rule `no-floating-promises` catches misses.
6. **Connection pooling at the process level.** One DB pool per process, one Redis client, one HTTP keep-alive agent. Do not create per-request.
7. **Structured logging.** `pino` as the logger, JSON output, include request ID on every log in a request. `logger.info({ taskId, userId }, 'task.created')` — fields, not string-concatenated noise.
8. **Graceful shutdown.** Handle `SIGTERM`: stop accepting connections, drain in-flight requests, close DB pool, exit. Kubernetes kills ungraceful processes in 30s; do not be one.

## Example invocation

```ts
// routes/tasks.ts
import type { FastifyInstance } from 'fastify';
import { z } from 'zod';
import { TaskService } from '../services/task.js';
import { TaskNotFoundError } from '../errors.js';

const createSchema = z.object({
  title: z.string().min(1).max(200),
  project_id: z.string().uuid(),
});

export async function tasksRoutes(app: FastifyInstance, { svc }: { svc: TaskService }) {
  app.post('/v1/tasks', async (req) => {
    const payload = createSchema.parse(req.body);
    const task = await svc.create(req.user.id, payload);
    return task;
  });

  app.get<{ Params: { id: string } }>('/v1/tasks/:id', async (req) => {
    const task = await svc.get(req.user.id, req.params.id);
    if (!task) throw new TaskNotFoundError(req.params.id);
    return task;
  });
}

// errors.ts
export class HttpError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}
export class TaskNotFoundError extends HttpError {
  constructor(id: string) { super(404, 'task.not_found', `Task ${id} not found`); }
}

// server.ts
import fastify from 'fastify';
import pino from 'pino';

const logger = pino({ level: process.env.LOG_LEVEL ?? 'info' });
const app = fastify({ logger });

app.setErrorHandler((err, req, reply) => {
  if (err instanceof HttpError) {
    reply.status(err.status).send({ error: { code: err.code, message: err.message } });
    return;
  }
  req.log.error({ err }, 'unhandled');
  reply.status(500).send({ error: { code: 'internal', message: 'Internal error' } });
});

async function shutdown() {
  logger.info('shutting down');
  await app.close();
  // close pool, redis...
  process.exit(0);
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
```

## Outputs

- TypeScript strict, Fastify (or chosen framework) wired.
- Three-layer structure: routes / services / repositories.
- Zod-validated input at every HTTP boundary.
- Centralized error handler mapping custom errors to HTTP.
- `pino` structured logs with request IDs.
- Graceful shutdown on SIGTERM.

## Common failure modes

- Routes calling the DB directly; can never refactor the data layer.
- Untyped request bodies; runtime crashes on unexpected input.
- Per-request DB connections; exhaustion under load.
- `console.log` debugging; production logs are unsearchable.
