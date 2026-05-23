# Fix Flow — How to Ship Fixes Found During Testing

When a test scenario fails, follow this protocol. The label tells the next agent to act, and the agent that writes a PR cannot be the agent that merges it.

Use `agent-personas.md` to decide whether the actor is operating as testing agent, coding subagent, or reviewing agent.

---

## The protocol (one sentence)

**Issues and PRs move between `needs-codex` and `needs-claude`; the tagged agent implements, then hands off to the other agent for review, approval, and merge. No self-merge.**

---

## Label protocol

Use exactly one ownership label on every active issue and PR:

| Label | Meaning on an issue | Meaning on a PR |
|---|---|---|
| `needs-codex` | Codex should pick up implementation or investigation next. | Codex should review, request changes, approve, or merge next. |
| `needs-claude` | Claude should pick up implementation or investigation next. | Claude should review, request changes, approve, or merge next. |

Rules:
- Issues and PRs must never have both labels at once.
- An open issue or PR with neither label is unowned and should be fixed immediately.
- The label means **next actor**, not "who wrote this."
- Codex-authored PRs get `needs-claude`.
- Claude-authored PRs get `needs-codex`.
- If the reviewer requests changes, they flip the PR back to the authoring agent's label.
- If the reviewer approves, the reviewer merges. The author does not merge their own PR.
- Label removal without merge must be paired with adding the other agent's label. A label simply disappearing on an open PR is a process bug.

Use exactly one creator label on every PR:

| Label | Meaning | Merge restriction |
|---|---|---|
| `codex-created` | Codex authored the PR's implementation. | Codex may not approve or merge. |
| `claude-created` | Claude authored the PR's implementation. | Claude may not approve or merge. |
| `mixed-agent-authors` | Both agents have authored implementation commits on the PR. | Neither agent may merge; split the PR or ask the operator to merge. |

Creator labels are durable. Review-turn labels change; creator labels do not. If the reviewer pushes code instead of only review comments, add `mixed-agent-authors` immediately and stop the normal merge flow.

Examples:

```bash
# Codex picks up an issue
gh issue edit <N> --remove-label needs-codex --add-label needs-claude
# Meaning: Codex is actively working; final PR will go to Claude.

# Codex opens implementation PR
gh pr create ... --label needs-claude --label codex-created

# Claude requests changes
gh pr edit <N> --remove-label needs-claude --add-label needs-codex
gh pr comment <N> --body "Changes requested: <summary>. Back to Codex."

# Codex fixes and hands back
gh pr edit <N> --remove-label needs-codex --add-label needs-claude
gh pr comment <N> --body "Addressed feedback in <sha>. Back to Claude."
```

---

## Model routing — when to use which model

The testing agent (you, the one driving this plan) runs on **Opus** for the higher-judgment work: deciding what's broken, deciding what scope is right, writing the PR description, and evaluating review feedback.

Sub-agents you spawn for mechanical work should use **Sonnet** when possible:
- Rebasing a branch against `main`.
- Updating stale docstrings / comments to match new behavior.
- Fixing a known mechanical lint failure.
- Running pytest and reporting the output.
- Re-applying a known fix pattern across multiple files.
- Pushing + tagging an already-correct branch.

Sub-agents should use **Opus** when the work involves:
- Touching task lifecycle invariants (§01).
- Modifying the heartbeat cascade (§01.5, §05).
- Changing storage boundaries (§02).
- Refactoring state-cache invalidation logic.
- Any fix where "what's the right scope" is genuinely uncertain.

When in doubt, default to Opus. The cost of a wrong fix on a critical path is much higher than the model-token savings of Sonnet.

To set model on a subagent dispatch, pass `model: "sonnet"` (or `"opus"`) in the Agent tool call.

---

## When to fix yourself vs. dispatch a sub-agent

**Fix yourself (the testing agent) when:**
- The fix is small (1–10 lines) and you have full context.
- It's a doc/comment update.
- It's a test-only change.
- You're already in the relevant file and a paragraph of context away from the fix.

