---
name: systematic-debugging
description: Methodical bug isolation — reproduce, minimize, bisect, fix, regression-test. No guessing.
when_to_trigger:
  - debug
  - stuck on a bug
  - intermittent failure
  - flaky test
  - cannot reproduce
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Systematic Debugging

## When to use

Reach for this the moment you catch yourself guessing. "Maybe it's the cache" or "let me just try X" means you do not have a model of the failure yet. This skill replaces guess-and-check with a pipeline that converges on the cause every time.

## Process

1. **Reproduce deterministically.** Write a shell one-liner or test that fails every time. If the bug is flaky, run in a loop until you catch it, then capture the inputs. No deterministic reproducer = no progress.
2. **Minimize the input.** Start from the failing case and chop. Remove one thing at a time: fewer rows, shorter path, simpler config. Keep chopping while the bug reproduces. Stop when removing any one thing makes it go away — that is the minimal reproducer.
3. **Bisect.** If it used to work, `git bisect` between a good and bad commit. If it is environmental, bisect the environment: Python version, OS, config flag. Always binary-search; do not linear-scan.
4. **Form one hypothesis.** State it precisely: "The bug is caused by the cache returning stale data because key X is not invalidated on path Y." One hypothesis at a time.
5. **Run a targeted experiment that would disprove the hypothesis.** If the experiment does not distinguish truth from falsity, it is not a valid experiment. "Add a log and rerun" is rarely valid — what would the log tell you that you do not already know?
6. **Fix the cause, not the symptom.** If a null check papers over a state bug, you have hidden the real failure. The fix must explain the original failure mechanism.
7. **Write a regression test** that would have caught this bug. Run the full test suite. Commit the test and the fix together with a message naming the mechanism: `fix: cache invalidation on path Y leaves stale entry`.

## Example invocation

```
Bug: "Task 47 sometimes shows status=running when it's actually succeeded."

Step 1: Reproduce. Ran `pm task show 47` in a loop — 3/50 returns running.
  Captured stdout/stderr for those 3 runs.
Step 2: Minimize. All failures happen within 500ms of a completion event.
  Narrowed to: `pm task show` immediately after `pm task complete` shows stale.
Step 3: Bisect. git bisect between v0.9 (works) and v1.0rc1 (fails) —
  first bad: commit 7aa8786.
Step 4: Hypothesis. That commit introduced the mtime config cache;
  WorkService may be reading a config snapshot that pre-dates the completion.
Step 5: Experiment. Disabled the cache, reran minimized repro 1000x — 0 failures.
  Hypothesis confirmed.
Step 6: Fix. Cache invalidates on config-file mtime, but the WorkService write
  does not touch the config file — the cache staleness is unrelated to config.
  Real bug: WorkService.complete returns before the status write has fsynced.
  Added explicit flush before returning.
Step 7: Regression test in tests/test_work_service.py ::test_complete_is_visible_immediately.
```

## Outputs

- A deterministic reproducer (test or shell command).
- A named mechanism explaining the failure.
- A fix that addresses the cause.
- A regression test that would have caught it.

## Common failure modes

- Adding log lines without a hypothesis — you end up drowning in noise.
- Fixing the symptom (null check, retry loop) and moving on; the bug returns in a new shape.
- Declaring "flaky test" and marking it skip. Flakiness is a signal, not a status.
- Skipping the regression test; the bug comes back in six months.
