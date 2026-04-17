---
name: impeccable-code
description: Production-grade patterns — error handling, observability, testability, simplicity — applied to every new module.
when_to_trigger:
  - production-ready
  - harden this
  - impeccable
  - production quality
  - ship quality
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Impeccable Code

## When to use

Apply when writing a module that will live in production, especially one other engineers will build on. The bar is not "works" — the bar is "another engineer can extend it two months from now without asking me what anything means." This skill is the checklist.

## Process

1. **Typed signatures everywhere.** Every public function has explicit parameter and return types. No `Any`, no `Optional` without a rationale. Types are documentation that the compiler enforces.
2. **Structured errors.** Raise specific exception classes (`TaskNotFound`, not `ValueError`). Catch only what you can actually handle; re-raise everything else with context. No bare `except:`.
3. **Observability from the start.** Log entry and exit of significant operations with `logger.debug`, errors with `logger.error` and full exception info, business events with structured fields (`logger.info('task.created', task_id=..., flow=...)`).
4. **Pure where possible, effectful where necessary.** Isolate side effects (filesystem, network, clock, random) behind thin wrappers that can be swapped in tests. Pass dependencies in rather than reaching into globals.
5. **Small surface area.** Public API is a dozen names, not a hundred. Private helpers get a leading underscore and stay in the module. A module with 40 public symbols is broken — split it.
6. **Tests as specification.** Test the behavior, not the implementation. Assertions should read like requirements: "when the parent is cancelled, all children transition to cancelled within 100ms."
7. **Document the why, not the what.** Module docstring: what this module is for, who uses it, what it does not do. Function docstring: purpose, non-obvious invariants, return contract. Do not paraphrase parameter names.
8. **Refuse clever in-code.** If a reviewer has to read a line three times, it is wrong — even if it is correct. Correctness per unit of eyeball-time is the metric.

## Example invocation

```python
# src/pollypm/work/service.py

"""WorkService — sealed task lifecycle layer.

Public contract: `create`, `list`, `get`, `update`, `cancel`, `archive`.
Consumers must go through `WorkService`; direct access to the storage
backend is a violation.
"""

import logging
from typing import Iterable

logger = logging.getLogger(__name__)


class TaskNotFound(LookupError):
    """Raised when a task_id is not present in the store."""


class WorkService:
    def __init__(self, store: WorkStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def cancel(self, task_id: str) -> None:
        """Cancel task and cascade to descendants.

        Idempotent: cancelling an already-cancelled task is a no-op.
        Raises TaskNotFound if the task does not exist.
        """
        task = self._store.get(task_id)
        if task is None:
            raise TaskNotFound(task_id)
        if task.status == 'cancelled':
            return
        self._store.update(task_id, status='cancelled', cancelled_at=self._clock.now())
        logger.info('task.cancelled', task_id=task_id, by='cascade' if task.parent_id else 'user')
        for child_id in self._store.children_of(task_id):
            self.cancel(child_id)
```

## Outputs

- Typed, structured, observable code.
- Errors that name what went wrong.
- A module docstring that states purpose and boundaries.
- Tests that read like a behavioral specification.

## Common failure modes

- Broad exception handling (`except Exception`) that swallows real bugs.
- Reaching into globals (singletons, environment variables) from deep inside logic — untestable.
- 200-line public API because "it might be useful"; guarantees the module becomes a fragile weight.
- Clever one-liners; correctness per eyeball-minute drops to zero.
