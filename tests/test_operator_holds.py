from __future__ import annotations

from types import SimpleNamespace

from pollypm.cockpit_inbox_items import InboxEntry, _triage_for_entry
from pollypm.cockpit_ui import PollyProjectDashboardApp
from pollypm.operator_holds import (
    ARCHITECT_ACTIONABLE_TAG,
    HUMAN_NEEDED_TAG,
    classify_on_hold_reason,
    human_needed_hold_prompt,
)


def test_human_needed_hold_parser_strips_routing_prefix() -> None:
    reason = "[human-needed] external credentials only S.E. can supply"

    assert classify_on_hold_reason(reason) == HUMAN_NEEDED_TAG
    assert human_needed_hold_prompt(reason) == (
        "external credentials only S.E. can supply"
    )
    assert classify_on_hold_reason("plain reviewer issue") == (
        ARCHITECT_ACTIONABLE_TAG
    )


def test_human_needed_on_hold_task_triages_as_operator_action() -> None:
    class Status:
        value = "on_hold"

    class Transition:
        to_state = "on_hold"
        reason = "human-needed: provide the production deploy token"

    item = InboxEntry(
        source="task",
        project="savethenovel",
        title="Deploy ready",
        description="",
        work_status=Status(),
        transitions=[Transition()],
    )

    assert _triage_for_entry(item, known_projects={"savethenovel"}) == (
        "action",
        0,
        "ready except for you",
    )


def test_project_banner_names_ready_except_for_you_hold() -> None:
    app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
    data = SimpleNamespace(
        task_counts={"on_hold": 1},
        task_buckets={
            "on_hold": [
                {
                    "task_number": 94,
                    "title": "Deploy smoke test",
                    "hold_reason": (
                        "[human-needed] external credentials only S.E. can supply"
                    ),
                }
            ],
            "review": [],
        },
        alert_count=0,
        active_worker=None,
    )

    banner = app._banner_on_hold(data)

    assert banner.startswith("Ready except for you:")
    assert "task #94: Deploy smoke test needs your input" in banner
    assert "external credentials only S.E. can supply" in banner
