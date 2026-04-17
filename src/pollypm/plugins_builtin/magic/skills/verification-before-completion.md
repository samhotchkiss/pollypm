---
name: verification-before-completion
description: Final validation pass before marking a task done — tests, acceptance criteria, smoke check.
when_to_trigger:
  - almost done
  - ready to ship
  - before pr
  - before merge
  - mark complete
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Verification Before Completion

## When to use

Run this before marking any task complete, before opening a PR for review, and before pushing to main. The cost is five minutes; the cost of skipping it is a bug report at 10pm. This is the last-mile check that separates "I think it works" from "I verified it works."

## Process

1. Re-read the task description or issue. Does the change actually satisfy it, or did you solve a related problem and hope that counted? Compare against the acceptance criteria explicitly.
2. Run the full relevant test suite — not just the test you wrote, the full module. `uv run pytest tests/test_work_service.py` for your module, then `uv run pytest` for everything the change touches.
3. Run the linter and type checker. `ruff check`, `mypy`, whatever the project uses. Do not silence warnings; fix or explicitly suppress with a comment explaining why.
4. Run the change end-to-end in the closest environment you have. For CLI changes, run the CLI. For library changes, exercise the caller. For content changes, load the content.
5. Run a grep pass: any `TODO`, `FIXME`, `XXX`, or `print(` that you added and forgot to remove. Any hardcoded paths. Any commented-out code.
6. Check the diff one more time with fresh eyes. `git diff main...HEAD`. Look for: unintended file changes, debug statements, oversized deletions, stale comments.
7. Update the task/issue with what shipped and what explicitly did not. If acceptance had 5 criteria and you nailed 4, say so — do not claim completion for what you did not deliver.

## Example invocation

```
Task: "Implement cancellation cascade (#311)."
Acceptance: (1) parent cancel cascades to children, (2) already-done children are not touched, (3) cancellation event is emitted.

Verification:
1. Re-read: yes — all 3 criteria apply.
2. uv run pytest tests/test_work_service.py  -> 34 passed
   uv run pytest  -> 412 passed, 3 skipped, 0 failed
3. ruff check src/  -> clean
4. Manual: pm task create parent; pm task create --parent <id> child; pm task cancel parent. Checked pm task list -> child cancelled.
5. grep "TODO\|print(" src/pollypm/work/service.py  -> nothing new
6. git diff main...HEAD  -> only service.py + test file changed, no stray edits
7. Updated #311 comment: "Shipped (1), (2), (3). Parent requeue case is out of scope — filed #312."
```

## Outputs

- All tests green, linter clean, types clean.
- Explicit pass/fail per acceptance criterion.
- A clean diff (no debug artifacts, no unintended changes).
- An issue comment recording exactly what shipped and what did not.

## Common failure modes

- Running only the test you wrote; a regression you caused elsewhere slips through.
- Suppressing linter warnings to get clean output; the warning was a real signal.
- Claiming done when 4/5 acceptance criteria pass; the missing one will become a surprise bug.
- Skipping the end-to-end check; unit tests passed but the CLI command is broken.
