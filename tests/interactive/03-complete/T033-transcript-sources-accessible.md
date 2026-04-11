# T033: Transcript Sources Declared and Accessible

**Spec:** v1/05-provider-sdk
**Area:** Provider Adapters
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that each provider adapter declares its transcript sources and that the system can access and read transcript data for auditing and cost tracking.

## Prerequisites
- `pm up` has been run with at least one active session
- Session has generated some conversation history (at least a few turns)

## Steps
1. Run `pm status` and identify an active worker session with recent activity.
2. Check the provider adapter's declared transcript sources: run `pm transcript sources` or `pm plugin info claude` and look for transcript-related capabilities.
3. Identify where transcripts are stored (e.g., JSONL files in the account home, SQLite, or provider-specific storage).
4. Access the transcript for the active worker: `pm transcript show <session-id>` or navigate to the transcript file path.
5. Verify the transcript contains:
   - Input messages (user/system prompts sent to the provider)
   - Output messages (responses from the provider)
   - Timestamps for each message
   - Token counts per message (if available)
6. Verify the transcript is in a parseable format (JSONL, JSON, or structured text).
7. If multiple providers are configured, verify each provider's adapter declares its own transcript source format.
8. Run `pm transcript summary <session-id>` (or equivalent) and verify it provides a readable summary of the conversation.
9. Verify that transcript data is used for cost calculation: check that `pm usage` numbers are consistent with transcript token counts.
10. Confirm transcript files are stored within the account's isolated home directory.

## Expected Results
- Each provider adapter declares its transcript source location and format
- Transcripts are accessible and contain input/output messages with timestamps
- Transcript format is consistent and parseable
- Token counts in transcripts align with usage tracking data
- Transcript files are stored in the correct account home directory
- Multiple providers each have their own transcript format handled correctly

## Log

**Date:** 2026-04-10 | **Result:** PASS

### Re-test — 2026-04-10 (Codex worker listing transcript files)

Worker ran `find .pollypm/homes -name "*.jsonl"`:
```
/homes/onboarding_claude_1/.claude/projects/.../e640ad83-7bf0-42f2-aeeb-a37a13e7f614.jsonl
/homes/onboarding_claude_1/.claude/projects/.../subagents/agent-a8bdedde506d639cb.jsonl
```
Real transcript JSONL files exist in the Claude account home under `.claude/projects/`. Heartbeat confirmed 154 token samples ingested from these transcripts. ✅
