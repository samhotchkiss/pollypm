# T010: Account Home Isolation Verified (Separate CLAUDE_CONFIG_DIR per Account)

**Spec:** v1/02-configuration-and-accounts
**Area:** Account Management
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that each configured account has its own isolated home directory (e.g., separate CLAUDE_CONFIG_DIR) and that sessions using different accounts do not share or corrupt each other's configuration.

## Prerequisites
- At least two accounts configured (e.g., "claude-primary" and "claude-secondary")
- `pm up` has been run or sessions can be started manually

## Steps
1. Run `pm account list` and identify at least two accounts. Note their home directory paths.
2. Verify the home directories are distinct: `echo <account-1-home>` and `echo <account-2-home>` should show different paths.
3. List the contents of each account home: `ls -la <account-1-home>` and `ls -la <account-2-home>`. Confirm they are separate directories.
4. Check the permissions on each directory: `stat -f '%Lp' <account-1-home>` (should be 700 or similarly restrictive).
5. Start a session with account 1 and verify the CLAUDE_CONFIG_DIR (or equivalent env var) is set to account 1's home. Attach to the session and run `echo $CLAUDE_CONFIG_DIR` inside the pane.
6. Start a session with account 2 and verify the CLAUDE_CONFIG_DIR is set to account 2's home. Attach and run `echo $CLAUDE_CONFIG_DIR`.
7. Confirm the two values are different.
8. Create a marker file in account 1's home: `touch <account-1-home>/.test-marker`.
9. Verify the marker file does NOT exist in account 2's home: `ls <account-2-home>/.test-marker` should fail.
10. Clean up: `rm <account-1-home>/.test-marker`.

## Expected Results
- Each account has a distinct, separate home directory
- CLAUDE_CONFIG_DIR (or equivalent) is set correctly per-session based on the assigned account
- Files in one account's home are not visible in another account's home
- Directory permissions are restrictive (700)
- No cross-contamination between account configurations

## Log
