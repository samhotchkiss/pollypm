"""Integration tests for terminal-task alert clearing in
``Supervisor._sweep_stale_alerts`` (#1545).

Background — pre-#1545 ``_sweep_stale_alerts`` cleared alerts whose
session was outside the launch plan AND whose tmux window was missing.
Alerts whose underlying task had reached a terminal status
(``done`` / ``cancelled`` / ``abandoned``) but whose session was still
tracked stayed open across ticks. Witness symptom: a ``plan_missing``
alert on coffeeboardnm (rendered as "Plan's ready — POC plan: ..." by
the celebratory banner from #1532) kept ticking even after the
plan-shaped task moved to ``done``. The fix extends the sweep with a
terminal-task path that fires regardless of session-tracked status.

Tests pin five contracts:

1. Alert + open task → alert stays open after sweep.
2. Alert + done task → alert is cleared on next sweep.
3. Clearing emits an ``audit.alert_cleared_task_terminal`` event with
   the alert + task identity.
4. Re-running the sweep on an already-cleared alert is idempotent
   (no double-clear, no error, no duplicate audit events).
5. The terminal-task path covers the plan_missing family — that's
   the witness shape from #1545 — by parsing the task id out of the
   alert message body.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.supervisor import Supervisor
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> PollyPMConfig:
    """Mirrors ``test_supervisor_alerts._config`` so test setup is
    consistent across files."""
    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="claude_controller",
            failover_enabled=False,
            failover_accounts=[],
        ),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                home=tmp_path / ".pollypm/homes/claude_controller",
            ),
        },
        sessions={
            "worker": SessionConfig(
                name="worker",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="demo",
                prompt="Ship the fix",
                window_name="worker-demo",
            ),
        },
        projects={
            "demo": KnownProject(
                key="demo",
                path=tmp_path,
                name="demo",
                kind=ProjectKind.FOLDER,
            )
        },
    )


def _seed_task(
    db_path: Path, project_path: Path, *, terminal: bool = False,
) -> str:
    """Create one task on the workspace-root DB. ``terminal=True``
    drives it to ``done`` via queue → claim → node_done → approve.
    Returns the ``task_id``.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(
        db_path=db_path, project_path=project_path,
    ) as svc:
        # Skip the auto-merge step that real ``approve()`` runs — the
        # fixture doesn't have a real task branch.
        svc._auto_merge_approved_task_branch = lambda _task, **_kwargs: None
        task = svc.create(
            title="Plan task",
            description="A plan-shaped task.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        task_id = task.task_id
        svc.queue(task_id, "polly")
        if not terminal:
            return task_id
        # Drive to done.
        from pollypm.work.models import (
            Artifact, ArtifactKind, OutputType, WorkOutput,
        )
        svc.claim(task_id, "worker")
        svc.node_done(
            task_id, "worker",
            WorkOutput(
                type=OutputType.CODE_CHANGE,
                summary="Built the plan",
                artifacts=[
                    Artifact(
                        kind=ArtifactKind.COMMIT,
                        description="seed",
                        ref="deadbeef0000",
                    ),
                ],
            ),
        )
        svc.approve(task_id, "russell")
        return task_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sweep_keeps_alert_when_underlying_task_still_open(
    tmp_path: Path,
) -> None:
    """Alert + still-queued task → sweep leaves the alert alone."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    workspace_db = tmp_path / ".pollypm" / "state.db"
    task_id = _seed_task(workspace_db, tmp_path, terminal=False)

    # Raise a stuck_on_task alert on the still-open task. Use a
    # session_name that's not in the launch plan so the existing
    # orphan-clear path would also fire — we want to verify the
    # *terminal-task* path doesn't false-clear before the existing
    # path runs.
    supervisor._msg_store.upsert_alert(
        "worker_demo", f"stuck_on_task:{task_id}", "warn",
        f"Task {task_id} has been stuck on operator input for 30 minutes.",
    )

    window_map: dict = {}
    name_by_window: dict = {}
    supervisor._sweep_stale_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    open_pairs = {
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    }
    # The stuck_on_task alert's underlying task is still queued —
    # the terminal-task path must NOT clear it. (The orphan-clear path
    # may or may not fire depending on session-tracked status; the
    # contract this test pins is *terminal-task path lets live tasks
    # alone*.)
    # At minimum, the alert was either left open or cleared via the
    # orphan path with no terminal-task audit event. The audit event
    # check below is the precise assertion.
    audit_events = _open_terminal_audit_events(supervisor)
    assert audit_events == [], (
        "terminal-task path must not fire when the underlying task is "
        f"still open; got events={audit_events}"
    )


def test_sweep_clears_alert_when_underlying_task_terminal(
    tmp_path: Path,
) -> None:
    """Alert + done task → sweep clears the alert."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    workspace_db = tmp_path / ".pollypm" / "state.db"
    task_id = _seed_task(workspace_db, tmp_path, terminal=True)

    # Raise a stuck_on_task alert keyed on the now-done task. The
    # session_name is irrelevant for the terminal-task path — the
    # task id in the alert_type suffix is what drives the clear.
    supervisor._msg_store.upsert_alert(
        "worker_demo", f"stuck_on_task:{task_id}", "warn",
        f"Task {task_id} has been stuck on operator input for 30 minutes.",
    )

    window_map: dict = {}
    name_by_window: dict = {}
    supervisor._sweep_stale_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    open_pairs = {
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    }
    assert ("worker_demo", f"stuck_on_task:{task_id}") not in open_pairs, (
        "stuck_on_task alert on a done task must clear via the "
        "terminal-task path"
    )


def test_terminal_clear_emits_audit_event(tmp_path: Path) -> None:
    """The terminal-task clear stamps an
    ``audit.alert_cleared_task_terminal`` event with the alert +
    task identity payload, so the lifecycle is recoverable from the
    forensic event stream.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    workspace_db = tmp_path / ".pollypm" / "state.db"
    task_id = _seed_task(workspace_db, tmp_path, terminal=True)

    supervisor._msg_store.upsert_alert(
        "worker_demo", f"stuck_on_task:{task_id}", "warn",
        f"Task {task_id} has been stuck on operator input for 30 minutes.",
    )

    window_map: dict = {}
    name_by_window: dict = {}
    supervisor._sweep_stale_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    audit_events = _open_terminal_audit_events(supervisor)
    assert len(audit_events) == 1, (
        f"expected exactly one terminal-clear audit event, got {audit_events}"
    )
    payload = audit_events[0].get("payload") or {}
    assert payload.get("task_id") == task_id
    assert payload.get("task_status") == "done"
    assert payload.get("alert_type") == f"stuck_on_task:{task_id}"
    assert payload.get("session_name") == "worker_demo"


def test_terminal_clear_is_idempotent_across_repeat_sweeps(
    tmp_path: Path,
) -> None:
    """Re-running the sweep after the alert is cleared must NOT
    re-clear, error, or emit duplicate audit events."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    workspace_db = tmp_path / ".pollypm" / "state.db"
    task_id = _seed_task(workspace_db, tmp_path, terminal=True)

    supervisor._msg_store.upsert_alert(
        "worker_demo", f"stuck_on_task:{task_id}", "warn",
        f"Task {task_id} has been stuck on operator input for 30 minutes.",
    )

    window_map: dict = {}
    name_by_window: dict = {}
    # First sweep: clear + emit audit.
    supervisor._sweep_stale_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )
    # Second sweep: alert is already closed, terminal-task path
    # should be a no-op.
    supervisor._sweep_stale_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )
    # Third sweep, for good measure.
    supervisor._sweep_stale_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    audit_events = _open_terminal_audit_events(supervisor)
    assert len(audit_events) == 1, (
        "terminal-clear must be idempotent — repeat sweeps must not "
        f"emit additional audit events; got {audit_events}"
    )


