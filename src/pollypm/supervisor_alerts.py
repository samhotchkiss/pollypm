"""Supervisor alert-update and nudge boundary helpers.

Contract:
- Inputs: a supervisor-like boundary object plus live launch/window
  state from :mod:`pollypm.supervisor`.
- Outputs: alert updates, nudge decisions, and the review-task cache
  used by the heartbeat path.
- Side effects: writes alert rows, appends events, and sends heartbeat
  nudges through the provided supervisor boundary.
- Invariants: behavior mirrors the legacy ``Supervisor`` methods so the
  public supervisor surface stays compatible while this seam is split
  out for future decomposition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from pollypm.config import PollyPMConfig
from pollypm.models import SessionLaunchSpec
from pollypm.store.protocol import Store
from pollypm.storage.state import StateStore
from pollypm.tmux.client import TmuxWindow


class SupervisorAlertBoundary(Protocol):
    config: PollyPMConfig
    store: StateStore
    msg_store: Store
    _STALL_NUDGE_MESSAGE: str

    def send_input(
        self,
        session_name: str,
        text: str,
        *,
        owner: str = "pollypm",
        force: bool = False,
        press_enter: bool = True,
    ) -> None: ...

    def pane_has_auth_failure(self, lowered_pane: str) -> bool: ...

    def pane_has_capacity_failure(self, lowered_pane: str) -> bool: ...

    def pane_has_provider_outage(self, lowered_pane: str) -> bool: ...


_REVIEW_NUDGE_CACHE: dict[str, tuple[float, list[str]]] = {}


def _review_tasks_for_project(project_key: str, db_path: Path) -> list[str]:
    """Return nudge lines for a project's review-queue tasks."""
    try:
        db_mtime = db_path.stat().st_mtime
    except OSError:
        _REVIEW_NUDGE_CACHE.pop(project_key, None)
        return []
    cached = _REVIEW_NUDGE_CACHE.get(project_key)
    if cached is not None and cached[0] == db_mtime:
        return cached[1]

    from pollypm.work.sqlite_service import SQLiteWorkService

    entries: list[str] = []
    try:
        with SQLiteWorkService(db_path=db_path) as svc:
            tasks = svc.list_tasks(work_status="review", project=project_key)
            for task in tasks:
                if task.current_node_id and "human" in task.current_node_id:
                    continue
                entries.append(f"  - {task.task_id}: {task.title}")
    except Exception:  # noqa: BLE001
        return []

    _REVIEW_NUDGE_CACHE[project_key] = (db_mtime, entries)
    return entries


def _update_alerts(
    supervisor: SupervisorAlertBoundary,
    launch: SessionLaunchSpec,
    window: TmuxWindow,
    *,
    pane_text: str,
    previous_log_bytes: int | None,
    previous_snapshot_hash: str | None,
    current_log_bytes: int,
    current_snapshot_hash: str,
) -> list[str]:
    session_name = launch.session.name
    shell_commands = {"bash", "zsh", "sh", "fish"}
    active_alerts: list[str] = []

    if window.pane_dead:
        supervisor.msg_store.upsert_alert(
            session_name,
            "pane_dead",
            "error",
            f"Pane {window.pane_id} in window {window.name} has exited",
        )
        active_alerts.append("pane_dead")
    else:
        supervisor.msg_store.clear_alert(session_name, "pane_dead")

    if window.pane_current_command in shell_commands:
        supervisor.msg_store.upsert_alert(
            session_name,
            "shell_returned",
            "warn",
            f"Window {window.name} appears to be back at the shell prompt ({window.pane_current_command})",
        )
        active_alerts.append("shell_returned")
    else:
        supervisor.msg_store.clear_alert(session_name, "shell_returned")

    if previous_log_bytes is not None and current_log_bytes <= previous_log_bytes:
        supervisor.msg_store.upsert_alert(
            session_name,
            "idle_output",
            "warn",
            f"No new pane output since the previous heartbeat for window {window.name}",
        )
        active_alerts.append("idle_output")
    else:
        supervisor.msg_store.clear_alert(session_name, "idle_output")

    if previous_snapshot_hash and previous_snapshot_hash == current_snapshot_hash:
        history = supervisor.store.recent_heartbeats(session_name, limit=3)
        recent_hashes = [item.snapshot_hash for item in history[:3]]
        if len(recent_hashes) == 3 and len(set(recent_hashes)) == 1:
            # #765 — route through the single classifier rather than
            # maintaining a parallel role-exclusion list here. The same
            # logic already gates heartbeats/local.py's detector.
            from pollypm.heartbeats.stall_classifier import (
                StallContext,
                classify_stall,
            )

            from pollypm.heartbeats.stall_classifier import (
                has_pending_work_for_session,
            )

            stall_class = classify_stall(
                StallContext(
                    role=launch.session.role or "",
                    session_name=session_name,
                    has_pending_work=has_pending_work_for_session(
                        supervisor.config, session_name,
                    ),
                )
            )
            if stall_class != "unrecoverable_stall":
                supervisor.msg_store.clear_alert(session_name, "suspected_loop")
            else:
                # #760 — action-forward copy matching the heartbeat path.
                supervisor.msg_store.upsert_alert(
                    session_name,
                    "suspected_loop",
                    "warn",
                    (
                        f"{launch.session.role or 'session'} {session_name} "
                        f"stalled — no new output for 3 heartbeats with "
                        f"queued work. Try: pm session restart {session_name}"
                    ),
                )
                active_alerts.append("suspected_loop")
                longer_history = supervisor.store.recent_heartbeats(session_name, limit=5)
                longer_hashes = [item.snapshot_hash for item in longer_history[:5]]
                if len(longer_hashes) == 5 and len(set(longer_hashes)) == 1:
                    _maybe_nudge_stalled_session(supervisor, launch)
        else:
            supervisor.msg_store.clear_alert(session_name, "suspected_loop")
    else:
        supervisor.msg_store.clear_alert(session_name, "suspected_loop")

    lowered_pane = pane_text.lower()
    if supervisor.pane_has_auth_failure(lowered_pane):
        supervisor.msg_store.upsert_alert(
            session_name,
            "auth_broken",
            "error",
            f"Window {window.name} reported authentication failure",
        )
        supervisor.store.upsert_account_runtime(
            account_name=launch.account.name,
            provider=launch.account.provider.value,
            status="auth_broken",
            reason="live session reported authentication failure",
        )
        active_alerts.append("auth_broken")
    else:
        supervisor.msg_store.clear_alert(session_name, "auth_broken")

    if supervisor.pane_has_capacity_failure(lowered_pane):
        supervisor.msg_store.upsert_alert(
            session_name,
            "capacity_exhausted",
            "error",
            f"Window {window.name} reported a usage or quota limit",
        )
        supervisor.store.upsert_account_runtime(
            account_name=launch.account.name,
            provider=launch.account.provider.value,
            status="exhausted",
            reason="live session reported capacity exhaustion",
        )
        active_alerts.append("capacity_exhausted")
    else:
        supervisor.msg_store.clear_alert(session_name, "capacity_exhausted")

    if supervisor.pane_has_provider_outage(lowered_pane):
        supervisor.msg_store.upsert_alert(
            session_name,
            "provider_outage",
            "warn",
            f"Window {window.name} appears to be hitting a provider outage",
        )
        supervisor.store.upsert_account_runtime(
            account_name=launch.account.name,
            provider=launch.account.provider.value,
            status="provider_outage",
            reason="live session reported upstream provider instability",
            available_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        )
        active_alerts.append("provider_outage")
    else:
        supervisor.msg_store.clear_alert(session_name, "provider_outage")

    return active_alerts


