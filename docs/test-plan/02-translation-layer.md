# 02 — Translation Layer (TUI ↔ Storage ↔ REST)

**Goal:** prove that what an agent emits in a tmux pane ends up identically in storage AND in the REST API — and vice versa, that what the REST API claims is in the system actually IS in the pane and storage.

This is where drift between three sources of truth (pane → ingestor → PG → REST → consumer) eats reliability. If the translation layer leaks information, the Web UI's reliability ceiling is capped at the translation layer's fidelity.

**Time:** 2–3 hours.

**Prereqs:** §00 baseline green. §01 has at least passed the assignment-path scenarios (1.1). `pm serve` running.

---

## What you need to know

### Sources of truth

1. **tmux pane.** The live visual buffer the agent sees and writes to.
2. **`events.jsonl` archive.** Per-session normalized event log; the canonical "what happened." Written by `TranscriptIngestor`.
3. **PG.** `tasks`, `messages`, `markers` tables — the structured state.
4. **REST API.** `GET /api/v1/chat/{session}/messages`, `GET /api/v1/tasks/...`, etc. The Web UI's window.

### Recent context

- **#2079** — preserves `thinking` content blocks in Claude transcripts, with `ParserInternalType.THINKING` envelope shape.
- **#2086** — wires `include_thinking=true` query param through HTTP route + OpenAPI; `MessageType.THINKING` now public.
- **#2069** — mtime cache on `parse_events_jsonl`.
- **#2084** — tail-read for `parse_events_jsonl_tail` when `?limit` is small. Cold path < 5ms for limit=50.
- **#2083** — `include_subagents=true` inlines child subagent transcripts via raw-JSONL parser.

### Setup

```bash
export BASE=http://$(tailscale ip -4):8765
export TOKEN=$(cat ~/.pollypm/api-token)
```

---

## 2.1 Transcript ingestor fidelity (Claude)

**Goal:** every type of Claude assistant content block round-trips through the ingestor → archive → REST without loss or reorder.

### 2.1.1 Plain text content

Setup: drive a Claude session in `pollypm:pm-operator`. Send "respond with exactly: 'roundtrip test 2.1.1'".

After response lands:
```bash
# Get the last assistant message via REST
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/chat/operator/messages?limit=1&direction=desc" | jq '.messages[0]'
```

**Expected:** envelope with `type: text`, `role: assistant`, text content exactly matches.

**Pass criterion:** byte-equal match between what's in the pane and what comes back from REST. Any escaping or whitespace drift is a bug.

### 2.1.1.1 Envelope schema

Independent of content, every envelope returned by the API must have a known schema. Sample one response and verify:

```bash
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/chat/operator/messages?limit=1&direction=desc" | \
  jq '.messages[0] | keys'
```

**Required keys per envelope (from `web_api/chat/envelope.py`):**
- `id` — stable identifier.
- `type` — one of `MessageType` enum values (must match `docs/api/openapi.yaml::ChatMessageType`).
- `role` — `user` | `assistant` | `tool` | `system`.
- `actor` — session-name attribution.
- `text` — rendered content; may be empty for tool envelopes.
- `metadata` — provider-specific fields (model, signature, tool_name, tool_use_id, etc.).
- `ts` — ISO timestamp.

**Pass:** keys match exactly; no extra keys leaking private state; `type` is in the public enum.

If you find a key whose name suggests internal state (e.g. `_internal_*`, `__cache_key`, `raw_provider_blob`), that's `bug:envelope-leak`.

### 2.1.2 Thinking blocks (post-#2079)

Send a prompt that triggers a thinking block: "Think carefully then answer: what's 17 × 23?"

```bash
# Without include_thinking — should NOT see thinking envelope
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/chat/operator/messages?limit=3&direction=desc" | jq '.messages[].type'
# Expect: text, user_turn, etc. No "thinking".

# With include_thinking — should see it
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/chat/operator/messages?limit=3&direction=desc&include_thinking=true" | jq '.messages[].type'
# Expect: thinking, text, user_turn (or similar — order matters!).
```