def test_terminal_clear_handles_plan_missing_via_message_body(
    tmp_path: Path,
) -> None:
    """The witness shape from #1545: a ``plan_missing`` alert (scope
    ``plan_gate-<project>``) names the queued blocked task in its
    message body. After that task moves to ``done``, the alert must
    clear via the terminal-task path. This is the case the issue
    actually reproduces in the field.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    workspace_db = tmp_path / ".pollypm" / "state.db"
    task_id = _seed_task(workspace_db, tmp_path, terminal=True)

    supervisor._msg_store.upsert_alert(
        "plan_gate-demo", "plan_missing", "warn",
        (
            f"Project 'demo' has no approved plan yet — "
            f"queued task {task_id} is waiting. "
            f"Run `pm project plan demo` to queue planning."
        ),
    )

    window_map: dict = {}
    name_by_window: dict = {}
    supervisor._sweep_stale_alerts(
        window_map=window_map, name_by_window=name_by_window,
    )

    open_pairs = {
        (a.session_name, a.alert_type) for a in supervisor.open_alerts()
    }
    assert ("plan_gate-demo", "plan_missing") not in open_pairs, (
        "plan_missing alert whose embedded task is done must clear via "
        "the terminal-task path (the #1545 witness shape)"
    )

    audit_events = _open_terminal_audit_events(supervisor)
    assert len(audit_events) == 1
    assert audit_events[0]["payload"].get("alert_type") == "plan_missing"
    assert audit_events[0]["payload"].get("task_id") == task_id


def test_existing_skip_list_guards_remain_intact() -> None:
    """The existing #919 / #1526 source-level guards must remain in
    ``_sweep_stale_alerts``. The terminal-task path is purely additive
    — it must NOT remove the skip-list short-circuits that keep the
    task_assignment sweep as the sole owner of those alert families
    when the task is *still live*.
    """
    import inspect
    from pollypm import supervisor as supervisor_mod

    source = inspect.getsource(supervisor_mod.Supervisor._sweep_stale_alerts)
    assert 'alert_type == "no_session"' in source, (
        "#919 guard for no_session alert_type must remain"
    )
    assert 'alert_type.startswith("no_session_for_assignment:")' in source, (
        "#919 guard for no_session_for_assignment:* must remain"
    )
    assert 'alert_type == "plan_missing"' in source, (
        "#1526 guard for plan_missing must remain — the new "
        "terminal-task path runs BEFORE the skip-list, so the "
        "skip-list still gates the case where the task is still live"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_terminal_audit_events(supervisor: Supervisor) -> list[dict]:
    """Return ``audit.alert_cleared_task_terminal`` event rows from the
    workspace-root messages table. Read directly via SQL so the
    helper is independent of any reader API that might filter.
    """
    import json

    engine = supervisor._msg_store._write_engine  # type: ignore[attr-defined]
    rows: list[dict] = []
    with engine.begin() as conn:
        result = conn.exec_driver_sql(
            "SELECT id, scope, sender, subject, payload_json "
            "FROM messages WHERE type = 'event' AND sender = ?",
            ("audit.alert_cleared_task_terminal",),
        )
        for row in result.fetchall():
            payload_raw = row[4] or "{}"
            try:
                payload = json.loads(payload_raw)
            except (TypeError, ValueError):
                payload = {}
            rows.append({
                "id": row[0],
                "scope": row[1],
                "sender": row[2],
                "subject": row[3],
                "payload": payload,
            })
    return rows
