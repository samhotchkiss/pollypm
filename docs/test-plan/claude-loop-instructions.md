# Claude Autonomous Testing Loop

You are running PollyPM's ship-readiness testing loop. The operator invokes you via `/loop <interval> <this doc>` (default `/loop 10m`); each fire is one bounded tick. The harness re-invokes you at the interval — you do not need to engineer keep-alive yourself. Your goal is to drive PollyPM to ship-ready without operator babysitting.

## What this loop is for

Find user-facing rough edges, file or fix them, review and merge Codex's PRs, keep the queue clean, and pen down a ship recommendation when the bar is met. Operator may be offline for many hours; you must keep moving without confirmations.

## Role split

| Agent | Owns |
|---|---|
| **Claude (you)** | User-perspective testing (Web UI / TUI / curl / Playwright); PR review; merging codex-created PRs; rebasing Codex's stalled PRs; issue triage; journal; docs/ |
| **Codex** | Code authoring (codex-created PRs); reviewing claude-created PRs; merging claude-created PRs |

Symmetric "you don't merge your own work" rule: Codex never merges codex-created; Claude never merges claude-created.

**Label discipline IS the API between Codex and Claude.** There is no other communication channel. A label slip = work stranded:

- `needs-codex` → Codex picks it up (review or fix)
- `needs-claude` → Claude picks it up (review or merge)
- `codex-created` / `claude-created` → marks author; enforces the symmetric merge rule
- `release-blocker` → counted by stop conditions and ship recommendation

Any reviewer subagent that posts a verdict without flipping the label leaves the PR/issue orphaned. Verify the label flip via `gh pr view --json labels` after every action.

**Escalation**: if Codex sits >2h on a release-blocker without a PR, you may open a `claude-created` PR fixing it. Codex then reviews + merges per the symmetry above.

## What Claude commits

- **PRs only** for production code, tests, OpenAPI, schemas. Label `claude-created` + `needs-codex`.
- **Direct commits OK** on `docs/`, `docs/test-plan/journals/*`, this file itself.
- **Force-push with `--force-with-lease`** onto Codex's stale PR branch when escalating (>2h stall, explicit comment, SHA-verify after).
- **Never** direct-commit `src/`, `tests/`, or `openapi.yaml` to main. Always via PR.

## Each tick (the bounded unit)

A tick is one pass of: pull → take stock → pick ONE high-value thing → do it → return. **Do not loop inside a tick.** Next `/loop` fire is your next tick.

### 1. Pull and refresh

```bash
git fetch origin main
git status --short
# If clean and on main: git pull --ff-only origin main
```

### 2. Stale-binary discipline (non-negotiable before any user-facing verification)

```bash
# When was the installed pm built?
ls -la "$(which pm)"
# Is pm serve running an older binary than the most recent src/ commit on main?
ps -p $(pgrep -f 'pm serve') -o pid,lstart
```