**Critical: order must match provider order.** If the original content was `[thinking, text]`, the envelopes must come back in that order. Per #2079 round-3 fix.

**Pass:**
- Default request: no thinking leaked into response.
- `include_thinking=true`: thinking envelope present, with `text` + `metadata.signature`.
- Order: thinking before text (or whatever provider order was).

### 2.1.3 Tool use blocks

Send a prompt that forces a tool call: "List files in /tmp using Bash."

```bash
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/chat/operator/messages?limit=5&direction=desc" | jq '.messages[] | {type, role}'
```

**Expected sequence (newest first):**
- `text` (assistant) — final response
- `tool_result` (tool) — bash output
- `tool_use` (assistant) — bash invocation
- `text` (user) — user prompt

Order is reverse-chronological in `desc`; verify by flipping to `asc` and confirming chronological order.

**Pass:** tool_use → tool_result envelopes present, with `metadata.tool_name`, `metadata.tool_input`, `metadata.tool_use_id` populated for Claude. For Codex, `metadata.tool_name` present even if generic.

### 2.1.4 Long messages (no truncation)

Send a prompt that produces a long response (>10KB). After it lands:
```bash
# Fetch full content
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/chat/operator/messages?limit=1&direction=desc" | jq -r '.messages[0].text' | wc -c
```

**Pass:** byte-count matches what the agent emitted. No silent truncation at 8KB or 16KB.

### 2.1.5 Subagent transcripts (post-#2083)

Trigger a subagent task. After completion:
```bash
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/chat/operator/messages?limit=20&include_subagents=true" | \
  jq '.messages[] | select(.metadata.subagent_transcript) | {parent: .id, child_count: (.metadata.subagent_transcript | length)}'
```

**Expected:** at least one envelope has `metadata.subagent_transcript` populated with the child's envelopes.

