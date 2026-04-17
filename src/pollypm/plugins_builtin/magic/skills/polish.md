---
name: polish
description: Refine code before merge — naming, comments, edge cases, dead code, assertion messages.
when_to_trigger:
  - polish
  - cleanup
  - before merge
  - refine
  - tidy up
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Polish

## When to use

Run this after the code works and before review. It is the last pass over a branch where you upgrade signal-to-noise: better names, fewer words, sharper error messages, zero dead code. Skip polish only for genuine prototypes — anything landing in main gets polished.

## Process

1. Read every new identifier out loud. If you stumble, rename. `tmp_result`, `do_thing`, `handler2` are all rename candidates. Good names describe **what the thing is** or **what the function returns**, not how it is implemented.
2. Delete comments that restate code: `# increment i` next to `i += 1`. Keep comments that explain **why**, which the code cannot say.
3. Check every error message. "Invalid input" tells the user nothing. "Expected a non-empty list of task IDs, got []" does. Errors are part of your API.
4. Grep for `# TODO`, `FIXME`, `XXX`, `HACK` you added. Each one is a decision: resolve it, file a follow-up issue with the comment's context, or delete it. No orphan TODOs.
5. Remove dead code — commented-out blocks, unreachable branches, unused imports, unused arguments. If the version control history is your backup, trust it; do not carry dead code.
6. Check edge cases. For each function: empty input, one element, maximum size, unicode, None. Add a test or inline assert for the ones that matter.
7. Move magic numbers to named constants. `if retries > 3` becomes `if retries > MAX_RETRIES`. Single-use is fine; multi-use is not.
8. Read the diff one more time, as a reviewer. Anywhere you would comment "why?", add a comment or restructure.

## Example invocation

```python
# Before polish
def do_thing(x, data):
    # Loop through data
    tmp = []
    for i in range(len(data)):
        if data[i] > 0:
            tmp.append(data[i] * x)
    # TODO: handle empty case
    return tmp

# After polish
SCALE_THRESHOLD = 0

def scale_positive(factor: float, values: list[float]) -> list[float]:
    """Multiply each positive value by `factor`; drop zero and negatives."""
    return [v * factor for v in values if v > SCALE_THRESHOLD]
```

## Outputs

- Every identifier reads clearly out loud.
- No restating comments; comments explain why.
- Error messages include the observed and expected values.
- Zero orphan TODOs; all resolved, filed, or deleted.
- Dead code removed; magic numbers named.

## Common failure modes

- Renaming for "consistency" without asking whether the new name is actually clearer.
- Deleting a comment that looks redundant but actually explained a non-obvious why.
- Leaving TODOs with no issue reference; they accrete like plaque.
- Adding edge-case handling without tests; the handler drifts and no one notices.
