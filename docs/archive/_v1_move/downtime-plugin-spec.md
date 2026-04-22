# Downtime Management Plugin Specification

**Status:** v1 shipped. This spec describes the built-in downtime plugin that is already live in PollyPM.
**Implementation:** Entry module: `src/pollypm/plugins_builtin/downtime/plugin.py`
**Depends on:** planner plugin, work service, session service, roster/jobs APIs, capacity subsystem, inbox view.

## 1. Purpose

Use-it-or-lose-it LLM budget is wasted every day. This plugin uses idle capacity to run autonomous exploration — speccing new features, drafting speculative implementations, auditing docs/security, exploring alternative approaches — and surfaces the results to the user for approval.

**Load-bearing principle: nothing produced in downtime is ever auto-deployed.** Every downtime task ends with an inbox message to the user. Thumbs up → commits / merges. Thumbs down → archives. No exceptions.

## 2. Plugin containment

```
plugins_builtin/downtime/
  pollypm-plugin.toml
  plugin.py
  profiles/
    explorer.md             # ambitious but scoped persona
  flows/
    downtime_explore.yaml   # flow every exploration task runs through
  handlers/
    downtime_tick.py        # @every 12h tick entry point
    pick_candidate.py       # candidate selection from backlog
    spec_feature.py         # writes to docs/ideas/<slug>.md
    build_speculative.py    # creates downtime/<slug> branch
    audit_docs.py           # drafts a PR against main
    security_scan.py        # report only — never creates branch
    try_alt_approach.py     # branch + comparison report
```

## 3. Roster tick

```python
# plugin.py initialize(api)
api.roster.register_recurring(
    schedule="@every 12h",
    handler_name="downtime.tick",
    payload={},
    dedupe_key="downtime.tick",
)
```

The tick handler:
1. Check `[downtime] enabled` in project config — default `true`; if false, exit.
2. Check `capacity.current_usage()`. If `used_pct >= 50%`, skip with reason logged.
3. Check for existing downtime tasks in `in_progress` or `review`. If any, skip (throttle).
4. Call `pick_candidate(project)`. If no candidates, exit.
5. Create a work-service task: `flow=downtime_explore`, `labels=["downtime"]`, `priority=low`, `kind=candidate.kind`, description from candidate.
6. Task enters the normal worker-pickup flow.

## 4. Candidate sources

Three sources feed `pick_candidate`, scored and shuffled:

1. **Planner output** — `docs/downtime-backlog.md`. Written by the planner during Magic and Synthesis stages. Unpicked tree-of-plans candidates, magic items deprioritized by critics. Each entry: title, kind, one-sentence spec, why deprioritized.
2. **User-queued** — `pm downtime add "Try X approach"` appends a candidate. `pm downtime list` shows queue.
3. **Auto-discovered**:
   - Doc drift scanner — files changed without docs touched in ≥7 days → "audit docs" candidate.
   - Security-audit scheduler — rotating focus on one subsystem per cycle.
   - Dep-audit — `pip-audit` / equivalent, surface findings as candidates.

Candidates store per-category priority hints. Selection favors variety over monotony (bandit-style: avoid picking the same kind twice in a row).

## 5. Flow: `downtime_explore`

```yaml
name: downtime_explore
description: Autonomous exploration during idle LLM budget.
roles:
  - name: explorer
    optional: false
nodes:
  - id: explore
    actor_type: role
    actor_role: explorer
    budget_seconds: 1800            # 30 min cap per exploration
    next_node: awaiting_approval
  - id: awaiting_approval
    actor_type: human
    gates:
      - name: inbox_notification_sent
        type: hard
    next_node: apply
  - id: apply
    actor_type: role
    actor_role: explorer            # the worker applies or archives based on approval
    next_node: done
```

Task can **never** advance past `awaiting_approval` without explicit user action. The `inbox_notification_sent` gate ensures the inbox message was actually dispatched before we wait.

## 6. Output routing by category

| Category | Artifact | On approval | On rejection |
|---|---|---|---|
| spec_feature | `docs/ideas/<slug>.md` (draft) | Move to `docs/specs/<slug>.md` (or committed location) | Archive under `.pollypm-state/archive/specs/` |
| build_speculative | Branch `downtime/<slug>` (commits, no merge) | User merges manually or approves auto-merge | Move branch ref to `archive/<slug>` and delete |
| audit_docs | Draft PR against main | User merges | Close PR, archive |
| security_scan | Report `.pollypm-state/security-reports/<date>-<scope>.md` — **no branch, no code changes** | Mark reviewed, keep report | Mark dismissed, keep report |
| try_alt_approach | Branch `downtime/<slug>` + comparison report | User decides direction | Archive |

Security-scan is the one category that deliberately never produces a branch. Findings sit in a report; the user decides whether the fix itself is a downtime task or scheduled planning work.

## 7. Inbox integration

When a downtime task reaches `awaiting_approval`, the handler:
1. Writes the artifact(s) per §6.
2. Creates an inbox message (via the inbox-replacement view from iv01) with kind=`downtime_result`, linking the artifact + a short summary.
3. User sees in `pm inbox` / cockpit inbox panel. Actions:
   - `pm task approve <id>` → advance to `apply` node, which commits/merges per §6.
   - `pm task reject <id>` → advance to `apply` node, which archives per §6.
   - `pm inbox reply <id> "..."` + approve → like approve, with the user's note logged.

## 8. Planner integration

Planner's `initialize(api)` gains one small contract: during Magic and Synthesis stages, write ranked deprioritized items to `docs/downtime-backlog.md`. Unpicked tree-of-plans candidates land there too. This is pure file output — the downtime plugin reads it, no cross-plugin API call.

## 9. Settings

`pollypm.toml`:
```toml
[downtime]
enabled = true              # default
threshold_pct = 50          # capacity % below which downtime fires
cadence = "@every 12h"      # schedule override
disabled_categories = []    # e.g. ["build_speculative"] to whitelist-by-exclusion
```

`pm downtime disable` writes `enabled = false`. `pm downtime pause` sets a 24h skip-until marker (for "not today"). `pm downtime resume` clears it.

## 10. Never-auto-deploy — enforcement

Three layers:

1. **Flow shape** (§5) — `awaiting_approval` node is `actor_type=human`. Non-negotiable.
2. **Flow validator** — a validator in the plugin's `initialize` rejects any downtime flow that lacks the human-approval node. Prevents a bad-faith override.
3. **Apply node idempotency** — `apply` node reads approval state from the inbox message; if approval is missing/rejected, it archives and returns. Even if the human node were bypassed somehow, this is a second stop.

## 11. Implementation roadmap (dt01–dt07)

1. **dt01** — Plugin skeleton, explorer persona, tick handler, capacity throttle, throttle-if-in-progress logic.
2. **dt02** — Flow template + `inbox_notification_sent` gate + validator for downtime flows.
3. **dt03** — Candidate sourcing: read downtime-backlog.md, `pm downtime add/list`, auto-discovery scaffolding.
4. **dt04** — Planner integration: planner writes `docs/downtime-backlog.md` during Magic + Synthesis.
5. **dt05** — Five exploration handlers (spec_feature, build_speculative, audit_docs, security_scan, try_alt_approach) + routing per §6.
6. **dt06** — Inbox notification + approve/reject → commit/archive logic.
7. **dt07** — Settings + CLI: `pm downtime add/list/pause/resume`, `[downtime]` config key with enabled/threshold/cadence/disabled_categories.
