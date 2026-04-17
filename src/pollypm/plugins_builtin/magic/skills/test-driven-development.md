---
name: test-driven-development
description: Write the failing test first, watch it fail, then implement — no code before a test sees red.
when_to_trigger:
  - TDD
  - write tests first
  - red-green-refactor
  - test driven
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Test-Driven Development

## When to use

Use whenever you are adding new behavior, fixing a bug with a reproducer, or touching code whose correctness matters. TDD is the default mode for PollyPM engineering work. Skip it only for throwaway exploration or UI tweaks where observation is the verification.

## Process

1. Name the behavior in one sentence. "When a task is cancelled, its children transition to cancelled." If the sentence is fuzzy, the test will be fuzzy.
2. Write the test for that behavior. Pick the smallest meaningful shape. One scenario per test — do not cram three assertions about three behaviors into one `def test_everything`.
3. Run the test. **It must fail, visibly, for the right reason.** If it fails because of an import error or a syntax bug, that is not red — fix the test until failure is a real assertion failure.
4. Commit the failing test. Yes, commit red. This makes the test-first step a real checkpoint and gives you a clean diff when the implementation lands.
5. Write the minimum code to make the test pass. Do not add features the test does not demand. "Minimum" is a discipline, not a guideline.
6. Run all tests. Green. If you broke a pre-existing test, pause — you just changed observable behavior elsewhere.
7. Refactor. Now and only now. Extract helpers, rename for clarity, remove duplication. Tests still green at every step.
8. Commit the implementation. Two commits total for the feature: `test: reproduce behavior X` then `feat: implement behavior X`.

## Example invocation

```python
# Step 1-3: write and run failing test
# tests/test_cancellation.py
def test_cancelling_parent_cancels_children():
    svc = WorkService.in_memory()
    parent = svc.create(title='parent')
    child = svc.create(title='child', parent_id=parent.id)
    svc.cancel(parent.id)
    assert svc.get(child.id).status == 'cancelled'

# $ pytest tests/test_cancellation.py::test_cancelling_parent_cancels_children
# FAILED — child.status == 'pending', expected 'cancelled'
# (This is the correct shape of red.)

# Step 4: commit the red test
# $ git commit -m "test: parent cancellation cascades to children"

# Step 5: implement minimum
# src/pollypm/work/service.py
def cancel(self, task_id: str) -> None:
    self._set_status(task_id, 'cancelled')
    for child in self.children_of(task_id):
        self.cancel(child.id)

# $ pytest  -> all green
# Step 8: commit the impl
# $ git commit -m "feat: cascade cancellation to children"
```

## Outputs

- Two commits: failing test, then passing implementation.
- No dead tests (every test drove a line of implementation).
- Test names describe behavior, not method names.

## Common failure modes

- Writing the test after the implementation "for completeness." That is not TDD; that is test-last. The discipline is what catches design mistakes.
- Treating an import error as red; it is not an assertion failure.
- Writing three scenarios in one test. When it fails, you do not know which scenario broke.
- Skipping the refactor step; the design debt compounds.