**Dispatch a sub-agent when:**
- The fix is multi-file or substantive (lifecycle / cascade / boundaries).
- Running the test consumed significant context and you want to preserve it for downstream scenarios.
- Multiple independent fixes can run in parallel (one sub-agent per PR).

**Pause and ask the operator BEFORE dispatching when:**
- The fix touches Section 01 task lifecycle invariants.
- The fix touches the heartbeat cascade.
- The fix touches storage boundaries (sqlite/pg).
- You're choosing between meaningfully different approaches.
- You're not sure if the scope is "fix this PR" or "rewrite this subsystem."

The operator approves the approach; then you dispatch.

---

## The PR creation protocol

### Step 1: Identify the issue category

Tag the issue / PR title with the category from the five axes:
- `bug:` — functional incorrect.
- `flake:` — works some of the time.
- `perf:` — works but too slow (especially 1-second click rule).
- `ux:` — works but operator can't figure it out.
- `magic-gap:` — works but doesn't anticipate / save steps.

### Step 2: Create the branch

```bash
git checkout -b fix/<category>-<short-name>
# Examples:
# fix/bug-task-claim-race
# fix/perf-dashboard-cold-paint
# fix/ux-stuck-task-visibility
# fix/magic-gap-cookie-expiry-banner
```

### Step 3: Make the change

**RC stability rules** (always):
- No `--no-verify`, no `--no-gpg-sign`.
- No force-push without `--force-with-lease`.
- No force-push to main, ever.
- NEW commits — never amend a pushed commit.
- If a pre-commit hook fails, fix the issue and make a NEW commit.

**Architecture rules** (always):
- Follow `architecture-guardrails.md`.
- Preserve plugin boundaries and module ownership.
- Keep routes thin, UI code componentized, and storage access behind facades.
- Do not solve a missing contract with a cross-layer shortcut.

### Step 4: Test the change

Run the targeted test that catches the failure:
```bash
.venv/bin/python -m pytest <relevant test path> -x
```

If you don't have a test that catches the failure: **write one before fixing**. The test should fail against current main, pass against your fix. This is a regression net.

For `perf:` fixes, also capture before/after evidence:
- Same SHA base and fixture scale where possible.
- Same machine, browser, network path, and cache state.
- p50/p95/p99/max before and after.
- Trace or raw timing file path.
- Confirmation that the fix did not increase payload size, query count, memory, or polling cost elsewhere.

"Feels faster" is not a test result. A perf PR without numbers goes back for more work.

### Step 5: Commit-early discipline

If your change involves a long-running test (>30s), **commit and push BEFORE running it.** Subagent worktrees occasionally get reaped mid-test; uncommitted work is lost. Commit → push → run tests → push fixup if needed.

```bash
git add <specific files>  # never `git add -A`
git commit -m "$(cat <<'EOF'
fix(web-ui): <short imperative description>

<one-paragraph reasoning: why this matters / what the user saw>

<Refs or Closes line>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push -u origin fix/<category>-<short-name>
```

### Step 6: Open the PR

Choose the reviewer label from the author:
- Codex-authored PR → `needs-claude` + `codex-created`.
- Claude-authored PR → `needs-codex` + `claude-created`.

Include an identity block in the PR body so the first comment tells reviewers whether they may merge:

```bash
gh pr create --title "<category>(<area>): <short>" --body "$(cat <<'EOF'
## Agent Identity
- Authoring agent: <Codex|Claude>
- Authoring persona: <Codex Builder|Claude Builder>
- Creator label: <codex-created|claude-created>
- Reviewer/merge agent: <Claude|Codex>
- Reviewer persona: <Claude Reviewer|Codex Reviewer>
- Ownership label: <needs-claude|needs-codex>
- Self-merge prohibited: yes

## Symptom
<what the user sees>

## Fix
<one paragraph>

## Test plan
- [x] regression test added: <test path>
- [x] verified via <how>
- [ ] verified in browser (if UI change)

## Architecture
- [x] Preserves plugin/module boundaries.
- [x] Uses public contracts/facades instead of private internals.
- [x] Does not add feature-specific logic to unrelated core paths.

Closes #<N> (or Refs #<N> for partial)
EOF
)" --label <reviewer-label> --label <creator-label>
```

