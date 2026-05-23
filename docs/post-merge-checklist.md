# Post-Merge Verification Checklist

**Audience:** Sam, the morning after a sprint-wave merge. Pre-coffee compatible.

**Purpose:** Step-by-step script for confirming that the PRs you just merged are
actually live in your running PollyPM, and that the system isn't quietly
running last week's code while showing this week's release notes.

Run this top-to-bottom after a multi-PR merge night. Each section either
*passes* (move on) or *fails* (jump to the troubleshooting tree in
[§10](#10-troubleshooting-tree)). Don't skip steps even if "obviously fine" —
the worst regressions hide in the steps that look obvious.

---

## 1. Pre-flight — count what you're about to merge

Open the latest overnight briefing on the Desktop:

```
~/Desktop/pollypm-overnight-YYYY-MM-DD.md
```

(For tonight's wave: `~/Desktop/pollypm-overnight-2026-05-21.md`.)

Find the **PRs opened tonight** table. Count rows. That's your wave size.

Decide right now whether you're merging *all* of them or holding any back.
Mark each row with one of:

- `MERGE` — merge as part of this wave
- `HOLD` — defer to a later wave
- `CLOSE` — close without merging (rare)

If anything is `HOLD`, note WHY in the briefing doc — future you will ask.

---

## 2. Merge order — CRITICAL

Most PRs in a wave are flat (no stacking) and can merge in any order. But
**stacked PRs must merge base-first**, or GitHub auto-closes the dependents
with merge conflicts.

### How to detect stacks

In the GitHub PR list, check each PR's **base branch**. If the base is not
`main`, that PR is stacked on the named branch — merge the parent first.

### Tonight's known stack (2026-05-21 wave)

```
Move A state-cache:
  #2026 (base: main)
    └─ #2029 (base: #2026)
```

**Merge #2026 first, then #2029.** All other PRs in tonight's wave are flat
(base: main) and can merge in any order.

### When in doubt

Reproduce the merge-order block from
`~/Desktop/pollypm-rc-decisions.md` ("⚠️ MERGE ORDER MATTERS"). That
document is updated per sprint with the canonical merge order for the
current wave, including:

- Stacked PR chains (must merge base-first)
- Migration version collisions (two PRs reserving the same `pg`
  migration version — pick a winner, rebase the loser to the next version)
- Re-targeted PRs (an agent rebased onto a sibling branch after the
  initial open — the new base must merge first)

If the rc-decisions file disagrees with this doc, **trust the rc-decisions
file** — it's regenerated per sprint, this checklist is generic.

---

## 3. Reinstall the CLI

```bash
cd /Users/sam/dev/pollypm
git fetch origin
git checkout origin/main
uv tool install --force --reinstall .
pm --version    # should report 1.0.0rc3.dev0
```

If `pm --version` reports the **old** version after a successful reinstall,
you've been bitten by the `build/` shadow trap (PR #2011 / issue #1983):
`uv`'s wheel cache + a leftover `build/` directory can quietly serve stale
sources. Recovery:

```bash
cd /Users/sam/dev/pollypm
rm -rf build/ dist/
uv tool install --force --reinstall .
pm --version    # try again
```

If that *still* reports the old version, jump to [§10](#10-troubleshooting-tree).

> **Why the explicit `git checkout origin/main`?** Reinstalling from a
> feature branch ships *that branch's* code, not main. Always reinstall from
> the merged-main tree.

---

## 4. Restart daemons cleanly

```bash
pkill -TERM -f "pollypm.rail_daemon" "pm heartbeat" "pm cockpit"
sleep 5
pm up
```

Verify all three came back up:

```bash
ps auxww | grep -E "(pollypm.rail_daemon|pm heartbeat|pm cockpit)" | grep -v grep
```

You should see **exactly three** rows, one per daemon. PIDs should be
fresh (i.e. younger than the timestamp of `pm up`).

If a daemon is missing, run `pm up` once more (idempotent), then re-check.
Still missing → [§10](#10-troubleshooting-tree).

> **Why `pkill -TERM` + sleep?** A graceful TERM gives daemons time to
> flush their state and release their sqlite/pg connections. A 5-second
> sleep is long enough for clean exit, short enough that you don't lose
> your morning. If a daemon hangs past 5s, `pm up` will not bring up a
> duplicate — it will detect the half-dead pid and refuse — so a second
> `pkill -KILL` may be needed.

---

## 5. Doctor check — must report 0 errors

```bash
pm doctor
```

Expected: **0 errors, 0 warnings.** A perfectly clean wave clears every
check. Tonight's wave specifically resolves:

| Check                       | Expected behaviour after this wave            | Owning PR |
|-----------------------------|-----------------------------------------------|-----------|
| `doubled-pollypm-path`      | No NEW files at `~/.pollypm/.pollypm/` after restart. Legacy artifacts (from before #2011's typed-path helpers shipped) can be cleaned with `pm doctor --fix` (moves to `.pollypm.bak-YYYYMMDD-HHMMSS/`, never deletes). | #2030 |
| `heartbeat-offline`         | Must NOT fire. Pre-#1987 this was a false-positive triggered by the supervisor reading sqlite while pg held the fresh heartbeat. | #1987 |
| `pg-sequence-alignment`     | Must report all pg-owned serial sequences aligned. If it fires after a restore/import, `pm doctor --fix` advances lagging sequences without lowering healthy ones. | #2087 |
| `project-guide-drift`       | Bulk-fixable via `pm doctor --fix`. The action now refreshes drifted guides in one shot instead of per-project. | #2028 |
| `agent-worktree-count`      | The prune handler now actually reaps stale agent worktrees. Pre-#1975 the check fired but the fix was a no-op. | #1975 |

> **Note on output format:** Post-#2038, `pm doctor` clusters human
> output when a check has 3+ sub-alerts (one summary row with sample
> subjects and a `(+N more)` tail instead of N near-identical Why/Fix
> blocks). That's intentional, not truncation — pass `--verbose` for
> the full per-row detail, or `--alert-type <name>` to drill into one
> check. `--json` output is unchanged (always full per-row payload).

### If `doubled-pollypm-path` fires

```bash
pm doctor --fix    # moves ~/.pollypm/.pollypm/* to .pollypm.bak-...
```

Re-run `pm doctor`. The check should now clear. The `.bak` directory is
safe to delete after a few days if no regression appears.

### If `heartbeat-offline` fires

Don't manually patch — that's the regression we're verifying didn't ship.
The recovery cascade should detect + self-heal stale heartbeat readings
automatically. If the alert is sticky, see [§6](#6-cascade-verification).

---

## 6. Cascade verification

Sample the per-project audit log for recent autonomous cascade actions:

```bash
tail -50 ~/dev/pollypm/.pollypm/audit.jsonl \
  | grep -E "task.queued|task.cancelled|task.status_changed"
```

You should see **recent rows** (within the last hour) showing tasks
queued, cancelled, or status-changed by `cli` or `architect` actors —
not by `worker` or `user`. That's the recovery cascade actually running:
the architect autonomously cancelling stale drafts, queuing handoffs, etc.

If you see **zero autonomous cascade activity**, the Lever 2 auth-marker
contract (PRs #2017/2018/2019) may not be wiring through. Symptoms:

- Architect briefs arriving but agent labels them "fake injection"
- Cascade findings escalating to tier-3/tier-4 with no auto-resolution
- `pm doctor` clean but `~/.pollypm/audit/<project>.jsonl` shows
  promotions forever, no resolutions

When in doubt, consult [`docs/recovery-cascade.md`](recovery-cascade.md)
(PR #2025) — section 7 has a 5-branch troubleshooting tree mapping
symptoms to the responsible files.

> **Reminder:** audit logs are per-project at `~/.pollypm/audit/<key>.jsonl`,
> *not* a single `audit.jsonl`. The `~/dev/<project>/.pollypm/audit.jsonl`
> referenced above is the **project-local** log, written by the work
> service for the project you're standing in.

---

## 7. Visual verification — cockpit UI

Open the cockpit (`pm cockpit` if not already up) and walk through these
four checks. Each takes under 10 seconds.

### 7a. Footer status bar — unified format

The footer should render a single line in the format:

```
12 projects · 38 agents · 23 inbox
```

(Numbers will differ; format must match.) Pre-#2014/#2027 the footer
showed multiple uncoordinated counters. If the format is wrong, the
`render_footer_status` wiring (#2027) didn't land.

### 7b. Glyph cheatsheet — full vocabulary

Press `?` in the cockpit. The help overlay should show the **full** glyph
vocabulary: `◆` `•` `○` `▲` `▶` `◇` `◉` `⚠` `✕` `✎` `♥` `♡` `◜◝◞◟`
(PR #1993 / #2010 extended `_RAIL_GLYPH_HELP` to include all of them).

If only a partial set shows, the help-overlay merge didn't ship.

### 7c. Heartbeat-offline footer — compact after 60min

If the system has been idle long enough to have a stale heartbeat
(> 60 minutes), the footer hint should read:

```
⚠ Heartbeat offline · open Settings
```

**not** the pre-#1991 verbose form `⚠ Heartbeat offline (1568m) — open
Settings to repair recovery`. Under 60 minutes the minute count is fine
(recovery is still actionable). Over 60 minutes, the compact form means
the dedup with the event-ticker (#1992 / D9) is also working.

### 7d. Dashboard theme — unified palette

Open the dashboard view. Yellow/red/green/blue state colors should all
match the rail colors exactly (canonical `State.*` hexes via
`cockpit_theme`). Pre-#2022 the dashboard CSS drifted; the inbox
plan-review row was using `#ff6b5b` instead of the rail's `#ff5f6d`.

If the surface tints look off (action-bar fills, scrollbar slate),
that's the deliberately-deferred surface-tint follow-up (tonight's
PR may or may not have shipped it — check #2022's notes).

---

## 8. State cache active

Post-merge of #2029, `POLLYPM_STATE_CACHE` defaults to **ON**.

```bash
echo "${POLLYPM_STATE_CACHE:-1}"    # should be 1 or empty (defaults to 1)
```

Cockpit cold-mount should feel snappier than yesterday. The cache routes
the 5 hottest rail call sites through a snapshot instead of re-querying
on every refresh tick. Subjective check: navigate rail → project → back
to rail. The second visit should be visibly faster than the first.

If you want to confirm the kill-switch still works:

```bash
POLLYPM_STATE_CACHE=0 pm cockpit
```

Should boot fine, just slower. Set back to default (unset or `=1`) for
normal use.

---

## 9. Architect chat re-test — rail-nav preserves conversation

This is the canary test for PR #1995 (the rail-nav conversation-wipe fix).
Pre-#1995, navigating away from an architect rail item and back wiped
your live Claude session.

**Test:**

1. In the cockpit, mount an architect rail item.
2. Type something in the architect chat pane (don't send — just buffer
   the input).
3. Navigate to a different rail item via arrow keys.
4. Navigate back to the architect item.

**Expected:** your buffered input is still there. The Claude session is
the same process you mounted in step 1 (pid unchanged via
`ps auxww | grep claude`). The conversation history is intact.

**If wiped:** PR #1995 either didn't merge or didn't reinstall cleanly.
The reversed policy in `_park_mounted_session` and
`safe_break_pane_to_storage` is the load-bearing change — re-check that
those files in the installed wheel match the post-#1995 source.

---

## 10. Troubleshooting tree

If a step above fails, jump here.

### Step 3 (reinstall) — `pm --version` shows old version

1. `rm -rf build/ dist/` and re-run `uv tool install --force --reinstall .`
2. Still wrong? Check the active `pm` shim: `which pm` should point to
   `~/.local/bin/pm` or similar. If it points to a stale shim in another
   location, your `PATH` has drifted.
3. Last resort: `uv tool uninstall pollypm && uv tool install .`

### Step 4 (daemons) — a daemon won't start

1. Check for stale pid files: `ls ~/.pollypm/*.pid` — delete any whose
   pid no longer maps to a real process.
2. Check for port conflicts (cockpit binds a unix socket): `ls -la
   ~/.pollypm/cockpit.sock` — delete if stale.
3. Try `pm cockpit --foreground` directly to see the actual error.

### Step 5 (doctor) — a check that should be clean fires

Map the failing check to its owning PR (table in §5). If the PR is in
this wave's merged list, verify the install actually picked up the
change: grep the installed wheel for a string from the PR's diff.

### Step 6 (cascade) — no autonomous activity in audit log

1. Read [`docs/recovery-cascade.md`](recovery-cascade.md) sections 5 (auth
   marker contract) and 7 (troubleshooting tree).
2. Check that sessions in `pollypm.toml` have an `auth_token` field. If
   missing, the migration in PR #2017 didn't run (or didn't persist).
   Trigger lazily by saving any session config change.
3. Check the central audit tail: `tail -200 ~/.pollypm/audit/*.jsonl |
   grep -E "tier4|escalation|resolution"`. Promotions without
   resolutions → cascade is firing but not closing the loop.

### Step 7 (UI) — visual checks fail

Each visual check maps to a single PR. If a check fails:

1. Confirm the PR is in this wave's merged list.
2. Confirm `pm --version` reports the post-merge version (i.e. install
   actually took effect).
3. Restart the cockpit specifically: `pkill -TERM -f "pm cockpit" &&
   sleep 2 && pm cockpit`. A long-lived cockpit can serve stale CSS
   from its in-process cache.

### Step 9 (rail-nav) — conversation wipes on rail navigation

This means PR #1995 either didn't merge, didn't install, or there's a
regression. **Stop using rail navigation until resolved** — every
nav-away will keep wiping live conversations.

1. Confirm #1995 merged: `git log origin/main --oneline | grep 1995`.
2. Confirm install: grep the installed wheel's
   `cockpit_storage_park.py` for the post-fix kill-orphan branch.
3. If both check out and the bug persists, file a regression issue
   immediately and revert to the pre-rail-nav workflow (open via
   `pm cockpit` direct attach).

---

## Quick-reference one-liner

For a sanity sweep when you don't have time for the full checklist:

```bash
cd /Users/sam/dev/pollypm && git fetch origin && git checkout origin/main \
  && uv tool install --force --reinstall . \
  && pm --version \
  && pkill -TERM -f "pollypm.rail_daemon" "pm heartbeat" "pm cockpit" \
  && sleep 5 && pm up \
  && pm doctor
```

If all of that exits clean, you've covered steps 3–5. Still walk through
steps 6–9 manually — they're the regressions you can't grep for.

---

## When to update this doc

- After a wave introduces a new mandatory verification step (e.g. a new
  doctor check, a new daemon, a new UI surface that can regress).
- After a wave removes or renames a referenced command/PR.
- After a real morning verification finds a gap (e.g. "the doc said X
  would catch this, but X didn't fire").

Keep the wave-specific examples (PR numbers, dates, doubled-path
artifact paths) as concrete as possible — generic checklists rot
fastest.
