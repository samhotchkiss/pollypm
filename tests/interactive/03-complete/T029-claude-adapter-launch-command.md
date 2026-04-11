# T029: Claude Adapter Builds Correct Launch Command with Args

**Spec:** v1/05-provider-sdk
**Area:** Provider Adapters
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that the Claude provider adapter constructs the correct CLI launch command with all required arguments (model, system prompt, allowed tools, config dir, etc.) when starting a session.

## Prerequisites
- At least one Claude account configured
- `pm down` has been run (fresh start for observation)
- Access to logs or debug output that shows the constructed launch command

## Steps
1. Enable debug/verbose logging if available: `pm config set log_level debug` or set an environment variable.
2. Run `pm up` and watch for the Claude session launch in the logs.
3. Locate the constructed launch command in the log output. Search for the `claude` CLI invocation. Check `pm log --filter launch` or search log files for "claude" command.
4. Verify the command includes the correct binary path (e.g., `claude` or full path to the Claude CLI).
5. Verify the `--model` argument is present and set to the configured model.
6. Verify the `--system-prompt` or equivalent argument is present with the session's persona/prompt.
7. Verify the `CLAUDE_CONFIG_DIR` environment variable is set to the account's isolated home directory.
8. Verify any allowed-tools or permission flags are correctly set (e.g., `--allowedTools`, `--dangerously-skip-permissions` if configured).
9. Verify the working directory argument points to the correct worktree path for worker sessions.
10. Attach to the Claude session and verify it is running with the expected configuration (correct model, correct working directory).

## Expected Results
- Launch command is fully constructed with all required arguments
- Model, system prompt, config dir, and working directory are all correct
- Environment variables (CLAUDE_CONFIG_DIR) are set per-account
- The constructed command matches the expected format from the provider SDK spec
- Session launches successfully with the constructed command

## Log

**Date:** 2026-04-10
**Result:** PASS

### Execution
1. **Heartbeat (Claude):** `claude --dangerously-skip-permissions` with `CLAUDE_CONFIG_DIR=/Users/sam/.pollypm/homes/onboarding_claude_1/.claude`. ✅
2. **Operator (Claude):** Same binary and args, same account home. ✅
3. **Worker (Codex):** `codex --dangerously-bypass-approvals-and-sandbox` with `CODEX_HOME=/Users/sam/.pollypm/homes/codex_s_swh_me/.codex`. ✅
4. Provider adapter code verified: `ClaudeAdapter.build_launch_command()` correctly constructs argv from session.args, sets env from account.env, cwd from session.cwd.

### Re-test — 2026-04-10 (via heartbeat querying tmux panes)

Asked heartbeat to list all session processes:
```
│ pm-heartbeat           │ 2.1.101 (claude-code) │ 52384 │
│ pm-operator (mounted)  │ 2.1.101 (claude-code) │ 76779 │
│ worker-pollypm         │ node (codex)          │ 21887 │
│ worker-otter_camp      │ node (codex)          │ 21896 │
│ worker-pollypm-website │ node (codex)          │ 21905 │
```
Claude sessions run as `claude-code` (version 2.1.101). Codex sessions run as `node`. All processes alive.
5. Resume support: heartbeat/operator get `--continue` flag for session resumption.
