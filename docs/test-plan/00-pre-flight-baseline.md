# 00 — Pre-flight Baseline

**Goal:** prove the existing automated coverage is green on current `main` before doing any qualitative testing. If this is red, fix it before doing anything else in this plan.

**Time:** 30 minutes.

**Prereqs:** clean checkout of `samhotchkiss/pollypm`, Python venv installed (`./.venv/bin/python` exists), `uv` available, `gh` CLI authed.

**What you're doing:** running every existing automated check exactly once on main and capturing the result. This is the baseline against which everything else is measured.

---

## 0.1 Sync main

```bash
cd /Users/sam/dev/pollypm
git fetch origin --prune
git checkout main
git pull --ff-only origin main
git rev-parse HEAD
```

Record the SHA you're testing in your test journal. Every issue you file from now on references this SHA.

---

## 0.2 pytest — full suite

```bash
.venv/bin/python -m pytest -q --ignore=tests/playwright --tb=short -p no:cacheprovider 2>&1 | tee /tmp/pytest-baseline.txt
```

Allow ~20 minutes. If `.venv/bin/python` doesn't exist, use `uv run --extra test pytest ...` instead.

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

---

## 0.4 `pm doctor`

```bash
pm doctor
```

**Expected:** zero alerts. If clustering output appears (3+ alerts share `alert_type`), the cluster is fine to skip during baseline.

**Pass criterion:** exit code 0, no `[FAIL]` lines.

---

## 0.5 `pm sessions health`

```bash
pm sessions health
```

**Expected:** lists active sessions; no `stuck` or `stale` heartbeats from before this session started.

Stale heartbeats from sessions you don't care about can be ignored — but note them. If a session you DO care about (e.g. `pm-operator`) shows stale, that's a problem before you start.

---

## 0.6 Performance environment baseline

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

## 0.7 Baseline result

Write down in your test journal:

```
Baseline date: 2026-MM-DD HH:MM
Tested SHA: <git rev-parse HEAD>
pytest:        <X passed / Y failed> — failures: <list or "only known">
playwright:    <X passed / Y failed> — failures: <list or "all pass">
pm doctor:     clean / <N alerts>
pm sessions:   clean / <N stale>
perf env:      <machine / browser / scale summary>
```

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