**Critical:** the reviewer label is what triggers the other agent. If you forget it, the PR sits unowned.

### Step 7: SHA-verify

```bash
gh pr view <N> --json headRefOid,labels,url
```

Confirm:
- `headRefOid` matches local `git rev-parse HEAD`.
- Exactly one of `needs-codex` / `needs-claude` is in `labels`, and it is the other agent.
- Exactly one of `codex-created` / `claude-created` is in `labels`, unless `mixed-agent-authors` is present.

---

## The review loop

The agent named by the PR label polls for work. `needs-codex` means Codex reviews next; `needs-claude` means Claude reviews next.

Before approving, the reviewer must check both behavior and architecture. A PR that passes tests but violates `architecture-guardrails.md` is not approvable.

Before merging, the reviewer must check creator labels:
- Codex may merge only PRs labeled `claude-created`.
- Claude may merge only PRs labeled `codex-created`.
- Neither agent may merge PRs labeled `mixed-agent-authors`.
- If the creator label is missing or contradicts the PR body, stop and fix labels before review continues.

**Possible outcomes:**

### A. Reviewer approves and merges

The reviewer agent approves and merges only if the creator label permits it. The ownership label is gone because the PR is `MERGED`. You're done.

### B. Reviewer requests changes

The reviewer found issues. Three places to look:

```bash
gh api repos/samhotchkiss/pollypm/issues/<N>/comments       # high-level blockers
gh api repos/samhotchkiss/pollypm/pulls/<N>/reviews         # review states
gh api repos/samhotchkiss/pollypm/pulls/<N>/comments        # inline file:line
```

**Open PRs must keep an ownership label.** If a label is removed and the PR is still open, immediately add the label for the next actor.

The reviewer flips the PR back to the authoring agent:
```bash
gh pr edit <N> --remove-label <reviewer-label> --add-label <author-label>
gh pr comment <N> --body "Changes requested: <short summary>. Back to <Codex|Claude>."
```

The author reads feedback, addresses it, pushes a NEW commit (do NOT amend), and flips back:

```bash
gh pr edit <N> --remove-label <author-label> --add-label <reviewer-label>
gh pr comment <N> --body "Addressed feedback in <sha>. Back to <Codex|Claude>."
```

### C. Label missing, PR still open

This is invalid state. Restore ownership based on who should act next:
```bash
gh pr edit <N> --add-label needs-codex   # if Codex should act next
# or
gh pr edit <N> --add-label needs-claude  # if Claude should act next
```

### D. PR sits with label for >15 min

Ping the tagged agent with a comment and verify the label is correct. Do not add the other label unless ownership is actually changing.

---

## Handling rebases

If `main` advances during your PR's review cycle and you get a merge conflict:

1. **Fetch**: `git fetch origin --prune`
2. **Rebase**: `git rebase origin/main` (or `git merge origin/main` if you prefer; merge commits are fine).
3. **Resolve conflicts** intentionally — look at both sides via `git diff --conflict=merge <file>`.
4. **Force-push with lease**: `git push --force-with-lease origin <branch>`.
5. **Never `--force` without lease.** That can silently overwrite a teammate's push.
6. **Hand back to reviewer**: `gh pr edit <N> --remove-label <author-label> --add-label <reviewer-label>`.
7. **Comment**: "Rebased onto main (resolved conflicts in X, Y, Z). New SHA: <sha>. Back to <reviewer>."

---

## Handling review rounds

In the 2026-05-23 wave, the deepest PR took 14 rounds. Most took 3–8. Each round narrows the scope.

**Patterns that emerge:**

- **Round 1**: usually an architectural / invariant concern. Fix substantively.
- **Round 2**: usually a doc/comment drift the runtime fix didn't address.
- **Round 3**: usually a stale test docstring or OpenAPI mismatch.
- **Round 4+**: increasingly narrow — module ownership, stale comments, naming.

