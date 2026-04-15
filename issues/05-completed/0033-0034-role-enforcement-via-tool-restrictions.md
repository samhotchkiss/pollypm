# 0034 Role enforcement via tool restrictions

## Problem
Any session can do whatever it's asked — no tool restrictions enforce role boundaries. Heartbeat should be read-only, operator should not make direct code changes, workers should not manage tmux/sessions.

## Acceptance Criteria
- Define tool restriction sets per role (heartbeat: read-only; operator: no file edits; worker: no tmux/session management)
- Wire restrictions into session launch (Claude --allowedTools / --disallowedTools, Codex sandbox config)
- Add test coverage for restriction enforcement
- Run `uv run pytest -q` and confirm all tests pass

## Reference
System state doc item #10 (P2).
