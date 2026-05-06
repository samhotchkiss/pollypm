"""Append-only forensic audit log for PollyPM mutations.

Born out of the ``savethenovel`` post-mortem (2026-05-06): a planning
task ran briefly, then the entire ``work_tasks`` table got wiped
wholesale. We had orphan worker-markers, an empty cockpit affordance,
and zero forensic trail — no idea who or what cleared the table.

This module provides a single append-only JSONL stream of high-signal
state mutations so that:

1. A heartbeat (separate PR, see #savethenovel-followup) can read
   recent events and detect orphan / stuck / "table just got wiped"
   conditions.
2. Operators can grep a single file post-hoc to figure out what
   happened to a project's work state.

Layout (intentional duplication):

* **Per-project log**: ``<project>/.pollypm/audit.jsonl`` is the
  source of truth for that project. It travels with the project
  directory (backups, version control of state, etc.) and survives
  workspace-wide DB rebuilds. When the project path is unknown
  (e.g. delete-project codepaths that have already torn down the
  project root), we fall through to central-only.
* **Central tail**: ``~/.pollypm/audit/<project>.jsonl`` mirrors
  every event so the heartbeat / ``pm doctor`` / external tooling
  can find logs at a single, predictable home regardless of where
  projects live on disk. One file per project keeps reads cheap
  (no filtering required, no cross-project line-interleaving from
  concurrent writers).

Why per-project + central instead of a single combined stream?
Two reasons. (a) The savethenovel wipe happened *while* the
project root was intact — only the workspace-wide DB row got
wiped — so a project-local tail would have caught it. (b) The
heartbeat will iterate registered projects; per-project central
files are O(1) to find without scanning a multiplexed log.

Schema (one JSON object per line, UTF-8, newline-terminated):

    {
      "ts":       "2026-05-06T17:26:44.123456+00:00",  # ISO-8601 UTC
      "project":  "savethenovel",                        # project key, or ""
      "event":    "task.created",                        # stable enum string
      "subject":  "savethenovel/1",                      # task_id / marker / ""
      "actor":    "polly",                               # user/agent identifier
      "status":   "ok",                                  # "ok" | "error" | "warn"
      "metadata": {...},                                 # free-form per-event
    }

Stable event names (extend cautiously — heartbeat rules pin them):

    task.created
    task.status_changed
    task.deleted
    marker.created
    marker.released
    marker.create_failed
    marker.leaked
    work_table.cleared

Writers are append-only and best-effort: a failed audit write must
never block the underlying mutation. We log the failure to the
standard logger and move on. Concurrent writers across processes
are handled by relying on POSIX append-mode atomicity for writes
under PIPE_BUF (4096 bytes) — every event line we emit is well
under that threshold.
"""

from __future__ import annotations

from pollypm.audit.log import (
    AuditEvent,
    central_log_path,
    emit,
    project_log_path,
    read_events,
)

__all__ = [
    "AuditEvent",
    "central_log_path",
    "emit",
    "project_log_path",
    "read_events",
]
