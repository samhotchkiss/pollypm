# T009: Add a New Codex Account via Onboarding

**Spec:** v1/02-configuration-and-accounts
**Area:** Account Management
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that the onboarding flow correctly adds a new Codex (OpenAI) account, creates its isolated home directory, and makes it available for session assignment.

## Prerequisites
- Polly is installed
- Valid OpenAI/Codex API credentials available for a new account
- The Codex account to be added is NOT already configured in Polly

## Steps
1. Run `pm account list` and note the currently configured accounts. Confirm no Codex account with the intended name is listed.
2. Run `pm account add` to start the account addition flow.
3. When prompted for provider, select "codex".
4. When prompted for account name, enter a descriptive name (e.g., "codex-test-1").
5. Follow the onboarding prompts to provide the OpenAI API key or authenticate via the Codex CLI.
6. When prompted for account home directory, accept the default or specify a custom path.
7. Complete the onboarding flow and note any confirmation messages.
8. Run `pm account list` and verify the new Codex account appears with correct provider ("codex"), name, and status.
9. Verify the account home directory was created: `ls -la <account-home-path>` and confirm it exists.
10. Run `pm config show` and verify the Codex account appears in the accounts section of the configuration.
11. Optionally, run `pm up` and verify the system can assign the Codex account to a worker session.

## Expected Results
- Onboarding flow for Codex completes without errors
- `pm account list` shows the new Codex account with correct provider type
- Account home directory exists on disk with proper permissions
- Account configuration is persisted in the config file
- System treats Codex accounts equivalently to Claude accounts for assignment

## Log
