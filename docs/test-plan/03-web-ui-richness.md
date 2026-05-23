# 03 — Web UI Richness & TUI Parity

**Goal:** the Web UI must (a) represent everything the TUI does, and (b) feel better than the TUI for the operator's daily workflow. Not just "renders" — *usable, intuitive, magical.*

**Time:** 4–6 hours.

**Prereqs:** §00 baseline green. §01 task lifecycle reliable. §02 translation layer reliable.

**Hard invariant:** **nothing the operator clicks in TUI or Web UI may take longer than 1 second to respond.** This is non-negotiable. Anywhere a click feels laggy, file `perf:click-latency` immediately and don't skip past it.

**User-level requirement:** this section must include real user-surface testing. Drive Web flows with Playwright plus exploratory real-browser use. Drive TUI parity with keystrokes sent to `pm cockpit` in tmux or a Textual `pilot` harness. CLI/API checks can corroborate state, but they do not prove the operator experience.

**Performance harness:** all timing claims in this section use the §06 methodology. Manual impressions are useful notes, but pass/fail requires traces or Playwright timing. Run the Web UI checks at S-scale during exploration and at M-scale before release.

Setup:
```bash
export BASE=http://$(tailscale ip -4):8765
export TOKEN=$(cat ~/.pollypm/api-token)
```

---

## What you need to know

- Web UI is served by `pm serve` at `/ui/` on the Tailscale IP, port 8765.
- Two access modes: **tailnet trust** (default; tailnet peers no-auth) and **explicit-host** (`pm serve --host 0.0.0.0 --allow-remote`; bearer required).
- Polling cadence:
  - Dashboard: 15s
  - Messages (selected surface): 5s
  - Surfaces list: 30s
- TUI: `pm cockpit` — for direct comparison.

### What the Web UI currently has (per #2065)

- Header with daemon status badge.
- Left rail: surface list grouped by type (operator / architect / advisor / worker).
- Right rail: 5 rollup cards (inbox count, plan reviews, alerts, activity 24h, daemon up/down).
- Center: selected surface transcript with send box.
- Cookie auth (HttpOnly, 7-day Max-Age).

### What it does NOT have yet (known gaps to verify)

