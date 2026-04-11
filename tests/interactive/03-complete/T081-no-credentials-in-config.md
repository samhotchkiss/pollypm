# T081: No Credentials in pollypm.toml

**Spec:** v1/13-security-and-observability
**Area:** Security
**Priority:** P0
**Duration:** 10 minutes

## Objective
Verify that the main configuration file (pollypm.toml) does not contain any credentials, API keys, tokens, or secrets in plain text.

## Prerequisites
- Polly is installed and configured with at least one account
- The main config file exists

## Steps
1. Locate the main config file: check `pollypm.toml`, `.pollypm/pollypm.toml`, or `pm config show --path`.
2. Read the entire config file: `cat <config-file>`.
3. Search for common credential patterns:
   - `grep -i "api.key\|secret\|token\|password\|credential" <config-file>`
   - `grep -i "sk-\|OPENAI_API_KEY\|ANTHROPIC_API_KEY" <config-file>`
4. Verify NO lines contain actual API keys or secrets.
5. If the config references accounts, verify credentials are stored in the account home directories (not in the config).
6. Check for indirect credential references: the config may reference a path to credentials (e.g., `credentials_path = "~/.pollypm/accounts/..."`) which is acceptable, but should not contain the actual credential values.
7. Check the user-global config as well: `cat ~/.config/pollypm/config.toml` and search for credentials.
8. Verify that any example or template config files also do not contain real credentials.
9. Check that the config file itself has reasonable permissions: `stat -f '%Lp' <config-file>` (should be 600 or 644, not world-readable if it contains any sensitive paths).
10. Run `pm config show` and verify the output does not display any credentials (keys should be masked or omitted).

## Expected Results
- No API keys, tokens, or passwords in pollypm.toml
- Credentials are stored only in account home directories
- Config file may reference credential paths but not values
- `pm config show` masks or omits sensitive values
- Example/template configs are also credential-free

## Log

**Date:** 2026-04-10
**Result:** PASS

### Execution
1. Config file at `~/.pollypm/pollypm.toml` contains project settings, account references (email + home path), session configs (role, provider, prompts), and project paths.
2. Searched for: api_key, secret, token, password, credential, sk-, OPENAI_API_KEY, ANTHROPIC_API_KEY — **zero matches**. ✅
3. Accounts reference `home = "homes/..."` paths to isolated directories, not credential values. ✅
4. No actual API keys, tokens, or passwords in config. ✅

### Re-test — 2026-04-10 (via operator session)

Asked operator: "Read pollypm.toml and search for API keys, passwords, tokens, secrets"
```
⏺ No secrets found. The file contains only configuration data:
  - Project paths and names
  - Session definitions with prompts and roles
  - Account references (provider + email, no credentials)
  - Backend and feature flags
  I searched for sk-, token, password, secret, credential, api_key, OPENAI — none present.
```
