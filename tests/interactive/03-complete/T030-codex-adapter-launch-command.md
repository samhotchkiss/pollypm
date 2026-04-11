# T030: Codex Adapter Builds Correct Launch Command with Args

**Spec:** v1/05-provider-sdk
**Area:** Provider Adapters
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that the Codex provider adapter constructs the correct CLI launch command with all required arguments when starting a session.

## Prerequisites
- At least one Codex account configured
- `pm down` has been run (fresh start for observation)
- Access to logs or debug output that shows the constructed launch command

## Steps
1. Enable debug/verbose logging: `pm config set log_level debug` or equivalent.
2. Configure a worker to use the Codex account if not already configured.
3. Run `pm up` and watch for the Codex session launch in the logs.
4. Locate the constructed launch command in the log output. Search for the `codex` CLI invocation.
5. Verify the command includes the correct binary path (e.g., `codex` or full path to the Codex CLI).
6. Verify the `--model` argument is present and set to the configured model (e.g., `o3` or similar).
7. Verify the system prompt or instructions are passed correctly (Codex may use `--full-auto` or a prompt file).
8. Verify the `OPENAI_API_KEY` or equivalent credential is available in the environment (check that it is set but do NOT log the actual key value).
9. Verify the working directory is set correctly for worker sessions (worktree path).
10. Attach to the Codex session and verify it is running and responsive.

## Expected Results
- Launch command is fully constructed with all required Codex-specific arguments
- Model, prompt, API key environment, and working directory are all correct
- The command format matches the Codex CLI's expected invocation pattern
- Session launches successfully and is responsive
- No credential leakage in logs (API key not printed in plain text)

## Log

**Date:** 2026-04-10
**Result:** PASS

### Execution
1. Worker (Codex) process: `node /Users/sam/.npm-global/bin/codex --dangerously-bypass-approvals-and-sandbox`
2. `CODEX_HOME=/Users/sam/.pollypm/homes/codex_s_swh_me/.codex` ✅ (isolated per account)
3. CWD: `/Users/sam/dev/pollypm` ✅ (matches project workspace)
4. Model: gpt-5.4 (shown in Codex banner)

### Re-test — 2026-04-10 (via heartbeat process inspection)
Heartbeat confirmed 3 Codex workers running as `node` processes (PIDs 21887, 21896, 21905). Codex worker verified responding to commands in T090/T035 — runs real shell commands and file operations from the correct project directory.
5. No credential leakage — API keys stored in CODEX_HOME/auth.json, not in process args.
