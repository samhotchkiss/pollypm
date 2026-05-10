# PollyPM Web API — Design Spec

Status: Draft (v0.1.0)
Audience: PollyPM maintainers, separate-repo frontend developers, typed-client codegen consumers.
Companion artifact: [`docs/api/openapi.yaml`](api/openapi.yaml)

This document describes the HTTP API that allows a separate web frontend
to drive PollyPM. The API surface mirrors what a user can do from the
TUI cockpit today — list projects, drive plan reviews, queue and approve
tasks, reply to inbox threads, and watch the activity stream live.

The design is **spec-only**. No `pm serve` implementation lands with this
PR; the OpenAPI document is the contract a frontend developer can target,
and the milestones at the end of this doc lay out the implementation
phases.

---

## 1. Goal

PollyPM today is a TUI cockpit. A separate-repo frontend (the user's
upcoming web app) needs to drive the same workflows: project drilldown,
plan review, task triage, inbox replies, and a live activity feed.

Hard requirements driving the design:

- **Read everything the cockpit shows.** Projects, tasks, plans, inbox
  items, doctor results, and the audit-log activity stream.
- **Write everything the cockpit lets you write.** Approve / reject
  plans and reviews, queue drafts, reply to inbox threads, kick off
  replans, send chat messages to the project PM persona.
- **Codegen-friendly.** Strict OpenAPI 3.1 so a TypeScript / Swift /
  Kotlin client generator produces a typed SDK out of the box. No
  custom JSON schemas embedded in prose — every response has a
  schema reference.
- **Realtime updates.** The cockpit's activity feed has to work in the
  browser too; the audit log is the source of truth, so the API streams
  it via Server-Sent Events.
- **Minimal but real auth.** Single-user, personal-use scope. No
  multi-tenant story in v1.

Non-goals (see §9): cockpit-specific shortcuts, file-system browsing,
worker-process control, multi-tenant authentication.

---

## 2. Architecture

### Process model

