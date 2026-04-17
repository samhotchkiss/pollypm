---
name: regression-suite-curation
description: Add a regression test for every fixed bug; keep the suite fast, meaningful, and pruned.
when_to_trigger:
  - fixed a bug
  - prevent regression
  - test suite maintenance
  - prune tests
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Regression Suite Curation

## When to use

Use immediately after you fix a bug and recurringly to keep the suite healthy. Test suites rot two ways: bugs escape because they had no test, and existing tests accrete into slow, duplicative masses. Both kill team velocity; this skill fights both.

## Process

1. **After every bug fix**: write a test that would have failed against the broken code. Name it after the bug: `test_cancel_cascade_leaves_siblings_alone_issue_311`. The name is the docstring — when it breaks three years later, someone will read the issue link.
2. Place the regression test next to existing tests for the module, not in a separate `regression/` folder. Regressions are not a different kind of test; they are tests we should have had.
3. Assert at the bug's level: if the bug was "status was stale," assert status directly. Do not assert a downstream symptom; that test fails for ten reasons.
4. **Quarterly**: profile the test suite. `pytest --durations=20` on unit, `--slowest=20` on e2e. Anything over 1s in unit, over 30s in e2e, needs investigation.
5. Split slow tests by seam: fixture heavy? Scope the fixture higher. External-call heavy? Mock. Setup-heavy? Extract a factory. Slow tests are fixable; they rarely require deletion.
6. Delete tests that test **implementation details** rather than behavior. If refactoring that does not change behavior breaks the test, the test was coupled to the wrong thing. Delete after confirming the behavior is covered elsewhere.
7. Detect flakiness with `pytest --count=20` in CI on a randomized subset. Any test that fails on 1/20 is flaky; fix or quarantine same week.
8. Keep a `tests/README.md` that describes the suite's layout and the conventions: where to put new tests, what goes in `integration/` vs `e2e/`, how to run a subset.

## Example invocation

```python
# Step 1: bug fix lands for #311 (cancellation cascade)
# Step 2: regression test

# tests/test_work_service.py

def test_cancel_cascade_does_not_touch_completed_children_issue_311():
    """When a parent is cancelled, already-completed children stay completed.

    Original bug: all descendants were unconditionally flipped to 'cancelled',
    losing history of successful sub-tasks. Fix: only cancel non-terminal
    children. See issue #311.
    """
    svc = WorkService.in_memory()
    parent = svc.create(title='parent')
    done = svc.create(title='done', parent_id=parent.id)
    pending = svc.create(title='pending', parent_id=parent.id)
    svc.complete(done.id)

    svc.cancel(parent.id)

    assert svc.get(parent.id).status == 'cancelled'
    assert svc.get(done.id).status == 'succeeded'   # unchanged
    assert svc.get(pending.id).status == 'cancelled'
```

## Outputs

- A named, issue-linked test accompanying every bug fix.
- Quarterly profiler report highlighting slow tests.
- Any test over the threshold either sped up or justified.
- A tests/README.md describing the suite structure.

## Common failure modes

- "We'll add the regression test later." Later never comes.
- Asserting on downstream symptoms; the test fails for ten reasons and drifts.
- Deleting coupled-to-implementation tests without verifying behavior coverage elsewhere; gaps open silently.
- Ignoring flakiness; the suite loses trust and gets bypassed.
