# 0024 Codex Send Auto Submit Coverage And Readiness Docs Cleanup

## Goal

Record and lock in the Codex `pm send` auto-submit behavior so it stays covered by tests, and clean up the readiness docs so they no longer describe it as an open blocker.

## Acceptance Criteria

- `Supervisor.send_input()` is covered by a regression test proving Codex sessions receive the extra Enter needed to submit input
- stale docs no longer claim that `pm send` fails to auto-submit for Codex
- launch-readiness docs/visuals no longer list Codex auto-submit as a current blocker
- launch-readiness blocker counts and timeline match the remaining blockers after the cleanup
- targeted `send_input` supervisor tests pass

## Verification

- `pytest -q tests/test_supervisor.py -k 'extra_enter_for_codex'` passes
- `pytest -q tests/test_supervisor.py -k 'send_input'` passes
- repo grep no longer finds the old launch-blocker wording for Codex auto-submit
