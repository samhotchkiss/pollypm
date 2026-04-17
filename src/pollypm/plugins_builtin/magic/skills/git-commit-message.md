---
name: git-commit-message
description: Write a clean commit — imperative subject, body explains why, footer for refs, Conventional Commits when the team uses them.
when_to_trigger:
  - commit message
  - ready to commit
  - write commit
  - conventional commit
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Git Commit Message

## When to use

Use every time you commit. A commit message is the one artifact future-you reads when bisecting a regression at 2am. The rules are small, the payoff is compounding.

## Process

1. **Subject line: imperative, under 72 characters, no trailing period.** `Fix cancellation cascade`, not `Fixed cancellation cascade` or `Cancellation fix.` Imagine the subject completing the sentence "If applied, this commit will...".
2. **Conventional Commits prefix when the repo uses them.** `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`, `perf:`, `build:`, `ci:`. Scope in parens: `feat(work): cascade cancellation`. Breaking changes: `feat!:` or `BREAKING CHANGE:` footer.
3. **Blank line, then body.** The body explains **why**. Do not repeat **what** — the diff shows that. "Parent cancellation was silently leaving children running. This caused orphan workers per task-ID 311. Fix cascades cancel to every descendant." 70-80 chars wrapped.
4. **Footer for references.** `Closes #311`, `Refs #247`, `Reviewed-by: Alice <a@e.com>`, `Co-Authored-By: ...`. GitHub uses `Closes` / `Fixes` / `Resolves` to auto-close issues on merge.
5. **One logical change per commit.** If your message needs the word "and" to list two changes, split into two commits. A commit that reverts cleanly is a commit that was scoped right.
6. **Write in the present tense.** "Add rate limiter" not "Added rate limiter". This is the standard because git uses commit subjects in lists ("This release includes: Add X, Fix Y") — past tense reads poorly there.
7. **No "WIP" or "fix later" in messages that will be merged.** If you commit WIP locally, fine — squash before the PR.
8. **Write the commit message before you hit save.** A commit message authored after the fact is a commit message that was not thought through.

## Example invocation

```
feat(work): cascade cancellation to descendants

Parent-task cancellation previously left descendants running. Under load,
this produced orphan workers that only exited on heartbeat timeout
(2 min), wasting capacity and emitting confusing status logs.

Cancel now recursively walks `children_of(task_id)` and cancels each
non-terminal child. Already-succeeded or failed children are preserved —
history is not rewritten.

Closes #311
Refs #309

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

```
fix(cli): correct --actor default when omitted

`--actor` fell through to `None` when not passed, which downstream
treated as "system", not "worker". This made worker-initiated tasks
appear to originate from Polly herself in the activity feed.

Default is now explicitly "worker".
```

## Outputs

- A subject line under 72 chars, imperative, optionally prefixed.
- A body explaining **why**, wrapped at 72-80 chars.
- A footer with issue refs and co-authors if applicable.
- One logical change per commit.

## Common failure modes

- Subject describes what, not why; body restates the subject.
- "Update" or "changes"; tells the reader nothing.
- Multiple logical changes in one commit; bisect points at a commit doing four things.
- Trailing period on subject; violates the convention and breaks some changelog tools.
