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

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from pollypm.config import PollyPMConfig
from pollypm.models import SessionLaunchSpec
from pollypm.signal_routing import (
    SignalActionability as _SignalActionability,
    SignalAudience as _SignalAudience,
    SignalEnvelope as _SignalEnvelope,
    SignalSeverity as _SignalSeverity,
    compute_dedupe_key as _compute_dedupe_key,
    envelope_for_alert as _envelope_for_alert,
    register_routed_emitter as _register_routed_emitter,
    route_signal as _route_signal,
)
from pollypm.store.protocol import Store
from pollypm.storage.state import StateStore
from pollypm.tmux.client import TmuxWindow

_logger = logging.getLogger(__name__)


# #894 — register the supervisor_alerts module as an emitter that
# routes through SignalEnvelope. The release gate's
# signal_routing_emitters check inspects ROUTED_EMITTERS for this
# name; representative migration site is the suspected_loop +
# stuck_session path that #765 cited as the canonical
# user-actionable case.
_register_routed_emitter("supervisor_alerts")


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


def _emit_routed_event(
    supervisor: "SupervisorAlertBoundary",
    *,
    scope: str,
    sender: str,
    subject: str,
    payload: dict,
) -> None:
    """#910 — single funnel for supervisor-alerts activity-feed events.

    Mirrors the heartbeat-side ``_emit_routed_event`` funnel: build a
    :class:`SignalEnvelope`, route it through :func:`route_signal`,
    then persist the legacy ``msg_store.record_event`` write so the
    routing policy is the source of truth for delivery surfaces.

    Supervisor-alerts events are operational (heartbeat-nudge skip
    notices and similar) so the envelope is constructed with
    ``OPERATOR`` audience + ``OPERATIONAL`` actionability — those
    classifications make :func:`route_signal` deliver to the
    Activity surface only, which matches the legacy event-store
    semantics this funnel preserves.
    """
    body = ""
    if isinstance(payload, dict):
        message_value = payload.get("message")
        if isinstance(message_value, str):
            body = message_value
    envelope = _SignalEnvelope(
        audience=_SignalAudience.OPERATOR,
        severity=_SignalSeverity.INFO,
        actionability=_SignalActionability.OPERATIONAL,
        source="supervisor_alerts",
        subject=subject,
        body=body,
        dedupe_key=_compute_dedupe_key(
            source="supervisor_alerts",
            kind=subject,
            target=scope,
        ),
        payload={"sender": sender, "scope": scope, **(payload or {})},
    )
    _route_signal(envelope)
    supervisor.msg_store.record_event(
        scope=scope,
        sender=sender,
        subject=subject,
        payload=payload,
    )


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
                # #894 — route through SignalEnvelope; the helper
                # ensures supervisor_alerts and the toast tier
                # classifier (cockpit_alerts.alert_channel) agree
                # on audience / actionability / dedupe.
                _suspect_body = (
                    f"{launch.session.role or 'session'} {session_name} "
                    f"stalled — no new output for 3 heartbeats with "
                    "queued work. Open Workers and restart the stalled session."
                )
                _route_signal(
                    _envelope_for_alert(
                        source="supervisor_alerts",
                        alert_type="suspected_loop",
                        severity_label="warn",
                        session_name=session_name,
                        subject=f"{session_name} appears stalled",
                        body=_suspect_body,
                        suggested_action="Open Workers and restart the stalled session.",
                    )
                )
                supervisor.msg_store.upsert_alert(
                    session_name,
                    "suspected_loop",
                    "warn",
                    _suspect_body,
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
        # #910 — routed through _emit_routed_event funnel so the
        # event passes through SignalEnvelope before persisting.
        _emit_routed_event(
            supervisor,
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
    # #804: route through the public ``pollypm.work.db_resolver`` so
    # supervisor monitoring isn't tied to a CLI-private helper. Narrow
    # the catch around the per-project DB read so a contract failure
    # in path resolution surfaces in logs instead of silently
    # short-circuiting the whole nudge.
    from pollypm.work.db_resolver import resolve_work_db_path

    review_tasks: list[str] = []
    live_keys: set[str] = set()
    for project_key in supervisor.config.projects:
        live_keys.add(project_key)
        try:
            # #928: pass the supervisor's bound config so isolated/test
            # configs aren't bypassed by an implicit load_config() that
            # picks up the developer's real workspace DB.
            db_path = resolve_work_db_path(
                project=project_key, config=supervisor.config
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "supervisor_alerts: review nudge could not resolve work DB for %s",
                project_key,
            )
            continue
        if not db_path.exists():
            _REVIEW_NUDGE_CACHE.pop(project_key, None)
            continue
        try:
            entries = _review_tasks_for_project(project_key, db_path)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "supervisor_alerts: review nudge query failed for %s",
                project_key,
            )
            continue
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
        "Open Tasks or Inbox, inspect each review, then use Approve or Reject.",
    ]
    return "\n".join(lines)


def _build_task_nudge(supervisor: SupervisorAlertBoundary, launch: SessionLaunchSpec) -> str | None:
    """Check for queued tasks assigned to this worker and build a nudge message."""
    # #804: same boundary fix as _build_review_nudge — public resolver,
    # logged failures instead of swallowing every exception.
    try:
        from pollypm.work.db_resolver import resolve_work_db_path
        from pollypm.work.sqlite_service import SQLiteWorkService

        project = launch.session.project
        # #928: same boundary fix as _build_review_nudge — route the
        # supervisor's bound config through the resolver so test
        # isolation (and any non-default-config supervisor) is honoured
        # instead of falling back to load_config() and reading the
        # developer's workspace DB.
        db_path = resolve_work_db_path(project=project, config=supervisor.config)
        if not db_path.exists():
            return None
        with SQLiteWorkService(db_path=db_path) as svc:
            task = svc.next(project=project)
            if task is None:
                return None
            return (
                f"You have work waiting. Task {task.task_id} — \"{task.title}\" "
                f"is queued for your project. "
                "Open Tasks to review the queue; Polly will claim it when worker capacity is available."
            )
    except Exception:
        return None
