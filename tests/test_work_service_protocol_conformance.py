"""Conformance test for the :class:`WorkService` protocol (#796).

Locks the published protocol to the same set of keyword arguments
that the concrete ``PgWorkService`` and ``MockWorkService``
implementations accept on the methods CLI/runtime callers exercise.
The pre-fix protocol omitted ``skip_gates`` (queue/claim/node_done/
approve), ``created_by`` (create), and ``entry_type`` (add_context/
get_context) — a third-party service satisfying the protocol could
silently reject those calls. This test is the contract that keeps
the surface honest going forward.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pollypm.work.mock_service import MockWorkService
from pollypm.work.pg_service import PgWorkService
from pollypm.work.service import WorkService


# Methods + the parameter names that must appear in the protocol AND
# in every shipped concrete implementation. Update both sides at once
# when adding a new optional flag.
_REQUIRED_PARAMETERS: dict[str, set[str]] = {
    # ``kind`` (#1565) is part of the create contract so producers can
    # stamp the structured inbox-item discriminator at task creation
    # time without reaching past the work-service boundary.
    "create": {"created_by", "priority", "description", "kind"},
    "queue": {"skip_gates"},
    "claim": {"skip_gates"},
    "node_done": {"skip_gates"},
    "approve": {"skip_gates"},
    "add_context": {"entry_type"},
    "get_context": {"entry_type"},
}


def _params(cls: type, name: str) -> set[str]:
    return set(inspect.signature(getattr(cls, name)).parameters)


@pytest.mark.parametrize(
    "method_name, expected_params", sorted(_REQUIRED_PARAMETERS.items()),
)
def test_protocol_accepts_required_parameters(
    method_name: str, expected_params: set[str],
) -> None:
    """The published protocol must declare every shared parameter."""
    proto_params = _params(WorkService, method_name)
    missing = expected_params - proto_params
    assert not missing, (
        f"WorkService.{method_name} missing parameters {missing}; "
        "protocol is narrower than the contract callers depend on."
    )


@pytest.mark.parametrize(
    "impl_cls", [PgWorkService, MockWorkService],
)
@pytest.mark.parametrize(
    "method_name, expected_params", sorted(_REQUIRED_PARAMETERS.items()),
)
def test_implementations_accept_required_parameters(
    impl_cls: type, method_name: str, expected_params: set[str],
) -> None:
    """PgWorkService + MockWorkService must accept every shared parameter."""
    impl_params = _params(impl_cls, method_name)
    missing = expected_params - impl_params
    assert not missing, (
        f"{impl_cls.__name__}.{method_name} missing parameters {missing}; "
        "implementation is narrower than the published protocol."
    )


def test_mock_update_allows_assignee_and_external_refs(tmp_path) -> None:
    """Mock + pg must accept the same ``update()`` field set (#2064 round-5).

    The pg backend was widened in round 1 to accept ``assignee`` and
    ``external_refs`` so the API's ``POST /reassign`` and
    ``PATCH metadata`` could share a single writer. The mock side had
    not been mirrored, so any test or consumer that swapped in
    ``MockWorkService`` hit ``ValidationError: Field '<x>' is not
    updatable`` for a contract pg + the web API both honored. This
    regression locks the surface so a future drift fails loudly.
    """
    svc = MockWorkService(project_path=tmp_path)
    task = svc.create(
        title="reassign-target",
        type="task",
        project="demo",
        flow_template="plan_project",
        roles={"architect": "architect"},
    )

    # assignee — set, then clear via None.
    updated = svc.update(task.task_id, assignee="nora")
    assert updated.assignee == "nora"
    cleared = svc.update(task.task_id, assignee=None)
    assert cleared.assignee is None

    # external_refs — replace, then clear via empty dict.
    refs = {"github": "issue#2064", "jira": "PPM-1"}
    updated = svc.update(task.task_id, external_refs=refs)
    assert updated.external_refs == refs
    cleared = svc.update(task.task_id, external_refs={})
    assert cleared.external_refs == {}

    # Combined write — same body shape the web API's PATCH path emits.
    updated = svc.update(
        task.task_id,
        assignee="olga",
        external_refs={"slack": "thread/abc"},
    )
    assert updated.assignee == "olga"
    assert updated.external_refs == {"slack": "thread/abc"}


def _create_mock_task(
    svc: MockWorkService,
    *,
    project: str = "demo",
    title: str = "inbox-ish task",
):
    return svc.create(
        title=title,
        type="task",
        project=project,
        flow_template="plan_project",
        roles={"architect": "architect"},
    )


def test_mock_list_replies_returns_reply_context_oldest_first(tmp_path) -> None:
    svc = MockWorkService(project_path=tmp_path)
    task = _create_mock_task(svc)

    svc.add_context(task.task_id, "system", "hidden note", entry_type="note")
    svc.add_context(task.task_id, "user", "first", entry_type="reply")
    svc.add_context(task.task_id, "user", "second", entry_type="reply")

    replies = svc.list_replies(task.task_id)

    assert [entry.text for entry in replies] == ["first", "second"]
    assert {entry.entry_type for entry in replies} == {"reply"}


def test_mock_bulk_list_replies_buckets_project_replies(tmp_path) -> None:
    svc = MockWorkService(project_path=tmp_path)
    first = _create_mock_task(svc, project="demo", title="first")
    second = _create_mock_task(svc, project="demo", title="second")
    other = _create_mock_task(svc, project="other", title="other")

    svc.add_context(first.task_id, "user", "first-a", entry_type="reply")
    svc.add_context(first.task_id, "user", "first-b", entry_type="reply")
    svc.add_context(second.task_id, "system", "note", entry_type="note")
    svc.add_context(second.task_id, "user", "second-a", entry_type="reply")
    svc.add_context(other.task_id, "user", "other-a", entry_type="reply")

    replies_by_number = svc.bulk_list_replies(project="demo")

    assert sorted(replies_by_number) == [first.task_number, second.task_number]
    assert [entry.text for entry in replies_by_number[first.task_number]] == [
        "first-a",
        "first-b",
    ]
    assert [entry.text for entry in replies_by_number[second.task_number]] == [
        "second-a",
    ]


def test_mock_latest_snoozes_bulk_feeds_web_api_snooze_filter(
    tmp_path,
) -> None:
    from pollypm.web_api.service import _active_snoozed_ids

    now = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    svc = MockWorkService(project_path=tmp_path)
    active = _create_mock_task(svc, title="active snooze")
    expired = _create_mock_task(svc, title="expired latest snooze")
    unsnoozed = _create_mock_task(svc, title="unsnoozed")

    past = (now - timedelta(hours=1)).isoformat()
    future = (now + timedelta(hours=1)).isoformat()
    svc.add_context(
        active.task_id,
        "user",
        f"until_iso={past}; older expired snooze",
        entry_type="snooze",
    )
    svc.add_context(
        active.task_id,
        "user",
        f"until_iso={future}; latest future snooze",
        entry_type="snooze",
    )
    svc.add_context(
        expired.task_id,
        "user",
        f"until_iso={future}; older future snooze",
        entry_type="snooze",
    )
    svc.add_context(
        expired.task_id,
        "user",
        f"until_iso={past}; latest expired snooze",
        entry_type="snooze",
    )

    snoozed = _active_snoozed_ids(
        svc, [active, expired, unsnoozed], now=now,
    )

    assert snoozed == {active.task_id}


def test_concrete_impls_carry_every_protocol_method() -> None:
    """``PgWorkService`` and ``MockWorkService`` must implement every
    method named on ``WorkService`` — guards regressions where a method
    is removed from one impl but not the other. Plain ``isinstance``
    won't work because ``WorkService`` isn't ``@runtime_checkable``.
    """
    proto_methods = {
        name for name, value in inspect.getmembers(WorkService)
        if not name.startswith("_") and callable(value)
    }
    for impl_cls in (PgWorkService, MockWorkService):
        impl_methods = {
            name for name, value in inspect.getmembers(impl_cls)
            if not name.startswith("_") and callable(value)
        }
        missing = proto_methods - impl_methods
        assert not missing, (
            f"{impl_cls.__name__} missing protocol methods: {sorted(missing)}"
        )
