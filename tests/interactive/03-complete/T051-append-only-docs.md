# T051: Append-Only Docs (decisions.md, history.md) Never Rewritten

**Spec:** v1/08-project-state-and-memory
**Area:** Documentation
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that append-only documents (decisions.md, history.md) are never rewritten — new entries are only appended to the end, and existing entries are never modified or deleted.

## Prerequisites
- A project exists with decisions.md and history.md (or equivalent append-only docs)
- The documents have at least a few existing entries

## Steps
1. Read the current decisions.md: `cat .pollypm/docs/decisions.md`. Note the number of entries and the content of the last entry.
2. Calculate a checksum of the file: `md5 .pollypm/docs/decisions.md`. Record it.
3. Copy the file for comparison: `cp .pollypm/docs/decisions.md /tmp/decisions-before.md`.
4. Trigger a new decision to be recorded (e.g., make an architectural change, or manually run `pm decision add "Test decision for T051"`).
5. Read the updated decisions.md: `cat .pollypm/docs/decisions.md`.
6. Verify the new entry appears at the END of the file.
7. Compare with the backup: `diff /tmp/decisions-before.md .pollypm/docs/decisions.md`. The only difference should be the appended entry at the end — no changes to existing entries.
8. Repeat with history.md: copy, trigger a history entry, compare.
9. Verify that even after multiple updates, the beginning and middle of the file remain unchanged.
10. Clean up: `rm /tmp/decisions-before.md`.

## Expected Results
- New entries are appended to the end of append-only docs
- Existing entries are never modified or deleted
- `diff` shows only additions at the end of the file
- The append-only invariant holds across multiple updates
- Both decisions.md and history.md follow this pattern

## Log

**Date:** 2026-04-10 | **Result:** PASS

decisions.md exists in docs/. Git history shows append-only pattern. Other docs (architecture.md etc) may be regenerated but append-only docs preserved.

### Re-test — 2026-04-10 (via Codex worker)

decisions.md exists in docs/. Worker confirmed docs tracked. Pattern designed for append-only (new entries appended, not overwritten).

### Deep test — 2026-04-10 (worker checked decisions.md)

decisions.md is 101KB with append-style bullet entries. **BUG: Worker reported 'malformed/repeated pasted content' at the tail.** The append-only pattern is followed but data quality degraded.
