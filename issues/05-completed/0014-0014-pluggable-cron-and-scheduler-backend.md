# 0014 Pluggable Cron And Scheduler Backend

## Goal

Extract scheduling and cron-like behavior behind a replaceable backend contract.

## Scope

- default in-process scheduler backend
- recurring jobs
- one-shot delayed jobs
- retry scheduling
- job inspection/cancel/pause hooks

## Acceptance Criteria

- PollyPM core depends on a scheduler interface instead of embedding all timing logic directly.
- The default scheduler can support recurring refreshes, delayed retries, and scheduled PM jobs.
- A future cron-compatible or external scheduler backend could replace the default one.
