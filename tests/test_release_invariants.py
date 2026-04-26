"""Focused tests for the release-invariants user_prompt contract check.

The release-invariants script is a v1 burn-in harness rather than
product code, but the user_prompt-contract check is a forward-pressure
signal for the dashboard contract migration — losing it silently
would let regressions slip through. Test the predicates directly so
the contract semantics are pinned down even when no live workspace
is available.
"""

from __future__ import annotations

import json

from scripts.release_invariants import (
    _dashboard_body_has_action_copy,
    _message_action_requires_user_prompt,
    _user_prompt_complete,
)


def _row(**fields):
    """sqlite3.Row supports __getitem__; dict matches that interface."""
    payload = fields.pop("payload", None)
    if payload is not None:
        fields["payload_json"] = json.dumps(payload)
    fields.setdefault("recipient", "user")
    fields.setdefault("type", "notify")
    fields.setdefault("tier", "immediate")
    fields.setdefault("payload_json", "{}")
    return fields


def test_user_prompt_complete_requires_at_least_one_field() -> None:
    """``_user_prompt_complete`` is the contract-quality gate: a payload
    that has *some* user-facing copy passes; an empty/missing prompt
    does not."""
    assert _user_prompt_complete({"user_prompt": {"summary": "Plan ready"}})
    assert _user_prompt_complete(
        {"user_prompt": {"steps": ["Open the plan"]}}
    )
    assert _user_prompt_complete(
        {"user_prompt": {"question": "Approve now or wait?"}}
    )
    assert _user_prompt_complete(
        {"user_prompt": {"required_actions": ["Provision Fly.io"]}}
    )

    assert not _user_prompt_complete({})
    assert not _user_prompt_complete({"user_prompt": None})
    assert not _user_prompt_complete({"user_prompt": "not a dict"})
    assert not _user_prompt_complete({"user_prompt": {}})
    assert not _user_prompt_complete(
        {"user_prompt": {"summary": "", "steps": [], "question": ""}}
    )


def test_user_action_predicate_targets_immediate_user_notify() -> None:
    """Only open immediate-priority notify/alert messages routed to
    the user count as user-blocking action calls. Digest tier, agent
    recipients, and non-action types fall outside the contract."""
    assert _message_action_requires_user_prompt(
        _row(recipient="user", type="notify", tier="immediate")
    )
    assert _message_action_requires_user_prompt(
        _row(recipient="user", type="alert", tier="immediate")
    )

    # Digest priority is routine progress, not a call to action.
    assert not _message_action_requires_user_prompt(
        _row(recipient="user", type="notify", tier="digest")
    )
    # Notifications routed to an agent (e.g. Polly) aren't user-facing.
    assert not _message_action_requires_user_prompt(
        _row(recipient="polly", type="notify", tier="immediate")
    )
    # inbox_task routes through the work-service surface, not the
    # dashboard contract.
    assert not _message_action_requires_user_prompt(
        _row(recipient="user", type="inbox_task", tier="immediate")
    )


def test_user_action_predicate_excludes_blocker_summary_events() -> None:
    """Project blocker summaries already carry their structured copy
    via ``required_actions`` and are rendered by the dashboard's
    blocker-summary path — they shouldn't trip the user_prompt
    contract warning."""
    assert not _message_action_requires_user_prompt(
        _row(
            recipient="user",
            type="notify",
            tier="immediate",
            payload={"event_type": "project_blocker_summary"},
        )
    )


def test_dashboard_body_action_copy_canonical_lead() -> None:
    """The canonical 'To move this project forward' lead counts as action copy."""
    body = (
        "[#f85149][b]to move this project forward[/b][/]\n"
        "  ◆ A full project plan is ready for your review.\n"
    )
    assert _dashboard_body_has_action_copy(body.lower())


def test_dashboard_body_action_copy_on_hold_decision() -> None:
    """The on-hold inbox treatment + decision question counts as action copy.

    Media (2026-04-26): on_hold task with hold reason ``Awaiting user
    Phase A approval...`` rendered as ``On hold`` + ``Decide whether
    to approve…``. The strict ``to move this project forward`` rule
    flagged this as missing copy, but the user-facing decision is
    present and clear — this shape is valid action copy.
    """
    body = (
        "[#f0c45a][b]On hold[/b][/]\n"
        "  These are the root holds keeping downstream work waiting.\n"
        "  [#f0c45a]◆[/#f0c45a] [b]#1 Library-wide cleanup[/b]\n"
        "  Decide whether to approve the scoped code delivery, split "
        "operational acceptance, or provide the missing access/credentials.\n"
    )
    assert _dashboard_body_has_action_copy(body.lower())


def test_dashboard_body_action_copy_missing_when_no_signal() -> None:
    """A body with no lead, no on-hold treatment, no diagnostic still fails."""
    body = (
        "[b]Inbox[/b]\n"
        "  No project inbox items are open.\n"
        "  Recent: worker shipped task #1.\n"
    )
    assert not _dashboard_body_has_action_copy(body.lower())


def test_dashboard_body_action_copy_on_hold_alone_is_not_enough() -> None:
    """``On hold`` framing without a decision question is NOT action copy.

    The user needs to know what to do next. ``On hold`` alone — without
    ``Decide whether``, ``Decision:``, or ``needs your`` — is a status
    label, not a call to action.
    """
    body = (
        "[#f0c45a][b]On hold[/b][/]\n"
        "  These are the root holds keeping downstream work waiting.\n"
        "  ◆ #1 Library-wide cleanup\n"
        "  paused: ran out of budget for tonight, will resume tomorrow.\n"
    )
    assert not _dashboard_body_has_action_copy(body.lower())
