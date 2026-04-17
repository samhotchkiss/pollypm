---
name: finishing-a-development-branch
description: Cleanup pass before merge — rebase, squash, review commits, delete worktree, archive.
when_to_trigger:
  - merge ready
  - wrap up branch
  - cleanup before merge
  - finish branch
kind: magic_skill
attribution: https://github.com/skills-sh/skills
---

# Finishing a Development Branch

## When to use

Run this before opening the final PR or hitting merge. Branches accumulate noise — work-in-progress commits, merge commits from rebases, stale files, unused imports. A ten-minute cleanup makes the history readable for the next decade.

## Process

1. **Rebase onto the latest main.** `git fetch origin && git rebase origin/main`. Resolve conflicts in the rebase, not in a merge commit. Never merge main into a feature branch — merge commits muddy history.
2. **Inspect the commit log.** `git log --oneline origin/main..HEAD`. Every commit should be: (a) a discrete unit of work, (b) a message that explains why, (c) tests passing at that commit. If any of these fail, fix it now.
3. **Squash or reorder as needed.** `git rebase -i origin/main`. Squash "fix typo" commits into their parent. Reorder so related changes land together. Goal: a linear story a reviewer can follow commit-by-commit.
4. **Verify tests pass at each commit.** `git rebase --exec 'uv run pytest -q' origin/main` runs the test suite after every rewritten commit. Catches cases where squashing hid a broken state.
5. **Run the full check suite one more time.** Tests + linter + type-checker. Every CI job that will run on the PR, run locally now.
6. **Write or update the PR description.** See `requesting-code-review`. This is a good time — the commit log is clean and fresh in your mind.
7. **Force-push with lease.** `git push --force-with-lease origin feat-branch`. With-lease fails safely if someone else pushed to the branch (unlikely for feature branches but a habit to keep).
8. **After merge, delete the branch and worktree.** `git worktree remove .worktrees/feat-branch`, `git branch -D feat-branch`, `git fetch --prune`. Leaving branches around clutters `git branch -a`.

## Example invocation

```bash
# Before finishing
cd .worktrees/feat-magic-skills
git log --oneline origin/main..HEAD
# 787928a feat(magic): v1 skills — Architecture & Visualization (10 skills)
# 2c28d89 feat(magic): v1 skills — Documents (8 skills)
# 4932fc5 feat(magic): v1 skills — Code Quality & Review (10 skills)
# ...plus 3 "fix smoke test" commits

# 1. Rebase onto main
git fetch origin
git rebase origin/main

# 2. Interactive: squash fixups into their parents
git rebase -i origin/main
# Mark the three fix-smoke-test commits as 'fixup' into their category parents.

# 3. Verify each commit still green
git rebase --exec 'uv run pytest -q tests/test_magic_skills_starter_pack.py' origin/main

# 4. Final check suite
uv run pytest
uv run ruff check src/

# 5. Push
git push --force-with-lease origin feat-magic-skills

# After merge
cd ~/dev/pollypm
git worktree remove .worktrees/feat-magic-skills
git branch -D feat-magic-skills
git fetch --prune
```

## Outputs

- A branch rebased onto the latest main.
- A commit log where each commit tells a self-contained story.
- Tests green at every commit.
- Force-with-lease push instead of bare force.
- Cleaned worktree and local branch after merge.

## Common failure modes

- Merging main into the branch; history becomes a diamond of noise.
- Squashing across semantic boundaries; one mega-commit hides the shape of the work.
- Force-pushing without `--force-with-lease`; collaborator's work silently overwritten.
- Leaving old worktrees; `git worktree list` with 40 entries.
