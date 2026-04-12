from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path

from pollypm.accounts import (
    AccountStatus,
    add_account_via_login,
    list_account_statuses,
    relogin_account,
    remove_account,
    set_controller_account,
    set_open_permissions_default,
    toggle_failover_account,
)
from pollypm.config_patches import apply_preference_patch, detect_preference_patch, list_project_overrides
from pollypm.config import load_config
from pollypm.messaging import (
    append_thread_message,
    create_message,
    create_thread,
    get_thread,
    InboxMessage,
    list_open_messages,
    list_threads,
    read_handoff,
    set_handoff,
    transition_thread,
)
from pollypm.models import ProviderKind
from pollypm.projects import (
    enable_tracked_project,
    register_project,
    remove_project,
    set_workspace_root,
)
from pollypm.schedulers.base import ScheduledJob
from pollypm.supervisor import Supervisor
from pollypm.task_backends import get_task_backend
from pollypm.task_backends.base import TaskRecord
from pollypm.task_backends.github import GitHubTaskBackendValidation
from pollypm.transcript_ledger import recent_token_usage as list_recent_token_usage
from pollypm.transcript_ledger import sync_token_ledger
from pollypm.workers import (
    create_worker_session,
    launch_worker_session,
    remove_worker_session,
    stop_worker_session,
    suggest_worker_prompt,
)


@dataclass(slots=True)
class StatusSnapshot:
    launches: list[object]
    windows: list[object]
    alerts: list[object]
    leases: list[object]
    errors: list[str]


@dataclass(slots=True)
class InboxRouteDecision:
    thread_id: str
    owner: str
    state: str
    note: str
    reason: str


