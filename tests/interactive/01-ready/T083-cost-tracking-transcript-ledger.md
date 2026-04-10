# T083: Cost Tracking via Transcript Ledger

**Spec:** v1/13-security-and-observability
**Area:** Observability
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that the transcript ledger tracks API costs per session and per account, enabling cost monitoring and budgeting.

## Prerequisites
- `pm up` has been run with active sessions generating API usage
- Sessions have been running long enough to accumulate cost data

## Steps
1. Run `pm usage` or `pm cost` (or equivalent) to view the current cost tracking data.
2. Verify the output includes:
   - Per-session cost (input tokens, output tokens, estimated cost)
   - Per-account cost (aggregated across sessions using that account)
   - Total cost across all accounts
3. Check the transcript ledger storage: locate the ledger file or database table (e.g., `.pollypm/ledger/` or `transcript_ledger` table in state.db).
4. Query the ledger: `sqlite3 <state.db> "SELECT session_id, account, input_tokens, output_tokens, estimated_cost FROM transcript_ledger ORDER BY timestamp DESC LIMIT 10;"` (adjust as needed).
5. Verify each ledger entry includes:
   - Session ID
   - Account name
   - Provider
   - Input token count
   - Output token count
   - Cost estimate (based on provider pricing)
   - Timestamp
6. Verify the cost estimates are reasonable (not zero, not absurdly high).
7. Cross-reference with the provider's own usage dashboard if accessible.
8. Run `pm usage --account <account-name>` to see per-account breakdown.
9. Run `pm usage --session <session-id>` to see per-session breakdown.
10. Verify cost data accumulates over time as sessions continue working.

## Expected Results
- Transcript ledger records cost data for every API interaction
- Per-session and per-account cost breakdowns are available
- Cost estimates are reasonable and based on token counts
- Ledger entries include all required fields
- Cost data is accessible via CLI commands
- Totals are consistent (sum of per-session = per-account total)

## Log
