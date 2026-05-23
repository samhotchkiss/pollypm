# Operator Day-in-the-Life

**Goal:** anchor every abstract test scenario in this plan to a concrete operator workflow. If a test in this plan doesn't trace to something in this doc, ask whether it's the right test.

This is what we are actually validating. Not "the parser correctly normalizes thinking blocks" — that matters because **the operator opens the Web UI on their phone and trusts that what they see is what the architect said.**

---

## Who is the operator?

A single human, running PollyPM as their daily project manager. They have:
- A Mac Studio at home running `pm serve`, `pm cockpit`, and the daemon.
- A laptop at the desk that they SSH into / Tailscale into for terminal work.
- A phone they pick up while away from the desk.
- 3–10 active projects, each with an architect + advisor + workers.
- Long-running sessions (the architect for the main project has been going for 13+ hours).
- A real workday — they're not testing PollyPM; they're using it to manage their actual work.

They do not read documentation between sessions. They expect to sit down and pick up where they left off.

---

## A typical day

### Morning: catch up

1. Phone, on the train. Opens `http://mac-studio.tail.../ui/`.
2. **What the operator needs (and the plan must verify):**
   - Page loads fast on cellular (§03.1 + §06.3 phone cells + §03.10).
   - Dashboard immediately shows what's new since they slept (§01.4 visibility, §03.4 detail richness).
   - Any task that broke overnight is surfaced with reason (§01.4.5 "why is this stuck", §05.2 cascade).
   - Inbox has the morning briefing (§01.4.4 plan-review handoff, §03.4 detail).
3. Operator skims the briefing. Notices an architect proposed a plan; clicks plan-review item.
4. **Verification path:** §03 plan-review flow → §02 transcript fidelity → §04.4 cross-agent context.

### Mid-morning: dispatch work

1. Back at the desk. Mac laptop with both TUI and Web UI open.
2. Operator approves the architect's plan in the Web UI (or TUI — both should work).
3. Architect emits tasks; workers pick them up.
4. Operator watches in the rail: glyphs change from queued → working.
5. **What the plan must verify:**
   - Auto-claim works within 60s (§01.1.1).
   - State indicators match between TUI and Web (§03.3).
   - Click on a worker surface shows its current activity within 1s (§03.5).
   - If a worker hangs, the operator sees it surface within 3 min via the cascade (§01.5 + §05.2).

### Late morning: pause one project

1. Operator decides project X needs a break — wants to focus on Y.
2. They click pause on project X in the Web UI.
3. **What the plan must verify:**
   - Project pause is atomic even with concurrent reads (§01.3.2).
   - Workers on X stop getting work (§05.4 per #2081, partial enforcement).
   - The audit log shows what stopped + why (§05.4.3 throttle, §1.4.4 visibility).
   - Web UI immediately reflects paused state (§03.3 indicators).

### Afternoon: respond to an inbox prompt

1. Inbox lights up — architect asks operator to decide between approaches.
2. Operator opens the prompt on the phone.
3. **What the plan must verify:**
   - Message renders correctly on mobile (§03.10 + §02 fidelity).
   - The operator can reply from phone (§02.4 REST injection from Web).
   - Their reply reaches the architect's session within 1s (§3.6.1 sync, §06.7.1 send-to-pane).
   - The architect actually uses the operator's input (§04.2 multi-turn coherence).

### Mid-afternoon: a worker gets stuck

1. Worker on project Y stopped responding (Claude session crashed in tmux).
2. **What the plan must verify (this is the heart of the product):**
   - Heartbeat tier detects within ~60s (§01.5.1, §05.2.3 cascade detection lag).
   - `no_session_spawn` respawns the worker within 3 min total (§01.5.1, §05.2.1).
   - Task gets re-claimed without operator intervention (§01.1.3).
   - Audit log shows the cascade trail (§01.5.1 audit verification).
   - Operator's UI surfaces "worker was stuck, has been restarted, task continues" — they DON'T have to act (§01.4.5, §03.4).
   - **The operator never had to manually claim, dispatch, or restart anything** (§1.5.6 self-heal rule audit).

If any of these fails, the product is not ship-ready. This is THE scenario.

### Late afternoon: review completed work

1. Operator opens a worker session that just finished a task.
2. Reads through the transcript.
3. Approves or sends back to rework.
4. **What the plan must verify:**
   - Transcript renders identical to what was in the pane (§02 triple-witness).
   - Thinking blocks visible when operator wants them (§02.1.2, §04.2 if applicable).
   - Tool calls + results render correctly (§02.1.3).
   - Approve flips task state correctly (§01.2.1 happy-path lifecycle).

### Evening: shut down vs. let it run

1. Operator decides to keep the daemon running overnight.
2. Closes laptop, takes phone.
3. **What the plan must verify (long-tail):**
   - Daemon doesn't accumulate resources overnight (§06.8 soak).
   - Architect / advisor sessions don't drift in long-context (§04.6.1, §04.2.2).
   - Recovery cascade still fires if something breaks at 3 AM (§05.2, journal entry for that fire).
   - Morning re-open: everything looks like the operator expected (§01.4 visibility, §03 dashboard).

---

## What this doc is NOT

- A user manual. The operator does not read this.
- A feature list. The plan tests workflows, not feature checkboxes.
- A marketing description. We're listing what must work, not what's cool.

---

## Anti-scenarios — things the operator should NEVER need to do

These are magic-gap indicators. If the operator catches themselves doing any of these during a real day, the system has failed.

- **Drop to TUI to figure out why something is broken in Web.** Web should surface enough state.
- **Run `pm doctor` to find a stuck task.** The cockpit / Web UI should already flag it.
- **Manually claim a task.** Auto-claim or worker assignment should be the path.
- **Manually restart a session.** The cascade should self-heal.
- **Read logs to understand state.** The audit log is for forensics; live state belongs in the UI.
- **Refresh the Web UI to see latest.** Polling should keep it current (until WebSocket lands).
- **Wait more than a second for any click.** §03.5 / §06 click rule.
- **Wonder which agent has authority over a task or message.** §04.3 auth-marker; §03 visibility.

Each anti-scenario maps to one or more test sections. When the testing persona observes any of these during the run, file `magic-gap:<short>` immediately.

---

## How to use this doc when running the plan

Before each section, ask: "Which part of the operator's day does this validate?" If you cannot answer, the section is either misnamed or doesn't earn its time.

After each section, update the journal with a per-section trace: "§01.5.1 validates the 'worker gets stuck' mid-afternoon scenario. Result: <pass/fail>. Risk to that scenario shipping: <none / yellow / red>."

At ship-readiness decision time, walk through the day above end to end. Every scenario should have green verification. If any moment in the day is unverified, that's the gap.
