# T091: Existing Config Loads After Update

**Spec:** v1/15-migration-and-stability
**Area:** Migration
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that after updating Polly to a new version, existing configuration files load correctly without errors or data loss.

## Prerequisites
- A working Polly installation with existing configuration (accounts, projects, settings)
- Access to the Polly source for simulating an update
- Backup of current configuration

## Steps
1. Back up the current configuration: `cp -r .pollypm /tmp/pollypm-backup-T091`.
2. Record the current config state: `pm config show > /tmp/config-before.txt`.
3. Record the current accounts: `pm account list > /tmp/accounts-before.txt`.
4. Simulate or perform a version update (e.g., `pip install --upgrade pollypm` or `git pull && pip install -e .`).
5. After the update, run `pm config show > /tmp/config-after.txt`.
6. Compare the config before and after: `diff /tmp/config-before.txt /tmp/config-after.txt`. Any differences should be new fields with defaults, not lost data.
7. Run `pm account list > /tmp/accounts-after.txt`.
8. Compare accounts: `diff /tmp/accounts-before.txt /tmp/accounts-after.txt`. Accounts should be identical.
9. Run `pm up` and verify all sessions start correctly with the existing configuration.
10. Run `pm status` and verify all sessions are healthy.
11. Clean up backups: `rm -rf /tmp/pollypm-backup-T091 /tmp/config-before.txt /tmp/config-after.txt /tmp/accounts-before.txt /tmp/accounts-after.txt`.

## Expected Results
- Existing config loads without errors after update
- No settings are lost or corrupted
- New config fields (if any) get sensible defaults
- Accounts are preserved
- `pm up` works with existing config
- Sessions start and run normally

## Log