class PollyPMService:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    def load_supervisor(self) -> Supervisor:
        config = load_config(self.config_path)
        return Supervisor(config)

    def status_snapshot(self) -> StatusSnapshot:
        supervisor = self.load_supervisor()
        launches, windows, alerts, leases, errors = supervisor.status()
        return StatusSnapshot(
            launches=launches,
            windows=windows,
            alerts=alerts,
            leases=leases,
            errors=errors,
        )

    def session_status(self, session_name: str | None = None) -> dict[str, object]:
        supervisor = self.load_supervisor()
        launches, windows, alerts, leases, errors = supervisor.status()
        window_map = {window.name: window for window in windows}
        alert_counts: dict[str, int] = {}
        for alert in alerts:
            alert_counts[alert.session_name] = alert_counts.get(alert.session_name, 0) + 1
        lease_map = {lease.session_name: lease for lease in leases}

        sessions: list[dict[str, object]] = []
        for launch in launches:
            if session_name is not None and launch.session.name != session_name:
                continue
            runtime = supervisor.store.get_session_runtime(launch.session.name)
            window = window_map.get(launch.window_name)
            lease = lease_map.get(launch.session.name)
            sessions.append(
                {
                    "name": launch.session.name,
                    "role": launch.session.role,
                    "project": launch.session.project,
                    "provider": launch.session.provider.value,
                    "account": launch.account.name,
                    "window_name": launch.window_name,
                    "running": window is not None,
                    "pane_dead": None if window is None else window.pane_dead,
                    "pane_command": None if window is None else window.pane_current_command,
                    "status": runtime.status if runtime else "healthy",
                    "last_failure_message": runtime.last_failure_message if runtime else None,
                    "alert_count": alert_counts.get(launch.session.name, 0),
                    "lease_owner": None if lease is None else lease.owner,
                    "lease_note": None if lease is None else lease.note,
                }
            )
        return {"sessions": sessions, "errors": errors}

    def list_account_statuses(self) -> list[AccountStatus]:
        return list_account_statuses(self.config_path)

    def create_and_launch_worker(
        self,
        *,
        project_key: str,
        prompt: str | None,
        on_status: Callable[[str], None] | None = None,
        skip_stabilize: bool = False,
    ):
        session = create_worker_session(
            self.config_path,
            project_key=project_key,
            prompt=prompt,
        )
        supervisor = self.load_supervisor()
        if supervisor.tmux.has_session(supervisor.config.project.tmux_session):
            launch_worker_session(
                self.config_path, session.name,
                on_status=on_status, skip_stabilize=skip_stabilize,
            )
        return session

    def suggest_worker_prompt(self, *, project_key: str) -> str:
        return suggest_worker_prompt(self.config_path, project_key=project_key)

    def focus_session(self, session_name: str) -> None:
        self.load_supervisor().focus_session(session_name)

    def send_input(self, session_name: str, text: str, *, owner: str = "human") -> None:
        self.load_supervisor().send_input(session_name, text, owner=owner)

    def raise_alert(self, alert_type: str, session_name: str, message: str, *, severity: str = "warn") -> object:
        supervisor = self.load_supervisor()
        supervisor._require_session(session_name)
        supervisor.store.upsert_alert(session_name, alert_type, severity, message)
        alert = next(
            (
                item
                for item in supervisor.store.open_alerts()
                if item.session_name == session_name and item.alert_type == alert_type
            ),
            None,
        )
        if alert is None:
            raise RuntimeError(f"Alert {alert_type} for {session_name} was not persisted")
        supervisor.store.record_event(session_name, "alert", f"Raised {severity} alert {alert_type}: {message}")
        return alert

    def list_alerts(self) -> list[object]:
        return self.load_supervisor().store.open_alerts()

    def clear_alert(self, alert_id: int) -> object:
        supervisor = self.load_supervisor()
        alert = supervisor.store.clear_alert_by_id(alert_id)
        if alert is None:
            raise KeyError(f"Unknown alert id: {alert_id}")
        supervisor.store.record_event(
            alert.session_name,
            "alert",
            f"Cleared alert {alert.alert_type}#{alert_id}",
        )
        return alert

    def set_session_status(self, session_name: str, status: str, *, reason: str = "") -> object:
        supervisor = self.load_supervisor()
        supervisor._require_session(session_name)
        supervisor.store.upsert_session_runtime(
            session_name=session_name,
            status=status,
            last_failure_message=reason or None,
        )
        supervisor.store.record_event(session_name, "session_status", f"Set status to {status}: {reason}".rstrip(": "))
        runtime = supervisor.store.get_session_runtime(session_name)
        if runtime is None:
            raise RuntimeError(f"Session runtime for {session_name} was not updated")
        return runtime

    def record_heartbeat(self, session_name: str, payload: dict[str, object]) -> object:
        supervisor = self.load_supervisor()
        supervisor._require_session(session_name)
        launch = supervisor._launch_by_session(session_name)
        supervisor.store.record_heartbeat(
            session_name=session_name,
            tmux_window=str(payload.get("tmux_window", launch.window_name)),
            pane_id=str(payload.get("pane_id", "")),
            pane_command=str(payload.get("pane_command", "")),
            pane_dead=bool(payload.get("pane_dead", False)),
            log_bytes=int(payload.get("log_bytes", 0)),
            snapshot_path=str(payload.get("snapshot_path", "")),
            snapshot_hash=str(payload.get("snapshot_hash", "")),
        )
        supervisor.store.record_event(session_name, "heartbeat", "Recorded heartbeat snapshot")
        record = supervisor.store.latest_heartbeat(session_name)
        if record is None:
            raise RuntimeError(f"Heartbeat for {session_name} was not recorded")
        return record

    def run_heartbeat(self) -> None:
        self.load_supervisor().run_heartbeat()

    def ensure_pollypm(self) -> str:
        supervisor = self.load_supervisor()
        session_name = supervisor.config.project.tmux_session
        if not supervisor.tmux.has_session(session_name):
            return supervisor.bootstrap_tmux()
        supervisor.ensure_console_window()
        supervisor.ensure_heartbeat_schedule()
        return supervisor.config.pollypm.controller_account

    def add_account(self, provider: ProviderKind) -> tuple[str, str]:
        return add_account_via_login(self.config_path, provider)

    def relogin_account(self, identifier: str) -> tuple[str, str]:
        return relogin_account(self.config_path, identifier)

    def remove_account(self, identifier: str, *, delete_home: bool = False) -> tuple[str, str]:
        return remove_account(self.config_path, identifier, delete_home=delete_home)

    def set_controller_account(self, identifier: str) -> tuple[str, str]:
        return set_controller_account(self.config_path, identifier)

    def set_open_permissions_default(self, enabled: bool) -> bool:
        return set_open_permissions_default(self.config_path, enabled)

    def toggle_failover_account(self, identifier: str) -> tuple[str, bool]:
        return toggle_failover_account(self.config_path, identifier)

    def claim_lease(self, session_name: str, owner: str, note: str = "") -> None:
        self.load_supervisor().claim_lease(session_name, owner, note)

    def release_lease(self, session_name: str) -> None:
        self.load_supervisor().release_lease(session_name)

    def switch_session_account(self, session_name: str, account_name: str) -> None:
        self.load_supervisor().switch_session_account(session_name, account_name)

    def schedule_job(
        self,
        *,
        kind: str,
        run_at: datetime,
        payload: dict[str, object] | None = None,
        interval_seconds: int | None = None,
    ) -> ScheduledJob:
        return self.load_supervisor().schedule_job(
            kind=kind,
            run_at=run_at,
            payload=payload,
            interval_seconds=interval_seconds,
        )

    def list_jobs(self) -> list[ScheduledJob]:
        return self.load_supervisor().list_scheduled_jobs()

    def run_scheduled_jobs(self) -> list[ScheduledJob]:
        return self.load_supervisor().run_scheduled_jobs()

    def sync_token_ledger(self, *, account: str | None = None) -> int:
        return len(sync_token_ledger(self.config_path, account=account))

    def recent_token_usage(self, *, limit: int = 24):
        return list_recent_token_usage(self.config_path, limit=limit)

    def register_project(self, path: Path) -> tuple[str, str]:
        return register_project(self.config_path, path)

    def list_tasks(self, project_key: str, *, states: list[str] | None = None) -> list[TaskRecord]:
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).list_tasks(states=states)

    def get_task(self, project_key: str, task_id: str) -> TaskRecord:
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).get_task(task_id)

    def next_available_task(self, project_key: str) -> TaskRecord | None:
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).next_available()

    def task_history(self, project_key: str, task_id: str) -> list[str]:
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).task_history(task_id)

    def validate_task_backend(self, project_key: str) -> GitHubTaskBackendValidation | dict[str, object]:
        config = load_config(self.config_path)
        backend = self._task_backend(config, project_key)
        validate = getattr(backend, "validate", None)
        if callable(validate):
            return validate()
        return {"passed": True, "checks": ["not_applicable"], "errors": []}

    def create_task(self, project_key: str, *, title: str, body: str = "", state: str = "01-ready") -> TaskRecord:
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).create_task(title=title, body=body, state=state)

    def move_task(self, project_key: str, task_id: str, *, to_state: str) -> TaskRecord:
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).move_task(task_id, to_state)

    def append_task_note(self, project_key: str, task_name: str, *, text: str) -> Path:
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).append_note(task_name, text)

    def append_task_handoff(
        self,
        project_key: str,
        task_name: str,
        *,
        what_done: str,
        how_to_test: str,
        branch_or_pr: str = "",
        deviations: str = "",
    ) -> Path:
        sections = [
            "## Handoff",
            "",
            "### What Was Done",
            what_done.strip(),
            "",
            "### How To Test",
            how_to_test.strip(),
        ]
        if branch_or_pr.strip():
            sections.extend(
                [
                    "",
                    "### Branch / PR",
                    branch_or_pr.strip(),
                ]
            )
        if deviations.strip():
            sections.extend(
                [
                    "",
                    "### Deviations",
                    deviations.strip(),
                ]
            )
        text = "\n".join(sections).rstrip() + "\n"
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).append_note(task_name, text)

    def review_task(
        self,
        project_key: str,
        task_id: str,
        *,
        approved: bool,
        summary: str,
        verification: str,
        changes_requested: str = "",
    ) -> TaskRecord:
        config = load_config(self.config_path)
        backend = self._task_backend(config, project_key)
        task = backend.get_task(task_id)
        if task.state == "03-needs-review":
            backend.move_task(task_id, "04-in-review")
        elif task.state != "04-in-review":
            raise ValueError(
                f"Review decisions require issue {task_id} to be in 03-needs-review or 04-in-review, not {task.state}"
            )

        sections = [
            "## Review",
            "",
            "### Decision",
            "Approved" if approved else "Request Changes",
            "",
            "### Summary",
            summary.strip(),
            "",
            "### Independent Verification",
            verification.strip(),
        ]
        if not approved and changes_requested.strip():
            sections.extend(
                [
                    "",
                    "### Change Requests",
                    changes_requested.strip(),
                ]
            )
        text = "\n".join(sections).rstrip() + "\n"
        backend.append_note(task_id, text)
        return backend.move_task(task_id, "05-completed" if approved else "02-in-progress")

    def task_state_counts(self, project_key: str) -> dict[str, int]:
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).state_counts()

    def detect_preference_override(self, project_key: str, text: str) -> object | None:
        config = load_config(self.config_path)
        project_root = self._project_root(config, project_key)
        patch = detect_preference_patch(project_root, text)
        if patch is None:
            return None
        return {
            "kind": patch.kind,
            "target_name": patch.target_name,
            "path": str(patch.path),
            "offer_text": patch.offer_text,
        }

    def apply_preference_override(self, project_key: str, text: str) -> object:
        config = load_config(self.config_path)
        project_root = self._project_root(config, project_key)
        patch = apply_preference_patch(project_root, text)
        return {
            "kind": patch.kind,
            "target_name": patch.target_name,
            "path": str(patch.path),
            "offer_text": patch.offer_text,
        }

    def list_overrides(self, project_key: str) -> list[str]:
        config = load_config(self.config_path)
        project_root = self._project_root(config, project_key)
        return [str(path) for path in list_project_overrides(project_root)]

    def enable_tracked_project(self, key: str) -> tuple[str, bool]:
        return enable_tracked_project(self.config_path, key)

    def remove_project(self, key: str) -> tuple[str, str]:
        return remove_project(self.config_path, key)

    def set_workspace_root(self, path: Path) -> Path:
        return set_workspace_root(self.config_path, path)

    def stop_session(self, session_name: str) -> None:
        stop_worker_session(self.config_path, session_name)

    def remove_session(self, session_name: str) -> None:
        remove_worker_session(self.config_path, session_name)

    def create_inbox_item(self, *, sender: str, subject: str, body: str) -> Path:
        config = load_config(self.config_path)
        return create_message(config.project.root_dir, sender=sender, subject=subject, body=body)

    def list_inbox_items(self) -> list[object]:
        config = load_config(self.config_path)
        return list_open_messages(config.project.root_dir)

    def triage_inbox_item(self, item_name: str, *, actor: str, owner: str = "pm") -> object:
        config = load_config(self.config_path)
        return create_thread(config.project.root_dir, item_name, actor=actor, owner=owner)

    def route_inbox_thread(self, thread_id: str, *, actor: str = "pm") -> InboxRouteDecision:
        config = load_config(self.config_path)
        thread = get_thread(config.project.root_dir, thread_id)
        latest = self._latest_thread_message(thread)
        if self._message_needs_pm_ownership(latest):
            note = "PM kept ownership because the thread needs policy, scope, or priority judgment."
            set_handoff(config.project.root_dir, thread_id, owner="pm", actor=actor, note=note)
            return InboxRouteDecision(
                thread_id=thread_id,
                owner="pm",
                state=thread.state,
                note=note,
                reason="policy",
            )

        note = "PM routed an execution-only task to PA."
        set_handoff(config.project.root_dir, thread_id, owner="pa", actor=actor, note=note)
        next_thread = thread
        if thread.state == "threaded":
            next_thread = transition_thread(
                config.project.root_dir,
                thread_id,
                "waiting-on-pa",
                actor=actor,
                note=note,
            )
        return InboxRouteDecision(
            thread_id=thread_id,
            owner="pa",
            state=next_thread.state,
            note=note,
            reason="execution",
        )

    def list_inbox_threads(self, *, include_closed: bool = False) -> list[object]:
        config = load_config(self.config_path)
        return list_threads(config.project.root_dir, include_closed=include_closed)

    def get_inbox_thread(self, thread_id: str) -> object:
        config = load_config(self.config_path)
        return get_thread(config.project.root_dir, thread_id)

    def transition_inbox_thread(self, thread_id: str, state: str, *, actor: str, note: str = "") -> object:
        config = load_config(self.config_path)
        return transition_thread(config.project.root_dir, thread_id, state, actor=actor, note=note)

    def handoff_inbox_thread(self, thread_id: str, *, owner: str, actor: str, note: str = "") -> dict[str, object]:
        config = load_config(self.config_path)
        set_handoff(config.project.root_dir, thread_id, owner=owner, actor=actor, note=note)
        payload = read_handoff(config.project.root_dir, thread_id)
        return {str(key): value for key, value in payload.items()}

    def append_inbox_thread_message(self, thread_id: str, *, sender: str, subject: str, body: str) -> Path:
        config = load_config(self.config_path)
        return append_thread_message(config.project.root_dir, thread_id, sender=sender, subject=subject, body=body)

    def record_worker_reply_via_pa(
        self,
        thread_id: str,
        *,
        worker_name: str,
        subject: str,
        body: str,
        actor: str = "pa",
    ) -> object:
        config = load_config(self.config_path)
        append_thread_message(
            config.project.root_dir,
            thread_id,
            sender=worker_name,
            subject=subject,
            body=body,
        )
        note = "PA surfaced a worker reply back to PM."
        thread = get_thread(config.project.root_dir, thread_id)
        if thread.state == "waiting-on-pa":
            thread = transition_thread(
                config.project.root_dir,
                thread_id,
                "waiting-on-pm",
                actor=actor,
                note=note,
            )
        set_handoff(config.project.root_dir, thread_id, owner="pm", actor=actor, note=note)
        return get_thread(config.project.root_dir, thread_id)

    def _latest_thread_message(self, thread: object) -> InboxMessage:
        thread_paths = getattr(thread, "message_paths", [])
        if not thread_paths:
            raise RuntimeError(f"Inbox thread {getattr(thread, 'thread_id', 'unknown')} has no messages.")
        latest = thread_paths[-1]
        raw = latest.read_text()
        header, _, body = raw.partition("\n\n")
        fields = {}
        for line in header.splitlines():
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        return InboxMessage(
            path=latest,
            subject=fields.get("Subject", latest.stem),
            sender=fields.get("Sender", "unknown"),
            created_at=fields.get("Created-At", ""),
            body=body.strip(),
        )

    def _message_needs_pm_ownership(self, message: InboxMessage) -> bool:
        text = f"{message.subject}\n{message.body}".lower()
        policy_markers = (
            "should",
            "scope",
            "priority",
            "plan",
            "policy",
            "approve",
            "approval",
            "decide",
            "direction",
            "strategy",
        )
        if any(marker in text for marker in policy_markers):
            return True
        if "?" not in text:
            return False
        return any(marker in text for marker in ("what", "which", "why", "should", "priority", "scope"))

    def _project_root(self, config, project_key: str) -> Path:
        project = config.projects.get(project_key)
        if project is not None:
            return project.path
        return config.project.root_dir

    def _task_backend(self, config, project_key: str):
        return get_task_backend(self._project_root(config, project_key))


def render_json(data: object) -> str:
    def _normalize(value: object) -> object:
        if is_dataclass(value):
            return {key: _normalize(item) for key, item in asdict(value).items()}
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(key): _normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_normalize(item) for item in value]
        return value

    return json.dumps(_normalize(data), indent=2) + "\n"
