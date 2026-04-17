---
name: api-design-principles
description: REST and GraphQL design — pagination, errors, versioning, idempotency, and the defaults that age well.
when_to_trigger:
  - design an api
  - rest endpoint
  - api design
  - graphql schema
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# API Design Principles

## When to use

Use when designing a new API or extending an existing one. API shape is path-dependent: the first three endpoints set conventions the next hundred will follow. Get this right before shipping the second consumer.

## Process

1. **Pick REST or GraphQL once.** REST: resources, HTTP verbs, composable URLs. GraphQL: one endpoint, typed schema, client-controlled selection. Do not hybrid — teams that hybrid end up with neither.
2. **Resource-first URLs** for REST. `POST /tasks`, `GET /tasks/{id}`, `PATCH /tasks/{id}`. Verb-in-URL (`/createTask`, `/getTaskById`) is a smell unless the action does not map to a resource (`POST /tasks/{id}:cancel` with `:verb` suffix for actions is acceptable).
3. **Errors as a structured body.** HTTP status for the category (`400` client error, `404` not found, `409` conflict, `500` server error). Body: `{ "error": { "code": "task.not_found", "message": "Task abc123 not found" } }`. `code` is stable for programmatic handling; `message` is human-readable.
4. **Pagination with cursors, not offsets.** Offset pagination breaks when rows are inserted. Cursor: `GET /tasks?after=cursor_abc&limit=50` returns `{ items, next_cursor }`. Stable under concurrent writes.
5. **Idempotency keys for non-idempotent operations.** `POST /payments` accepts `Idempotency-Key: <uuid>`; the server caches the response for 24h. Retries become safe.
6. **Versioning in the URL for breaking changes.** `/v1/tasks`, `/v2/tasks`. Header-based versioning (`Accept: application/vnd.api+json; version=2`) is cleaner but harder to debug. When in doubt, URL.
7. **Envelope or raw?** Return the resource directly (`GET /tasks/{id}` returns the task object). Do not wrap every response in `{ data: ..., meta: ... }` — it doubles client code. Exception: collections with pagination need meta.
8. **Types via OpenAPI or GraphQL schema.** Generate client SDKs from the schema. Hand-written docs drift; generated docs stay correct.

## Example invocation

```yaml
# openapi.yaml — REST shape that scales
paths:
  /v1/tasks:
    get:
      parameters:
        - name: after
          in: query
          schema: { type: string }
        - name: limit
          in: query
          schema: { type: integer, maximum: 100, default: 50 }
      responses:
        '200':
          content:
            application/json:
              schema:
                type: object
                properties:
                  items:
                    type: array
                    items: { $ref: '#/components/schemas/Task' }
                  next_cursor:
                    type: string
                    nullable: true
    post:
      parameters:
        - name: Idempotency-Key
          in: header
          required: false
          schema: { type: string, format: uuid }
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: '#/components/schemas/TaskCreate' }
      responses:
        '201':
          content: { application/json: { schema: { $ref: '#/components/schemas/Task' } } }
        '409':
          description: Idempotency conflict
          content: { application/json: { schema: { $ref: '#/components/schemas/Error' } } }

components:
  schemas:
    Error:
      type: object
      required: [error]
      properties:
        error:
          type: object
          required: [code, message]
          properties:
            code: { type: string, example: 'task.not_found' }
            message: { type: string }
            details: { type: object, additionalProperties: true }
```

## Outputs

- An OpenAPI (REST) or GraphQL schema document.
- Generated client SDKs in the consumer languages.
- A `CHANGELOG.md` for the API, tracking breaking-vs-additive changes.
- Documented error codes, pagination, and idempotency keys.

## Common failure modes

- Offset pagination; pages duplicate or skip under load.
- Unstable error shape; every client does its own error parsing.
- No version strategy; first breaking change is chaos.
- Hand-rolled docs; drift from reality within weeks.
