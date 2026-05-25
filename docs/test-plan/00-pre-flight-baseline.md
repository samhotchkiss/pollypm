# 00 — Pre-flight Baseline

**Goal:** prove the existing automated coverage is green on current `main` before doing any qualitative testing. If this is red, fix it before doing anything else in this plan.

**Time:** 30 minutes.

**Prereqs:** clean checkout of `samhotchkiss/pollypm`, Python venv installed (`./.venv/bin/python` exists), `uv` available, `gh` CLI authed.

**What you're doing:** running every existing automated check exactly once on main and capturing the result. This is the baseline against which everything else is measured.

---

## 0.0 Environment safety + clean working state

**Before running any baseline checks, confirm you are NOT pointing at production.** Several later sections are destructive (§05 kills daemons, drops PG, fills disk). Running them against the operator's live workload destroys real state.

### Test-env marker

The marker file `~/.pollypm/.test-env-marker` opts the local PollyPM instance into destructive testing. Without it, this plan refuses to proceed.

**To enable test mode on a fresh test instance:**
```bash
mkdir -p ~/.pollypm
touch ~/.pollypm/.test-env-marker
```

**To verify before each run:**
```bash
test -f ~/.pollypm/.test-env-marker && echo "OK: test env marker present" || \
  { echo "REFUSE: ~/.pollypm/.test-env-marker missing — see §0.0"; exit 1; }
```

If you're on the operator's daily-driver machine and don't want destructive scenarios touching it: do NOT create the marker. Run §00–§04 only (none are destructive). Skip §05 and the destructive parts of §06 entirely. Document this in your journal as a partial run.

### Working tree + worktrees

```bash
# Confirm git working tree is clean (or, if not, that the dirty files are this plan itself).
git -C /Users/sam/dev/pollypm status -s

# Confirm no leftover agent worktrees from prior runs.
git -C /Users/sam/dev/pollypm worktree list
# Worktrees under .claude/worktrees/ are agent scratch space. Reap any that are stale via:
# git worktree remove <path> --force
```

If any of these surface issues, **stop** and resolve before proceeding. A dirty worktree means the baseline SHA you record isn't actually what's running.

## 0.1 Sync main

```bash
cd /Users/sam/dev/pollypm
git fetch origin --prune
git checkout main
git pull --ff-only origin main
git rev-parse HEAD
```

Record the SHA you're testing in your test journal. Every issue you file from now on references this SHA.

### Clean local build artifacts before reinstall evidence

If this run uses a globally installed `pm` from the checkout, remove local build
artifacts before reinstalling. `uv tool install --force --no-cache .` can still
consume a stale `build/lib/pollypm/` snapshot left by an earlier wheel build; a
fresh `src/pollypm/release_check.py` with `GLOBAL_CONFIG_DIR` imported is not
evidence that the installed tool copied that file.

```bash
cd /Users/sam/dev/pollypm
rm -rf build/ dist/
uv tool install --force --reinstall .
python - <<'PY'
import inspect
import pollypm.release_check as rc
print(inspect.getsourcefile(rc))
print("GLOBAL_CONFIG_DIR" in inspect.getsource(rc))
PY
pm doctor
```

If the installed module does not come from the just-reinstalled wheel or the
source check prints `False`, record the run as blocked on install freshness; do
not treat `pm doctor` output as valid baseline evidence.

---

## 0.2 pytest — full suite

```bash
.venv/bin/python -m pytest -q --ignore=tests/playwright --tb=short -p no:cacheprovider 2>&1 | tee /tmp/pytest-baseline.txt
```

Allow ~20 minutes. If `.venv/bin/python` doesn't exist, use `uv run --extra test pytest ...` instead.

**Timeout rule:** if pytest does not produce final summary within 30 minutes, force-cancel (Ctrl-C) and treat the baseline as red. A hung pytest run is itself a release-blocker. File `bug:pytest-hang` with the partial output, then either fix or document the hang before proceeding.

**Expected:** all green, or only failures that match the known-pre-existing list below.

**Known pre-existing flakes (acceptable):**
- `tests/test_pg_activity_feed_projector.py::*` — `unsupported filter ['subject']` from PG store, unrelated to recent work.
- `tests/test_pg_events_retention.py::*` — same root cause.
- `tests/test_parse_tail_perf_beats_full_parse_on_10k_archive` — wall-clock perf flake on shared-CPU runs.
- `tests/test_recovery_prompt.py::TestLiveGitStateFromSessionCwd::test_falls_back_to_project_root_without_session_cwd` — `FileExistsError` race on `tmp_path/repo/mkdir`.

If you see failures **outside** that list, **stop**. Investigate. Either fix on main directly via PR (preferred — see `fix-flow.md`) or document and proceed with caveat.

**Pass criterion:** every novel failure is either fixed or explicitly accepted with a filed issue.

---

## 0.3 Playwright — full suite

```bash
cd /Users/sam/dev/pollypm/tests/playwright
npm install
npx playwright install chromium
```

Then start `pm serve` in a separate tmux pane (must be running before Playwright):

```bash
# In a different tmux window
pm serve
# Should print: [pm serve] bound 100.x.y.z:8765 (tailscale mode; UI at http://100.x.y.z:8765/ui/)
```

Then run:

```bash
cd /Users/sam/dev/pollypm/tests/playwright
POLLYPM_BASE_URL=http://$(tailscale ip -4):8765 npx playwright test --workers=1 2>&1 | tee /tmp/playwright-baseline.txt
```

