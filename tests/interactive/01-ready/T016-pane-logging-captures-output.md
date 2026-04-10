# T016: Pane Logging Captures All Output to Log Files

**Spec:** v1/03-session-management-and-tmux
**Area:** Session Management
**Priority:** P1
**Duration:** 10 minutes

## Objective
Verify that all tmux pane output is captured to persistent log files on disk, enabling post-mortem analysis and audit.

## Prerequisites
- `pm up` has been run and sessions are active
- Knowledge of where pane logs are stored (e.g., `.pollypm/logs/` or similar)

## Steps
1. Run `pm status` and confirm all sessions are running.
2. Locate the log directory: check `.pollypm/logs/`, `pm config show` for log path, or `pm log --path`.
3. List the log files: `ls -la <log-directory>`. There should be one log file per pane (e.g., `heartbeat.log`, `operator.log`, `worker-0.log`).
4. Check the size of each log file: `wc -l <log-directory>/*.log`. Files should be non-empty if sessions have been running.
5. Tail the heartbeat log: `tail -20 <log-directory>/heartbeat.log`. Verify it contains heartbeat cycle output matching what you see when attached to the heartbeat pane.
6. Tail the operator log: `tail -20 <log-directory>/operator.log`. Verify it contains operator activity.
7. Attach to a worker session and type a distinctive message or command (e.g., "echo UNIQUE_TEST_MARKER_T016").
8. Detach and check the worker log: `grep UNIQUE_TEST_MARKER_T016 <log-directory>/worker-0.log`. The marker should appear.
9. Verify logs are being written in real-time by watching a log file: `tail -f <log-directory>/heartbeat.log` for 30 seconds. New lines should appear with each heartbeat cycle.
10. Stop the tail and verify the log files persist after `pm down`: run `pm down`, then `ls -la <log-directory>/*.log` — files should still exist.

## Expected Results
- Log files exist for each active pane
- Log content matches the output visible in the tmux panes
- Distinctive markers typed in a session appear in the corresponding log file
- Logs are written in real-time (minimal delay)
- Log files persist after `pm down` for post-mortem analysis

## Log
