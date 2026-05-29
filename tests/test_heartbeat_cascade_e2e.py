"""End-to-end chaos harness for the heartbeat cascade (#2448).

The harness drives production detector/routing seams with record-only
sinks. It must never touch live tmux, real account homes, the operator's
audit directory, or the ambient production database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pollypm.audit.log import (
    EVENT_ACCOUNT_FAILOVER_PROACTIVE,
    EVENT_TIER4_BUDGET_EXHAUSTED,
    EVENT_TIER4_DEMOTED,
    EVENT_TIER4_PROMOTED,
    EVENT_WATCHDOG_ESCALATION_DISPATCHED,
    EVENT_WATCHDOG_OPERATOR_DISPATCHED,
    EVENT_WATCHDOG_OPERATOR_TIER4_DISPATCHED,
    EVENT_WATCHDOG_WORKER_LANE_FAILED,
    EVENT_WATCHDOG_WORKER_LANE_SPAWNED,
    read_events,
)
from pollypm.audit.tier4 import (
    AUTO_PROMOTE_THRESHOLD,
    TIER4_BUDGET_SECONDS,
    Tier4PromotionTracker,
)
from pollypm.audit.watchdog import (
    ESCALATION_THROTTLE_SECONDS,
    OPERATOR_DISPATCH_THROTTLE_SECONDS,
    Finding,
    RULE_ROLE_SESSION_MISSING,
    RULE_TASK_PROGRESS_STALE,
    TIER_1,
    TIER_2,
    WatchdogConfig,
)
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
from pollypm.session_auth import AUTH_MARKER_PREFIX


@pytest.fixture(autouse=True)
def _isolate_cascade_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    audit_home = tmp_path / "audit-home"
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))
    monkeypatch.setenv("POLLYPM_DISABLE_ERROR_NOTIFICATIONS", "1")
    return audit_home


@pytest.fixture
def now() -> datetime:
    return (datetime.now(timezone.utc) + timedelta(days=30)).replace(
        microsecond=0,
    )


class _StubStatus:
    def __init__(self, value: str) -> None:
        self.value = value


@dataclass
class _StubTask:
    project: str
    task_number: int
    work_status_str: str
    executions: list[Any]
    roles: dict[str, str] | None = None
    assignee: str | None = None
    updated_at: datetime | None = None
    created_at: datetime | None = None
    created_by: str | None = None
    title: str | None = None
    labels: tuple[str, ...] = ()
    kind: str | None = None

    @property
    def work_status(self) -> _StubStatus:
        return _StubStatus(self.work_status_str)


class _RecordingAlertStore:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, str, str, str]] = []

    def upsert_alert(
        self,
        scope: str,
        alert_type: str,
        severity: str,
        message: str,
    ) -> None:
        self.alerts.append((scope, alert_type, severity, message))


class _RecordingMessageStore:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def enqueue_message(self, **kwargs: Any) -> int:
        self.messages.append(kwargs)
        return len(self.messages)


class _RecordingServices:
    def __init__(
        self,
        db_path: Path,
        *,
        project_key: str = "demo",
        project_path: Path | None = None,
    ) -> None:
        from pollypm.storage.state import StateStore

        self.state_store = StateStore(db_path)
        self.msg_store = _RecordingMessageStore()
        self.known_projects = (
            (SimpleNamespace(key=project_key, path=project_path),)
            if project_path is not None
            else ()
        )
        self.storage_closet_name = "polly-storage"

    def close(self) -> None:
        self.state_store.close()


class _DispatchRecorder:
    def __init__(self) -> None:
        self.operator_inbox: list[dict[str, Any]] = []
        self.tier4_inbox: list[dict[str, Any]] = []
        self.architect_briefs: list[tuple[str, str]] = []
        self.pushes: list[tuple[str, str]] = []

    def patch(self, monkeypatch: pytest.MonkeyPatch, cadence: Any) -> None:
        monkeypatch.setattr(
            cadence, "_create_operator_inbox_task", self._operator_inbox,
        )
        monkeypatch.setattr(
            cadence, "_create_operator_tier4_inbox_task", self._tier4_inbox,
        )
        monkeypatch.setattr(
            cadence, "_send_brief_to_architect", self._architect_send,
        )
        monkeypatch.setattr(
            cadence, "_send_tier4_global_action_push", self._push,
        )

    def _operator_inbox(self, **kwargs: Any) -> str:
        self.operator_inbox.append(kwargs)
        project = kwargs.get("project_key", "workspace")
        return f"{project}/T3-{len(self.operator_inbox)}"

    def _tier4_inbox(self, **kwargs: Any) -> str:
        self.tier4_inbox.append(kwargs)
        project = kwargs.get("project_key", "workspace")
        return f"{project}/T4-{len(self.tier4_inbox)}"

    def _architect_send(self, target: str, brief: str) -> bool:
        self.architect_briefs.append((target, brief))
        return True

    def _push(self, *, title: str, body: str) -> bool:
        self.pushes.append((title, body))
        return True


class _FailoverMsgStore:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.alerts: list[tuple[str, str, str, str]] = []
        self.cleared: list[tuple[str, str, str]] = []

    def append_event(self, *, scope: str, sender: str, subject: str, payload: dict[str, Any]) -> None:
        self.events.append({
            "scope": scope,
            "sender": sender,
            "subject": subject,
            "payload": payload,
        })

    def upsert_alert(
        self,
        session_name: str,
        alert_type: str,
        severity: str,
        message: str,
    ) -> None:
        self.alerts.append((session_name, alert_type, severity, message))

    def clear_alert(self, session_name: str, alert_type: str, *, who_cleared: str = "system") -> None:
        self.cleared.append((session_name, alert_type, who_cleared))


def _assert_under(path: Path, root: Path) -> None:
    path.resolve().relative_to(root.resolve())


def _patch_workspace_db(
    monkeypatch: pytest.MonkeyPatch,
    cadence: Any,
    db_path: Path,
    tmp_path: Path,
) -> None:
    _assert_under(db_path, tmp_path)
    monkeypatch.setattr(
        cadence, "_resolve_workspace_state_db_path", lambda: db_path,
    )


def _patch_scan_inputs(
    monkeypatch: pytest.MonkeyPatch,
    cadence: Any,
    open_tasks: list[_StubTask],
) -> None:
    monkeypatch.setattr(
        cadence, "_gather_open_tasks",
        lambda _project_key, _project_path: list(open_tasks),
    )
    monkeypatch.setattr(
        cadence, "_gather_storage_windows",
        lambda _storage_closet_name: [],
    )
    monkeypatch.setattr(
        cadence, "_gather_done_plan_tasks",
        lambda _project_key, _project_path: [],
    )
    monkeypatch.setattr(
        cadence, "_gather_bypassed_plan_tasks",
        lambda _project_key, _project_path: [],
    )
    monkeypatch.setattr(
        cadence, "_gather_worker_cap_back_pressure",
        lambda _project_key, _project_path, _config_path: {},
    )


def _drive_queue_to_tier4(
    *,
    cadence: Any,
    alert_store: _RecordingAlertStore,
    now: datetime,
) -> tuple[str, datetime, list[dict[str, int]]]:
    cfg = WatchdogConfig(queue_motion_threshold_seconds=600)
    counters_by_tick: list[dict[str, int]] = []
    spacing = OPERATOR_DISPATCH_THROTTLE_SECONDS + 1

    for i in range(AUTO_PROMOTE_THRESHOLD + 1):
        tick = now + timedelta(seconds=spacing * i)
        counters = cadence._scan_one_project(
            project_key="demo",
            project_path=None,
            msg_store=alert_store,
            state_store=None,
            now=tick,
            config=cfg,
            storage_closet_name="polly-storage",
            config_path=None,
        )
        counters_by_tick.append(counters)

    promoted = read_events("demo", event=EVENT_TIER4_PROMOTED)
    assert len(promoted) == 1
    rch = (promoted[0].metadata or {})["root_cause_hash"]
    return str(rch), now + timedelta(seconds=spacing * AUTO_PROMOTE_THRESHOLD), counters_by_tick


def _route(
    cadence: Any,
    finding: Finding,
    *,
    alert_store: _RecordingAlertStore,
    now: datetime,
    config_path: Path | None = None,
) -> dict[str, int]:
    counters = cadence._scan_one_project_counters()
    cadence._route_one_finding(
        finding,
        project_key=finding.project,
        project_path=None,
        msg_store=alert_store,
        state_store=None,
        storage_closet_name="polly-storage",
        config_path=config_path,
        now=now,
        counters=counters,
    )
    return counters


def _config_with_architects(tmp_path: Path) -> PollyPMConfig:
    root = tmp_path / "repo"
    root.mkdir()
    return PollyPMConfig(
        project=ProjectSettings(
            name="Test",
            root_dir=root,
            base_dir=root / ".pollypm",
            logs_dir=root / ".pollypm/logs",
            snapshots_dir=root / ".pollypm/snapshots",
            state_db=root / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="claude_main",
            failover_enabled=True,
            failover_accounts=["claude_backup"],
            failover_usage_threshold_pct=85,
        ),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=root / "homes" / "claude_main",
            ),
            "claude_backup": AccountConfig(
                name="claude_backup",
                provider=ProviderKind.CLAUDE,
                home=root / "homes" / "claude_backup",
            ),
        },
        sessions={
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=root,
            ),
            "architect_demo": SessionConfig(
                name="architect_demo",
                role="architect",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=root,
                project="demo",
                auth_token="ab" * 32,
            ),
            "architect_other": SessionConfig(
                name="architect_other",
                role="architect",
                provider=ProviderKind.CLAUDE,
                account="claude_main",
                cwd=root,
                project="other",
                auth_token="",
            ),
        },
        projects={
            "demo": KnownProject(
                key="demo",
                path=root,
                name="Demo",
                kind=ProjectKind.FOLDER,
            ),
            "other": KnownProject(
                key="other",
                path=root,
                name="Other",
                kind=ProjectKind.FOLDER,
            ),
        },
    )


def test_harness_reuses_pg_live_dsn_guard() -> None:
    from tests.conftest_pg import _is_ambient_live_pg_dsn

    assert _is_ambient_live_pg_dsn("postgresql://localhost:5432/pollypm") is True
    assert _is_ambient_live_pg_dsn("dbname=pollypm host=/tmp port=5432") is True
    assert _is_ambient_live_pg_dsn("postgresql://localhost:5432/pollypm_test") is False
    assert _is_ambient_live_pg_dsn("postgresql://localhost:55432/pollypm") is False


def test_queue_without_motion_promotes_then_budget_exhausts_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
) -> None:
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence
    from pollypm.storage.product_state import is_product_broken

    db_path = tmp_path / "state.db"
    _patch_workspace_db(monkeypatch, cadence, db_path, tmp_path)
    recorder = _DispatchRecorder()
    recorder.patch(monkeypatch, cadence)
    alert_store = _RecordingAlertStore()
    task = _StubTask(
        project="demo",
        task_number=4,
        work_status_str="queued",
        executions=[],
        updated_at=now - timedelta(hours=2),
    )
    _patch_scan_inputs(monkeypatch, cadence, [task])

    rch, promoted_at, counters = _drive_queue_to_tier4(
        cadence=cadence,
        alert_store=alert_store,
        now=now,
    )

    assert [c["operator_dispatches_sent"] for c in counters[:AUTO_PROMOTE_THRESHOLD]] == [1] * AUTO_PROMOTE_THRESHOLD
    assert counters[-1]["tier4_dispatches_sent"] == 1
    assert len(read_events("demo", event=EVENT_WATCHDOG_OPERATOR_DISPATCHED)) == AUTO_PROMOTE_THRESHOLD
    assert len(read_events("demo", event=EVENT_WATCHDOG_OPERATOR_TIER4_DISPATCHED)) == 1
    assert len(recorder.operator_inbox) == AUTO_PROMOTE_THRESHOLD
    assert len(recorder.tier4_inbox) == 1
    assert "TIER 4 BROADER AUTHORITY DISPATCH" in recorder.tier4_inbox[0]["body"]
    assert "<tier4_runtime>" in recorder.tier4_inbox[0]["body"]

    tracker = Tier4PromotionTracker(db_path)
    active = tracker.get(rch)
    assert active is not None and active.tier4_active is True

    services = _RecordingServices(db_path)
    try:
        exhausted_at = promoted_at + timedelta(seconds=TIER4_BUDGET_SECONDS + 60)
        sweep = cadence._sweep_tier4_budget_and_demotion(
            services=services,
            now=exhausted_at,
            seen_root_cause_hashes={rch},
        )

        assert sweep["tier4_budget_exhausted"] == 1
        exhausted = read_events("demo", event=EVENT_TIER4_BUDGET_EXHAUSTED)
        assert len(exhausted) == 1
        assert (exhausted[0].metadata or {})["root_cause_hash"] == rch
        assert services.msg_store.messages
        terminal = services.msg_store.messages[0]
        assert terminal["state"] == "open"
        assert "tier4-terminal" in terminal["labels"]
        assert is_product_broken(services.state_store) is not None
        assert recorder.pushes and recorder.pushes[-1][0] == "PollyPM tier-4 cascade exhausted"
    finally:
        services.close()

    parked = tracker.get(rch)
    assert parked is not None
    assert parked.tier4_active is False
    assert parked.terminal_handoff_at is not None


def test_tier4_demotes_when_real_scan_seen_hashes_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
) -> None:
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence

    db_path = tmp_path / "state.db"
    _patch_workspace_db(monkeypatch, cadence, db_path, tmp_path)
    recorder = _DispatchRecorder()
    recorder.patch(monkeypatch, cadence)
    alert_store = _RecordingAlertStore()
    open_tasks = [
        _StubTask(
            project="demo",
            task_number=4,
            work_status_str="queued",
            executions=[],
            updated_at=now - timedelta(hours=2),
        )
    ]
    _patch_scan_inputs(monkeypatch, cadence, open_tasks)
    rch, promoted_at, _counters = _drive_queue_to_tier4(
        cadence=cadence,
        alert_store=alert_store,
        now=now,
    )

    open_tasks.clear()
    resolved_scan = cadence._scan_one_project(
        project_key="demo",
        project_path=None,
        msg_store=alert_store,
        state_store=None,
        now=promoted_at + timedelta(minutes=5),
        config=WatchdogConfig(queue_motion_threshold_seconds=600),
        storage_closet_name="polly-storage",
        config_path=None,
    )
    seen = resolved_scan.pop("_seen_root_cause_hashes")
    assert seen == set()

    services = _RecordingServices(db_path)
    try:
        sweep = cadence._sweep_tier4_budget_and_demotion(
            services=services,
            now=promoted_at + timedelta(minutes=6),
            seen_root_cause_hashes=seen,
        )
    finally:
        services.close()

    assert sweep["tier4_demoted_cleared"] == 1
    assert sweep["tier4_budget_exhausted"] == 0
    demoted = read_events("demo", event=EVENT_TIER4_DEMOTED)
    assert len(demoted) == 1
    assert (demoted[0].metadata or {}) == {
        "root_cause_hash": rch,
        "reason": "finding_resolved",
    }
    state = Tier4PromotionTracker(db_path).get(rch)
    assert state is not None and state.tier4_active is False


def test_architect_rule_counts_dispatches_promotes_and_signs_by_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
) -> None:
    from pollypm.config import write_config
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence

    db_path = tmp_path / "state.db"
    _patch_workspace_db(monkeypatch, cadence, db_path, tmp_path)
    config = _config_with_architects(tmp_path)
    config_path = tmp_path / "pollypm.toml"
    _assert_under(config_path, tmp_path)
    write_config(config, config_path, force=True)
    recorder = _DispatchRecorder()
    recorder.patch(monkeypatch, cadence)
    alert_store = _RecordingAlertStore()

    signed = Finding(
        rule=RULE_TASK_PROGRESS_STALE,
        tier=TIER_2,
        project="demo",
        subject="demo/7",
        message="Task demo/7 has stale progress.",
        evidence={"task_id": "demo/7"},
    )
    unsigned = Finding(
        rule=RULE_TASK_PROGRESS_STALE,
        tier=TIER_2,
        project="other",
        subject="other/1",
        message="Task other/1 has stale progress.",
        evidence={"task_id": "other/1"},
    )

    spacing = ESCALATION_THROTTLE_SECONDS + 1
    _route(
        cadence,
        signed,
        alert_store=alert_store,
        now=now,
        config_path=config_path,
    )
    _route(
        cadence,
        unsigned,
        alert_store=alert_store,
        now=now,
        config_path=tmp_path / "missing-pollypm.toml",
    )
    for i in range(1, AUTO_PROMOTE_THRESHOLD + 1):
        _route(
            cadence,
            signed,
            alert_store=alert_store,
            now=now + timedelta(seconds=spacing * i),
            config_path=config_path,
        )

    signed_briefs = [
        brief for target, brief in recorder.architect_briefs
        if target == "polly-storage:architect-demo"
    ]
    unsigned_briefs = [
        brief for target, brief in recorder.architect_briefs
        if target == "polly-storage:architect-other"
    ]
    assert len(signed_briefs) == AUTO_PROMOTE_THRESHOLD
    assert signed_briefs[0].startswith(f"{AUTH_MARKER_PREFIX}{'ab' * 32}]\n")
    assert len(unsigned_briefs) == 1
    assert AUTH_MARKER_PREFIX not in unsigned_briefs[0]
    assert len(recorder.tier4_inbox) == 1
    assert len(read_events("demo", event=EVENT_TIER4_PROMOTED)) == 1
    assert len(read_events("demo", event=EVENT_WATCHDOG_ESCALATION_DISPATCHED)) == AUTO_PROMOTE_THRESHOLD


def test_role_session_missing_tier1_heals_or_fails_without_escalation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
) -> None:
    from pollypm import cli as cli_mod
    from pollypm.plugins_builtin.core_recurring import audit_watchdog as cadence

    config_path = tmp_path / "pollypm.toml"
    _assert_under(config_path, tmp_path)
    alert_store = _RecordingAlertStore()
    supervisor = SimpleNamespace(config=SimpleNamespace(sessions={}))
    launches: list[str] = []

    monkeypatch.setattr(cli_mod, "_load_supervisor", lambda _path: supervisor)
    monkeypatch.setattr(
        cli_mod,
        "create_worker_session",
        lambda *_args, **_kwargs: SimpleNamespace(
            name="architect_demo",
            role="architect",
            project="demo",
            enabled=True,
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "launch_worker_session",
        lambda _path, session_name: launches.append(session_name),
    )

    finding = Finding(
        rule=RULE_ROLE_SESSION_MISSING,
        tier=TIER_1,
        project="demo",
        subject="demo/42",
        message="architect lane missing",
        metadata={"role": "architect"},
    )
    counters = _route(
        cadence,
        finding,
        alert_store=alert_store,
        now=now,
        config_path=config_path,
    )

    assert counters["worker_lane_spawned"] == 1
    assert counters["worker_lane_failed"] == 0
    assert launches == ["architect_demo"]
    assert len(read_events("demo", event=EVENT_WATCHDOG_WORKER_LANE_SPAWNED)) == 1
    assert read_events("demo", event=EVENT_WATCHDOG_ESCALATION_DISPATCHED) == []
    assert read_events("demo", event=EVENT_WATCHDOG_OPERATOR_DISPATCHED) == []
    assert read_events("demo", event=EVENT_TIER4_PROMOTED) == []

    def _raise_launch(_path: Path, _session_name: str) -> None:
        raise RuntimeError("simulated spawn failure")

    monkeypatch.setattr(cli_mod, "launch_worker_session", _raise_launch)
    failed = _route(
        cadence,
        finding,
        alert_store=alert_store,
        now=now + timedelta(minutes=1),
        config_path=config_path,
    )

    assert failed["worker_lane_spawned"] == 0
    assert failed["worker_lane_failed"] == 1
    failures = read_events("demo", event=EVENT_WATCHDOG_WORKER_LANE_FAILED)
    assert len(failures) == 1
    assert (failures[0].metadata or {})["reason"] == "spawn_raised:RuntimeError"
    assert read_events("demo", event=EVENT_WATCHDOG_ESCALATION_DISPATCHED) == []
    assert read_events("demo", event=EVENT_WATCHDOG_OPERATOR_DISPATCHED) == []
    assert read_events("demo", event=EVENT_TIER4_PROMOTED) == []


def test_heartbeat_role_crash_recovers_then_crash_loop_escalates() -> None:
    from tests.test_heartbeat_role_respawn import (
        _RoleCrashFakeAPI,
        _backend_with_pending_work,
        _crashed_pane_context,
    )
    from pollypm.heartbeats.local import LocalHeartbeatBackend

    healed_api = _RoleCrashFakeAPI(recovery_attempts=0)
    backend = _backend_with_pending_work(has_work=True)
    backend._process_session(healed_api, _crashed_pane_context(role="architect"))

    assert len(healed_api.recover_calls) == 1
    call = healed_api.recover_calls[0]
    assert call["session_name"] == "architect_x"
    assert call["failure_type"] == "role_crashed"
    assert "shell" in call["message"]
    assert "role_crashed" in healed_api.checkpoints[-1][1]

    loop_api = _RoleCrashFakeAPI(
        recovery_attempts=LocalHeartbeatBackend._CRASH_LOOP_ATTEMPT_THRESHOLD,
    )
    backend._process_session(loop_api, _crashed_pane_context(role="architect"))

    assert loop_api.recover_calls == []
    assert any(a["alert_type"] == "crash_loop" for a in loop_api.alerts_raised)
    assert loop_api.appended_events[-1]["subject"] == "crash_loop_escalated"


def test_proactive_failover_switch_and_no_capacity_paths_emit_audit(
    tmp_path: Path,
) -> None:
    from pollypm.capacity import ProactiveFailoverDecision
    from pollypm.plugins_builtin.core_recurring.maintenance import (
        _apply_proactive_controller_failover,
    )

    config = _config_with_architects(tmp_path)
    config_path = tmp_path / "pollypm.toml"
    _assert_under(config_path, tmp_path)
    msg_store = _FailoverMsgStore()
    switched: list[tuple[str, str]] = []

    switch_summary = _apply_proactive_controller_failover(
        config_path,
        config,
        ProactiveFailoverDecision(
            action="switch",
            primary_account="claude_main",
            current_account="claude_main",
            selected_account="claude_backup",
            reason="used_pct_threshold",
            threshold_pct=85,
            candidates_evaluated=1,
        ),
        msg_store=msg_store,
        switcher=lambda session_name, account_name: switched.append(
            (session_name, account_name)
        ),
    )
    alert_summary = _apply_proactive_controller_failover(
        config_path,
        config,
        ProactiveFailoverDecision(
            action="alert",
            primary_account="claude_main",
            current_account="claude_main",
            selected_account=None,
            reason="no_failover_account_below_threshold",
            threshold_pct=85,
            candidates_evaluated=1,
        ),
        msg_store=msg_store,
        switcher=lambda *_args: None,
    )

    assert switch_summary["applied"] is True
    assert alert_summary["action"] == "alert"
    assert switched == [("operator", "claude_backup")]
    assert [e["subject"] for e in msg_store.events] == [
        "account.failover.proactive",
        "account.failover.proactive",
    ]
    assert msg_store.events[0]["payload"]["to"] == "claude_backup"
    assert msg_store.events[1]["payload"]["to"] is None
    assert any(a[1] == "proactive_failover_no_capacity" for a in msg_store.alerts)

    rows = read_events("_workspace", event=EVENT_ACCOUNT_FAILOVER_PROACTIVE)
    assert [row.status for row in rows] == ["ok", "warn"]
    assert (rows[0].metadata or {})["to"] == "claude_backup"
    assert (rows[1].metadata or {})["to"] is None