**Pass:**
- Subagent transcript is parsed from raw JSONL (per #2083), not the normalized archive shape.
- Setting `include_subagents=true` does NOT poison the cache for subsequent default requests (per #2083 round 2 — copy-on-write fix). Send a default request after; assert no `subagent_transcript` field appears.

---

## 2.2 Transcript ingestor fidelity (Codex)

**Goal:** same as 2.1, but for Codex sessions.

### 2.2.1 Codex text + tools

Drive a Codex session. Send "List /tmp via shell."

Per #2071 / #2079 reconciliation: Codex sessions are normalized via `_normalize_codex_line`, NOT via tmux capture-pane (that's a staleness fallback only).

```bash
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/chat/codex_pollypm/messages?limit=5&direction=desc" | \
  jq '.messages[] | {type, role, has_tool_name: (.metadata.tool_name != null)}'
```

**Expected:**
- `tool_use` / `tool_result` envelopes present.
- `metadata.tool_name` populated (best-effort from Codex payload — generic name is OK, missing is a bug).
- Source is the normalized JSONL archive, not capture-pane.

### 2.2.2 Capture-pane fallback (staleness only)

Manually stale the archive: don't run any new commands for 60+ seconds (or whatever the staleness threshold is — check `_load_envelopes` in `chat_messages.py`).

```bash
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/chat/codex_pollypm/messages?source=auto&limit=5"
# Auto should detect staleness and fall back to capture-pane.
```

**Pass:** fallback fires on staleness. Source field in response (if exposed) indicates `capture`. Does NOT fire just because provider == codex.

---

## 2.3 REST recall correctness

### 2.3.1 Pagination

```bash
# First page
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/chat/operator/messages?limit=10&direction=desc" | jq '.messages[].id'

# Get cursor, fetch next page
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/chat/operator/messages?limit=10&direction=desc&since_id=<last_id>" | jq '.messages[].id'
```

**Pass:**
- No duplicates across pages.
- No gaps (every message ID accounted for).
- `direction=asc` returns chronological order; `desc` returns reverse.

### 2.3.2 mtime cache (post-#2069)

```bash
# Cold call
time curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/chat/operator/messages?limit=50" > /dev/null

# Immediate repeat — should be much faster
time curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/chat/operator/messages?limit=50" > /dev/null
```

**Pass:** second call is sub-50ms (per #2069 — cache hit on unchanged mtime).

### 2.3.3 Tail-read cold path (post-#2084)

For a long transcript (>1MB):
```bash
time curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/chat/operator/messages?limit=50&direction=desc" > /dev/null
```

**Pass:** cold call (cache miss) completes in <50ms. Per #2084, tail-read avoids parsing the full 1MB file.

### 2.3.4 Cache key tuple correctness (post-#2079, #2086)

`_PARSE_CACHE` is keyed by `(path, include_thinking)`. Make a request without `include_thinking`, then one with `include_thinking=true`. Verify they don't share a cache entry (no flag-flip pollution).

The internal cache state isn't exposed via API; verify indirectly by checking that the `include_thinking=true` response includes thinking envelopes AND a subsequent default request does NOT include them.

---

## 2.4 REST injection (send into pane)

### 2.4.1 Happy path

```bash
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"injection test 2.4.1"}' $BASE/api/v1/chat/operator/send
# Expect: 200 with pane info

# Within 1 second:
tmux capture-pane -t pollypm:pm-operator -p | tail -5 | grep "injection test 2.4.1"
```

**Pass:** literal text appears in pane within 1s. **Per the 1-second click rule, this is a hard budget.**

### 2.4.2 Mid-stream safety

Start the agent on a long task. While it's actively typing/thinking, try to inject:
```bash
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"mid-stream test"}' $BASE/api/v1/chat/operator/send
```

**Expected:** `409 unsafe_mid_stream` typed envelope. Message did NOT reach the pane.

### 2.4.3 Force bypass

```bash
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"force test"}' "$BASE/api/v1/chat/operator/send?safety=force"
```

**Expected:** 200, message lands even mid-stream. Confirms force bypass works.

### 2.4.4 Dead session

```bash
tmux kill-window -t pollypm:pm-operator  # warning: destructive
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"dead"}' $BASE/api/v1/chat/operator/send
# Expect: 503 window_missing typed envelope, NOT a 500 traceback
```

Restart the pane afterward.

---

## 2.5 Storage facade integrity

### 2.5.1 No sqlite reactivation

```bash
grep -rn 'register_backend.*sqlite' src/pollypm/ | grep -v 'tests/'
# Expect: only the guard in store/registry.py
```

Per #2074, production code can't re-register sqlite. Verify by attempting:
```python
.venv/bin/python -c "from pollypm.store.registry import register_backend; register_backend('sqlite', None)"
# Expect: ValueError raised (outside pytest)
```

### 2.5.2 Audit-stream canonical schema

Every audit event written must use the canonical writer in `pollypm.audit.log.emit`. Verify a recent audit row:
```bash
tail -1 ~/.pollypm/audit/pollypm.jsonl | jq 'keys'
# Expect: ["actor", "event", "metadata", "project", "schema", "status", "subject", "ts"]
```

**Pass:** all expected keys present. `ts` is ISO format, not Unix timestamp. `schema` is the canonical version.

---

## 2.6 Cross-surface read consistency

**Goal:** the live pane, the events.jsonl, and the REST API all agree on what was said.

### 2.6.1 Triple-witness assertion

Send a message via REST. Immediately capture all three witnesses and compare.

**Manual procedure:**
```bash
MSG="triple-witness-$(date +%s%N)"
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"text\":\"$MSG\"}" $BASE/api/v1/chat/operator/send

# Wait briefly for ingestion
sleep 2

# Witness 1: tmux pane (raw visual buffer)
tmux capture-pane -t pollypm:pm-operator -p | grep "$MSG" | tail -1

# Witness 2: events.jsonl (canonical normalized)
tail -100 ~/.pollypm/sessions/pm-operator/events.jsonl | \
  jq -c "select(.text? | test(\"$MSG\"))" | tail -1

# Witness 3: REST API
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/chat/operator/messages?limit=10&direction=desc" | \
  jq -c ".messages[] | select(.text | test(\"$MSG\"))" | tail -1
```

**All three must agree on:**
- The message text appears verbatim (modulo provider-side reformatting for tmux capture).
- The actor / role matches (`user` for an operator-injected send).
- The timestamps are within ±5s of each other.
- No additional copies of the message in any witness (no double-write).

**Pass:** zero drift. If any of the three disagrees, that's `bug:translation-drift`.

**Automation contract for promotion:**
The pytest integration test must:
1. Drive `POST /send` with a known unique token.
2. Poll until the token appears in events.jsonl (max 10s).
3. Read the same envelope back via the REST API.
4. Use `tmux capture-pane` in a subprocess to read the pane.
5. Assert: byte-equal text content across all three sources, matching role/actor, timestamps within tolerance.

This is the only test that catches slow drift between sources, so it ships first when §02 promotes to pytest.

---

## 2.7 Translation performance under realistic history

**Goal:** prove transcript fidelity does not collapse into full-file parsing or oversized payloads as history grows.

Create or identify surfaces at three sizes:

| Fixture | Minimum history | What it proves |
|---|---:|---|
| Small | 50 messages | Daily operator path |
| Medium | >1MB `events.jsonl` | Release gate path |
| Large | >10MB `events.jsonl` | Headroom / no accidental full parse |

For each fixture, run:

```bash
measure_http "messages-desc-50" "$BASE/api/v1/chat/<surface>/messages?limit=50&direction=desc"
measure_http "messages-asc-100" "$BASE/api/v1/chat/<surface>/messages?limit=100&direction=asc"
measure_http "messages-thinking" "$BASE/api/v1/chat/<surface>/messages?limit=50&direction=desc&include_thinking=true"
measure_http "messages-subagents" "$BASE/api/v1/chat/<surface>/messages?limit=50&direction=desc&include_subagents=true"
```

Use `scripts/perf/measure_http.sh` (per §06 promotion target — built by Codex lane E). The helper is also documented inline in `06-performance-budgets.md::6.2` for one-off use, but the script is the canonical source of truth.

```bash
scripts/perf/measure_http.sh messages-desc-50 \
  "$BASE/api/v1/chat/<surface>/messages?limit=50&direction=desc"
```

If the script does not exist yet, use the inline helper from §06.2 but file `bug:perf-script-missing` against lane E.

**Pass:**
- `direction=desc&limit=50` stays within §06 cold/warm budgets at every size.
- Default responses do not include thinking or subagent payloads.
- Opt-in responses do not poison later default cache entries.
- Payload sizes remain under §06 budgets.
- 10 concurrent readers of the same long transcript do not create 5xxs or p95 spikes above §06 M-scale budgets.

If this fails, the Web UI cannot be highly performant no matter how good the frontend feels on an empty transcript.

---

## Promotion to automation

- **2.1, 2.2, 2.3** → pytest integration tests. Drive a known fixture transcript, assert envelope shape exactly.
- **2.4** → pytest + Playwright (the Playwright tests already exercise send).
- **2.5** → pytest sanity (already partly covered by `tests/test_store_registry_sqlite_guard.py` and `tests/test_state_cache_no_sqlite_imports.py`).
- **2.6** → integration test that captures all three sources and asserts equality.
- **2.7** → perf harness with generated transcript fixtures; release gate, not every PR.

§2.6 is the most important to automate — it's the only way to catch slow drift between sources.

---

## Out of scope

- UI rendering — §03.
- Agent response quality — §04.
- Performance under load — §06.

---

## When you're done

Update test journal. Note any drift found between the three sources of truth — that's the headline finding for this section.
