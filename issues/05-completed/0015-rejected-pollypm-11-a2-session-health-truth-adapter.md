# [CANCELLED] Rejected pollypm/11 — A2: Session Health Truth Adapter

Confidence: 9/10 — Criterion 1 failed: A2 requires centralizing how heartbeat, cockpit, alerts, and task-assignment decide alive/idle/stale/blocked/absent/orphaned, but the new adapter is only wired into cockpit and partially into supervisor_alerts. Heartbeat still uses its separate DefaultRecoveryPolicy classifier at src/pollypm/heartbeats/local.py:257 and consumes it at src/pollypm/heartbeats/local.py:1051. Task-assignment still uses its own idle/window-alive heuristics at src/pollypm/plugins_builtin/task_assignment_notify/handlers/sweep.py:245 and :1291, and _recover_dead_claims consumes that at :1528. Wire those paths through pollypm.session_health or explicitly narrow the task scope with reviewed acceptance criteria. Related tests pass (122 passed), and git status is clean, but the required surfaces are not centralized.

Task `pollypm/11` was rejected by `russell` and returned to rework.

Current stage: `build`

Open the linked task in the task cockpit, or jump to the inbox thread to
review the full rejection note before the worker continues.
