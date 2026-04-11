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

**Date:** 2026-04-10 | **Result:** PASS

### Re-test — 2026-04-10 (via cockpit Settings TUI)

Navigated cockpit: pressed 's' → Settings pane opened. Account details visible in TUI:

```
Account: claude_claude_swh_me
Email:      claude@swh.me
Provider:   claude
Logged in:  no
Health:     healthy
Plan:       max
Usage:      max
Controller: yes
Failover:   no
Home:       /Users/sam/.pollypm/homes/onboarding_claude_1
Isolation:  host-profile-keyring
Storage:    keychain
Expires:    2026-04-10T22:00:22.188000+00:00
Reason:     local auth metadata loaded
```

Account properly onboarded with isolated home, keychain auth, controller role.

**Note:** "Logged in: no" despite the account being healthy and usable. This may indicate the login status check is looking for something different than auth availability. **Possible bug:** Claude Code sessions work fine (we verified in T002/T003) but the settings TUI shows "no" for login.
