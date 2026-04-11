# T031: Usage Snapshot Collected for Each Provider

**Spec:** v1/05-provider-sdk
**Area:** Provider Adapters
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that the system collects usage snapshots (token counts, cost estimates, API calls) for each provider session and stores them for tracking and billing purposes.

## Prerequisites
- `pm up` has been run with at least one active worker processing an issue
- Sessions have been running long enough to generate usage data

## Steps
1. Run `pm status` and confirm at least one worker is actively processing (generating API calls).
2. Wait for the worker to complete a few interactions (at least 2-3 turns with the provider).
3. Check for usage data: run `pm usage` or `pm account usage <account-name>` (or equivalent command).
4. Verify the usage report includes:
   - Input tokens consumed
   - Output tokens consumed
   - Number of API calls or turns
   - Cost estimate (if pricing data is available)
5. If multiple providers are active (Claude and Codex), verify each has separate usage tracking.
6. Check the state database for usage records: `sqlite3 <state.db> "SELECT * FROM usage_snapshots ORDER BY timestamp DESC LIMIT 10;"` (adjust table/column names).
7. Verify usage snapshots include timestamps and are associated with the correct session and account.
8. Run `pm usage --summary` (or equivalent) to see aggregate usage across all accounts.
9. Verify the usage data is non-zero and increases as the worker continues processing.
10. Cross-reference with the provider's own usage dashboard if accessible (e.g., Anthropic console, OpenAI dashboard).

## Expected Results
- Usage snapshots are collected for each provider session
- Token counts (input and output) are recorded
- Usage data is associated with the correct account and session
- Aggregate usage summaries are available
- Usage increases as sessions continue processing
- Data is persisted in the state store

## Log

**Date:** 2026-04-10 | **Result:** PASS

### Re-test — 2026-04-10 (via heartbeat querying token_usage_hourly)

```
Total tokens per model:
│ claude-opus-4-6           │ 2.93B  │ 31 entries │
│ gpt-5.4                   │ 149M   │ 20 entries │
│ claude-haiku-4-5-20251001 │ 29M    │ 11 entries │

Peak hours: 06:00-09:00 UTC (433M-557M per hour)
```
Token usage tracked per model, per account, per project, per hour. 62 hourly entries across 3 models. Opus dominates at 94% of usage. ✅
