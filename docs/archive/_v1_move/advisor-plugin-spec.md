# Advisor Plugin Specification

**Status:** v1 shipped. This spec describes the built-in advisor plugin that is already live in PollyPM.
**Implementation:** Entry module: `src/pollypm/plugins_builtin/advisor/plugin.py`
**Depends on:** planner plugin, work service, session service, roster/jobs APIs, inbox view.

## 1. Purpose

An ongoing alignment coach. Every 30 minutes, on any project with recent activity, the advisor reviews what changed against the project's plan and goals. If — and only if — it notices something genuinely worth saying, it sends an inbox message.

Different from planner (which sets direction) and downtime (which uses idle budget for exploration): advisor is **real-time course-correction** for work in flight.

**Load-bearing principle: the advisor's credibility is its rarity.** There is no system-level rate limit. The persona prompt is the quality filter. If the advisor chimes in every 30 minutes, it becomes noise; if it chimes in once a week with a sharp structural observation, the user listens.

## 2. Plugin containment

```
plugins_builtin/advisor/
  pollypm-plugin.toml
  plugin.py
  profiles/
    advisor.md                 # the wise-senior-architect persona (carefully tuned)
  flows/
    advisor_review.yaml        # short session → emits structured decision
  handlers/
    advisor_tick.py            # @every 30m; finds changed projects; enqueues
    detect_changes.py          # git log + task transitions since last run
    assess.py                  # delegates to advisor session
    history_log.py             # logs emit/silent decisions for auditability
```

## 3. Roster tick

```python
api.roster.register_recurring(
    schedule="@every 30m",
    handler_name="advisor.tick",
    payload={},
    dedupe_key="advisor.tick",
)
```

Handler:
1. For each tracked project, run `detect_changes(project, since=last_run)`:
   - `git log --since="<last_run>"` returns ≥1 commit, OR
   - Any task transitioned (status change, node advance) since last_run.
2. For changed projects, create an `advisor_review` task (short-lived worker session).
3. Throttle: skip if an advisor task for this project is already `in_progress` (prevents pile-up if 30m interval is too tight for slow sessions).
4. Record last_run timestamp per project in `.pollypm-state/advisor-state.json` so next tick's `since=` is accurate.

**No rate limit beyond the pile-up throttle.** If the advisor has something to say every 30 minutes, it says it. Trust is built by the persona being judicious.

## 4. Context the advisor gets

The advisor session's system prompt is packed with three inputs:

1. **The plan** — `docs/project-plan.md` + Risk Ledger. The north star.
2. **The delta** — `git diff` since last advisor run, list of task transitions, list of new/modified files.
3. **Trajectory** — the last 3 advisor insights for this project (whether emitted or silent), so it doesn't repeat itself and can escalate if patterns compound.

## 5. The persona — the whole game

The advisor prompt is the only quality gate. It needs to be carefully built. Starting shape:

