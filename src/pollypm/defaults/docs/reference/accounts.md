# Accounts & Providers

PollyPM manages isolated accounts for Claude and Codex. Each account has its own home directory, credentials, and usage tracking.

## Configured Accounts

Check with `pm accounts`. Each account shows:
- Provider (claude/codex), email, login status
- Health (healthy/exhausted/auth_broken)
- Usage plan and remaining quota
- Isolation mode (keychain, file-based auth)
- Home directory (700 permissions, isolated per account)

## Multi-Account & Failover

- **Controller account** — used for heartbeat and operator sessions
- **Failover accounts** — used when the controller is exhausted or broken
- Configure in `~/.pollypm/pollypm.toml`:
  ```toml
  [pollypm]
  controller_account = "claude_primary"
  failover_enabled = true
  failover_accounts = ["codex_backup"]
  ```

When a session fails auth or hits quota, the recovery system automatically tries failover accounts (same provider first, then cross-provider).

## Quota Rotation

The heartbeat tracks token usage per account via transcript ingestion. When an account approaches its limit, sessions can be moved to accounts with remaining quota. Check usage with `pm tokens`.

## Re-authentication

If an account loses auth: `pm relogin <account_name>`

The cockpit Settings view (press `s`) also has a Relogin button per account.
