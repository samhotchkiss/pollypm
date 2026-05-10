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
    """Alert + still-queued task → sweep leaves the alert alone.

    PR #1560 codex-review MED-1 — the assertion must positively pin
    "alert remains open", not just "no terminal-task audit fired".
    Using the ``plan_missing`` family keeps the existing orphan-clear
    path (which fires on any non-tracked session) from clearing the
    alert independently of the terminal-task path under test: the
    #1526 skip-list excludes ``plan_missing`` from orphan-clear, so
    a still-open alert here proves the terminal-task path itself
    let the live task alone.
    """
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    workspace_db = tmp_path / ".pollypm" / "state.db"
    task_id = _seed_task(workspace_db, tmp_path, terminal=False)

    # Raise a plan_missing alert that names the still-queued task.
    # plan_missing is skip-listed in _sweep_stale_alerts (#1526) so
    # the existing orphan-clear path won't false-pass this test.
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
    # Positive assertion: the alert is still open. The terminal-task
    # path must NOT clear it because the underlying task is still
    # queued (live).
    assert ("plan_gate-demo", "plan_missing") in open_pairs, (
        "terminal-task path must leave alerts alone while the "
        f"underlying task is still live; open_pairs={open_pairs}"
    )

    # Independent assertion: no terminal-task audit was emitted —
    # the alert wasn't cleared via the terminal-task path.
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


def _alias_config(tmp_path: Path) -> tuple[PollyPMConfig, Path, str]:
    """Build a config where the project is registered under a slug
    config key (``health_coach``) but its on-disk directory / display
    name uses the hyphenated alias form (``health-coach``).

    The work-service stores ``project`` columns under whatever the
    caller passes — which is the alias-form ``health-coach`` for
    tasks created by the per-project tooling, not the underscore
    config key. Returns ``(config, project_path, alias_project)``.
    """
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    project_path = tmp_path / "projects" / "health-coach"
    project_path.mkdir(parents=True, exist_ok=True)

    config_key = "health_coach"
    alias_project = "health-coach"

    config = PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=workspace_root,
            workspace_root=workspace_root,
            base_dir=workspace_root / ".pollypm",
            logs_dir=workspace_root / ".pollypm/logs",
            snapshots_dir=workspace_root / ".pollypm/snapshots",
            state_db=workspace_root / ".pollypm/state.db",
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
                home=workspace_root / ".pollypm/homes/claude_controller",
            ),
        },
        sessions={},
        projects={
            config_key: KnownProject(
                key=config_key,
                path=project_path,
                # name carries the alias form so project_storage_aliases
                # surfaces ``health-coach`` for the slug key.
                name=alias_project,
                kind=ProjectKind.FOLDER,
            ),
        },
    )
    return config, project_path, alias_project


def test_terminal_clear_resolves_alias_named_project_in_legacy_db(
    tmp_path: Path,
) -> None:
    """Regression for PR #1560 codex-review HIGH-1.

    Before the fix, ``_lookup_task_status`` looked up
    ``self.config.projects.get(<task-id project segment>)`` using the
    literal project segment from the alert's task id. When that
    segment is the storage-form alias (``health-coach``) but the
    config key is the slug (``health_coach``), the lookup returned
    None and the per-project legacy DB candidate
    (``<project_path>/.pollypm/state.db``) was never added — so the
    terminal task was unreachable and the stale alert stayed open
    forever. The fix walks ``project_storage_aliases`` to recover
    the configured project from the alias form.

    Test topology:

    * config key:                  ``health_coach`` (slug)
    * project ``name`` / on-disk:  ``health-coach`` (alias)
    * workspace_root DB:           empty / does not exist
    * per-project legacy DB:       ``<project_path>/.pollypm/state.db``
      with a terminal ``health-coach/N`` task
    * alert references             ``health-coach/N``
    """
    config, project_path, alias_project = _alias_config(tmp_path)
    supervisor = Supervisor(config)
    supervisor.ensure_layout()

    # Seed the terminal task ONLY in the per-project legacy DB, under
    # the alias-form project. The workspace-root DB is left untouched
    # so the only way for the lookup to find the task is via the
    # per-project candidate path — which requires resolving the
    # ``health-coach`` task-id segment back to the ``health_coach``
    # config entry.
    legacy_db = project_path / ".pollypm" / "state.db"
    legacy_db.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(
        db_path=legacy_db, project_path=project_path,
    ) as svc:
        svc._auto_merge_approved_task_branch = lambda _task, **_kwargs: None
        task = svc.create(
            title="Plan task",
            description="A plan-shaped task.",
            type="task",
            project=alias_project,  # storage form, not the slug
            flow_template="standard",
            roles={"worker": "pete", "reviewer": "russell"},
            priority="normal",
            created_by="polly",
        )
        task_id = task.task_id
        assert task_id.startswith(f"{alias_project}/"), (
            "test fixture invariant: task_id must carry the alias-form "
            f"project segment; got {task_id}"
        )
        from pollypm.work.models import (
            Artifact, ArtifactKind, OutputType, WorkOutput,
        )
        svc.queue(task_id, "polly")
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

    # Pre-flight assertion: the lookup itself must resolve the
    # alias-form project segment to ``done``. This pins the HIGH-1
    # fix at the unit level so a future regression in
    # ``_resolve_project_by_alias`` shows up before the integration
    # surface masks it.
    status = supervisor._lookup_task_status(alias_project, task_id)
    assert status == "done", (
        "alias-form project segment must resolve to the per-project "
        f"legacy DB and return work_status='done'; got {status!r}"
    )

    # Integration assertion: an alert keyed on the alias-form task id
    # must clear via the terminal-task path. Use ``plan_missing`` so
    # the orphan-clear path is skip-listed and can't false-pass.
    supervisor._msg_store.upsert_alert(
        f"plan_gate-{alias_project}", "plan_missing", "warn",
        (
            f"Project '{alias_project}' has no approved plan yet — "
            f"queued task {task_id} is waiting."
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
    assert (f"plan_gate-{alias_project}", "plan_missing") not in open_pairs, (
        "alert on alias-named project (config key differs from task-id "
        "project segment) must clear via the terminal-task path once "
        "the underlying task is terminal in the per-project legacy DB"
    )

    audit_events = _open_terminal_audit_events(supervisor)
    assert len(audit_events) == 1
    assert audit_events[0]["payload"].get("task_id") == task_id
    assert audit_events[0]["payload"].get("task_status") == "done"


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
