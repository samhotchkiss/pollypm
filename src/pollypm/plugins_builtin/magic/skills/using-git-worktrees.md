---
name: using-git-worktrees
description: Parallel development across branches via git worktrees — independent working directories, shared object store, safe cleanup.
when_to_trigger:
  - git worktree
  - parallel branches
  - stash switching
  - multiple branches
kind: magic_skill
attribution: https://git-scm.com/docs/git-worktree
---

# Using Git Worktrees

## When to use

Use when you have two or more branches you need to work on in parallel — a long-running feature plus an urgent fix, or multiple agents working concurrently on the same repo. Worktrees beat `git stash` + `git checkout` for anything longer than a 60-second interruption. PollyPM uses worktrees as the primary parallelism unit for its agent pool.

## Process

1. **One repo, many working directories.** A worktree is a linked checkout — a separate directory, its own working tree and index, sharing the same `.git/` object store. Disk is cheap; context is expensive.
2. **Create worktrees under a dedicated sibling directory.** `.worktrees/` or `../polly-worktrees/`. Do not nest worktrees inside the main checkout — tools that walk the filesystem get confused by `.git` files inside `.git`.
3. **Name worktrees after their branch.** `git worktree add ../polly-worktrees/feat-magic feat-magic`. The branch comes with the worktree; they are one-to-one.
4. **List and prune regularly.** `git worktree list` shows all; `git worktree prune` removes stale entries where the directory was deleted without `git worktree remove`.
5. **Never share `node_modules` or `.venv` across worktrees.** Each worktree gets its own because branches have different lockfiles. Let each worktree install its own; the object store deduplication is only for git data.
6. **Branch naming discipline.** Worktrees expose every branch name in the filesystem — `feat-X`, `fix-Y`, `experiment-Z`. Long or cryptic names become unreadable directories.
7. **Remove cleanly.** `git worktree remove <path>` deletes the worktree directory and unregisters it. `git branch -D <branch>` separately if the branch should also go.
8. **Detached HEAD for disposable work.** `git worktree add ../polly-worktrees/scratch --detach HEAD` for a quick exploration without creating a branch you will have to clean up later.

## Example invocation

```bash
# Main checkout at /Users/sam/dev/pollypm
cd /Users/sam/dev/pollypm

# Create a worktree for a feature branch
git worktree add .worktrees/feat-magic-skills -b feat-magic-skills origin/main

# Work in it
cd .worktrees/feat-magic-skills
# ... code, commit, push

# From anywhere, list
git worktree list
# /Users/sam/dev/pollypm                                       c59480f [main]
# /Users/sam/dev/pollypm/.worktrees/feat-magic-skills          787928a [feat-magic-skills]

# When done (PR merged)
cd /Users/sam/dev/pollypm
git worktree remove .worktrees/feat-magic-skills
git branch -D feat-magic-skills      # optional, only if not kept
git worktree prune                   # clean up stale refs
```

```bash
# Rescue pattern: worktree directory accidentally deleted
rm -rf .worktrees/feat-magic-skills    # (simulating the accident)
git worktree list                      # still shows it (stale)
git worktree prune                     # cleans up the stale entry
```

## Outputs

- Worktrees under a sibling or nested-sibling directory.
- `git worktree list` clearly showing every active checkout.
- Cleaned up via `git worktree remove`, not `rm -rf`.
- Independent `node_modules` / `.venv` per worktree.

## Common failure modes

- Nesting worktrees inside the main checkout; tooling and Git alike get confused.
- Deleting the directory without `git worktree remove`; refs go stale.
- Sharing `node_modules` across worktrees via symlink; version skew destroys builds.
- Hoarding worktrees; `git worktree list` with 40 entries means you are not cleaning up.
