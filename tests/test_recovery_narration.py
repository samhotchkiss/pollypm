from __future__ import annotations

from pollypm.recovery.narration import (
    narrate_recovery_event,
    narrate_watchdog_escalation_group,
    summarize_audit_event,
)


def test_recovery_spawn_narration_scrubs_session_and_failure_ids() -> None:
    sentence = narrate_recovery_event(
        "recovery.spawn",
        {
            "failure_type": "capacity_exhausted",
            "reason": "recovery_restart",
            "target_session": "architect_polly_remote",
            "account": "codex_primary",
            "provider": "codex",
        },
        subject="architect_polly_remote",
        project="polly_remote",
        status="ok",
    )

    assert sentence == (
        "I restarted the architect for polly remote after capacity was exhausted."
    )
    assert "architect_polly_remote" not in sentence
    assert "capacity_exhausted" not in sentence


def test_watchdog_dispatch_narration_uses_finding_metadata() -> None:
    sentence = narrate_recovery_event(
        "watchdog.escalation_dispatched",
        {
            "finding_type": "stuck_draft",
            "subject": "samblog/32",
            "brief": "raw internal brief body",
            "dedup_hash": "abc123",
        },
        project="samblog",
        status="warn",
    )

    assert sentence == (
        "I sent an unstick brief for a stuck draft on task 32 in samblog "
        "so the project could keep moving."
    )
    assert "samblog/32" not in sentence
    assert "dedup_hash" not in sentence


def test_watchdog_dispatch_group_merges_same_task_recovery_reasons() -> None:
    sentence = narrate_watchdog_escalation_group(
        ["task_review_stale", "task_rework_stale"],
        subject="itsalive/55",
        project="itsalive",
    )

    assert sentence == (
        "I sent unstick briefs for stale review and rework on task 55 in itsalive "
        "so the project could keep moving."
    )
    assert "task_review_stale" not in sentence
    assert "task_rework_stale" not in sentence


def test_non_recovery_audit_summary_keeps_generic_shape() -> None:
    summary = summarize_audit_event(
        event_name="task.status_changed",
        subject="demo/5",
        actor="worker_demo",
        status="ok",
        project="demo",
        metadata={},
    )

    assert summary == "task.status_changed · task 5 in demo · ok · by worker for demo"