`pm serve --port N` is a **separate process** from the cockpit. It is a
peer that reads and writes the same `state.db` and `audit.jsonl` via the
existing `create_work_service` factory (see #1389) and the existing
`pollypm.service_api.v1.PollyPMService` facade. The cockpit and the API
server are independent — the frontend keeps working when the cockpit
is down, and vice versa.

```
                ┌─────────────────┐
                │  Frontend repo  │
                │  (separate)     │
                └────────┬────────┘
                         │ HTTPS + SSE
                         ▼
                ┌─────────────────┐
                │   pm serve      │  ← FastAPI (recommended)
                └────────┬────────┘
                         │ SDK (PollyPMService, create_work_service)
                         ▼
              ┌──────────────────────┐
              │ state.db, audit.jsonl│  ← shared with cockpit
              └──────────────────────┘
```

### Why a separate process?

- **Crash isolation.** The cockpit is a Textual TUI with its own
  rendering loop and tmux session. Hosting an HTTP server inside it
  would bind two unrelated lifecycles (TUI process + HTTP process)
  and force a frontend developer to keep `pm cockpit` running just to
  serve requests.
- **Restart independently.** API regressions won't kick the user out
  of their cockpit window; cockpit hot-reload won't drop SSE
  subscriptions.
- **Same backing store.** Both processes go through
  `create_work_service` and the audit log — there is no second source
  of truth, no cache invalidation problem, and the audit log
  guarantees ordering even with concurrent writers.

### Why FastAPI?

FastAPI is recommended because:

- It generates an OpenAPI document directly from typed Python models,
  so the `docs/api/openapi.yaml` artifact stays in sync with the
  implementation when it lands.
- SSE is one `EventSourceResponse` away.
- The dependency-injection model maps naturally to "every endpoint
  needs a `PollyPMService`."
- Async support means a single worker can hold many SSE
  subscriptions open without blocking.

The spec does not depend on FastAPI; any HTTP framework that can
emit the contract is acceptable. FastAPI is just the path of least
resistance.

### Concurrency model

`pm serve` is a single-process, single-worker server in v1. SQLite
WAL handles multi-reader / single-writer fine for personal-use
volumes; the audit log is append-only JSONL. If we need to scale,
the upgrade path is documented separately (process-per-request or
SQLite → Postgres).

---

## 3. Authentication

### Bearer token

Every request (except `GET /api/v1/health`) must carry:

```
Authorization: Bearer <token>
```

### Token storage

- Token lives at `~/.pollypm/api-token` (mode `0600`).
- Generated on first `pm serve` startup if absent.
- 256-bit random, base64url-encoded (≈43 chars).
- Regenerated via `pm api regen-token` (rotates the token, prints the
  new value once, invalidates all prior tokens).

### Threat model

- Single-user, personal-use. No multi-tenant separation.
- The token is a simple shared secret; loss equals full access.
- TLS is the operator's responsibility — `pm serve` defaults to
  binding `127.0.0.1` and refuses non-loopback binds without
  `--allow-remote`.
- Out of scope: OAuth, JWT, role-based access, audit logging of API
  callers (the frontend developer is the same human as the cockpit
  user).

### Failure modes

- **Missing token:** `401 Unauthorized` with
  `{"error":{"code":"unauthorized","message":"Bearer token required"}}`.
- **Wrong token:** `401 Unauthorized` with
  `{"error":{"code":"invalid_token","message":"Bearer token rejected"}}`.

---

## 4. Realtime

### SSE design

`GET /api/v1/events` returns a `text/event-stream` of audit-log
events. Each SSE message is one audit-log line as JSON:

```
event: audit
id: 2026-05-07T12:01:33.022Z
data: {"schema":1,"ts":"2026-05-07T12:01:33.022Z","project":"pollypm","event":"task.status_changed","subject":"pollypm/142","actor":"pm","status":"done","metadata":{}}

```

### Reconnect semantics

- Client sends `Last-Event-ID` on reconnect (the timestamp from the
  last seen event).
- Server replays from the audit log starting after `Last-Event-ID`,
  then tails live.
- For an explicit cold start, client sends `?since=<ISO-8601>` (no
  `Last-Event-ID`). Server emits all events since `since`, then tails.
- If `since` is empty / missing on initial connect, server only tails
  (no replay).

### Filtering

Two query parameters, both optional:

- `project=<key>` — only events for this project.
- `event=<glob>` — wildcard match on event name; e.g.
  `event=task.*`, `event=watchdog.*`, `event=plan.version_incremented`.

Both filters AND together when both are supplied. Filtering is
server-side, so a noisy fleet doesn't drown a single-project
frontend.

### Heartbeats

Server emits a comment line (`: keep-alive\n\n`) every 15 seconds so
intermediate proxies don't drop idle connections. The client SDK
ignores comment lines.

### Why SSE and not WebSocket?

- The audit log is one-way (server → client). WebSocket buys a duplex
  channel we don't need.
- SSE is plain HTTP — easy to load through a CDN, transparent to
  proxies, easy to auth with the same bearer token.
- Browser `EventSource` handles auto-reconnect; we get the
  `Last-Event-ID` machinery for free.
- Codegen tooling for OpenAPI handles `text/event-stream` cleanly;
  WebSocket lives outside the spec.

WebSocket is **explicitly out of scope for v1.**

---

## 5. Versioning

- All endpoints live under `/api/v1/...`.
- Bump to `/api/v2/...` on **breaking** changes (response shape
  removals, semantics changes, status-code changes for existing
  flows).
- Additive changes (new endpoints, new optional fields) ship in v1
  forever.
- The OpenAPI `info.version` field tracks the same major version
  ("0.1.0", "0.2.0", ..., "1.0.0", then "2.0.0" on the v2 cutover).

The `pm serve` process exposes its OpenAPI document at
`/api/v1/openapi.json` so the frontend's typed-client generator can
fetch the live contract during CI.

---

## 6. Errors

### Format

Every 4xx and 5xx response body is:

```json
{
  "error": {
    "code": "string_error_code",
    "message": "Human-readable explanation",
    "hint": "Optional remediation hint"
  }
}
```

- `code` is a stable string (snake_case) — clients should match on
  this, not the message.
- `message` is for display.
- `hint` is optional remediation text ("Run `pm doctor` to diagnose
  the database mismatch.").

### Standard codes

| HTTP | Code | When |
|------|------|------|
| 400 | `invalid_request` | Malformed body, bad query parameters |
| 400 | `invalid_state` | Action not allowed in the current task / inbox state (e.g. approving a plan that has no review pending) |
| 401 | `unauthorized` | Missing bearer token |
| 401 | `invalid_token` | Bearer token did not match |
| 403 | `forbidden` | Action requires capabilities the API doesn't expose (e.g. multi-tenant) |
| 404 | `not_found` | Project / task / inbox item does not exist |
| 409 | `conflict` | Concurrent write conflict (state changed between read and write) |
| 422 | `validation_error` | Body shape valid, but values failed validation (e.g. empty `reason`) |
| 429 | `rate_limited` | Burst protection (future) |
| 500 | `internal_error` | Unhandled server exception |
| 503 | `service_unavailable` | Backing store unreachable (`state.db` lock contention beyond retry) |

Validation errors carry an extra `details` field shaped like
`[{"field": "reason", "message": "must be non-empty"}]`.

---

## 7. Resource model

These are the conceptual types; the OpenAPI document is the
authoritative shape definition.

### Project

A registered repository PollyPM tracks. Mirrors `KnownProject` in
`pollypm.config`.

Fields: `key`, `name`, `path`, `kind`, `tracked`, `persona_name`,
plus derived: `state` (briefing-derived signal), `glyph` (stop-light
indicator), `task_counts` (by status), `pending_plan_review` (bool),
`open_inbox_count`.

### Task

The atomic unit of work. Mirrors `pollypm.work.models.Task`.

Fields: `project`, `task_number`, `task_id` (`{project}/{n}`),
`title`, `type`, `priority`, `work_status` (`TaskStatus` enum),
`current_node_id`, `assignee`, `description`, `labels`,
`relationships`, `flow_template_id`, `flow_template_version`,
`plan_version`, `predecessor_task_id`, `transitions`, `executions`
(optional, only on detail), `total_input_tokens`,
`total_output_tokens`, `session_count`, timestamps.

### TaskStatus (enum)

`draft`, `queued`, `in_progress`, `rework`, `blocked`, `on_hold`,
`review`, `done`, `cancelled`.

Mirrors `WorkStatus` in `pollypm.work.models`.

### Plan

The structured plan body for a task at `status=review` on the
`user_approval` node. Mirrors PR #1408's structured output and the
inbox `plan_review` payload (see `_extract_plan_judgment_calls` in
`cockpit_ui.py`).

Fields:
- `task_id`
- `version` (`plan_version`; integer, increments on refinement)
- `predecessor_task_id` (nullable; populated when a replan creates a
  new task)
- `summary` (one-paragraph synthesis)
- `judgment_calls` (`PlanJudgmentCall[]`; the bulleted list under
  `## Judgment calls` in the plan body)
- `body` (full plan markdown)
- `critic_synthesis` (architect critic notes; nullable)
- `created_at`

### PlanJudgmentCall

`{ "point": "string" }` — one decision the architect flagged for
operator review. Currently a single string; modeled as an object so
future fields (severity, suggested-resolution) ship without a
breaking change.

### InboxItem

A triaged item in `<project>/.pollypm/inbox/`. Mirrors the inbox
plugin's item shape (see `docs/v1/09-inbox-and-threads.md`).

Fields: `id`, `project`, `type` (`message`, `plan_review`,
`blocking_question`, `alert`, ...), `state` (`open`, `threaded`,
`waiting-on-pa`, `waiting-on-pm`, `resolved`, `closed`), `subject`,
`preview`, `owner` (`pm` / `pa` / `worker`), `created_at`,
`updated_at`, `thread_id` (nullable), `metadata` (sidecar labels),
`messages` (`InboxMessage[]`, only on detail).

### Event

A single audit-log line. Mirrors `pollypm.audit.log.AuditEvent`.

Fields: `schema`, `ts`, `project`, `event`, `subject`, `actor`,
`status`, `metadata`.

### ErrorResponse

`{ "error": { "code", "message", "hint?" } }` plus optional
`details[]` for validation errors.

---

## 8. Endpoint table

| Method | Path | Purpose |
|--------|------|---------|
| GET    | `/api/v1/health` | Liveness + version info (no auth) |
| GET    | `/api/v1/doctor` | Structured `pm doctor` output (#1347) |
| GET    | `/api/v1/projects` | List registered projects with state, glyph, counts |
| POST   | `/api/v1/projects` | Register a project (mirrors `pm add-project`) |
| GET    | `/api/v1/projects/{key}` | Project drilldown — state, recent activity, top tasks, pending plan review |
| POST   | `/api/v1/projects/{key}/plan` | Kick off `pm project plan` (initial or replan) |
| POST   | `/api/v1/projects/{key}/chat` | Send a chat message to the project's PM persona |
| GET    | `/api/v1/projects/{key}/tasks` | Task list (`?status=&limit=&cursor=`) |
| GET    | `/api/v1/projects/{key}/plan` | Structured plan body (`?version=N`) |
| GET    | `/api/v1/tasks/{project}/{n}` | Task detail — status, node, executions, transitions |
| POST   | `/api/v1/tasks/{project}/{n}/approve` | Approve plan or code review |
| POST   | `/api/v1/tasks/{project}/{n}/reject` | Reject + capture reason |
| POST   | `/api/v1/tasks/{project}/{n}/queue` | Queue a draft task |
| GET    | `/api/v1/inbox` | List inbox items (`?project=&type=&state=&limit=&cursor=`) |
| GET    | `/api/v1/inbox/{id}` | Inbox item detail (with full thread messages) |
| POST   | `/api/v1/inbox/{id}/reply` | Reply to a thread |
| POST   | `/api/v1/inbox/{id}/archive` | Archive (close) the item |
| GET    | `/api/v1/events` | SSE stream of audit-log events (`?since=&project=&event=`) |

That's 17 endpoints in v1. The OpenAPI document is the authoritative
list; if anything here drifts, the YAML wins.

---

## 9. Out of scope (v1)

The following are intentionally not in v1 so the surface stays small
and the frontend developer can ship something the day this lands:

- **Multi-tenant auth.** No user accounts, no roles, no per-project
  ACLs. Single shared bearer token.
- **File-system browsing.** No "show me the files in this repo"
  endpoint. Frontend uses GitHub URLs / external hosting if it needs
  to render a repo tree.
- **Worker process control.** No "kill this worker session,"
  "restart this tmux pane," "spawn a new worker." That is cockpit /
  CLI territory; the API is a project-management surface, not a
  process supervisor.
- **Cockpit-specific actions.** Keyboard shortcuts, palette
  commands, focus management — none of that maps to HTTP. The API
  exposes the underlying actions, not the cockpit's UI affordances.
- **WebSocket transport.** SSE only. Reconsidered in v2 if the
  frontend needs to push.
- **Plugin discovery / configuration.** The cockpit's plugin
  settings panel does not have an API counterpart. Plugins are
  configured via `pollypm.toml`, period.
- **Account / provider management.** No login flows for Claude /
  Codex providers; that's the cockpit's account TUI.
- **Bulk mutation endpoints.** No "approve all," "queue all
  drafts." If the frontend wants bulk, it loops.

---

## 10. Frontend integration notes

### Generating a typed client

The OpenAPI document at `/api/v1/openapi.json` is consumable by any
codegen tool. Recommended:

- **TypeScript:** [`openapi-typescript`](https://github.com/drwpow/openapi-typescript) +
  [`openapi-fetch`](https://github.com/drwpow/openapi-fetch) for a
  zero-dependency typed client. No runtime overhead beyond fetch.
- **Vite + React:** the generated types feed directly into TanStack
  Query hooks. Pattern:

  ```ts
  import createClient from "openapi-fetch";
  import type { paths } from "./generated/pollypm";

  const client = createClient<paths>({
    baseUrl: "http://127.0.0.1:8765/api/v1",
    headers: { Authorization: `Bearer ${token}` },
  });

  const { data, error } = await client.GET("/projects");
  ```

CI step: `npx openapi-typescript http://localhost:8765/api/v1/openapi.json -o src/generated/pollypm.ts`
when `pm serve` is reachable in CI; otherwise vendor the YAML and
generate from it.

### SSE reconnect

The frontend should:

1. On mount, hit `GET /api/v1/events?since=<bootstrap-ts>` where
   `bootstrap-ts` is the `ts` of the most recent event already in
   the local cache (or the page-load time on cold start).
2. Use the browser `EventSource` API; it handles `Last-Event-ID`
   automatically on reconnect.
3. On `error` events, back off (1s, 2s, 5s, 30s), then re-open with
   `since` set to the last seen `ts`.

The audit log is append-only and event IDs are timestamps, so a
duplicate replay is harmless — the frontend deduplicates by
`(ts, event, subject)`.

### Handling plan-review

The plan-review surface is the most UI-heavy piece. Recommended
pattern:

1. List view: hit `GET /api/v1/inbox?type=plan_review&state=open` to
   show the plan-review queue. Each item carries a preview and the
   `judgment_calls` so the operator can scan without drilldown.
2. Drilldown: hit `GET /api/v1/projects/{key}/plan` for the
   structured plan body. Render `summary` prominently,
   `judgment_calls` as a checklist, `body` as collapsed markdown,
   `critic_synthesis` as a sidebar.
3. Approve: `POST /api/v1/tasks/{project}/{n}/approve` with
   `{ "kind": "plan" }` (the action body discriminates plan vs
   code-review approvals; see OpenAPI for `ApproveRequest`).
4. Reject: `POST /api/v1/tasks/{project}/{n}/reject` with
   `{ "reason": "..." }` — the reason is required and non-empty
   per `validation_error`.

Cockpit semantics to mirror: PR #1421's approve flow displays a
toast with a 10-second undo. The frontend gets the same affordance
by deferring the API call until the toast expires; it's a UI
concern, not an API concern.

### Pagination

List endpoints (`/projects/{key}/tasks`, `/inbox`) use cursor
pagination with `?limit=` and `?cursor=`. Response body carries
`{ items, next_cursor? }`. `next_cursor` absent ⇒ end of list.

### Idempotency

`POST` endpoints accept an optional `Idempotency-Key` header. When
present, the server stores the response for 24h and replays it for
duplicate keys. Recommended for approve / reject / queue / reply,
where a network glitch + retry could otherwise double-submit.

---

## 11. Implementation milestones

This PR ships only the spec. The implementation lands across three
phases, each as a separate issue (issues to be opened by the user
after this PR merges).

### Phase 1 — Read + auth + SSE (issue: #1547)

- `pm serve` skeleton with FastAPI, bearer-token auth, OpenAPI doc at
  `/api/v1/openapi.json`, `pm api regen-token` CLI.
- `GET /api/v1/health`
- `GET /api/v1/projects`, `GET /api/v1/projects/{key}`
- `GET /api/v1/projects/{key}/tasks`, `GET /api/v1/tasks/{project}/{n}`
- `GET /api/v1/projects/{key}/plan`
- `GET /api/v1/inbox`, `GET /api/v1/inbox/{id}`
- `GET /api/v1/events` (SSE)

Outcome: frontend can render the dashboard and stream live updates.

### Phase 2 — Write endpoints (issue: #1548)

- `POST /api/v1/projects` (register)
- `POST /api/v1/projects/{key}/plan` (kick off / replan)
- `POST /api/v1/projects/{key}/chat`
- `POST /api/v1/tasks/{project}/{n}/approve`
- `POST /api/v1/tasks/{project}/{n}/reject`
- `POST /api/v1/tasks/{project}/{n}/queue`
- `POST /api/v1/inbox/{id}/reply`
- `POST /api/v1/inbox/{id}/archive`

Outcome: frontend has feature parity with the cockpit for the
documented surface.

### Phase 3 — Doctor + admin (issue: #1549)

- `GET /api/v1/doctor` (#1347)
- Idempotency-Key persistence
- Rate limiting (`429`)
- `--allow-remote` non-loopback bind support with TLS guidance

Outcome: production-grade for personal-use deployments.

---

## Open questions

These are intentionally not blocking the spec but should be
resolved during implementation:

- **Chat backpressure.** `POST /projects/{key}/chat` enqueues a
  message to the PM persona; the response can either (a) wait for
  the persona to reply (high-latency, simple) or (b) return
  immediately with a thread ID and let the frontend subscribe via
  SSE. Recommendation: (b), to keep request handlers fast.
- **Plan replan vs initial plan.** `POST /projects/{key}/plan` does
  both today via `pm project plan`. The endpoint accepts an optional
  `{ "kind": "replan", "reason": "..." }` body, defaulting to
  initial. Document the precise semantics when the implementation
  lands.
- **Token rotation timing.** Should `pm api regen-token` invalidate
  the old token immediately or grant a 60-second overlap? Short
  overlap is friendlier for an in-progress frontend session;
  immediate is simpler.

---

## References

- `pollypm.service_api.v1.PollyPMService` — the facade `pm serve`
  composes against.
- `pollypm.work.factory.create_work_service` (#1389) — canonical
  work-service constructor.
- `pollypm.audit.log` — audit-log writer / reader (`AuditEvent`,
  `SCHEMA_VERSION`).
- `pollypm.work.models` — Task, WorkStatus, FlowNodeExecution,
  Transition.
- `pollypm.projects.register_project` — backing call for `POST
  /api/v1/projects`.
- PR #1408 — structured plan-review output.
- PR #1347 — `pm doctor` structured JSON.
- PR #1421 — approve flow with undo toast (UI mirror).
- `docs/v1/09-inbox-and-threads.md` — inbox state machine the API
  exposes.
- `docs/work-service-spec.md` — the sealed work-service contract
  the API operates through.
