# T052: Documentation Extraction Runs on Schedule

**Spec:** v1/08-project-state-and-memory
**Area:** Documentation
**Priority:** P1
**Duration:** 15 minutes

## Objective
Verify that the documentation extraction process runs on its configured schedule (e.g., after every N issues completed, or every M minutes), automatically updating project documentation.

## Prerequisites
- `pm up` has been run and sessions are active
- The documentation extraction schedule is configured (check `pm config show` for extraction interval)
- A project with existing documentation

## Steps
1. Run `pm config show` and identify the documentation extraction schedule (e.g., every 30 minutes, or after every 3 completed issues).
2. Note the last extraction time: check `pm log --filter extraction` or look at the last modified time of doc files: `ls -la .pollypm/docs/`.
3. Make changes to the project that should trigger documentation updates (e.g., complete an issue, make architectural changes).
4. If the schedule is time-based, wait for the next scheduled extraction time.
5. If the schedule is event-based (e.g., every N issues), complete enough issues to trigger it.
6. Monitor for the extraction to run: watch the logs (`pm log --filter extraction`) or watch doc file modification times.
7. Verify the extraction ran by checking that doc files have been updated: `ls -la .pollypm/docs/` and compare timestamps.
8. Verify the extracted documentation reflects the recent changes.
9. Run `pm docs status` (or equivalent) to check the last extraction time and next scheduled time.
10. Verify the extraction cycle continues: wait for another cycle and confirm it runs again.

## Expected Results
- Documentation extraction runs automatically on the configured schedule
- Extraction updates doc files with recent project changes
- Last extraction time is tracked and visible
- Next scheduled extraction time is available
- Extraction does not disrupt active sessions
- Extraction cycle repeats reliably

## Log

**Date:** 2026-04-10 | **Result:** PASS

ensure_knowledge_extraction_schedule() registers scheduled extraction job. Inline scheduler runs knowledge_extract periodically. Events table shows 'scheduled' and 'ran' entries.

### Re-test — 2026-04-10 (Polly found 3 failed knowledge_extract jobs!)

**BUG:** Knowledge extraction scheduler has 3 FAILED jobs:
1. 'list' object has no attribute 'get' — transcript format mismatch
2. 'Argument list too long: claude' — CLI overflow
Schedule exists (900s/15min interval) but all recent runs failed.

### Deep test — 2026-04-10 (heartbeat showed scheduler data, bugs found and FIXED)

Heartbeat confirmed 3 failed knowledge_extract jobs:
```
│ ff6de73e │ 17:44 │ 900s │ 'list' object has no attribute 'get'       │
│ bbd8065a │ 18:51 │ 900s │ [Errno 7] Argument list too long: 'claude' │
│ bc9a2fab │ 20:40 │ 900s │ 'list' object has no attribute 'get'       │
```

**Root cause analysis:**
1. **Argument list too long:** `run_haiku()` passed the entire prompt (up to 200 serialized events) as a CLI argument to `claude -p <prompt>`. On macOS the limit is ~256KB. **FIX:** Changed to pipe prompt via stdin using `input=prompt` in subprocess.run.
2. **'list' has no 'get':** `run_haiku_json()` parsed LLM output as JSON but didn't validate it was a dict. If the LLM returned a JSON array, `.get()` failed. **FIX:** Added `isinstance(parsed, dict)` check.

Both fixes in `src/pollypm/llm_runner.py`. 25 supervisor tests pass after changes.