- WebSocket / SSE — all updates poll.
- Surface filter / search.
- "Stop the agent" (Ctrl-C / Esc) affordance.
- Audit log panel.
- Task surfaces (tasks aren't in the rail; only sessions).
- Cookie expiry banner.

Document these as known gaps and decide which become this-sprint vs. next-sprint.

---

## 3.1 First load + cold paint

### 3.1.1 Tailnet first load

Open `http://<tailnet-ip>:8765/ui/` in a fresh incognito tab.

**Within 3s of navigation:** header + rails + center should be visible (even if center is empty state).

**Within 1s of fully painted:** clicking a surface should yield response.

Devtools checks:
- Application → Cookies: `pollypm-session` present, HttpOnly, SameSite=lax, expires ~7 days out.
- Network: only expected XHR endpoints. No 4xx/5xx.
- Console: zero errors.

**Score on five axes:**
- Functional: page renders. ✓ if so.
- Reliable: repeat 5x; no flake. ✓ if so.
- Fast: First Contentful Paint <3s, fully usable <5s. File `perf:cold-paint` if not.
- Intuitive: can you tell what each rail/card means without docs? File `ux:layout-clarity` if not.
- Magical: does it feel like the system already knows what you need? Or is it a generic dashboard? File `magic-gap:dashboard-relevance` for boring.

### 3.1.2 Already-warm reload

Reload the tab. Cached resources should make this near-instant.

**Pass:** reload completes in <500ms; no console errors.

### 3.1.3 Paint under realistic data

Repeat 3.1.1 at M-scale from `06-performance-budgets.md`:
- 20 active surfaces.
- 500 tasks.
- 5,000 total transcript messages.
- One selected surface with >1MB `events.jsonl`.
- 3 desktop tabs plus 1 real phone polling.

**Pass:** Web cold paint, usable time, and interaction budgets meet §06. If the UI only feels fast on an empty fixture, it is not ship-ready.

---

## 3.2 Surface enumeration parity

**Goal:** every surface visible in TUI is visible in Web, and vice versa.

```bash
# TUI surface list (from cockpit)
pm cockpit  # then visually count or use a CLI proxy:
pm sessions list --json | jq '.[].name' | sort > /tmp/tui-sessions.txt

# Web surface list
curl -sS -H "Authorization: Bearer $TOKEN" $BASE/api/v1/chat/sessions | \
  jq -r '.sessions[].name' | sort > /tmp/web-sessions.txt

diff /tmp/tui-sessions.txt /tmp/web-sessions.txt
```

**Pass:** no diff. Any session in one and not the other is a `bug:surface-enum`.

---

## 3.3 State indicator parity

For each surface (operator, architect_pollypm, worker_pollypm/1, advisor_pollypm), compare:
- **TUI:** what glyph appears? What color? Time-in-state?
- **Web:** what badge / indicator? Color? Time-in-state?

| State | TUI glyph | TUI color | Web equivalent | Match? |
|---|---|---|---|---|
| Working | ◆ | green | ? | ? |
| Waiting | ◇ | amber | ? | ? |
| Idle | ○ | slate | ? | ? |
| Blocked | ▲ | red | ? | ? |
| Done | ● | green | ? | ? |
| Paused | (badge) | (color) | ? | ? |

Fill in the Web column by inspection. Any mismatch is `bug:state-indicator-drift`.

**Per #2076** (cockpit_theme.py migration), TUI now uses semantic State namespace. Web has its own CSS palette. Verify the semantic mapping matches even if the exact hex differs.

---

## 3.4 Detail panel richness

**Goal:** clicking a surface in Web should show at least as much info as the TUI detail pane.

For each surface type, compare side-by-side:
- Recent transcript? ✓ in both?
- Active state + last-activity timestamp?
- Inbox items addressed to this surface?
- Linked tasks?
- "Why is this session paused/idle?" if applicable?

**Pass:** Web shows ≥ TUI's detail content. If TUI shows more, file `bug:detail-parity` per missing field.

---

## 3.5 The 1-second click rule

**This is the headline UX invariant.** Every interaction in the Web UI must respond within 1 second.

Test plan: click every clickable element, time the response with DevTools/Playwright trace, and record p50/p95/max per §06.

| Action | Expected response | Budget |
|---|---|---|
| Click surface in rail | Center loads transcript | < 1s |
| Click send button | Acknowledgment toast + pane update | < 1s |
| Refresh dashboard | New numbers in cards | < 1s |
| Switch surface mid-poll | New transcript loads, old clears | < 1s |
| Mobile: swipe between panes | Layout shifts | < 1s |

**Cache warmth matters:** measure both cold (first click after page load) and warm (subsequent clicks). Cold must stay under 1s; warm should stay under 250ms at S-scale and under the §06 M-scale budget.

**If any cell breaks 1s:** file `perf:click-latency:<action>` immediately. This is a ship-blocker.

Repeat for TUI: every `j`/`k` rail nav, every Enter into a pane, every back-out, every refresh trigger. Same 1-second budget.

### 3.5.1 Main-thread blocking

While running the click sweep, inspect browser traces for:
- Any main-thread long task >200ms.
- Layout thrash caused by switching surfaces.
- Large JSON parse or DOM render after every poll.
- Re-rendering non-selected transcripts.

**Pass:** no long task >200ms on the critical interaction path. A click can technically finish under 1s and still feel cheap/fragile if it blocks the main thread repeatedly.

---

## 3.6 Bidirectional sync

Verify both directions still work, with latency assertions.

### 3.6.1 Web → TUI

In Web UI, select `operator`, type a message, send.

**Within 1s:** appears in `tmux capture-pane -t pollypm:pm-operator -p | tail -10`.

### 3.6.2 TUI → Web

Attach to pollypm pane in tmux. Type a user message and Enter.

**Within 5s:** appears in Web UI's message list (per 5s polling).

### 3.6.3 Cross-device

Open Web UI on laptop AND phone. Send from laptop. **Within 5s:** appears on phone.

Switch surface on phone. **Does NOT affect laptop selection.**

Keep both devices open for 10 minutes. **Pass:** polling from one device does not degrade click latency on the other, and server-side §06 endpoint budgets remain green.

### 3.6.4 Phone via Tailscale

Open `http://mac-studio.taild21804.ts.net:8765/ui/` on actual phone in Safari.

- Layout stacks vertically.
- No horizontal scroll.
- Text legible without pinch-zoom.
- All click interactions still <1s on the phone (slower CPU; this is the real test).

---

## 3.7 Daemon-down behavior

Kill `pm serve`:
```bash
tmux send-keys -t pm-serve:serve C-c
```

**Within 30s** (next poll cycle):
- UI conn-status badge: green → warn → error.
- No white-screen of death.
- Refresh the page: should show clean error state, not stack trace.

Restart:
```bash
tmux send-keys -t pm-serve:serve 'pm serve' Enter
```

**Within 30s:** UI recovers.

**Pass:** graceful degradation, clean recovery, never a 500-page-in-the-browser.

---

## 3.8 Auth boundary

### 3.8.1 Tailnet trust mode (default)

| Test | Expected |
|---|---|
| GET `/api/v1/dashboard` from tailnet peer, no creds | 200 |
| GET `/api/v1/dashboard` from tailnet peer, bad bearer | 401 (explicit bad creds always reject) |
| GET `/ui/` from tailnet peer | HTML + Set-Cookie |
| GET `/ui/` from 127.0.0.1 (loopback) | connection refused (bound to tailnet IP) |

### 3.8.2 Explicit-host mode

```bash
# Kill default serve; restart in explicit-host mode
pm serve --host 0.0.0.0 --allow-remote
```

| Test | Expected |
|---|---|
| GET `/api/v1/dashboard` from CGNAT peer, no creds | 401 |
| GET `/api/v1/dashboard` with bearer | 200 |
| GET `/ui/` from CGNAT peer | HTML but NO Set-Cookie |
| GET `/ui/` from loopback | HTML + cookie |
| GET `/ui/` with bearer from non-tailnet | HTML + cookie |

### 3.8.3 Bearer rotation mid-session

```bash
# UI open + working
pm api regen-token
```

**Expected:**
- Next 5s message poll returns 401.
- UI displays auth error.
- Refresh `/ui/`: new cookie issued from disk. UI recovers.

---

## 3.9 Magic-feel checks

**These are the hardest to verify but the most important.** Self-evaluation by the testing agent produces optimistic results — the agent knows too much about the system to fairly simulate a new user. Use the "I wish I could…" log instead; it captures real friction the testing agent encounters without pretending to be someone else.

### 3.9.1 The "I wish I could…" log

Sit with the system as a user for 30 minutes. Keep a notebook open. Every time you think "I wish I could…", "huh, why doesn't it…", or "I had to drop to CLI for…", write it down.

These are the magic-gap candidates. After 30 minutes, each one becomes a `magic-gap:` issue with:
- What you were trying to do.
- What the UI made you do instead.
- What would have been magical.

**Pass:** the list captured at least 3 specific wishes (a list of 0 means you didn't push hard enough; a list of 20 means the magic gap is large).

The bar from `operator-day-in-the-life.md`: **a user who has been using PollyPM should never need to drop to TUI / CLI / `pm doctor` to understand their daily state.** Every "I had to drop to X" is a magic-gap issue.

True cold-operator testing (someone who has never seen PollyPM) is out of scope for this plan — it's covered by the separate onboarding test suite.

---

## 3.9.2 TUI parity — measurable via Textual pilot

For TUI interactions, manual stopwatching is unreliable. Use Textual's `pilot` harness to drive cockpit interactions in a test context.

**Pattern:**
```python
# tests/test_cockpit_click_rule.py (Codex lane G builds the suite)
from pollypm.cockpit_ui import CockpitApp
import time

async def test_rail_navigation_under_1s():
    app = CockpitApp()
    async with app.run_test() as pilot:
        t0 = time.monotonic()
        await pilot.press("j")
        await pilot.pause(0)  # process events
        elapsed = time.monotonic() - t0
        assert elapsed < 0.250, f"rail j took {elapsed:.3f}s (>250ms budget)"
```

**Manual fallback (acceptable for §03 exploratory only):**
- Use `tmux capture-pane -p | wc -l` as a coarse render check.
- Count seconds aloud (better than nothing, worse than `pilot`).

**Pass criterion:** TUI rail navigation, pane mount, and detail render all measurable via `pilot`. If a behavior is not measurable via `pilot`, that's `bug:tui-untestable` against the cockpit module.

## 3.10 Mobile-specific UX

Open the Web UI on a phone for a sustained period. Look for:

- **Thumb reach.** Are critical actions accessible without two-handed grip?
- **Tap targets.** Are buttons ≥44px tall (Apple guidance)?
- **Keyboard.** When the send box is focused, does the keyboard cover content that matters?
- **Backgrounding.** Open another app, come back. Does the UI recover state? Or reload from scratch?
- **Notifications.** When a new message lands while UI is backgrounded, is there any signal? (Currently no — but worth verifying.)
- **Low-power reality.** Repeat the surface-click and send checks with low-power mode enabled if available.
- **Network reality.** Move between Wi-Fi and cellular/tailnet; recovery should be clean and clicks should return under 1s after reconnection.

---

## Promotion to automation

- **3.1, 3.6, 3.7** → Playwright; partly already covered.
- **3.2, 3.3, 3.4** → Playwright with fixture data, exhaustive assertions.
- **3.5 (1-second click rule)** → Playwright performance probe with traces, p95/max assertions, and M-scale fixtures for release runs.
- **3.8** → Playwright + curl integration tests.
- **3.9 + 3.10** → manual, repeat every sprint.

The 1-second click rule deserves a dedicated Playwright spec that fails CI if any tested interaction exceeds budget. Release readiness also requires the §06 M-scale browser run; empty-state Playwright is not enough.

---

## Out of scope

- Task lifecycle correctness — §01.
- Translation-layer drift — §02.
- Agent response quality — §04.
- Failure injection beyond `pm serve` kill — §05.
- Cross-load performance — §06.

---

## When you're done

Update test journal. The headline output:
- List of `ux:` and `magic-gap:` issues found.
- 1-second-click-rule violations (each is a ship-blocker).
- TUI/Web parity gaps.
- "I wished for X" list — this becomes the next-sprint backlog.
