#!/usr/bin/env bash
set -euo pipefail

# Poll GitHub for open issues/PRs labelled "needs-codex" and hand them to Codex.
# Intended overnight runner:
#   tmux new -s pollypm-pr-watch 'caffeinate -dimsu scripts/codex_pr_watch_needs_review.sh'
#
# Completion contract:
# - issues: implement/investigate, open a codex-created PR, hand it to Claude
# - PRs: review code and comments, merge clean Claude-created PRs or request changes
# - remove the needs-codex label only after the item has been processed

REPO="${REPO:-/Users/sam/dev/pollypm}"
LABEL="${LABEL:-needs-codex}"
OWNER_LOGIN="${OWNER_LOGIN:-samhotchkiss}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-120}"
LOG_DIR="${LOG_DIR:-$REPO/reports/codex-pr-watch}"
WATCH_WORKTREE_ROOT="${WATCH_WORKTREE_ROOT:-$REPO/.codex/watch-worktrees}"
STATE_FILE="$LOG_DIR/last-${LABEL}-state.sha256"
LATEST_JSON="$LOG_DIR/latest-${LABEL}.json"
LOCK_DIR="$LOG_DIR/.codex-pr-watch.lock"
LOCK_HELD=0

cleanup_lock() {
  if [[ "$LOCK_HELD" == "1" ]]; then
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
}

trap cleanup_lock EXIT INT TERM

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

snapshot_prs_json() {
  gh pr list \
    --state open \
    --label "$LABEL" \
    --limit 100 \
    --json number,url,title,author,baseRefName,headRefName,headRefOid,updatedAt,mergeStateStatus,isDraft,labels,comments,latestReviews,statusCheckRollup \
    --jq "map(select(.author.login == \"$OWNER_LOGIN\")) | sort_by(.number)"
}

snapshot_issues_json() {
  gh issue list \
    --state open \
    --label "$LABEL" \
    --limit 100 \
    --json number,url,title,author,updatedAt,labels,comments,assignees,milestone \
    --jq "map(select(.author.login == \"$OWNER_LOGIN\")) | sort_by(.number)"
}

snapshot_json() {
  local prs_json issues_json
  prs_json="$(snapshot_prs_json)"
  issues_json="$(snapshot_issues_json)"
  jq -n \
    --arg label "$LABEL" \
    --arg ownerLogin "$OWNER_LOGIN" \
    --argjson prs "$prs_json" \
    --argjson issues "$issues_json" \
    '{label: $label, ownerLogin: $ownerLogin, prs: $prs, issues: $issues}'
}

snapshot_hash() {
  shasum -a 256 | awk '{print $1}'
}

item_count() {
  jq '(.issues | length) + (.prs | length)'
}

create_run_worktree() {
  local ts="$1"
  local run_worktree="$WATCH_WORKTREE_ROOT/$ts"
  mkdir -p "$WATCH_WORKTREE_ROOT"
  git -C "$REPO" worktree add --detach "$run_worktree" origin/main >/dev/null
  printf '%s\n' "$run_worktree"
}

