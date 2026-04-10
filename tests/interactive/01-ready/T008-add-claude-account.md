# T008: Add a New Claude Account via Onboarding

**Spec:** v1/02-configuration-and-accounts
**Area:** Account Management
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that the onboarding flow correctly adds a new Claude account, creates its isolated home directory, and makes it available for session assignment.

## Prerequisites
- Polly is installed
- Valid Claude API credentials or Claude CLI login available for a new account
- The account to be added is NOT already configured in Polly

## Steps
1. Run `pm account list` and note the currently configured accounts. Confirm the new account is not listed.
2. Run `pm account add` (or `pm onboard`) to start the account addition flow.
3. When prompted for provider, select "claude".
4. When prompted for account name, enter a descriptive name (e.g., "claude-test-2").
5. Follow the onboarding prompts to provide credentials or authenticate. This may involve pasting an API key or running `claude login` in a subprocess.
6. When prompted for account home directory, accept the default or specify a custom path.
7. Complete the onboarding flow and note any confirmation messages.
8. Run `pm account list` and verify the new account appears with correct provider ("claude"), name, and status ("healthy" or "available").
9. Verify the account home directory was created: `ls -la <account-home-path>` and confirm it exists with correct ownership.
10. Run `pm status` to confirm the system recognizes the new account as available for session assignment.

## Expected Results
- Onboarding flow completes without errors
- `pm account list` shows the new Claude account
- Account home directory exists on disk
- Account is marked as healthy/available
- System can assign the account to sessions going forward

## Log
