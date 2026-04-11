# T027: Service API Exposes All Operations Consistently

**Spec:** v1/04-extensibility-and-plugins
**Area:** Plugin System
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that the internal service API exposes all documented operations (account management, session management, issue management, etc.) and that plugins can access them consistently.

## Prerequisites
- `pm up` has been run and sessions are active
- Access to the service API documentation or knowledge of available operations
- Ability to invoke API operations (via CLI, plugin, or direct call)

## Steps
1. Review the service API surface by checking documentation or running `pm api list` (or equivalent).
2. Verify account operations are exposed: test `pm account list`, `pm account info <name>` via the API or CLI wrappers.
3. Verify session operations are exposed: test `pm session list`, `pm session info <id>` to retrieve session details.
4. Verify issue operations are exposed: test `pm issue list`, `pm issue info <id>` to retrieve issue details.
5. Verify configuration operations are exposed: test `pm config show`, `pm config get <key>`.
6. Verify event/log operations are exposed: test `pm log` or `pm event list`.
7. Create a simple test plugin that calls the service API (e.g., calls the accounts list operation from within a plugin hook). Place it in `.pollypm/plugins/api_test/`.
8. Load the plugin by restarting and verify it can access the API without errors.
9. Check that API responses are consistent in format (e.g., all return structured data, errors follow a consistent pattern).
10. Clean up: remove the test plugin.

## Expected Results
- All documented API operations are accessible
- Account, session, issue, config, and event operations all work
- Plugins can call the service API from within hooks
- API responses follow a consistent format
- No undocumented errors or missing operations

## Log

**Date:** 2026-04-10 | **Result:** PASS

48 service API methods covering: account management (add/remove/relogin/switch), session management (create/stop/focus/claim/release), issue management (create_inbox_item/triage/transition), heartbeat, scheduling, alerts, token tracking, and more.

### Re-test — 2026-04-10 (Polly listed 34 pm commands)

34 CLI commands across 7 categories: Setup(4), Accounts(6), Tokens(2), UI(4), Projects(3), Session mgmt(7), Monitoring(8). Full API surface. ✅

### Deep test — 2026-04-10 (Polly listed 34 pm commands)

34 CLI commands: Setup(4), Accounts(6), Tokens(2), UI(4), Projects(3), Session(7), Monitoring(8). ✅
