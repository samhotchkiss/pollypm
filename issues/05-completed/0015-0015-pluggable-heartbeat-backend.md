# 0015 Pluggable Heartbeat Backend

## Goal

Extract session heartbeat and monitoring execution behind a replaceable backend contract.

## Scope

- default local heartbeat backend
- transcript-driven heartbeat inputs
- tmux/process liveness inputs
- normalized heartbeat result model
- alert/recovery handoff into core

## Acceptance Criteria

- PollyPM core defines heartbeat policy, but heartbeat execution is delegated to a backend interface.
- The default backend can monitor local sessions using transcript and liveness signals.
- A future external or distributed heartbeat worker could replace the default backend.
