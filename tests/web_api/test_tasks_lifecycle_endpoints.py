"""Tests for the lifecycle REST endpoints — done / approve / hold /
rework / block / review / in_progress (#2137).

Each endpoint mirrors the existing ``/claim`` / ``/cancel`` shape:
``TaskActionResult`` envelope on success, 409 ``invalid_state`` on
illegal transitions, 422 on validation failures, 404 on unknown
project/task. Tests pin the happy path + an illegal-transition case
per verb so future drift trips here instead of in operator
bug reports.

The pg_schema_pool fixture (from ``tests/conftest_pg.py``) provides
per-test pg schema isolation so each test starts with empty
work_tasks / work_node_executions / work_transitions tables. Without
it tests would accumulate state on the dev machine's real
``pollypm`` pg DB.
"""

from __future__ import annotations

import pytest

from pollypm.work.factory import create_work_service

from .conftest import make_task


pytestmark = pytest.mark.usefixtures("pg_schema_pool")


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _seed_task_in_state(api_config, project_root, *, state: str):
    """Create one task and drive it to ``state`` via work-service calls.

    For ``in_progress`` / ``review`` we drive through the real
    state machine (``svc.claim`` then ``svc.node_done``) so the
    current_node_id is wired correctly for downstream verbs like
    ``/approve`` / ``/rework`` that inspect the active flow node.
    The ``force_*`` bypasses are intentionally NOT used here — they
    leave current_node_id null which breaks role validation.
    """
    db_path = api_config.project.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with create_work_service(db_path=db_path, project_path=project_root) as svc:
        task = make_task(svc, project="myproj", title=f"Task in {state}")
        if state == "draft":
            return task
        svc.queue(task.task_id, actor="tester")
        if state == "queued":
            return svc.get(task.task_id)
        # ``svc.claim`` activates the flow's first work node and sets
        # current_node_id; the actor must match the worker role
        # ("agent-1") that ``make_task`` wires by default.
        svc.claim(task.task_id, actor="agent-1")
        if state == "in_progress":
            return svc.get(task.task_id)
        if state == "review":
            # Advance the worker node so the task lands on the review
            # node with current_node_id pointing at it.
            svc.node_done(
                task.task_id,
                actor="agent-1",
                work_output={
                    "type": "code_change",
                    "summary": "test fixture",
                    "artifacts": [
                        {
                            "kind": "commit",
                            "description": "fixture",
                            "ref": "HEAD",
                        }
                    ],
                },
            )
            return svc.get(task.task_id)
        if state == "on_hold":
            svc.hold(task.task_id, actor="tester")
            return svc.get(task.task_id)
        if state == "done":
            svc.mark_done(task.task_id, actor="tester")
            return svc.get(task.task_id)
        if state == "cancelled":
            svc.cancel(task.task_id, actor="tester", reason="test setup")
            return svc.get(task.task_id)
        raise ValueError(f"unsupported test state: {state}")


# ---------------------------------------------------------------------------
# /done
# ---------------------------------------------------------------------------


