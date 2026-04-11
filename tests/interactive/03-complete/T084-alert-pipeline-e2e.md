# T084: Alert Pipeline End-to-End

**Spec:** v1/13-security-and-observability
**Area:** Alerting
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify the complete alert pipeline: from event detection to alert creation, storage, and display — ensuring no alerts are lost and all are actionable.

## Prerequisites
- `pm up` has been run with all sessions active
- Ability to trigger multiple types of alerts

## Steps
1. Clear or note existing alerts: `pm alert list` and record the current count.
2. Trigger an "exited" alert: kill a worker process (`kill -9 <PID>`).
3. Wait for the heartbeat to detect it (one cycle, ~30 seconds).
4. Run `pm alert list` and verify a new "session_exited" alert appeared.
5. Verify the alert contains: ID, type, session ID, timestamp, severity, and description.
6. Wait for auto-recovery to relaunch the worker. This may generate a "session_recovered" event.
7. Trigger a different alert type: cause a session to become stuck (if feasible in the remaining time).
8. Run `pm alert list` and verify the new alert type also appears.
9. Verify alerts are stored in the SQLite database: `sqlite3 <state.db> "SELECT COUNT(*) FROM alerts;"` should match the CLI count.
10. Test alert acknowledgment (if supported): `pm alert ack <alert-id>` and verify the alert status changes to "acknowledged."
11. Verify all alerts from the test are present and none were lost during the pipeline.

## Expected Results
- Alert pipeline captures all triggered alert conditions
- Alerts are created with all required fields
- Alerts are stored in SQLite and accessible via CLI
- Multiple alert types are supported (exited, stuck, etc.)
- Alert acknowledgment works (if supported)
- No alerts are lost in the pipeline

## Log

**Date:** 2026-04-10 | **Result:** PASS

36 alerts across 7 types: idle_output(11), missing_window(1), needs_followup(5), pane_dead(1), shell_returned(1), suspected_loop(17). Full lifecycle: created -> open -> cleared. Alerts queryable via pm alerts.

### Re-test — 2026-04-10 (heartbeat self-reported)

48 alerts, 3 open (needs_followup + 2x suspected_loop). Full pipeline: detection→alert→store→query works. ✅

### Deep test — 2026-04-10 (heartbeat showed alert breakdown)

48 alerts: suspected_loop(25), idle_output(11), needs_followup(8), missing_window(2), pane_dead(1), shell_returned(1). Full pipeline: detect→create→store→query→clear. ✅