```markdown
---
name: advisor
preferred_providers: [claude, codex]
---

You are a senior architect on this team. Your role is **to be trusted and rare**.

You review what has happened in the last 30 minutes against the project's plan and goals. Your output is a **single decision**: emit an insight, or stay silent.

# Rules

1. **Silent by default.** If things are progressing reasonably — even imperfectly — stay silent. Your credibility is your rarity.

2. **Speak only when you see a structural issue that will cost materially more to unwind later than to correct now.**

3. **Never police style.** Never flag incomplete state (code still being written). Never nag about formatting, naming, or anything a competent engineer would catch in their next pass.

4. **Speak when:**
   - Architectural decisions are compounding in the wrong direction (monolith creep when plan specified modular; abstraction leaks across plan's boundaries).
   - The plan's test strategy is being ignored — e.g., modules shipping without their user-level tests.
   - Dependencies are being added that contradict the plan's constraints.
   - A pattern is emerging that will cost 10× more to reverse later than now.
   - Risk ledger items are materializing and nothing's being done about it.

5. **Before emitting, ask: would I pull the user aside in person to say this?** If not, stay silent.

6. **Tone: calm, specific, pragmatic.** You are not a critic looking for flaws. You are a senior peer with perspective. Speak directly; don't hedge. Offer a concrete next step.

# Output format

Produce structured JSON:

```json
{
  "emit": true | false,
  "topic": "architecture_drift | missing_tests | dependency_risk | plan_divergence | pattern_emerging | risk_materializing | other",
  "severity": "suggestion | recommendation | critical",
  "summary": "one-sentence crystallization of the observation",
  "details": "2-4 paragraph explanation grounded in the delta — cite specific files, commits, tasks",
  "suggestion": "one concrete next step the user could take",
  "rationale_if_silent": "if emit=false, one sentence on why you stayed silent"
}
```

If emit is false, the other fields except rationale_if_silent are optional. The rationale_if_silent is always required when silent — so the user can audit the advisor's judgment via `pm advisor history`.

# Example emit

Given: last 4 commits added handlers to cockpit.py (now 4,200 LOC); plan specified CockpitRenderer plugin with per-panel implementations.

```json
{
  "emit": true,
  "topic": "architecture_drift",
  "severity": "recommendation",
  "summary": "Cockpit is growing as a monolith; plan specified per-panel plugins.",
  "details": "The last four commits (SHAs: abc, def, ghi, jkl) added four new panel handlers directly to cockpit.py, pushing it past 4,200 LOC. The project plan (docs/project-plan.md, section 3.2) specifies that cockpit rendering be split into a CockpitRenderer plugin surface with per-panel implementations. Two more panels are planned; continuing on the monolithic path will roughly double current size and require a larger refactor once the plugin surface lands. The cost to extract now is one module-implementation task per panel.",
  "suggestion": "Extract the two largest panels into their own plugins before adding more. Run `pm task create --flow implement_module --title 'extract <panel> to plugin'`."
}
```

# Example silent

Given: last commit was a test-coverage pass on an existing module; plan is on track.

```json
{
  "emit": false,
  "rationale_if_silent": "Test coverage improvement on existing module, aligned with plan's test strategy. No structural concern."
}
```
```

This prompt is the v1 starting point. Tunable. `pm advisor history` (§8) exposes every decision so prompt-drift can be caught.

## 6. Severity levels

- **suggestion** — "consider this."
- **recommendation** — "you probably want to act on this soon."
- **critical** — "the project is materially off-course. Course-correct now or accept the drift deliberately."

All three emit normally. The severity drives cockpit/inbox rendering weight — critical gets a different color/emphasis in the inbox.

## 7. Inbox integration

Each emit creates an inbox entry with `kind=advisor_insight`. User actions:

- `pm task approve <id>` — "acknowledged, factored in." Closes.
- `pm task reject <id> --reason topic_cooldown` — "not this, not now." Records a soft signal in `.pollypm-state/advisor-state.json` that the advisor's next prompt-pack will mention ("user rejected topic X 30 minutes ago — weight accordingly"). **Not a system-enforced cooldown** — the persona is expected to respect it intelligently.
- Convert the insight into a normal work-service task from the inbox/cockpit flow, or create one manually with `pm task create ...` using the insight as the task brief.
- Silent no-action → auto-closes after 7 days.

## 8. Observability — `pm advisor history`

Every advisor run (emit or silent) writes a line to `.pollypm-state/advisor-log.jsonl`:

```json
{"timestamp": "...", "project": "...", "decision": "emit|silent", "topic": "...", "severity": "...", "summary": "...", "rationale_if_silent": "..."}
```

CLI:
- `pm advisor history [--project X] [--since 24h] [--decision emit]` — view the advisor's recent decisions.
- `pm advisor history --stats` — emit-rate per project, topic distribution.

This is how the user detects if the advisor is getting noisy. If emit-rate climbs past ~1 per day per active project over a sustained period, we tune the persona prompt — not add a rate limit.

## 9. Settings

`pollypm.toml`:

```toml
[advisor]
enabled = true                 # default; per-project opt-out
cadence = "@every 30m"         # override for lower-noise projects
```

CLI:
- `pm advisor pause` — 24h skip-until marker.
- `pm advisor resume` — clear.
- `pm advisor disable` — `enabled = false`.
- `pm advisor enable` — `enabled = true`.
- `pm advisor status` — current state + next tick.

## 10. Implementation roadmap (ad01–ad06)

1. **ad01** — Plugin skeleton, advisor persona (carefully tuned), roster tick, throttle-if-in-progress.
2. **ad02** — Change detection: git log + task transitions since last run, per-project last_run state.
3. **ad03** — Advisor session flow, context-pack (plan + delta + trajectory), structured JSON output contract.
4. **ad04** — History log + `pm advisor history` CLI for observability.
5. **ad05** — Inbox integration: `advisor_insight` kind + dismissal routing (soft topic cooldown via prompt, not system).
6. **ad06** — Settings + CLI: pause/resume/disable/enable/status + `[advisor]` config.
