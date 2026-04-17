---
name: extract-module
description: Refactor inlined logic into a reusable module or function, with a clean boundary and tests.
when_to_trigger:
  - extract function
  - refactor to module
  - pull out helper
  - deduplicate
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Extract Module

## When to use

Use when you see the same logic in two or more places, or when a function has grown past a screen of code and the pieces want to live on their own. Resist extracting too early — one duplication is coincidence, two might be shape, three is a pattern. Wait for the third.

## Process

1. Identify the exact duplication or overgrown region. Copy both instances (or the full function) into a scratch buffer so you can compare them side by side.
2. Diff the instances. What is the same? What is the parameterized variation? The variation becomes your function's arguments; the sameness becomes the body.
3. Name the new thing before you write it. The name is the contract. `validate_task_payload` beats `check` every time. If you cannot name it in 1-4 words, the abstraction is not tight enough — keep thinking.
4. Decide where it lives. Inside the same module if used by siblings only. In a shared `utils` submodule if used across modules. Never in a top-level `utils.py` — that becomes a graveyard. Prefer `work.validation` over `utils.validate`.
5. Write the extraction with the minimum surface area. One function, one doc string, typed signature. No optional arguments for "future flexibility" — YAGNI.
6. Replace every call site. Run tests after each replacement, not all at the end. If a test breaks on the third replacement and you have already done two more, you do not know which replacement caused it.
7. Remove the inlined versions completely. An orphan copy is the worst outcome — future edits will update one and miss the other.
8. Add a test for the extracted function directly. Call-site tests are indirect coverage; the extracted function deserves its own.

## Example invocation

```python
# Before — duplicated in three handlers
# src/pollypm/work/handlers.py
def create_task(payload):
    if not payload.get('title'):
        raise ValueError('title required')
    if len(payload['title']) > 200:
        raise ValueError('title too long')
    ...

def update_task(task_id, payload):
    if not payload.get('title'):
        raise ValueError('title required')
    if len(payload['title']) > 200:
        raise ValueError('title too long')
    ...

# After
# src/pollypm/work/validation.py
MAX_TITLE_LENGTH = 200

def validate_title(payload: dict) -> None:
    """Raise ValueError if the payload's title is missing or too long."""
    title = payload.get('title')
    if not title:
        raise ValueError('title required')
    if len(title) > MAX_TITLE_LENGTH:
        raise ValueError(f'title too long ({len(title)} > {MAX_TITLE_LENGTH})')

# src/pollypm/work/handlers.py
from pollypm.work.validation import validate_title

def create_task(payload):
    validate_title(payload)
    ...

def update_task(task_id, payload):
    validate_title(payload)
    ...

# tests/test_validation.py: direct tests for validate_title edge cases.
```

## Outputs

- A new function or module with a typed signature and docstring.
- All call sites updated, tests green after each replacement.
- Direct tests for the extracted function.
- Zero inlined duplicates remaining.

## Common failure modes

- Extracting after one duplication; premature abstraction traps you in a weird shape.
- Leaving the old inlined version "just in case" — future edits diverge.
- Putting the extraction in a top-level `utils.py`; becomes a dumping ground.
- Adding optional arguments for imagined future cases; makes the signature noisy.
