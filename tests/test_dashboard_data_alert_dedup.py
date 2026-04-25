"""Polly-dashboard ``alert_count`` mirrors the cycle 45/53/55 dedup.

When ``stuck_on_task:<id>`` fires because the architect session sat
idle waiting for the user to respond and the task is already in a
user-waiting status, the alert is the same fact in different words.
The polly dashboard's ``alert_count`` is what drives "1 alerts" in
the top stats line; counting redundant stuck alerts there inflates
the badge for non-faults the user already sees as yellow.
"""

from __future__ import annotations

from pollypm.dashboard_data import _stuck_alert_already_user_waiting


def test_stuck_alert_already_user_waiting_filters_when_task_is_waiting() -> None:
    assert _stuck_alert_already_user_waiting(
        "stuck_on_task:polly_remote/12",
        frozenset({"polly_remote/12"}),
    )


def test_stuck_alert_already_user_waiting_keeps_alert_for_other_tasks() -> None:
    assert not _stuck_alert_already_user_waiting(
        "stuck_on_task:polly_remote/9",
        frozenset({"polly_remote/12"}),
    )


def test_stuck_alert_already_user_waiting_only_handles_stuck_prefix() -> None:
    assert not _stuck_alert_already_user_waiting(
        "no_session_for_assignment:polly_remote/12",
        frozenset({"polly_remote/12"}),
    )
    assert not _stuck_alert_already_user_waiting("", frozenset())


def test_stuck_alert_already_user_waiting_handles_malformed_alert() -> None:
    assert not _stuck_alert_already_user_waiting(
        "stuck_on_task:",
        frozenset({"polly_remote/12"}),
    )
    assert not _stuck_alert_already_user_waiting(
        "stuck_on_task:   ",
        frozenset({"polly_remote/12"}),
    )