**Keep PRs small to keep rounds shallow.** A 200-line PR took 3 rounds; a 1500-line PR took 14. If your fix is touching > 5 files, decompose first.

---

## Perf-fix discipline

Performance fixes are especially prone to local wins that create product regressions somewhere else. Before opening a `perf:` PR, answer:

- Which user action got faster?
- What was the p95/max before and after?
- Which scale did you measure: S, M, or L?
- Did payload size change?
- Did DB query count or scan breadth change?
- Did memory or file descriptors grow during a 5-minute idle check?
- Did the 1-second click rule still pass in Web and TUI surfaces touched by the change?

If the fix is a cache, also prove:
- Cache key includes every behavior-changing flag.
- Cache invalidates on the write path.
- Cached response cannot leak thinking/subagent/private data into default responses.
- The cold path is still acceptable.

---

## Dispatching sub-agents — the prompt template

When dispatching a sub-agent for a fix (Sonnet model unless flagged Opus):

```
You are fixing <reviewer-agent> review feedback on PR #<N> (branch `<branch>`).

**V1 RC stability mode.** No --no-verify, no force-push (or force-with-lease if rebasing). NEW commit — never amend.

**Cross-agent merge rule:** If you author commits on this PR, you do NOT merge it. Hand it to the other agent with the correct label.

**Architecture rule:** Preserve plugin/module boundaries from `architecture-guardrails.md`; do not add cross-layer shortcuts just to make the test pass.

**Creator-label rule:** If you are Codex, the PR you open must include `codex-created` and `needs-claude`. If you are Claude, the PR you open must include `claude-created` and `needs-codex`. Do not remove creator labels during review rounds.

**Commit-first discipline:** Edit → spot-check → commit → push BEFORE running any long test.

**Context-protection (CRITICAL):**
- Read ONLY files I list. Use grep for extra lookups.
- Do NOT bulk-read tests/.

**Review feedback (verbatim):**
> <paste the comment>

**Fix scope:**
<concrete instructions; what to change, where, what the new shape should be>

**Files to read (narrow):**
- <specific file:lines>
- ...

**Tests to add/update:**
- <specific test>

**Workflow:**
1. `git fetch origin && git checkout <branch> && git pull --ff-only origin <branch>`
2. Make edits.
3. **Commit immediately.** HEREDOC + Co-Authored-By.
4. Push.
5. Flip ownership back to reviewer: `gh pr edit <N> --remove-label <author-label> --add-label <reviewer-label>`.
6. Post acknowledgement comment.
7. SHA-verify.
8. Optionally run tests after push; push fixup if needed.

Report PR URL, new SHA, reviewer label confirmed, summary of changes, and architecture boundary touched.
```

The `Files to read` section is **critical** — it protects the sub-agent's context window and produces better work. Sub-agents that scan the whole codebase produce worse output.

---

## When to stop the fix loop

After 8+ rounds on the same PR with no convergence:
- Consider that the scope was wrong; close + restart with a smaller PR.
- Or escalate to operator: "this approach isn't converging, want to step back?"

After all the operator-approved fixes are in:
- Verify the original failing scenario from the test plan now passes.
- Update test journal.
- Move to next scenario.

---

## Quick reference — common commands

```bash
# See all PRs awaiting Codex
gh pr list --state open --label needs-codex

# See all PRs awaiting Claude
gh pr list --state open --label needs-claude

# Find open PRs missing both ownership labels
gh pr list --state open --json number,labels --jq '
  map(select(([.labels[].name] | index("needs-codex") | not) and ([.labels[].name] | index("needs-claude") | not))) | .[].number
'

# Latest review comment
gh api repos/samhotchkiss/pollypm/issues/<N>/comments --jq '.[-1].body'

# Hand from Codex author to Claude reviewer
gh pr edit <N> --remove-label needs-codex --add-label needs-claude

# Hand from Claude author to Codex reviewer
gh pr edit <N> --remove-label needs-claude --add-label needs-codex
```