**Expected:** all 54 tests pass (per `#2066` round-3 fix).

**Acceptable degradation:** if 1–2 mobile-chrome tests fail with stub-related noise, capture the failures and file as a flake issue, then continue. **Do not** continue if chromium-desktop is red.

**Timeout rule:** if Playwright doesn't produce a final summary within 20 minutes (it's normally <5 min), force-cancel and treat as red.

---

## 0.4 `pm doctor`

```bash
pm doctor
```

**Expected:** zero alerts. If clustering output appears (3+ alerts share `alert_type`), the cluster is fine to skip during baseline.

**Pass criterion:** exit code 0, no `[FAIL]` lines.

---

## 0.5 `pm sessions --health`

```bash
pm sessions --health
```

**Expected:** lists active sessions; no `stuck` or `stale` heartbeats from before this session started.

Stale heartbeats from sessions you don't care about can be ignored — but note them. If a session you DO care about (e.g. `pm-operator`) shows stale, that's a problem before you start.

---

## 0.6 Provider + model version capture (for §04 eval trace)

Agent behavior depends on which model version each session is configured to use. Capture this at baseline so §04 evals can be replayed against the same configuration and so a future model upgrade is a visible variable, not a silent one.

```bash
# Find the model setting per session role:
grep -A2 'model' ~/.pollypm/pollypm.toml | head -40

# Or, programmatically (preferred — depends on pollypm.toml shape):
.venv/bin/python -c "
from pollypm.config import load_config
c = load_config()
for s in c.sessions:
    print(s.name, '->', getattr(s, 'model', 'default'))
" 2>/dev/null
```

Record:
- Operator session model.
- Each architect's model.
- Each advisor's model.
- Each worker template's model.

If any of these change during the run (e.g., operator regens config), §04 results from the prior model version are no longer valid. Note the change and re-baseline.

If you cannot tell which model a session uses, file `bug:model-version-opaque` — being unable to identify the model is itself a release blocker for an agent product.

**Subscription / account inventory:**
PollyPM is configured with at least one Claude subscription (the primary) and should also have a backup. Capture both so §5.5.3 failover testing can verify the transition.

```bash
# Inspect the configured Claude accounts (path depends on pollypm.toml shape):
grep -E 'claude|anthropic|account|api_key' ~/.pollypm/pollypm.toml | head -20

# Or via programmatic inspection (preferred — find the canonical accessor):
.venv/bin/python -c "
from pollypm.config import load_config
c = load_config()
# Look for the accounts/subscriptions structure on the config
print('claude accounts:', getattr(c, 'claude_accounts', None) or getattr(c, 'accounts', None))
" 2>/dev/null
```

Record:
- Primary Claude subscription identifier (account name or token tail, not the full token).
- Backup Claude subscription identifier.
- Which is currently active.
- The failover trigger (manual? limit-based? error-based?).

If no backup is configured, file `magic-gap:no-failover-sub` against the next sprint — graceful failover is in scope for ship-readiness per the operator's day-in-the-life.

## 0.7 Performance environment baseline

Before running qualitative sections, capture the environment that all later performance numbers depend on:

```bash
date
git rev-parse HEAD
uname -a
sysctl -n machdep.cpu.brand_string 2>/dev/null || true
sysctl -n hw.memsize 2>/dev/null || true
pm --version 2>/dev/null || true
tailscale ip -4 2>/dev/null || true
pgrep -f 'pm serve' | head -1 | xargs -I{} ps -o pid,rss,%cpu,command -p {}
psql -d pollypm -c "SELECT count(*) FROM pg_stat_activity WHERE datname='pollypm';"
```

Record:
- Browser + version used for Web UI checks.
- Phone model + browser for mobile checks.
- Network path: loopback, tailnet desktop, tailnet phone.
- Current fixture scale: number of configured sessions, tasks, and largest `events.jsonl`.
- Whether caches are cold or warm.

This becomes the header for every §06 result. A perf number without environment + scale is not release evidence.

---

## 0.8 Baseline result

Write down in your test journal:

```
Baseline date: 2026-MM-DD HH:MM
Tested SHA: <git rev-parse HEAD>
Test env marker: present / absent
pytest:        <X passed / Y failed> — failures: <list or "only known">
playwright:    <X passed / Y failed> — failures: <list or "all pass">
pm doctor:     clean / <N alerts>
pm sessions:   clean / <N stale>
models:        operator=<model>, architect=<model>, advisor=<model>, worker=<model>
perf env:      <machine / browser / scale summary>
```

Copy this header into the top of your journal entry (per `journal-template.md`). Every later section references this baseline.

This is the reference point. Every later section's failures must be evaluated **on top of** this baseline. If pytest had 3 failures here and 4 in §01, the new one is the one you investigate.

---

## Promotion to automation

Already automated. The point of §00 is "do the existing automation pass *right now*."

If something in pytest is red **and** that thing is in scope of this plan's later sections, fix it before continuing. Don't accumulate UI tests on top of a broken backend.

---

## Out of scope

- Performance budgets — that's §06.
- UI behavior — that's §03.
- Anything that requires manual judgment — those sections are 01 onwards.

---

## When you're done

Move to §01 (task lifecycle) next. It's the highest-leverage section: if tasks don't flow correctly, nothing else in the system matters.
