# T080: Account Homes Created with Mode 700

**Spec:** v1/13-security-and-observability
**Area:** Security
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that all account home directories are created with restrictive permissions (mode 700 — owner read/write/execute only), preventing other users from accessing account credentials.

## Prerequisites
- At least two accounts are configured
- Account home directories exist on disk

## Steps
1. Run `pm account list` and note the home directory path for each account.
2. For each account home directory, check the permissions:
   ```
   stat -f '%Lp %N' <account-home-1>
   stat -f '%Lp %N' <account-home-2>
   ```
3. Verify each directory has permissions of exactly `700` (or `drwx------` in long format).
4. Run `ls -la <parent-of-account-homes>` and verify the permission column shows `drwx------` for each account directory.
5. Verify the owner of each directory is the current user: `stat -f '%Su' <account-home>`.
6. Create a new account via `pm account add` (use test credentials).
7. Check the newly created account's home directory permissions immediately after creation.
8. Verify the new directory also has mode 700.
9. Attempt to access an account home from a different user context (if possible): `sudo -u nobody ls <account-home>` should fail with "Permission denied."
10. Verify that files INSIDE the account homes also have restrictive permissions (no world-readable files): `find <account-home> -perm +o=r` should return nothing.

## Expected Results
- All account home directories have mode 700
- Only the owner user can read/write/execute the directories
- Newly created account homes automatically get mode 700
- Files inside account homes are not world-readable
- Access from other user accounts is denied

## Log
