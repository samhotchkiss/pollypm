# Chat HTTP API reference

> **Status:** Reference doc for endpoints landing in PRs #2043, #2044,
> #2045, #2047. Will be cross-checked against final implementation
> before merge. Treat the contracts here as the *intended* shape — the
> P3 send endpoint in particular (#2043) is still in fix-loop, so
> field names and error codes may shift before merge.

Reference for the `/api/v1/chat/*` endpoints — the HTTP surface for
reading transcripts from, and sending messages into, the four live
tmux-backed chat surfaces PollyPM runs (operator, architect, advisor,
per-task worker). Audience: anyone building tooling on top of PollyPM
(custom dashboards, browser clients, scripted reply bots) and operators
running ad-hoc `curl` against the local daemon.

This document covers phase 1 of the chat API (PRs #2042–#2047) and is
self-contained — read this file for the contract; the implementation
under `src/pollypm/web_api/routes/chat_*.py` is the source of truth
for the wire format. Phase 2 (dashboard / inbox / task-manipulation
endpoints) is out of scope.

---

## 1. What the chat API is

PollyPM keeps four kinds of long-lived chat surfaces alive in tmux:

- **Operator (`operator`)** — the Polly session, your top-level
  conversational PM.
- **Architect (`architect_<project>`)** — per-project planning session
  ("Archie").
- **Advisor (`advisor_<project>`)** — per-project advisory session,
  used for second opinions and code review.
- **Worker (`task-<project>-<task_number>`)** — short-lived per-task
  Claude session that actually does the work.

Each surface has exactly one tmux window backing it and (usually) one
Claude Code JSONL transcript on disk. The chat API gives you uniform
HTTP access to both: GET to pull the message history, POST to push a
reply through tmux into the agent's input box.

Use it when you want to:

- Render PollyPM's conversations in a browser, mobile app, or
  third-party cockpit without scraping `tmux capture-pane` yourself.
- Script a reply into Polly from another service ("when GitHub action
  X fails, ping the operator").
- Build an `AskUserQuestion` reply bot that answers the agent's
  multi-choice prompts programmatically.
- Pull the structured tool-call / subagent / file-attachment data that
  is invisible if you just stare at the tmux pane.

The `pm chat` CLI is a thin wrapper on top of these endpoints (see §6).
If you find yourself re-implementing parts of `pm chat`, hit the HTTP
API directly instead.

### Auth model

The chat API reuses PollyPM's existing daemon-wide bearer-token auth.

- Token lives at `~/.pollypm/api-token` (created on first `pm up`).
- Pass it via `Authorization: Bearer <token>` on every request.
- The daemon listens on `127.0.0.1:8765` by default. Remote access
  requires `pm serve --host 0.0.0.0` (and is your problem to firewall).

The chat API does **not** use the per-session `auth_token` field on
`SessionConfig`. That token is for outbound PollyPM → agent control
messages (the `[PollyPM-Auth: ...]` marker, see
`docs/recovery-cascade.md` §3). Inbound API clients authenticate with
the daemon-wide bearer token only.

`auth_token_present` on the `sessions` response is informational only.
GET reads and POST sends both work regardless of its value.

---

## 2. Quickstart

Assume `TOKEN=$(cat ~/.pollypm/api-token)` and the daemon is on the
default `127.0.0.1:8765`.

**List every chat surface:**

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8765/api/v1/chat/sessions \
  | jq '.sessions[] | {session_name, surface_type, project, present: .window.present}'
```

Sample output:

```json
{"session_name": "operator",          "surface_type": "operator",  "project": null,      "present": true}
{"session_name": "architect_samblog", "surface_type": "architect", "project": "samblog", "present": true}
{"session_name": "advisor_samblog",   "surface_type": "advisor",   "project": "samblog", "present": true}
{"session_name": "task-samblog-47",   "surface_type": "worker",    "project": "samblog", "present": true}
```

**Pull the last 20 messages from Polly:**

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/api/v1/chat/operator/messages?limit=20" \
  | jq '.messages[] | {ts, role, type, text: (.text[:80])}'
```

**Send a message into the architect for `samblog`:**

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8765/api/v1/chat/architect_samblog/send \
  -d '{"text": "What is blocking samblog/47 right now?"}'
```

That last call will return `409 unsafe_mid_stream` if the architect is
actively streaming output (see §5.3). Override with
`"safety": "loose"` if you want to queue the text anyway.

---

## 3. Endpoint reference

All endpoints are mounted under `/api/v1/chat/` on the PollyPM web API
(`src/pollypm/web_api/app.py`). All responses are JSON. All endpoints
require `Authorization: Bearer <token>`; absent or wrong token returns
`401 Unauthorized` (handled by the daemon-wide auth middleware, not by
the chat router).

### 3.1 `GET /api/v1/chat/sessions`

Enumerate every chat surface the daemon currently knows about.
Discovery endpoint for clients that don't know session names a priori.
No query params.

**Response:**

```json
{
  "sessions": [
    {
      "session_name": "operator",
      "surface_type": "operator",
      "persona": "Polly",
      "project": null,
      "task_id": null,
      "window": {
        "tmux_session": "storage-closet",
        "window_name": "pm-operator",
        "present": true,
        "pane_id": "%17",
        "pane_dead": false
      },
      "transcript": {
        "source": "jsonl",
        "path": "~/.pollypm/projects/samblog/.pollypm/transcripts/abc/events.jsonl"
      },
      "cwd": "/Users/sam/dev/samblog",
      "provider": "claude",
      "auth_token_present": true,
      "worktree_path": null
    }
  ]
}
```

Field notes:

- `window.present=false` when the configured window doesn't exist in
  tmux. The transcript path may still be readable (the agent crashed
  but the JSONL was flushed) — render the surface as greyed-out, not
  as deleted.
- `window.pane_dead=true` is reported when tmux flags the pane as
  exited; POST `/send` against a dead pane returns `409 pane_dead`.
- `transcript.source` from discovery is `"jsonl"` or `null` — discovery
  never reports `"capture"`. `null` means no events.jsonl archive exists
  yet (brand-new surface, spec §4.8); the history endpoint can still
  fall back to a live tmux capture when the window is present.
- `task_id` is populated for worker surfaces only; `null` for
  operator/architect/advisor.
- `worktree_path` is populated for worker surfaces running inside an
  isolated worktree; `null` otherwise.

### 3.2 `GET /api/v1/chat/{session_name}/messages`

Pull a window of messages from a single session's transcript.

**Path params:**

| Param | Meaning |
|---|---|
| `session_name` | Canonical session key. For operator/architect/advisor: the key from `config.sessions`. For workers: the computed `task-<project>-<task_number>` string. |

**Query params:**

| Param | Default | Meaning |
|---|---|---|
| `since` | none | ISO-8601 lower bound on message timestamp. Both `...Z` and `...+00:00` suffix shapes accepted. |
| `since_id` | none | Message id (string). Return messages strictly after this id. Both `since` and `since_id` are applied if passed together (the `since` lower-bound is applied first, then the cursor walk). |
| `limit` | `100` | Max messages, must be `1..500`. Values outside that range return `422 validation_error` (FastAPI Pydantic-level rejection). |
| `direction` | `desc` | `desc` (newest first) or `asc`. Pagination cursors assume the same direction. |
| `include_subagents` | `false` | When `true`, inline subagent transcripts inside their `subagent_result` parent's `metadata.subagent_transcript[]`. See §5.2. |
| `include_thinking` | `false` | Include `type=thinking` blocks. Default off — usually noisy. |
| `source` | `auto` | `auto` (JSONL with capture fallback when archive is missing or >60s stale), `jsonl` (force JSONL; 404 if absent), `capture` (force live `tmux capture-pane`). Any other value returns `422 validation_error`. |

**Response:**

```json
{
  "session_name": "operator",
  "surface_type": "operator",
  "persona": "Polly",
  "transcript_source": "jsonl",
  "transcript_path": "/Users/sam/dev/samblog/.pollypm/transcripts/abc/events.jsonl",
  "messages": [ /* MessageEnvelope[], see §4 */ ],
  "has_more": false,
  "next_cursor": "msg_xyz"
}
```

`transcript_source` is one of `"jsonl"`, `"capture"`, or `null`
(empty surface, spec §4.8). `transcript_path` is non-null only for
`transcript_source="jsonl"`. `next_cursor` is the `id` of the last
message returned; pass it back as `since_id` to fetch the next page
when `has_more=true`.

**Error codes:**

| Status | Code | Meaning |
|---|---|---|
| 404 | `session_unknown` | `session_name` is not in `config.sessions` and not a live worker. |
| 404 | `archive_missing` | `source=jsonl` forced, but no `events.jsonl` archive exists for this session. |
| 400 | `invalid_request` | `since` is not ISO-8601 parseable. (Other request-shape errors share this code; see the message body for the offending field.) |
| 422 | `validation_error` | FastAPI's standard Pydantic rejection envelope — returned for out-of-range `limit`, unknown `source`, unknown `direction`, etc. Shape is FastAPI's default `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`, NOT the chat router's `{code, message, hint}` envelope. |

**Curl:**

```bash
# Last 5 messages
curl -sH "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/api/v1/chat/operator/messages?limit=5"

# Everything since a known message id, oldest first
curl -sH "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/api/v1/chat/architect_samblog/messages?since_id=msg_abc&direction=asc&limit=200"
```

### 3.3 `POST /api/v1/chat/{session_name}/send`

> **Status:** P3 (#2043) implementation is in fix-loop; contracts
> below may shift before merge. Cross-check against
> `src/pollypm/web_api/routes/chat_send.py` at merge time.

Inject a message into the agent's input box via tmux. Returns
synchronously after the send; does **not** wait for the agent to
respond. Poll `GET /messages` for the reply.

**Request body:**

```json
{
  "text": "What is blocking samblog/47 right now?",
  "press_enter": true,
  "answer_to": null,
  "selections": [],
  "notes": null,
  "safety": "strict",
  "pane": null
}
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `text` | string \| null | `null` | Free-text message. Required when `answer_to` is null. When `answer_to` is set, `text` is an optional free-form reply (used if `selections` is empty). |
| `press_enter` | bool | `true` | Press Enter after typing. `false` to stage a long paste without submitting. |
| `answer_to` | string \| null | `null` | Id of the `ask_user` envelope this is replying to. See §5.5. |
| `selections` | string[] | `[]` | One or more option labels for an `ask_user` reply. Only meaningful when `answer_to` is set. |
| `notes` | string \| null | `null` | Free-text addendum after a selection. Also usable as the body of an `answer_to` reply when both `text` and `selections` are absent. |
| `safety` | enum | `strict` | `strict` (reject mid-stream + mid-tool), `loose` (mid-tool still blocks, mid-stream allowed with warning header), `force` (bypass both gates). |
| `pane` | int \| null | `null` | 0-based pane index inside the target window. `null` (or `0`) = primary/active pane. See §5.4. |

**Response:**

```json
{
  "ok": true,
  "message_id": "msg_abc123def456",
  "session_name": "operator",
  "window_target": "samblog-storage-closet:pm-operator",
  "characters_sent": 47,
  "method": "send_keys",
  "press_enter_at": "2026-05-21T20:53:12.500Z"
}
```

- `message_id` is a server-minted correlation id of the form
  `msg_<uuid4-hex>`. It is **not** guaranteed to match the eventual
  envelope `id` in the transcript — the transcript layer mints its
  own id on ingest. Use `press_enter_at` + the next user envelope's
  `ts` to correlate if you need to.
- `window_target` is the tmux `session:window` (or `session:window.pane`)
  string the daemon addressed.
- `method` is `send_keys` for messages ≤100 characters, `paste_buffer`
  for messages over 100 characters (see issue #808 for why
  paste-buffer is the default for long text).
- `press_enter_at` is `null` when `press_enter=false`.
- When `safety=loose` and the agent appeared streaming, the response
  includes header `X-PollyPM-Warning: agent-may-be-streaming`.

**Error codes:**

| Status | Code | Meaning |
|---|---|---|
| 404 | `session_unknown` | session_name not in config and not a live worker. |
| 503 | `window_missing` | Window configured but not present in tmux. Restart the session. |
| 409 | `pane_dead` | tmux flagged the pane as dead. |
| 409 | `unsafe_mid_tool` | Latest assistant turn has an open `tool_use` with no matching `tool_result`. Override with `safety=force`. See §5.1. |
| 409 | `unsafe_mid_stream` | Heartbeat shows the session streamed within the last 2s. Override with `safety=loose` (warn-and-send) or `safety=force` (bypass). See §5.3. |
| 400 | `answer_to_missing` | `answer_to` id was not found in the session's recent transcript. |
| 400 | `selections_no_question` | `answer_to` references a message that isn't an `ask_user` envelope. |
| 400 | `selections_invalid` | One or more selections don't match the question's option labels. Response message includes the valid options. |
| 400 | `invalid_request` | Catch-all for body-shape problems: missing `text` when `answer_to` is unset; `answer_to` set but no `selections`/`text`/`notes` supplied; etc. The message body identifies the specific problem. |
| 422 | `validation_error` | FastAPI Pydantic rejection — malformed JSON, wrong field types, `safety` not in `{strict, loose, force}`, etc. Default FastAPI envelope shape (`{"detail": [...]}`). |

**Curl examples:**

```bash
# Plain send
curl -sX POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8765/api/v1/chat/operator/send \
  -d '{"text": "hello Polly"}'

# Answer an AskUserQuestion
curl -sX POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8765/api/v1/chat/architect_samblog/send \
  -d '{"answer_to": "msg_q1", "selections": ["dayjs"], "notes": "go light"}'
```

---

## 4. MessageEnvelope schema

Every entry in `messages[]` is the same shape. The `type` field tags
which variant the envelope represents and what shape `metadata` takes.

```json
{
  "id": "msg_abc",
  "ts": "2026-05-21T20:53:12.123Z",
  "role": "user" | "assistant" | "tool" | "system",
  "actor": "Sam" | "Polly" | "Archie" | "Codex" | "system",
  "type": "text" | "tool_use" | "tool_result" | "thinking" | "ask_user" | "file" | "subagent_spawn" | "subagent_result" | "system_event",
  "text": "...display-ready string...",
  "metadata": { "...": "type-specific" }
}
```

`text` is always populated with a best-effort, one-line, human-readable
rendering — even a `tool_use` envelope has `text` like `[Bash] git
status` so a dumb client can render it without parsing `metadata`. The
structured original lives in `metadata`.

### Type catalog

| Type | Role | When | Notes |
|---|---|---|---|
| `text` | user / assistant | Plain turn text | The 80% case. `metadata: {}`. |
| `tool_use` | assistant | Tool call | `metadata`: `tool_use_id`, `tool_name`, `tool_input`. |
| `tool_result` | tool | Tool return | `metadata`: `tool_use_id`, `is_error`, `content[]`. |
| `thinking` | assistant | Extended-thinking block | Opt-in via `?include_thinking=true`. |
| `ask_user` | assistant | `AskUserQuestion` tool | `metadata`: `questions[]`, `answered`, `answers`. See §5.5. |
| `file` | assistant | `SendUserFile` tool | `metadata`: `files[]` (local paths), `caption`, `status`. |
| `subagent_spawn` | assistant | `Task` tool call | `metadata`: `subagent_id`, `subagent_type`, `description`, `prompt`, `isolation`. |
| `subagent_result` | tool | `Task` tool return | `metadata`: `subagent_id`, `summary`, `duration_ms`, `total_tokens`, `worktree_path`, `subagent_transcript` (null unless §5.2). |
| `system_event` | system | Compaction, session-start, error | `metadata.subtype` ∈ `compaction`, `session_start`, `session_resume`, `error`. |

### Example payloads

`tool_use`:

```json
{
  "type": "tool_use",
  "text": "[Bash] git status",
  "metadata": {
    "tool_use_id": "toolu_abc",
    "tool_name": "Bash",
    "tool_input": {"command": "git status", "description": "Show working tree status"}
  }
}
```

`tool_result` (linked by `tool_use_id`):

```json
{
  "type": "tool_result",
  "text": "M src/foo.py\nA tests/bar.py",
  "metadata": {
    "tool_use_id": "toolu_abc",
    "is_error": false,
    "content": [{"type": "text", "text": "M src/foo.py\nA tests/bar.py"}]
  }
}
```

`ask_user`:

```json
{
  "id": "msg_q1",
  "type": "ask_user",
  "text": "Which library should we use for date formatting?",
  "metadata": {
    "questions": [{
      "question": "Which library should we use for date formatting?",
      "header": "Library",
      "multiSelect": false,
      "options": [
        {"label": "date-fns", "description": "Modular, tree-shakeable"},
        {"label": "dayjs",    "description": "Lightweight Moment.js replacement"},
        {"label": "luxon",    "description": "Immutable, locale-aware"}
      ]
    }],
    "answered": false,
    "answers": null
  }
}
```

Once answered, the envelope reappears with `answered=true` and
`answers` populated.

`file`:

```json
{
  "type": "file",
  "text": "[file] ~/Desktop/report.pdf",
  "metadata": {
    "files": ["~/Desktop/report.pdf"],
    "caption": "Here's the report",
    "status": "proactive"
  }
}
```

`subagent_spawn` / `subagent_result` (paired by `subagent_id`):

```json
{
  "type": "subagent_spawn",
  "text": "[subagent] Fix #1234 sweep dedupe",
  "metadata": {
    "subagent_id": "a8a0c7ddcd5e43028",
    "subagent_type": "general-purpose",
    "description": "Fix #1234 sweep dedupe",
    "prompt": "...full prompt...",
    "isolation": "worktree",
    "run_in_background": true
  }
}
```

```json
{
  "type": "subagent_result",
  "text": "Done. PR #1235 pushed.",
  "metadata": {
    "subagent_id": "a8a0c7ddcd5e43028",
    "summary": "Agent 'Fix #1234' completed",
    "duration_ms": 159150,
    "total_tokens": 45533,
    "worktree_path": "/Users/sam/dev/pollypm/.claude/worktrees/agent-a8a0c7ddcd5e43028",
    "result_body_truncated": false,
    "subagent_transcript": null
  }
}
```

When `?include_subagents=true`, `subagent_transcript` becomes a
`MessageEnvelope[]`. Recursive — a subagent's own subagents nest the
same way.

`system_event`:

```json
{
  "type": "system_event",
  "text": "Conversation compacted (123k tokens → 32k)",
  "metadata": {"subtype": "compaction", "tokens_before": 123000, "tokens_after": 32000}
}
```

---

## 5. Edge cases ("Claude's fancy stuff")

Twelve gates the chat API has to handle correctly. Each has a defined
behavior, an override where applicable, and a reason it exists.

### 5.1 Mid-tool sends (`409 unsafe_mid_tool`)

The agent's last turn has a `tool_use` block whose `tool_use_id` has
no matching `tool_result` later in the transcript — it's paused
waiting for the tool to return.

**Why:** sending arbitrary text via `send_keys` while the agent's loop
isn't reading stdin is at best a no-op; at worst the next turn picks
the typed text out of Claude Code's input buffer and prepends it to
the user's next real message, confusing the agent.

**Behavior:** GET surfaces the open `tool_use` normally. POST with
`safety=strict` (default) returns `409 unsafe_mid_tool`. `safety=force`
overrides.

**When to override:** interrupting a wedged tool call (e.g. Bash on a
hung network request) by sending Ctrl-C followed by a recovery
instruction.

### 5.2 Subagents (Task / Agent tool)

Claude can spawn subagents via the `Task` tool. Each subagent gets its
own JSONL at the path in the parent's `task-notification.output-file`
block.

**Default:** parent transcript surfaces `subagent_spawn` +
`subagent_result` envelopes; the subagent's full transcript is NOT
inlined.

**Opt-in:** `?include_subagents=true` populates
`subagent_result.metadata.subagent_transcript[]`. Recursive — a
subagent's subagents nest the same way. The server resolves the
transcript path automatically; clients never need to walk
`task-notification` blocks themselves.

### 5.3 Mid-stream sends (`409 unsafe_mid_stream`)

The agent is currently emitting output — the session heartbeat fired
within the last 2 seconds.

**Why:** typing into the input box during a stream queues characters
with a race. They can interleave with the agent's own output frame,
garbling the display and occasionally producing the "phantom user
message" failure (typed characters land inside the agent's previous
turn as if the agent typed them).

**Behavior:**

- `safety=strict` (default) — reject with `409 unsafe_mid_stream`.
- `safety=loose` — allow, with response header
  `X-PollyPM-Warning: agent-may-be-streaming`.
- `safety=force` — bypass all safety gates.

**When to override:** stop-the-world messages (`STOP`, a correction,
mid-stream context-update). Reach for `loose` first; use `force` only
if `loose` also rejects.

### 5.4 Multi-pane (split-window) sessions

PollyPM doesn't split agent windows itself, but some users do
manually. The window now has multiple panes, only one of which is the
Claude process.

**Behavior:** POST accepts `pane=<index>` in the body; default is the
first pane in window-pane-list order (which is almost always the
Claude pane). GET never differentiates panes — transcripts are
one-per-Claude-process, not one-per-pane. If you don't know which
pane, list them via `tmux list-panes -t <window>` on the daemon host.

### 5.5 Answering `AskUserQuestion`

The agent emitted an `ask_user` envelope (§4). In Claude Code's UI,
the user clicks a button; in tmux, the user types the option label.

```json
{
  "answer_to": "msg_q1",
  "selections": ["dayjs"],
  "notes": "actually, prefer luxon if dayjs has no timezone story"
}
```

Translation rules:

- `selections: ["dayjs"]` → typed as `dayjs\n`.
- `selections: ["dayjs"]` + `notes: "..."` → typed as
  `dayjs\n...notes...\n`.
- Multi-select `selections: ["a", "b"]` → typed as newline-separated
  labels. If your install needs a different delimiter, fall back to
  plain `text`.

If a selection label doesn't exactly match any option, the API returns
`400 selections_invalid` with the valid labels in the response body.

**OPEN QUESTION (Sam to confirm):** the exact stdin format Claude Code
expects for AskUserQuestion replies. Phase 1 assumes "type the option
label verbatim, newline-terminated." If you find a session where this
doesn't work, fall back to free-text `text` sends and let the agent
parse the natural-language answer.

### 5.6 Compaction events

Claude Code occasionally compacts the conversation, producing a
`system_event` envelope with `metadata.subtype=compaction`. These are
always included in the GET response (phase 1 has no
`include_system_events` toggle — filter client-side by
`type=="system_event"` if your UI doesn't render them). Don't drop them
silently if the user might wonder why the conversation "jumps" —
render at least a horizontal rule.

### 5.7 Stale JSONL → tmux capture fallback

The JSONL exists but hasn't been written for >60s and the tmux pane is
live — usually means Claude Code hasn't flushed mid-stream.

**Behavior (under `source=auto`):** fall back to
`tmux capture-pane -p -S -3000`. Each captured line becomes one
envelope with `type=text` and `metadata.from_capture=true`. Ids are
synthetic (`cap_<hash>`), stable across reads.

Force pure JSONL with `?source=jsonl` (accept the possibility of a
`404 archive_missing` response). Force pure capture with
`?source=capture`.

### 5.8 New session, no transcript yet

The session was just spawned; no Claude turn has landed in the JSONL.
GET returns `messages: []` with `transcript_source: null`. **Not** an
error; render an empty pane.

### 5.9 Configured session, window missing

The session is in `config.sessions` but the window is not in tmux
(process crashed, user killed the pane).

- GET still works against the on-disk transcript.
- POST returns `503 window_missing`. Restart via
  `pm session restart <session_name>` and retry.

### 5.10 Bracket-paste interactions

Claude Code's input box understands tmux bracket-paste sequences.
PollyPM's `tmux/client.py::send_keys` already picks the `paste_buffer`
method for text >100 chars to avoid character-level interleaving on
concurrent sends (issue #808). The POST endpoint just delegates — no
new logic. The response's `method` field tells you which path was
taken.

### 5.11 Concurrent sends from multiple clients

Two clients POST to the same session simultaneously. The paste-buffer
mechanism prevents character-level interleaving within a message, but
Enter presses can still interleave at message-boundary granularity, so
ordering is racy. Phase 1 documents this as known behavior; future
work could serialize POSTs per-session via a daemon-side lock.

### 5.12 Codex sessions (non-Claude)

PollyPM also drives Codex (per memory: the `codex-fixer` tmux
session). Codex doesn't write the Claude Code JSONL shape, so the
server detects Codex via `SessionConfig.provider` and falls back to
`tmux capture-pane` automatically. Envelope `type` is always `text`
(Codex's tool calls aren't structured the way Claude Code's are). If
you need structured tool data, use a Claude session.

---

## 6. CLI alternative — `pm chat`

> The `pm chat` CLI lands in PR #2047 — these commands work once that
> merges. The HTTP endpoints (§3) are usable directly today via `curl`.

The `pm chat` CLI is a thin client over these endpoints. It exists
because typing curl with bearer-token plumbing every time gets old.

```
pm chat list
pm chat history <session_name> [--limit N] [--json] [--include-subagents]
pm chat send <session_name> <text> [--safety strict|loose|force]
pm chat send <session_name> --answer-to <msg_id> --selection <label> [--notes "..."]
```

- `pm chat list` → `GET /api/v1/chat/sessions`.
- `pm chat history` → `GET /api/v1/chat/<session>/messages`. Renders
  envelopes with sensible default formatting; pass `--json` for the
  raw payload.
- `pm chat send` → `POST /api/v1/chat/<session>/send`. Default
  `safety=strict`; prompts to bump to `loose` interactively if the
  daemon returns `unsafe_mid_stream` and stdin is a tty.

If you find yourself reaching for `curl | jq` against the chat API,
check `pm chat --help` first — most ad-hoc cases are already there.

---

## 7. Troubleshooting

Common failures and what to check.

### (a) Every request returns `401 Unauthorized`

Bearer token is wrong, missing, or empty.

```bash
ls -l ~/.pollypm/api-token         # exists, non-empty?
wc -c < ~/.pollypm/api-token       # should be ~64
```

If empty, restart the daemon (`pm up`) — it mints a fresh token on
startup when missing.

### (b) `GET /messages` returns `messages: []` for a session you know is active

Three likely reasons:

1. **Session was just spawned.** No turn has landed in the JSONL yet.
   Wait, or pass `?source=capture` to read the live pane directly.
2. **Wrong `session_name`.** Workers use the computed
   `task-<project>-<task_number>` form, not the human-readable
   description. Confirm via `GET /api/v1/chat/sessions`.
3. **JSONL path resolution failed.** Check `transcript.source` in the
   `sessions` response — `null` means the daemon couldn't find a
   JSONL or a live pane.

### (c) `POST /send` keeps returning `409 unsafe_mid_stream`

The heartbeat thinks the agent is streaming. Either:

- The agent really is — poll `GET /messages?limit=1` until the latest
  envelope's `ts` is more than 2s old, then retry.
- The agent crashed mid-stream and never wrote a final heartbeat. The
  2s window will never expire on its own. Confirm with
  `tmux capture-pane -t <window>` (is anything actually changing?);
  if not, override with `safety=loose` once.

### (d) `POST /send` returns `503 window_missing`

The agent's tmux window is gone. The on-disk transcript is still
readable, but you can't send until the session is restarted:

```bash
pm session restart <session_name>
```

### (e) AskUserQuestion replies don't register

You POSTed with `answer_to` + `selections`, but the agent ignored the
selection.

1. **Was the selection label exact?** Case and punctuation matter.
   `dayjs` and `Dayjs` are different labels.
2. **Did the agent see the input?** Pull `GET /messages?limit=3` after
   the POST — there should be a `text` envelope with `role=user`
   containing your selection. If yes but the agent ignored it, the
   `AskUserQuestion` stdin protocol may have changed (see §5.5 open
   question). File a bug.

### (f) Subagent transcripts come back empty under `?include_subagents=true`

`subagent_result` is there, but `metadata.subagent_transcript` is
`null` or `[]`. Either the subagent's `output-file` doesn't exist on
disk yet (subagent crashed before flushing), or it's a Codex subagent
(no JSONL) — Codex subagents surface as `subagent_result` with
`subagent_transcript: null` and nothing structured to inline.

### (g) Transcript appears truncated or out of order

Two gotchas:

1. **You hit the `limit` cap.** Default 100, max 500. If `has_more`,
   paginate with `since_id=<next_cursor>`.
2. **Compaction.** A compaction event can leave the visible turn
   count smaller than expected. Pre-compaction messages are still in
   the JSONL but the model's working context no longer references
   them.

### (h) `POST /send` returns `400 invalid_request` with no obvious clue

Things to check:

- Content-Type header (`Content-Type: application/json`).
- Body is valid JSON (jq it first).
- Right `session_name` (worker sessions are `task-<project>-<task_number>`,
  hyphen between project and number, not underscore).
- When `answer_to` is set, supply at least one of `selections`, `text`,
  or `notes` — an `answer_to` with all three empty is rejected.
- When `answer_to` is unset, `text` is required.

---

## 8. Cross-references

- Implementation (source of truth for the wire format):
  - `src/pollypm/web_api/routes/chat_messages.py` — `GET /sessions`
    and `GET /{session_name}/messages` (PR #2045).
  - `src/pollypm/web_api/routes/chat_send.py` — `POST /{session_name}/send`
    (PR #2043).
- Recovery cascade and the `[PollyPM-Auth: ...]` marker contract:
  `docs/recovery-cascade.md` — explains why the per-session `auth_token`
  field exists and why it's NOT what inbound API clients use.
- Cockpit interaction surfaces: `docs/cockpit-interaction-contract.md`.
- Web API mount point: `src/pollypm/web_api/app.py`.
- Tmux client (paste-buffer behavior): `src/pollypm/tmux/client.py`
  and issue #808 for the concurrent-send rationale.