def _maybe_nudge_stalled_session(supervisor: SupervisorAlertBoundary, launch: SessionLaunchSpec) -> None:
    if launch.session.role == "reviewer":
        _maybe_nudge_reviewer_review(supervisor, launch)
        return
    if launch.session.role == "operator-pm":
        return
    if launch.session.role != "worker":
        return
    lease = supervisor.store.get_lease(launch.session.name)
    if lease is not None and lease.owner == "human":
        supervisor.msg_store.record_event(
            scope=launch.session.name,
            sender=launch.session.name,
            subject="heartbeat_nudge_skipped",
            payload={
                "message": (
                    "Skipped stalled-worker nudge because session is "
                    "leased to human"
                ),
            },
        )
        return
    nudge = _build_task_nudge(supervisor, launch)
    supervisor.send_input(
        launch.session.name,
        nudge or supervisor._STALL_NUDGE_MESSAGE,
        owner="heartbeat",
        force=lease is not None and lease.owner != "human",
    )


def _maybe_nudge_reviewer_review(
    supervisor: SupervisorAlertBoundary,
    launch: SessionLaunchSpec,
) -> None:
    """Nudge the reviewer if tasks are waiting for review."""
    lease = supervisor.store.get_lease(launch.session.name)
    if lease is not None and lease.owner == "human":
        return
    nudge = _build_review_nudge(supervisor)
    if nudge is None:
        return
    try:
        supervisor.send_input(
            launch.session.name,
            nudge,
            owner="heartbeat",
            force=lease is not None and lease.owner != "human",
        )
    except Exception:  # noqa: BLE001
        pass


def _build_review_nudge(supervisor: SupervisorAlertBoundary) -> str | None:
    """Check all projects for tasks in review state."""
    try:
        from pollypm.work.cli import _resolve_db_path

        review_tasks: list[str] = []
        live_keys: set[str] = set()
        for project_key in supervisor.config.projects:
            live_keys.add(project_key)
            db_path = _resolve_db_path(".pollypm/state.db", project=project_key)
            if not db_path.exists():
                _REVIEW_NUDGE_CACHE.pop(project_key, None)
                continue
            entries = _review_tasks_for_project(project_key, db_path)
            review_tasks.extend(entries)
        for stale in set(_REVIEW_NUDGE_CACHE) - live_keys:
            _REVIEW_NUDGE_CACHE.pop(stale, None)
        if not review_tasks:
            return None
        n = len(review_tasks)
        word = "task" if n == 1 else "tasks"
        lines = [
            f"You have {n} {word} waiting for your review:",
            *review_tasks,
            "",
            "Review with: pm task status <id>, then pm task approve <id> --actor russell or pm task reject <id> --actor russell --reason \"...\"",
        ]
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return None


def _build_task_nudge(supervisor: SupervisorAlertBoundary, launch: SessionLaunchSpec) -> str | None:
    """Check for queued tasks assigned to this worker and build a nudge message."""
    try:
        from pollypm.work.cli import _resolve_db_path
        from pollypm.work.sqlite_service import SQLiteWorkService

        project = launch.session.project
        db_path = _resolve_db_path(".pollypm/state.db", project=project)
        if not db_path.exists():
            return None
        with SQLiteWorkService(db_path=db_path) as svc:
            task = svc.next(project=project)
            if task is None:
                return None
            return (
                f"You have work waiting. Task {task.task_id} — \"{task.title}\" "
                f"is queued for your project. "
                f"Claim it: pm task claim {task.task_id}"
            )
    except Exception:
        return None