def test_done_happy_path(api_config, client, auth_headers, project_root) -> None:
    """Force-done a queued task — work_status becomes ``done``."""
    task = _seed_task_in_state(api_config, project_root, state="queued")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/done",
        json={"actor": "operator"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["work_status"] == "done"


def test_done_illegal_from_terminal(
    api_config, client, auth_headers, project_root
) -> None:
    """Calling /done on a cancelled task returns 409 invalid_state, not 500."""
    task = _seed_task_in_state(api_config, project_root, state="cancelled")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/done",
        json={"actor": "operator"},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "invalid_state"


# ---------------------------------------------------------------------------
# /approve
# ---------------------------------------------------------------------------


def test_approve_happy_path(
    api_config, client, auth_headers, project_root
) -> None:
    """Approving a review-state task transitions it (status reflects the
    flow's next node — done or another review)."""
    task = _seed_task_in_state(api_config, project_root, state="review")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/approve",
        json={"actor": "reviewer", "reason": "looks good"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    # Approve drives the task past review; the exact next status
    # depends on the standard flow's review node configuration. Pin
    # only the "moved past review" invariant — not the exact target.
    assert body["task"]["work_status"] != "review"


def test_approve_illegal_from_queued(
    api_config, client, auth_headers, project_root
) -> None:
    """Calling /approve on a non-review task returns 409 invalid_state."""
    task = _seed_task_in_state(api_config, project_root, state="queued")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/approve",
        json={"actor": "reviewer"},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "invalid_state"


# ---------------------------------------------------------------------------
# /hold
# ---------------------------------------------------------------------------


def test_hold_happy_path(api_config, client, auth_headers, project_root) -> None:
    """Holding a queued task transitions it to on_hold."""
    task = _seed_task_in_state(api_config, project_root, state="queued")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/hold",
        json={"actor": "operator", "reason": "waiting for budget"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["work_status"] == "on_hold"


def test_hold_illegal_from_draft(
    api_config, client, auth_headers, project_root
) -> None:
    """Holding a draft task returns 409 invalid_state."""
    task = _seed_task_in_state(api_config, project_root, state="draft")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/hold",
        json={"actor": "operator"},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "invalid_state"


# ---------------------------------------------------------------------------
# /rework
# ---------------------------------------------------------------------------


def test_rework_happy_path(
    api_config, client, auth_headers, project_root
) -> None:
    """Rejecting a review-state task bounces it back to rework."""
    task = _seed_task_in_state(api_config, project_root, state="review")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/rework",
        json={"actor": "reviewer", "reason": "needs more tests"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["work_status"] == "rework"


def test_rework_illegal_from_queued(
    api_config, client, auth_headers, project_root
) -> None:
    """Rework on a non-review task returns 409 invalid_state."""
    task = _seed_task_in_state(api_config, project_root, state="queued")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/rework",
        json={"actor": "reviewer", "reason": "wrong state"},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "invalid_state"


def test_rework_missing_reason_422(
    api_config, client, auth_headers, project_root
) -> None:
    """Missing ``reason`` fails at the request validator with 422."""
    task = _seed_task_in_state(api_config, project_root, state="review")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/rework",
        json={"actor": "reviewer"},
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# /block
# ---------------------------------------------------------------------------


def test_block_happy_path(api_config, client, auth_headers, project_root) -> None:
    """Blocking an in_progress task by another task flips it to blocked."""
    blocker = _seed_task_in_state(api_config, project_root, state="queued")
    task = _seed_task_in_state(api_config, project_root, state="in_progress")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/block",
        json={"actor": "operator", "blocker_task_id": blocker.task_id},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["work_status"] == "blocked"


def test_block_illegal_from_queued(
    api_config, client, auth_headers, project_root
) -> None:
    """Block refuses non-(in_progress | review) states with 409."""
    blocker = _seed_task_in_state(api_config, project_root, state="queued")
    task = _seed_task_in_state(api_config, project_root, state="queued")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/block",
        json={"actor": "operator", "blocker_task_id": blocker.task_id},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "invalid_state"


# ---------------------------------------------------------------------------
# /review
# ---------------------------------------------------------------------------


def test_review_happy_path(
    api_config, client, auth_headers, project_root
) -> None:
    """Forcing an in_progress task to review transitions to ``review``."""
    task = _seed_task_in_state(api_config, project_root, state="in_progress")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/review",
        json={"actor": "operator"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["work_status"] == "review"


def test_review_illegal_from_draft(
    api_config, client, auth_headers, project_root
) -> None:
    """Forcing review on a draft task returns 409 invalid_state."""
    task = _seed_task_in_state(api_config, project_root, state="draft")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/review",
        json={"actor": "operator"},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "invalid_state"


# ---------------------------------------------------------------------------
# /in_progress
# ---------------------------------------------------------------------------


def test_in_progress_happy_path(
    api_config, client, auth_headers, project_root
) -> None:
    """Forcing a queued task to in_progress transitions to in_progress."""
    task = _seed_task_in_state(api_config, project_root, state="queued")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/in_progress",
        json={"actor": "operator"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["work_status"] == "in_progress"


def test_in_progress_illegal_from_done(
    api_config, client, auth_headers, project_root
) -> None:
    """In-progress override refuses terminal tasks with 409."""
    task = _seed_task_in_state(api_config, project_root, state="done")
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/in_progress",
        json={"actor": "operator"},
        headers=auth_headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "invalid_state"


# ---------------------------------------------------------------------------
# Cross-cutting — auth + unknown-project
# ---------------------------------------------------------------------------


def test_lifecycle_endpoints_require_auth(client) -> None:
    """Every new verb honors the same bearer-auth gate as /claim."""
    for verb in (
        "done",
        "approve",
        "hold",
        "rework",
        "block",
        "review",
        "in_progress",
    ):
        response = client.post(
            f"/api/v1/tasks/myproj/1/{verb}",
            json={"actor": "operator", "reason": "x", "blocker_task_id": "x/1"},
        )
        assert response.status_code == 401, (verb, response.text)


# ---------------------------------------------------------------------------
# /reopen — claim-cleanup regression (#2220)
# ---------------------------------------------------------------------------


def test_reopen_clears_claimed_by_session(
    api_config, client, auth_headers, project_root
) -> None:
    """A reopened task must NULL out claimed_by_session (#2220).

    Black-box session 2026-05-24 found a cancel+reopen cycle left the
    queued row carrying the prior worker's session id. The next claim
    attempt could then trip "already claimed" guards on a task the
    operator just explicitly re-queued. Pin the cleanup on the reopen
    path so the bug can't regress.
    """
    # Seed a cancelled task whose flow has been claimed once — that's
    # what writes claimed_by_session in the first place. The shared
    # ``_seed_task_in_state(..., state="cancelled")`` helper does:
    # queue -> claim (claimed_by_session="agent-1") -> cancel.
    task = _seed_task_in_state(api_config, project_root, state="cancelled")
    assert task.claimed_by_session is not None, (
        "test precondition: cancel-from-in-progress should leave "
        "claimed_by_session set so reopen has something to clear"
    )

    # ``TaskReopenRequest`` is reason-only (no ``actor``); the route
    # supplies actor="api" itself.
    response = client.post(
        f"/api/v1/tasks/myproj/{task.task_number}/reopen",
        json={"reason": "rerun"},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["task"]["work_status"] == "queued"
    assert body["task"]["claimed_by_session"] is None, (
        "claimed_by_session must be cleared on reopen (#2220), got "
        f"{body['task']['claimed_by_session']!r}"
    )


def test_lifecycle_endpoints_unknown_project_404(client, auth_headers) -> None:
    """Unknown project key surfaces as 404 with the standard envelope."""
    for verb in (
        "done",
        "approve",
        "hold",
        "review",
        "in_progress",
    ):
        response = client.post(
            f"/api/v1/tasks/nosuchproject/1/{verb}",
            json={"actor": "operator"},
            headers=auth_headers,
        )
        assert response.status_code == 404, (verb, response.text)
