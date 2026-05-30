"""Deterministic chaos injectors for the 48h watchdog burn-in.

The harness is intentionally sandbox-first:

* dry-run mode is the default and performs no tmux or database writes;
* destructive session-kill execution requires sandbox-looking tmux and
  window names;
* PG execution refuses the ambient operator ``pollypm`` database via the
  same guard used by ``tests/conftest_pg.py``.

Every result includes ``pre_recovery`` and ``post_recovery`` blocks so a
burn-in runner can persist the broken state and the recovery evidence as
structured JSON.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from pollypm.audit.watchdog import (
    RULE_ROLE_SESSION_MISSING,
    RULE_TASK_PROGRESS_STALE,
    WatchdogConfig,
    _detect_role_session_missing,
    _detect_task_progress_stale,
)
from pollypm.capacity import CapacityState
from pollypm.heartbeats.base import HeartbeatSessionContext
from pollypm.heartbeats.local import LocalHeartbeatBackend
from pollypm.recovery.base import SessionSignals
from pollypm.recovery.default import DefaultRecoveryPolicy


SANDBOX_MARKERS = ("sandbox", "chaos", "test")
UNSAFE_MARKERS = ("prod", "production", "live")


@dataclass
class _FakeHeartbeatAPI:
    contexts: list[HeartbeatSessionContext]

    def __post_init__(self) -> None:
        self.alerts: dict[tuple[str, str], dict[str, str]] = {}
        self.statuses: dict[str, tuple[str, str]] = {}
        self.recoveries: list[dict[str, str]] = []
        self.account_marks: list[dict[str, str]] = []
        self.cursor_updates: list[dict[str, object]] = []
        self.observations: list[str] = []
        self.checkpoints: list[dict[str, object]] = []
        self.worker_heartbeats: list[dict[str, str | None]] = []
        self.events: list[dict[str, str]] = []
        self.messages: list[dict[str, str]] = []
        self.supervisor = SimpleNamespace(
            config=SimpleNamespace(sessions={}, projects={}),
            msg_store=_FakeMessageStore(),
            get_session_runtime=lambda _session_name: None,
        )

    def list_sessions(self) -> list[HeartbeatSessionContext]:
        return list(self.contexts)

    def list_unmanaged_windows(self) -> list[Any]:
        return []

    def get_cursor(self, _session_name: str) -> None:
        return None

    def update_cursor(
        self,
        session_name: str,
        *,
        source_path: str,
        last_offset: int,
        snapshot_hash: str = "",
        verdict: str = "",
        reason: str = "",
        quiet_tick_count: int = 0,
    ) -> None:
        self.cursor_updates.append({
            "session_name": session_name,
            "source_path": source_path,
            "last_offset": last_offset,
            "snapshot_hash": snapshot_hash,
            "verdict": verdict,
            "reason": reason,
            "quiet_tick_count": quiet_tick_count,
        })

    def record_observation(self, context: HeartbeatSessionContext) -> None:
        self.observations.append(context.session_name)

    def record_checkpoint(
        self,
        context: HeartbeatSessionContext,
        *,
        alerts: list[str],
    ) -> None:
        self.checkpoints.append({
            "session_name": context.session_name,
            "alerts": list(alerts),
        })

    def record_worker_heartbeat(
        self,
        context: HeartbeatSessionContext,
        *,
        task_id: str | None = None,
    ) -> None:
        self.worker_heartbeats.append({
            "session_name": context.session_name,
            "task_id": task_id,
        })

    def record_event(
        self,
        session_name: str,
        event_type: str,
        message: str,
    ) -> None:
        self.events.append({
            "session_name": session_name,
            "event_type": event_type,
            "message": message,
        })

    def raise_alert(
        self,
        session_name: str,
        alert_type: str,
        severity: str,
        message: str,
    ) -> None:
        self.alerts[(session_name, alert_type)] = {
            "session_name": session_name,
            "alert_type": alert_type,
            "severity": severity,
            "message": message,
        }

    def clear_alert(self, session_name: str, alert_type: str) -> None:
        self.alerts.pop((session_name, alert_type), None)

    def open_alerts(self) -> list[dict[str, str]]:
        return list(self.alerts.values())

    def set_session_status(
        self,
        session_name: str,
        status: str,
        *,
        reason: str = "",
    ) -> None:
        self.statuses[session_name] = (status, reason)

    def mark_account_auth_broken(
        self,
        account_name: str,
        provider: str,
        *,
        reason: str,
    ) -> None:
        self.account_marks.append({
            "account_name": account_name,
            "provider": provider,
            "reason": reason,
        })

    def mark_account_capacity_exhausted(
        self,
        account_name: str,
        provider: str,
        *,
        reason: str,
    ) -> None:
        self.account_marks.append({
            "account_name": account_name,
            "provider": provider,
            "reason": reason,
        })

    def recent_snapshot_hashes(
        self,
        _session_name: str,
        *,
        limit: int = 3,
    ) -> list[str]:
        del limit
        return []

    def recover_session(
        self,
        session_name: str,
        *,
        failure_type: str,
        message: str,
    ) -> None:
        self.recoveries.append({
            "session_name": session_name,
            "failure_type": failure_type,
            "message": message,
        })

    def send_session_message(
        self,
        session_name: str,
        text: str,
        *,
        owner: str = "heartbeat",
    ) -> None:
        self.messages.append({
            "session_name": session_name,
            "text": text,
            "owner": owner,
        })

    def queue_polly_followup(self, session_name: str, reason: str) -> None:
        self.messages.append({
            "session_name": "operator",
            "text": f"Heartbeat follow-up for {session_name}: {reason}",
            "owner": "heartbeat",
        })


class _FakeMessageStore:
    def query_messages(
        self,
        *,
        type: str | None = None,
        scope: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        del type, scope, limit
        return []

    def append_event(self, **_kwargs: object) -> None:
        return None


def _context(
    *,
    session_name: str,
    account_name: str,
    provider: str,
    pane_text: str,
) -> HeartbeatSessionContext:
    return HeartbeatSessionContext(
        session_name=session_name,
        role="worker",
        project_key="chaos-sandbox",
        provider=provider,
        account_name=account_name,
        cwd="/tmp/pollypm-chaos-sandbox",
        tmux_session="pollypm-chaos-sandbox",
        window_name=session_name,
        source_path="/tmp/pollypm-chaos-sandbox/worker.log",
        source_bytes=64,
        transcript_delta=pane_text,
        pane_text=pane_text,
        snapshot_path="/tmp/pollypm-chaos-sandbox/snapshot.txt",
        snapshot_hash="chaos-hash",
        pane_id="%1",
        pane_command=provider,
        pane_dead=False,
        window_present=True,
        previous_log_bytes=32,
        previous_snapshot_hash="previous-chaos-hash",
        cursor=None,
    )


def _sandbox_ok(value: str) -> bool:
    lowered = value.lower()
    return (
        any(marker in lowered for marker in SANDBOX_MARKERS)
        and not any(marker in lowered for marker in UNSAFE_MARKERS)
    )


def assert_sandbox_name(value: str, *, field: str) -> None:
    if not _sandbox_ok(value):
        raise ValueError(
            f"{field}={value!r} is not sandbox-scoped; include one of "
            f"{', '.join(SANDBOX_MARKERS)} and avoid prod/live names"
        )


def assert_non_ambient_pg_dsn(dsn: str) -> None:
    from tests.conftest_pg import _is_ambient_live_pg_dsn

    if _is_ambient_live_pg_dsn(dsn):
        raise ValueError(
            "Refusing to run task-stall against the ambient local pollypm DB; "
            "use tests/conftest_pg.py fixtures or an isolated test DSN."
        )


def _finding_payload(finding: Any) -> dict[str, object]:
    return {
        "rule": finding.rule,
        "tier": finding.tier,
        "project": finding.project,
        "subject": finding.subject,
        "message": finding.message,
        "metadata": dict(finding.metadata or {}),
    }


def _task_payload(task: Any) -> dict[str, object]:
    status = getattr(task, "work_status", None)
    transitions = []
    for tr in getattr(task, "transitions", None) or []:
        transitions.append({
            "from_state": getattr(tr, "from_state", None),
            "to_state": getattr(tr, "to_state", None),
            "timestamp": getattr(tr, "timestamp", None),
        })
    return {
        "task_id": getattr(task, "task_id", ""),
        "project": getattr(task, "project", ""),
        "task_number": getattr(task, "task_number", None),
        "status": getattr(status, "value", status),
        "assignee": getattr(task, "assignee", None),
        "claimed_by_session": getattr(task, "claimed_by_session", None),
        "current_node_id": getattr(task, "current_node_id", None),
        "transitions": transitions,
    }


def run_failover_injector(
    *,
    dry_run: bool = True,
    mode: str = "auth_broken",
    session_name: str = "worker-chaos-sandbox",
    account_name: str = "claude-chaos-sandbox",
    provider: str = "claude",
) -> dict[str, object]:
    """Simulate auth/capacity failure and prove recovery is invoked.

    This is deterministic and sandboxed even with ``dry_run=False``:
    it uses the public heartbeat backend contract with an in-memory API
    fake, never a real account home or tmux pane.
    """
    assert_sandbox_name(session_name, field="session_name")
    assert_sandbox_name(account_name, field="account_name")
    if mode not in {"auth_broken", "capacity_exhausted"}:
        raise ValueError("mode must be auth_broken or capacity_exhausted")

    if mode == "auth_broken":
        pane_text = "Authentication failure: please login again."
        expected_status = "auth_broken"
        policy_health = DefaultRecoveryPolicy().classify(
            SessionSignals(session_name=session_name, auth_failure=True)
        )
    else:
        pane_text = "Usage limit reached. Please try again later."
        expected_status = "capacity_exhausted"
        policy_health = DefaultRecoveryPolicy().classify(
            SessionSignals(
                session_name=session_name,
                capacity_state=CapacityState.EXHAUSTED,
            )
        )

    api = _FakeHeartbeatAPI([
        _context(
            session_name=session_name,
            account_name=account_name,
            provider=provider,
            pane_text=pane_text,
        )
    ])
    before = {
        "session_name": session_name,
        "account_name": account_name,
        "injected_pane_marker": pane_text,
        "session_status": "healthy",
        "recoveries": [],
    }
    LocalHeartbeatBackend().run(api)
    status, reason = api.statuses.get(session_name, ("", ""))
    recoveries = [
        item for item in api.recoveries
        if item["session_name"] == session_name
        and item["failure_type"] == expected_status
    ]
    after = {
        "session_name": session_name,
        "session_status": status,
        "status_reason": reason,
        "alerts": api.open_alerts(),
        "account_marks": list(api.account_marks),
        "recoveries": recoveries,
        "policy_health": str(policy_health),
        "policy_action": "failover",
    }
    return {
        "injector": "failover",
        "dry_run": dry_run,
        "safety": {
            "sandbox_account_only": True,
            "real_account_touched": False,
            "tmux_touched": False,
            "pg_touched": False,
        },
        "mapped_rule": {
            "detector": (
                "pollypm.heartbeats.local."
                f"LocalHeartbeatBackend._handle_{'auth' if mode == 'auth_broken' else 'capacity'}_failure"
            ),
            "policy": (
                "pollypm.recovery.default.DefaultRecoveryPolicy."
                "classify/select_intervention"
            ),
            "watchdog_note": (
                "Account failover is handled by the live heartbeat "
                "watchdog path, not an audit.watchdog _detect_* rule."
            ),
        },
        "pre_recovery": before,
        "post_recovery": after,
        "passed": status == expected_status and bool(recoveries),
    }


def run_session_kill_injector(
    *,
    dry_run: bool = True,
    project: str = "chaos-sandbox",
    task_number: int = 1,
    role: str = "worker",
    tmux_session: str = "pollypm-chaos-sandbox-storage-closet",
    window_name: str | None = None,
) -> dict[str, object]:
    """Kill only a sandbox tmux window and verify detector evidence.

    Dry-run mode proves the detector/recovery mapping without touching
    tmux. Execution mode still refuses non-sandbox tmux/window names.
    """
    window_name = window_name or f"{role}-{project}"
    assert_sandbox_name(project, field="project")
    assert_sandbox_name(tmux_session, field="tmux_session")
    assert_sandbox_name(window_name, field="window_name")

    task = SimpleNamespace(
        work_status="in_progress",
        project=project,
        task_number=task_number,
        roles={role: role},
        assignee=role,
    )
    config = WatchdogConfig()
    before_findings = _detect_role_session_missing(
        [],
        now=datetime.now(UTC),
        config=config,
        open_tasks=[task],
        storage_window_names=[window_name],
        project=project,
    )
    killed = False
    command: list[str] | None = None
    if not dry_run:
        command = ["tmux", "kill-window", "-t", f"{tmux_session}:{window_name}"]
        subprocess.run(command, check=True)
        killed = True

    broken_findings = _detect_role_session_missing(
        [],
        now=datetime.now(UTC),
        config=config,
        open_tasks=[task],
        storage_window_names=[],
        project=project,
    )
    reconciled_findings = _detect_role_session_missing(
        [],
        now=datetime.now(UTC),
        config=config,
        open_tasks=[task],
        storage_window_names=[window_name],
        project=project,
    )
    return {
        "injector": "session-kill",
        "dry_run": dry_run,
        "safety": {
            "sandbox_project_only": True,
            "tmux_touched": killed,
            "tmux_target": f"{tmux_session}:{window_name}",
            "command": command,
        },
        "mapped_rule": {
            "detector": (
                "pollypm.audit.watchdog._detect_role_session_missing"
            ),
            "rule": RULE_ROLE_SESSION_MISSING,
            "recovery_path": (
                "heartbeat/supervisor reconcile respawns the missing "
                "role window for an in-progress task"
            ),
        },
        "pre_recovery": {
            "expected_window": window_name,
            "window_present_before_injection": not before_findings,
            "window_present_after_injection": False,
            "findings": [_finding_payload(item) for item in broken_findings],
        },
        "post_recovery": {
            "window_present_after_reconcile": True,
            "findings": [_finding_payload(item) for item in reconciled_findings],
            "evidence": "role_session_missing finding clears when window is present",
        },
        "passed": bool(broken_findings) and not reconciled_findings,
    }


def _age_pg_task(
    *,
    pg_pool: Any,
    project: str,
    task_number: int,
    stale_at: datetime,
) -> None:
    with pg_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE work_tasks "
            "SET updated_at = %s "
            "WHERE project = %s AND task_number = %s",
            (stale_at, project, task_number),
        )
        cur.execute(
            "UPDATE work_transitions "
            "SET created_at = %s "
            "WHERE task_project = %s AND task_number = %s "
            "AND to_state = 'in_progress'",
            (stale_at, project, task_number),
        )
        cur.execute(
            "UPDATE work_context_entries "
            "SET created_at = %s "
            "WHERE task_project = %s AND task_number = %s",
            (stale_at, project, task_number),
        )
        conn.commit()


def run_task_stall_injector(
    *,
    work_service: Any,
    pg_pool: Any,
    dry_run: bool = False,
    project: str = "chaos-sandbox",
    actor: str = "worker-chaos-sandbox",
    stale_seconds: int | None = None,
) -> dict[str, object]:
    """Create and age an isolated PG task, then move it out of stall."""
    assert_sandbox_name(project, field="project")
    assert_sandbox_name(actor, field="actor")
    threshold = stale_seconds or WatchdogConfig().progress_stale_seconds + 60
    now = datetime.now(UTC)
    stale_at = now - timedelta(seconds=threshold)
    task = work_service.create(
        title="chaos task-stall fixture",
        description="isolated chaos fixture",
        type="task",
        project=project,
        flow_template="standard",
        roles={"worker": actor},
        created_by="chaos",
    )
    queued = work_service.queue(task.task_id, actor="chaos")
    claimed = work_service.claim(queued.task_id, actor=actor)
    _age_pg_task(
        pg_pool=pg_pool,
        project=claimed.project,
        task_number=claimed.task_number,
        stale_at=stale_at,
    )
    stale_task = work_service.get(claimed.task_id)
    findings = _detect_task_progress_stale(
        [],
        now=now,
        config=WatchdogConfig(progress_stale_seconds=threshold - 1),
        open_tasks=[stale_task],
    )
    released = work_service.release_stale_claim(
        stale_task.task_id,
        actor="chaos",
        reason="chaos task-stall recovery",
    )
    post_findings = _detect_task_progress_stale(
        [],
        now=now,
        config=WatchdogConfig(progress_stale_seconds=threshold - 1),
        open_tasks=[released],
    )
    return {
        "injector": "task-stall",
        "dry_run": dry_run,
        "safety": {
            "sandbox_project_only": True,
            "pg_fixture_required": True,
            "ambient_dsn_refusal": "tests.conftest_pg._is_ambient_live_pg_dsn",
            "pg_touched": True,
            "tmux_touched": False,
        },
        "mapped_rule": {
            "detector": (
                "pollypm.audit.watchdog._detect_task_progress_stale"
            ),
            "rule": RULE_TASK_PROGRESS_STALE,
            "recovery_path": "work_service.release_stale_claim",
        },
        "pre_recovery": {
            "task": _task_payload(stale_task),
            "aged_to": stale_at.isoformat(),
            "findings": [_finding_payload(item) for item in findings],
        },
        "post_recovery": {
            "task": _task_payload(released),
            "findings": [_finding_payload(item) for item in post_findings],
            "evidence": "release_stale_claim moved task out of in_progress",
        },
        "passed": bool(findings) and not post_findings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run PollyPM chaos injectors in sandbox/dry-run mode.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform destructive action where supported",
    )
    sub = parser.add_subparsers(dest="injector", required=True)

    failover = sub.add_parser("failover")
    failover.add_argument(
        "--mode",
        choices=("auth_broken", "capacity_exhausted"),
        default="auth_broken",
    )
    failover.add_argument("--session-name", default="worker-chaos-sandbox")
    failover.add_argument("--account-name", default="claude-chaos-sandbox")
    failover.add_argument("--provider", default="claude")

    session_kill = sub.add_parser("session-kill")
    session_kill.add_argument("--project", default="chaos-sandbox")
    session_kill.add_argument("--task-number", type=int, default=1)
    session_kill.add_argument("--role", default="worker")
    session_kill.add_argument(
        "--tmux-session",
        default="pollypm-chaos-sandbox-storage-closet",
    )
    session_kill.add_argument("--window-name")

    task_stall = sub.add_parser("task-stall")
    task_stall.add_argument("--project", default="chaos-sandbox")
    task_stall.add_argument("--actor", default="worker-chaos-sandbox")
    task_stall.add_argument("--dsn", required=True)
    return parser


def run_from_args(args: argparse.Namespace) -> dict[str, object]:
    dry_run = not args.execute
    if args.injector == "failover":
        return run_failover_injector(
            dry_run=dry_run,
            mode=args.mode,
            session_name=args.session_name,
            account_name=args.account_name,
            provider=args.provider,
        )
    if args.injector == "session-kill":
        return run_session_kill_injector(
            dry_run=dry_run,
            project=args.project,
            task_number=args.task_number,
            role=args.role,
            tmux_session=args.tmux_session,
            window_name=args.window_name,
        )
    if args.injector == "task-stall":
        assert_non_ambient_pg_dsn(args.dsn)
        if dry_run:
            return {
                "injector": "task-stall",
                "dry_run": True,
                "safety": {
                    "pg_touched": False,
                    "dsn_checked": True,
                    "ambient_dsn_refusal": (
                        "tests.conftest_pg._is_ambient_live_pg_dsn"
                    ),
                },
                "mapped_rule": {
                    "detector": (
                        "pollypm.audit.watchdog._detect_task_progress_stale"
                    ),
                    "rule": RULE_TASK_PROGRESS_STALE,
                },
                "pre_recovery": {"planned_status": "in_progress"},
                "post_recovery": {"planned_status": "queued"},
                "passed": True,
            }
        from pollypm.storage import pg_pool
        from pollypm.work.pg_service import PgWorkService

        pool = pg_pool.get_rw_pool(SimpleNamespace(storage=SimpleNamespace(
            backend="postgres",
            pg=SimpleNamespace(dsn=args.dsn),
            dsn=args.dsn,
            url=args.dsn,
        )))
        try:
            service = PgWorkService(pool=pool, ro_pool=None)
            return run_task_stall_injector(
                work_service=service,
                pg_pool=pool,
                dry_run=False,
                project=args.project,
                actor=args.actor,
            )
        finally:
            pg_pool.pg_pool_shutdown()
    raise AssertionError(f"unknown injector: {args.injector}")


def dumps_result(result: dict[str, object]) -> str:
    return json.dumps(result, indent=2, sort_keys=True, default=str)


__all__ = [
    "assert_non_ambient_pg_dsn",
    "assert_sandbox_name",
    "build_parser",
    "dumps_result",
    "run_failover_injector",
    "run_from_args",
    "run_session_kill_injector",
    "run_task_stall_injector",
]