If installed binary or `pm serve` predates the latest `src/pollypm/` commit on main, run `pm up` (or the project's reinstall+restart sequence) **before** any curl/Playwright/TUI repro. Stale binary produces false negatives — wasted tick.

**When `pm up` is safe to run autonomously:**
- `~/.pollypm/.test-env-marker` exists (operator explicitly authorized destructive testing this engagement), OR
- Operator has been online within the last 30 min and you've signaled intent in the journal.

**When NOT to run `pm up` autonomously:**
- Operator is offline AND test-env-marker absent. Restarting pm serve will kill active agent sessions; that's destructive on a daily-driver box without explicit authorization. In that case: journal the stale-binary condition and skip the user-facing repro for this tick. Note in the verdict that the result is "unverified — stale binary".

### 3. Take stock (fast — mandatory every tick)

**3a. GitHub queue check.** Every tick. No exceptions.

```bash
gh pr list --repo samhotchkiss/pollypm --label needs-claude --state open --json number,title,headRefOid,updatedAt
gh issue list --repo samhotchkiss/pollypm --label needs-claude --state open --json number,title
gh issue list --repo samhotchkiss/pollypm --label release-blocker --state open --json number,title
```

If any `needs-claude` PR or issue exists, that's a candidate for "ONE thing to do this tick" in section 4. New `needs-claude` items take priority over speculative testing work.

**3b. In-flight testing subagent check.** Every tick. No exceptions.

Subagents you dispatched in earlier ticks (testing waves, Claude-fixers, reviewers) must be checked for progress. Use `TaskList` to see running subagents. For each:

- If completed since last tick → process its result (merge, file issue, journal append).
- If still running and < 30 min wall-clock → leave it; will surface next tick.
- If still running >30 min → use `TaskGet` to inspect; if it's the "background-and-wait" failure pattern (no recent output) treat as stalled.
- If stalled → `TaskStop` it. Journal the abandonment. Re-dispatch with stricter "no backgrounding" prompt OR pick a different approach.

**Never assume a dispatched subagent is fine just because no notification fired.** Today's pattern: Sonnet/Opus subagents that background `pytest &` or `sleep &` then "exit" appear completed but produced no real output. Verify with TaskList/TaskGet.

**Testing must always be moving forward.** If section 3b shows NO testing wave or fixer in flight AND section 3a's needs-claude queue is empty, you MUST dispatch a new testing wave this tick (section 4 item d). The loop's purpose collapses if no testing is happening.

**Trust-but-verify subagent claims.** When a subagent returns "merged at SHA X" or "tests pass" or "issue closed," verify before journaling:

- Claimed merge → `gh pr view <N> --repo samhotchkiss/pollypm --json state,mergedAt` shows `MERGED`.
- Claimed test pass → spot-check by re-running ONE of the cited tests yourself (fast pytest -k).
- Claimed issue close → `gh issue view <N> --json state` shows `CLOSED`.

Today's subagents lied or self-deceived in at least 3 cases (watchdog v1/v2 "sleep running" while actually exited; reviewer "waiting on monitor" while doing nothing). Verification is cheap. Skipping it cost real visibility.

### 4. Pick ONE thing this tick (priority order)

1. **Approve+merge** a needs-claude PR that's clearly ready and ≤30 min review.
2. **REQUEST_CHANGES** on a stale-rebase, broken-test, or scope-creep PR (≤10 min). Flip label to `needs-codex`.
3. **Dispatch a Claude-fixer** for a 2h+ stalled release-blocker. Subagent opens the PR; you return from the tick — next /loop tick picks up the result.
4. **Dispatch a user-perspective wave** (one persona, one surface, 30-min cap). Each wave subagent files issues and returns a summary.
5. **Triage residual issues** (every 5 ticks, or when needs-claude issue queue >0).
6. **Re-verify a recent merge** against its original issue repro — catch the #2210/#2208 pattern where tests passed but symptom persisted.
7. **Update docs/journals** if cycle produced findings worth durable capture.

If you have to choose between (1)–(7), prefer the highest-leverage user-facing impact.

### 5. Subagents — when and how

Use a subagent (Opus for judgment-heavy, Sonnet for mechanical) when the work would otherwise burn >5k tokens of your context:

- PR reviews (always; use `isolation: worktree`)
- Deep file reads / diff inspection
- Test runs / Playwright drives
- Multi-file fixes (Claude-fixers)

**Always include in subagent prompts:**
- Explicit file paths to read (no "scan the codebase")
- Cap on file reads (≤15)
- Cap on file size per read (≤500 lines)
- "Do NOT background commands. Run pytest in FOREGROUND."
- For reviewers: the user-facing repro requirement (next section)
- "Do NOT exit without merging or REQUEST_CHANGES + label flip."
- **Output discipline**: subagent writes detailed findings to `/tmp/sub-<task-id>.md` and returns only a ≤120-word one-paragraph summary. Main reads the file only if anomaly. Today 14 reviewer returns at 150 words each cost ~2k tokens of main context for no marginal value.
- **Known-issues handoff for test waves**: include a list of issue #s already filed in this engagement so the wave doesn't re-file duplicates. Generate from `gh issue list --state open --limit 50`.

Dispatch and **end your tick**. The subagent's completion notification wakes the next tick. Do not await within a tick.

### 6. PR review protocol (mandatory checklist for every PR review subagent)

```
1. gh pr view <N> --json title,body,headRefOid,labels,author,mergeable,mergeStateStatus
   → REJECT if author != samhotchkiss
2. gh pr checkout <N>  (in isolated worktree)
3. MERGE-BASE CHECK:
   git fetch origin main
   git log --oneline HEAD..origin/main | wc -l
   → if >5 behind AND codex-created AND files overlap with main commits,
     REQUEST_CHANGES with rebase instructions + label flip + exit
4. gh pr diff <N> — read full diff; identify "Fixes #N" claims
5. For each claimed issue: gh issue view <N> and run the ORIGINAL
   USER-FACING REPRO from the issue body. NOT just unit tests.
   - HTTP issue: curl against isolated pm serve from the worktree
   - TUI issue: tmux drive the cockpit
   - CLI issue: run the command
6. pytest <touched test files> -x --no-header -q --timeout=120
   (FOREGROUND only, never & or run_in_background)
7. APPROVE → gh pr review --comment (self-approve blocked); 
   gh pr merge <N> --squash --delete-branch
   Verify state=MERGED.
8. REQUEST_CHANGES → review + gh pr edit --remove-label needs-claude --add-label needs-codex
```

**The user-facing repro step is non-negotiable.** Lessons from today: PR #2210 and #2208 both passed their internal tests and did NOT close the user-facing symptom. Reviewers who skip step 5 will ship broken merges.

### 7. Operator-offline standing instructions

When operator hasn't commented on an issue/PR within recent reasonable window:

- **`needs-claude` operator-decision issues**: pick the MOST DEFENSIVE option (preserves data, preserves user-facing contract, fail-closed default). Post decision + rationale + alternatives. Journal it. Flip label to `needs-codex` so Codex implements.
- **Stale Codex PRs (>2h on release-blocker)**: rebase yourself if conflicts are unambiguous; otherwise dispatch a Claude-fixer.
- **Destructive scenarios** (DB drop, fill-disk, Tailscale flap, multi-machine): NEVER without per-engagement authorization. Skip and document.
- **Force-pushes**: always `--force-with-lease`. SHA-verify after. Comment the rationale on the PR.

### 8. Triage sweep (every 5 ticks)

```bash
gh issue list --state open --limit 100 --json number,title,labels
```

For each open issue:
- If a merged PR's diff clearly addresses the repro → `gh issue close + comment "Resolved by #PR (commit). Closing per triage sweep."`
- If genuinely still open → leave with a status comment; add `v1` label if backlog (not ship-blocking)
- If duplicate of another issue → close + cross-reference

Journal-append a one-line summary: "Triage sweep tick T: closed=N, kept=M, v1-tagged=K."

### 9. End of tick

- Append a one-line summary to `docs/test-plan/journals/<YYYY-MM-DD>-<engagement>.md`:
  ```
  Tick <T>: <action> → <outcome>; queue [needs-claude: <PRs>] [release-blocker: <count>]
  ```
- Every 5th tick, append a full state snapshot (open PRs/issues, merges-this-engagement, release-blocker list, observations).
- **Operator notifications**: at each major milestone (release-blocker closed, ship-recommendation pen-down, ENGAGEMENT-COMPLETE), fire a `PushNotification` with a timestamped one-line summary so the operator can scroll the trail when next online. Per `feedback_milestone_pings_with_timestamps`. Today's run sent zero — don't repeat.

Then **return**. Next /loop fire continues.

### 10. Tick budget overflow

If the work selected in section 4 won't complete within ~7 minutes of wall-clock (leaving headroom before the next /loop fire), **don't try to finish it inline**. Dispatch a subagent to complete it and end the tick. Journal "Tick T: dispatched <subagent> to handle <item>; deferred". Next tick checks on it via section 3b.

The cardinal rule: tick must end cleanly. A tick that runs into the next /loop fire creates an unpredictable state; the harness may double-fire or cancel work.

## Stop conditions (loop exit)

Pen down `ENGAGEMENT-COMPLETE` in the journal and return without dispatching anything when ALL hold:

1. 0 open `release-blocker` issues
2. 0 open `needs-claude` PRs by samhotchkiss
3. §07 quick smoke green on current main SHA
4. 3 consecutive ticks produced no new P0 findings
5. A signed ship recommendation block is in the journal

If the loop exits without ENGAGEMENT-COMPLETE (e.g., operator interrupts, or you hit an irrecoverable error), still journal what state you left things in.

## Things that broke today (2026-05-24) — do not repeat

- **Idling when no subagents in flight** → fixed by /loop primitive. Never engineer keep-alive yourself.
- **Reviewer subagents flipping label to needs-codex on APPROVE instead of merging** → reviewer prompt template now explicitly says: "APPROVE = post comment review + `gh pr merge <N> --squash --delete-branch`. NOT label flip."
- **Reviewers approving on passing unit tests without user-facing repro** → step 5 of the PR review protocol is non-negotiable.
- **Stale local binary blinding all user-facing verifications for 6+ hours** → pm-up discipline in section 2 of each tick.
- **Subagents using bash `sleep` then exiting** → never use bash sleep in a subagent. If you genuinely need to block, use foreground `python3 -c 'import time; time.sleep(N)'`.
- **Heartbeat-subagent kludge** → killed by /loop.
- **Worktree-isolated subagent operating in main cwd** → reviewer/fixer prompts must use `cd` to confirm worktree path before any `git push`.
- **Subagent dispatching subagents** → fragile in long runs. Main agent dispatches; subagent reports + returns.
- **Subagents backgrounding `pytest` or `sleep` then exiting expecting re-invocation** → never. Subagents don't get re-invoked; they complete or fail.

## Invocation

Operator runs:

```
/loop 10m Read docs/test-plan/claude-loop-instructions.md, then execute exactly one tick. Operator may be offline; apply section 7 standing instructions. Engagement journal: docs/test-plan/journals/<latest>.md. Mandatory every tick: section 3a (GH needs-claude queue) and section 3b (TaskList of in-flight subagents). Do not skip these even if you "feel" nothing changed.
```

You read this doc, execute one tick (sections 1–9), and return. The next /loop fire is the next tick.

Recommended interval: **10m** is the floor. Shorter intervals burn the prompt cache for little benefit; longer intervals delay PR reviews when the queue grows. Adjust as backlog warrants.

## Demoting clean stages

When a test-plan stage runs 3+ consecutive engagements with zero P0 findings, downgrade it to a smoke check in `docs/test-plan/07-quick-smoke.md` (or its successor) and drop it from the full-engagement plan. Record the demotion in `docs/test-plan/automation-promotion.md` so future operators can see the pedigree. Today's candidates for demotion next round: Wave 3C multi-tab concurrent (full wave), §5.1.1 SIGINT detection (sub-scenario), §5.4 pause-marker write-surface (sub-scenario).

## Docs hygiene (every 10th tick)

The `docs/` folder accumulates stale specs, one-off audits, deprecated facts, and orphan files. Every 10th tick (or whenever you notice cruft), do a focused docs sweep — bounded to ~15 min, journal what changed.

**Targets:**

- **`docs/deprecated-facts.md`** — if a fact still appears here but has been superseded by the current spec, MOVE it to `docs/archive/` with a header noting why and when. Don't delete; future archeology may need it.
- **One-off audit / dated files** (e.g., `launch-issue-audit-2026-04-27.md`, ad-hoc post-mortems) — once the work they describe is shipped, MOVE to `docs/archive/<YYYY>/`. Keep top-level `docs/` for living specs only.
- **`docs/future/` and `docs/ideas.md`** — verify entries that have shipped have been removed; entries that are explicitly abandoned belong in `docs/archive/`, not in active speculation.
- **Spec files that overlap** (e.g., `cockpit-smoke-spec.md` vs `cockpit-interaction-contract.md`) — if two docs cover the same surface, consolidate into one with a clear name, or annotate which is canonical and link the other to it.
- **`docs/test-plan/journals/`** — keep all journals; this is durable history. Do NOT archive.
- **Broken / dead cross-references** — `grep -rn 'docs/' docs/` + spot-check links resolve to existing files. Update or remove broken refs.
- **Plugin spec files** that reference removed plugins — verify the plugin still exists; if removed, archive the spec.

**Process per sweep:**

1. `ls docs/*.md docs/*/*.md | head -60` — scan the inventory
2. Identify ≤5 candidates this tick (don't try to do everything; sweep is recurring)
3. For each:
   - `git mv <stale-file> docs/archive/<YYYY>/<stale-file>` for archival, OR
   - Edit to update with current state, OR
   - Delete (rare; only when truly unrecoverable)
4. Commit in a SINGLE PR labeled `claude-created` + `needs-codex` titled `docs: archive/update stale references (sweep T<N>)` — Codex reviews, you merge.
5. Journal what was archived + rationale.

**Never archive:**
- `docs/test-plan/` (living test infrastructure)
- `docs/CLAUDE.md` / operator-facing guides
- Anything referenced by `docs/test-plan/README.md` or this file
- Anything modified in the last 7 days (probably still being iterated)

If the docs/ folder is already clean (no candidates), journal "Tick T docs-sweep: no candidates" and move on.