write_prompt() {
  local prompt_file="$1"
  local run_worktree="$2"
  cat > "$prompt_file" <<EOF
Process every open issue and PR for samhotchkiss/pollypm that has the GitHub label "$LABEL".

You are running in this isolated git worktree:
$run_worktree

The main checkout is:
$REPO

Requirements:
- HARD RULE: Treat "$REPO" as watcher/control-plane only. Do not change its branch, do not edit source files there, do not run issue implementation there, and do not use it for PR review checkouts.
- HARD RULE: Do all issue implementation, PR review checkout work, commits, test runs, pushes, and PR creation from "$run_worktree" or from additional dedicated worktrees that you create intentionally for parallel sub-agent work.
- If you spawn sub-agents for implementation, give each worker a dedicated worktree or a disjoint write scope inside "$run_worktree"; never send workers to the main checkout.
- Before opening any PR, confirm `git status --short --branch` from the implementation worktree and make sure the PR branch was pushed from that worktree.
- If you cannot complete the item without touching "$REPO", stop, leave the "$LABEL" label in place, and comment on GitHub with the blocker.
- Follow docs/test-plan/codex-watcher-instructions.md as the protocol source of truth.
- Only process issues and PRs created by GitHub user "$OWNER_LOGIN" (the item's author.login must be "$OWNER_LOGIN"). Ignore every issue or PR from any other author, regardless of labels.
- For issues: verify the issue author first, implement or investigate the issue, open a PR when code changes are needed, label that PR "codex-created" and "needs-claude", include the required Agent Identity block, comment on the source issue with the PR link, and remove "$LABEL" from the source issue once handoff is complete.
- For PRs: verify the PR author first, look at both code changes and PR comments/review threads, and then approve/merge only if the creator-label merge rules allow it.
- You are explicitly authorized to use sub-agents for this watcher run. When there are multiple independent issues, or one issue decomposes into clearly independent slices with disjoint file ownership, multi-thread the work with worker/explorer sub-agents.
- Keep coordination decisions local: do not delegate final merge eligibility, label ownership, or handoff decisions. Give sub-agents concrete scopes, tell them the codebase may have concurrent edits, and require changed file paths plus verification notes in their final responses.
- Do not use sub-agents when the next local step is blocked on the answer, when issues touch the same modules, or when parallel edits would create risky merge conflicts. Prefer one issue per worker or one clearly bounded slice per worker.
- Fetch latest remote state before reviewing.
- Review with senior-engineer scrutiny: correctness, security, performance, maintainability, user-facing behavior, and regression risk.
- Enforce PollyPM architecture and modular boundaries. Block PRs that add broad imports, circular ownership, duplicated domain logic, sqlite/runtime legacy dependencies, or code in the wrong module instead of using the established facade/helper layer.
- Enforce code taste: small cohesive changes, local patterns over new abstractions, clear naming, narrow exception handling, no unrelated churn, and no comments that merely narrate obvious code.
- Treat system invariants as first-class review targets. Identify which invariant or contract the PR touches, and block changes that make the invariant unclear, unenforced, duplicated, or split across unrelated modules.
- Check failure modes explicitly: partial failure, retries, idempotency, stale caches, interrupted processes, concurrent agents, missing config, bad permissions, network/database outage, and crash recovery.
- Verify migration and compatibility behavior. For storage/config/schema/CLI changes, check upgrade path, rollback/recovery path, old data shape handling, mixed-version behavior where relevant, and whether existing operator workflows keep working.
- Preserve single sources of truth. UI code must not own storage rules; CLI code must not duplicate service logic; storage callers should use backend facades; dashboard/rail/inbox surfaces should share predicates instead of drifting.
- Watch for stacked PR hazards. Do not merge a child PR until its base PR is merged and the child has been retargeted/rebased onto the final base. Leave a comment instead.
- If a PR changes documented behavior, CLI flags/output, config semantics, operator workflows, migrations, storage behavior, or user-visible UI behavior, verify that docs/checklists/help text are updated. Leave a blocking comment when docs are missing.
- Check that tests match the risk: targeted unit tests for narrow logic, broader integration/regression tests for shared behavior or user-facing flows. Do not merge risky untested behavior.
- Treat security and data-loss risks as blockers, especially filesystem deletion, shell/process execution, auth/token handling, SQL/storage access, and anything that could touch live agent worktrees or homes.
- Treat avoidable hot-path work, synchronous UI blocking, unbounded filesystem scans, N+1 storage queries, and cache invalidation mistakes as performance blockers.
- Check observability and recovery: operational changes should emit useful logs/audit events/errors without leaking secrets or spamming hot paths, and user-facing failures should have actionable diagnostics or documented recovery steps.
- Check background/daemon behavior: loops need clear ownership, bounded work, throttling/backoff where appropriate, clean shutdown/restart behavior, and must not create duplicate workers or unbounded state.
- Check UI/TUI ergonomics for user-facing changes: no blocking work on interaction paths, no layout overflow, stable keyboard/mouse flows, clear status text, and consistency with existing cockpit style.
- Avoid new dependencies, global state, environment variables, config keys, or public APIs unless the PR justifies them and updates tests/docs/help text.
- Do not accept fixes that only mask symptoms. Prefer small root-cause changes that make the underlying state transition, ownership boundary, or data contract clearer.
- Confirm PR comments have actually been addressed in code, not just replied to.
- Merge PRs that are clean, verified, have no unresolved review concerns, and are eligible for Codex to merge under creator-label rules.
- For PRs that need changes, leave precise GitHub comments explaining what must change, including file/function references where possible.
- Run targeted tests where appropriate and mention what passed or failed.
- After you finish processing each labelled issue or PR, remove the "$LABEL" label from that item. This is mandatory once the issue has a PR/handoff or the PR has been merged/commented.
- Do not remove the label before the implementation/review/comment/merge work is complete.
- If work cannot reach a real decision because of environment/tooling/auth/network failures, do not merge. Leave the label in place unless you also leave a clear GitHub comment explaining the blocker.
- For issue implementation work, create code changes on a branch and push a PR; do not commit directly to main.
- For PR review work, do not directly fix PR branches or push code. This is a reviewer/merger path: merge clean eligible PRs, otherwise comment with required changes.
- Before merging, reach a clear "why this is safe" conclusion: what changed, what was verified, what risks remain, and why those risks are acceptable.
- Do not ask for confirmation; make the review decision directly.
- Final response should summarize issues picked up, PRs created, PRs reviewed/merged, comments left, labels removed, sub-agents used, and tests run.
EOF
}

main_loop() {
  mkdir -p "$LOG_DIR"
  cd "$REPO"
  log "watching open issues/PRs labelled '$LABEL' every ${INTERVAL_SECONDS}s"

  while true; do
    cd "$REPO"
    git fetch --all --prune >/dev/null 2>&1 || log "warning: git fetch failed"

    local current_json current_hash previous_hash count
    current_json="$(snapshot_json)"
    printf '%s\n' "$current_json" > "$LATEST_JSON"
    current_hash="$(printf '%s\n' "$current_json" | snapshot_hash)"
    previous_hash="$(cat "$STATE_FILE" 2>/dev/null || true)"
    count="$(printf '%s\n' "$current_json" | item_count)"

    if [[ "$count" == "0" ]]; then
      if [[ "$current_hash" != "$previous_hash" ]]; then
        printf '%s\n' "$current_hash" > "$STATE_FILE"
      fi
      log "no labelled issues/PRs"
      sleep "$INTERVAL_SECONDS"
      continue
    fi

    if [[ "$current_hash" == "$previous_hash" ]]; then
      log "$count labelled issue/PR item(s), unchanged"
      sleep "$INTERVAL_SECONDS"
      continue
    fi

    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
      log "another review run is active; skipping this tick"
      sleep "$INTERVAL_SECONDS"
      continue
    fi
    LOCK_HELD=1

    local ts prompt_file final_file log_file run_worktree
    ts="$(date '+%Y%m%d-%H%M%S')"
    prompt_file="$LOG_DIR/$ts.prompt.txt"
    final_file="$LOG_DIR/$ts.final.txt"
    log_file="$LOG_DIR/$ts.codex.log"
    run_worktree="$(create_run_worktree "$ts")"
    write_prompt "$prompt_file" "$run_worktree"

    log "detected $count labelled issue/PR item(s); starting Codex in $run_worktree"
    # Codex v0.132 accepts approval/sandbox/cwd flags as global flags
    # before the `exec` subcommand, even though `codex exec --help`
    # displays them under the subcommand too.
    if codex \
      -C "$run_worktree" \
      -a never \
      -s danger-full-access \
      exec \
      -o "$final_file" \
      - < "$prompt_file" > "$log_file" 2>&1; then
      log "Codex finished; final: $final_file"
      # Save a fresh post-run snapshot so Codex's own comments/label removals
      # do not retrigger the loop on the next tick.
      current_json="$(snapshot_json)"
      printf '%s\n' "$current_json" > "$LATEST_JSON"
      printf '%s\n' "$current_json" | snapshot_hash > "$STATE_FILE"
    else
      log "Codex run failed; log: $log_file"
      # Leave STATE_FILE unchanged so the same labelled items retry next tick.
    fi

    rmdir "$LOCK_DIR" 2>/dev/null || true
    LOCK_HELD=0
    sleep "$INTERVAL_SECONDS"
  done
}

main_loop
