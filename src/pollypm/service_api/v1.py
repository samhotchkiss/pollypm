"""PollyPM Service API v1.

Stable facade that TUIs, CLIs, and integration tests consume. Prefer this
module over importing :class:`pollypm.supervisor.Supervisor` directly —
outside of :mod:`pollypm.core`, direct Supervisor imports are deprecated and
the import-boundary test (``tests/test_import_boundary.py``) enforces the
allow-list.

The public surface is re-exported from :mod:`pollypm.service_api`. Anything
underscore-prefixed is internal.

Example:
    from pathlib import Path
    from pollypm.models import ProviderKind
    from pollypm.service_api.v1 import PollyPMService

    service = PollyPMService(Path("/path/to/pollypm.toml"))
    sessions = service.session_status()["sessions"]
    accounts = service.list_cached_account_statuses()
    service.add_account(ProviderKind.CLAUDE)
"""

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
    list_cached_account_statuses,
    list_account_statuses,
    relogin_account,
    remove_account,
    set_controller_account,
    set_open_permissions_default,
    toggle_failover_account,
)
from pollypm.checkpoints import create_issue_completion_checkpoint, record_checkpoint
from pollypm.config_patches import apply_preference_patch, detect_preference_patch, list_project_overrides
from pollypm.config import load_config
from pollypm.plugins_builtin.activity_feed.summaries import activity_summary
from pollypm.itsalive import deploy_site, pending_deploys, sweep_pending_deploys
from pollypm.models import ProviderKind, SessionConfig, SessionLaunchSpec
from pollypm.projects import (
    enable_tracked_project,
    register_project,
    remove_project,
    set_workspace_root,
)
from pollypm.schedulers.base import ScheduledJob
from pollypm.storage.state import AlertRecord, StateStore
from pollypm.supervisor import Supervisor
from pollypm.task_backends import FileTaskBackend, get_task_backend
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
    """Point-in-time read of launches, tmux windows, alerts, leases, and errors."""

    launches: list[object]
    windows: list[object]
    alerts: list[object]
    leases: list[object]
    errors: list[str]


class PollyPMService:
    """Service facade for PollyPM — the stable surface TUIs and CLIs call."""

    def __init__(self, config_path: Path) -> None:
        """Bind the service to a config file; all calls use this path."""
        self.config_path = config_path

    def load_supervisor(self, *, readonly_state: bool = False) -> Supervisor:
        """Construct a fresh Supervisor from the bound config (internal helper)."""
        config = load_config(self.config_path)
        return Supervisor(config, readonly_state=readonly_state)

    def status_snapshot(self) -> StatusSnapshot:
        """Return a read-only snapshot of launches/windows/alerts/leases/errors."""
        supervisor = self.load_supervisor(readonly_state=True)
        launches, windows, alerts, leases, errors = supervisor.status()
        return StatusSnapshot(
            launches=launches,
            windows=windows,
            alerts=alerts,
            leases=leases,
            errors=errors,
        )

    def session_status(self, session_name: str | None = None) -> dict[str, object]:
        """Return session status (one or all) with runtime, lease, and alert counts."""
        supervisor = self.load_supervisor(readonly_state=True)
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
        return {"config_path": str(self.config_path), "sessions": sessions, "errors": errors}

    def list_account_statuses(self) -> list[AccountStatus]:
        """Return live status (logged-in / expired / etc.) for every configured account."""
        return list_account_statuses(self.config_path)

    def list_cached_account_statuses(self) -> list[AccountStatus]:
        """Return cached account status for fast interactive views."""
        return list_cached_account_statuses(self.config_path)

    def create_and_launch_worker(
        self,
        *,
        project_key: str,
        prompt: str | None,
        on_status: Callable[[str], None] | None = None,
        skip_stabilize: bool = False,
    ):
        """Create a worker session and launch it if the PollyPM tmux is running."""
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
        """Return a PollyPM-curated launch prompt for a worker in this project."""
        return suggest_worker_prompt(self.config_path, project_key=project_key)

    def focus_session(self, session_name: str) -> None:
        """Focus the given session in tmux (select its window and pane)."""
        self.load_supervisor().focus_session(session_name)

    def send_input(
        self,
        session_name: str,
        text: str,
        *,
        owner: str = "human",
        force: bool = False,
        press_enter: bool = True,
    ) -> None:
        """Send input text to a session's pane, attributed to ``owner``.

        ``force=True`` bypasses outstanding lease checks (used by humans
        who are interacting directly). ``press_enter=False`` leaves the
        text on the input bar without submitting.
        """
        self.load_supervisor().send_input(
            session_name,
            text,
            owner=owner,
            force=force,
            press_enter=press_enter,
        )

    def raise_alert(self, alert_type: str, session_name: str, message: str, *, severity: str = "warn") -> object:
        """Raise a session alert; persists to state store and records an event."""
        supervisor = self.load_supervisor()
        supervisor.require_session(session_name)
        # #349: writer on ``messages`` via the unified Store.
        supervisor.msg_store.upsert_alert(session_name, alert_type, severity, message)
        alert = next(
            (
                item
                for item in supervisor.open_alerts()
                if item.session_name == session_name and item.alert_type == alert_type
            ),
            None,
        )
        if alert is None:
            raise RuntimeError(f"Alert {alert_type} for {session_name} was not persisted")
        supervisor.msg_store.append_event(
            scope=session_name,
            sender=session_name,
            subject="alert",
            payload={
                "message": activity_summary(
                    summary=f"Raised {severity} alert {alert_type}: {message}",
                    severity=(
                        "critical" if severity == "critical" else "recommendation"
                    ),
                    verb="alerted",
                    subject=alert_type,
                ),
                "alert_type": alert_type,
                "severity": severity,
            },
        )
        return alert

    def list_alerts(self) -> list[object]:
        """Return all currently open alerts across sessions."""
        # #349: route through ``Supervisor.open_alerts`` so the read matches
        # the writer flip onto ``messages``.
        return self.load_supervisor().open_alerts()

    def clear_alert(self, alert_id: int) -> object:
        """Clear a single open alert by its id and record the clearance event.

        Alert rows live exclusively on the unified ``messages`` table
        (#349 + #342 drained the legacy ``alerts`` surface), so we
        resolve ``alert_id`` against ``messages`` and close via
        :meth:`Store.close_message`. Raises :class:`KeyError` when no
        matching open alert exists.
        """
        supervisor = self.load_supervisor()
        try:
            rows = supervisor.msg_store.query_messages(
                type="alert",
                state="open",
                limit=200,
            )
        except Exception:  # noqa: BLE001
            rows = []
        target = next((row for row in rows if int(row.get("id", 0)) == alert_id), None)
        if target is None:
            raise KeyError(f"Unknown alert id: {alert_id}")
        try:
            supervisor.msg_store.close_message(alert_id)
        except Exception:  # noqa: BLE001
            pass
        payload = target.get("payload") or {}
        subject = str(target.get("subject") or "")
        message_text = (
            subject[len("[Alert] "):] if subject.startswith("[Alert] ")
            else subject
        )
        alert = AlertRecord(
            session_name=str(target.get("scope") or ""),
            alert_type=str(target.get("sender") or ""),
            severity=str(payload.get("severity") or ""),
            message=message_text,
            status="cleared",
            created_at=str(target.get("created_at") or ""),
            updated_at=str(target.get("updated_at") or ""),
            alert_id=alert_id,
        )

        supervisor.msg_store.append_event(
            scope=alert.session_name,
            sender=alert.session_name,
            subject="alert",
            payload={
                "message": activity_summary(
                    summary=f"Cleared alert {alert.alert_type}#{alert_id}",
                    severity="routine",
                    verb="cleared",
                    subject=alert.alert_type,
                ),
                "alert_type": alert.alert_type,
                "alert_id": alert_id,
            },
        )
        return alert

    def set_session_status(self, session_name: str, status: str, *, reason: str = "") -> object:
        """Override a session's runtime status with an optional failure reason."""
        supervisor = self.load_supervisor()
        supervisor.require_session(session_name)
        supervisor.store.upsert_session_runtime(
            session_name=session_name,
            status=status,
            last_failure_message=reason or None,
        )
        supervisor.msg_store.append_event(
            scope=session_name,
            sender=session_name,
            subject="session_status",
            payload={
                "message": activity_summary(
                    summary=f"Set status to {status}: {reason}".rstrip(": "),
                    severity="routine",
                    verb="status_changed",
                    subject=session_name,
                    status=status,
                    reason=reason or None,
                ),
                "status": status,
                "reason": reason or None,
            },
        )
        runtime = supervisor.store.get_session_runtime(session_name)
        if runtime is None:
            raise RuntimeError(f"Session runtime for {session_name} was not updated")
        return runtime

    def record_heartbeat(self, session_name: str, payload: dict[str, object]) -> object:
        """Persist a heartbeat snapshot (pane + log + hash) for a session."""
        supervisor = self.load_supervisor()
        supervisor.require_session(session_name)
        launch = supervisor.launch_by_session(session_name)
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
        # Heartbeat snapshots are the canonical "nothing changed" event;
        # we keep the plain string so the projector's noise filter (see
        # event_projector._is_noise) drops them from the feed. A
        # structured payload here would make every heartbeat tick leak
        # into the UI.
        supervisor.msg_store.append_event(
            scope=session_name,
            sender=session_name,
            subject="heartbeat",
            payload={"message": "Recorded heartbeat snapshot"},
        )
        record = supervisor.store.latest_heartbeat(session_name)
        if record is None:
            raise RuntimeError(f"Heartbeat for {session_name} was not recorded")
        return record

    def run_heartbeat(self) -> None:
        """Execute one heartbeat tick across all managed sessions."""
        self.load_supervisor().run_heartbeat()

    def itsalive_deploy(
        self,
        *,
        project_key: str,
        subdomain: str | None = None,
        email: str | None = None,
        publish_dir: str = ".",
    ):
        """Deploy a project site via itsalive and return the deployment handle."""
        config = load_config(self.config_path)
        project = config.projects[project_key]
        return deploy_site(project.path, subdomain=subdomain, email=email, publish_dir=publish_dir)

    def itsalive_pending(self, *, project_key: str):
        """Return the list of pending itsalive deploys for a project."""
        config = load_config(self.config_path)
        project = config.projects[project_key]
        return pending_deploys(project.path)

    def itsalive_sweep(self, *, project_key: str):
        """Sweep completed pending itsalive deploys for a project."""
        config = load_config(self.config_path)
        project = config.projects[project_key]
        return sweep_pending_deploys(project.path)

    def ensure_pollypm(self) -> str:
        """Ensure the PollyPM tmux session exists and return the controller account name."""
        supervisor = self.load_supervisor()
        session_name = supervisor.config.project.tmux_session
        if not supervisor.tmux.has_session(session_name):
            return supervisor.bootstrap_tmux()
        supervisor.ensure_console_window()
        supervisor.ensure_heartbeat_schedule()
        return supervisor.config.pollypm.controller_account

    def add_account(self, provider: ProviderKind) -> tuple[str, str]:
        """Drive the provider login flow and register a new account."""
        return add_account_via_login(self.config_path, provider)

    def relogin_account(self, identifier: str) -> tuple[str, str]:
        """Re-run the login flow for an existing account to refresh credentials."""
        return relogin_account(self.config_path, identifier)

    def remove_account(self, identifier: str, *, delete_home: bool = False) -> tuple[str, str]:
        """Delete an account config, optionally removing its home directory."""
        return remove_account(self.config_path, identifier, delete_home=delete_home)

    def set_controller_account(self, identifier: str) -> tuple[str, str]:
        """Promote an account to be the PollyPM controller."""
        return set_controller_account(self.config_path, identifier)

    def set_open_permissions_default(self, enabled: bool) -> bool:
        """Toggle the global default for bypassing session permission prompts."""
        return set_open_permissions_default(self.config_path, enabled)

    def toggle_failover_account(self, identifier: str) -> tuple[str, bool]:
        """Toggle whether an account participates in failover and return the new state."""
        return toggle_failover_account(self.config_path, identifier)

    def claim_lease(self, session_name: str, owner: str, note: str = "") -> None:
        """Claim an exclusive lease on a session for ``owner``."""
        self.load_supervisor().claim_lease(session_name, owner, note)

    def release_lease(self, session_name: str) -> None:
        """Release the currently held lease on a session."""
        self.load_supervisor().release_lease(session_name)

    def switch_session_account(self, session_name: str, account_name: str) -> None:
        """Rebind a session to a different configured account."""
        self.load_supervisor().switch_session_account(session_name, account_name)

    def schedule_job(
        self,
        *,
        kind: str,
        run_at: datetime,
        payload: dict[str, object] | None = None,
        interval_seconds: int | None = None,
    ) -> ScheduledJob:
        """Schedule a recurring or one-shot job and return the persisted record."""
        return self.load_supervisor().schedule_job(
            kind=kind,
            run_at=run_at,
            payload=payload,
            interval_seconds=interval_seconds,
        )

    def list_jobs(self) -> list[ScheduledJob]:
        """List all currently scheduled jobs."""
        return self.load_supervisor().list_scheduled_jobs()

    def run_scheduled_jobs(self) -> list[ScheduledJob]:
        """Run any due scheduled jobs and return the executed records."""
        return self.load_supervisor().run_scheduled_jobs()

    def sync_token_ledger(self, *, account: str | None = None) -> int:
        """Pull new token-usage rows into the ledger and return the count synced."""
        return len(sync_token_ledger(self.config_path, account=account))

    def recent_token_usage(self, *, limit: int = 24):
        """Return the most recent ``limit`` token-usage snapshots."""
        return list_recent_token_usage(self.config_path, limit=limit)

    def register_project(self, path: Path) -> tuple[str, str]:
        """Register a new tracked project rooted at ``path``."""
        return register_project(self.config_path, path)

    def list_tasks(self, project_key: str, *, states: list[str] | None = None) -> list[TaskRecord]:
        """List tasks for a project, optionally filtered by states."""
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).list_tasks(states=states)

    def get_task(self, project_key: str, task_id: str) -> TaskRecord:
        """Fetch a single task record by id for a project."""
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).get_task(task_id)

    def next_available_task(self, project_key: str) -> TaskRecord | None:
        """Return the next pickup-able task in a project, or ``None``."""
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).next_available()

    def task_history(self, project_key: str, task_id: str) -> list[str]:
        """Return the human-readable history entries for a task."""
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).task_history(task_id)

    def validate_task_backend(self, project_key: str) -> GitHubTaskBackendValidation | dict[str, object]:
        """Run backend-specific validation (e.g. GitHub auth/scopes) for a project."""
        config = load_config(self.config_path)
        backend = self._task_backend(config, project_key)
        validate = getattr(backend, "validate", None)
        if callable(validate):
            return validate()
        return {"passed": True, "checks": ["not_applicable"], "errors": []}

    def create_task(self, project_key: str, *, title: str, body: str = "", state: str = "01-ready") -> TaskRecord:
        """Create a new task in a project with an initial state."""
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).create_task(title=title, body=body, state=state)

    def move_task(self, project_key: str, task_id: str, *, to_state: str) -> TaskRecord:
        """Transition a task to a new state and record a completion checkpoint if terminal."""
        config = load_config(self.config_path)
        backend = self._task_backend(config, project_key)
        task = backend.get_task(task_id)
        moved = backend.move_task(task_id, to_state)
        self._record_completion_checkpoint_if_needed(
            config,
            project_key,
            backend,
            task=task,
            moved=moved,
        )
        return moved

    def append_task_note(self, project_key: str, task_name: str, *, text: str) -> Path:
        """Append a free-form note to a task's history and return the note file path."""
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
        """Append a structured handoff note (what done, how to test, branch/PR) to a task."""
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
        """Record a review decision on a task and transition state accordingly."""
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
        moved = backend.move_task(task_id, "05-completed" if approved else "02-in-progress")
        self._record_completion_checkpoint_if_needed(
            config,
            project_key,
            backend,
            task=task,
            moved=moved,
            review_summary=summary,
            verification=verification,
        )
        return moved

    def task_state_counts(self, project_key: str) -> dict[str, int]:
        """Return counts of tasks grouped by state for a project."""
        config = load_config(self.config_path)
        return self._task_backend(config, project_key).state_counts()

    def detect_preference_override(self, project_key: str, text: str) -> object | None:
        """Detect a project-preference override in user text; return a patch dict or None."""
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
        """Apply a detected preference override to a project config."""
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
        """List override file paths currently layered over a project's config."""
        config = load_config(self.config_path)
        project_root = self._project_root(config, project_key)
        return [str(path) for path in list_project_overrides(project_root)]

    def _record_completion_checkpoint_if_needed(
        self,
        config,
        project_key: str,
        backend,
        *,
        task: TaskRecord,
        moved: TaskRecord,
        review_summary: str = "",
        verification: str = "",
    ) -> None:
        if moved.state != "05-completed" or not isinstance(backend, FileTaskBackend):
            return
        launch = self._checkpoint_launch_for_project(config, project_key)
        checkpoint_data, checkpoint_artifact = create_issue_completion_checkpoint(
            config,
            launch,
            task_title=task.title,
            task_path=moved.path,
            review_summary=review_summary,
            verification=verification,
        )
        store = StateStore(config.project.state_db)
        # TODO(#342-followup): ``record_checkpoint`` still writes the
        # ``checkpoints`` domain table through StateStore; port to a Core
        # Table def when the checkpoint surface moves.
        record_checkpoint(
            store,
            launch,
            project_key=project_key,
            level="level1",
            artifact=checkpoint_artifact,
            snapshot_path=moved.path,
            memory_backend_name=config.memory.backend,
        )
        # #349: audit event lands on the unified ``messages`` table via Store.
        from pollypm.store.registry import get_store

        msg_store = get_store(config)
        try:
            msg_store.append_event(
                scope=launch.session.name,
                sender=launch.session.name,
                subject="checkpoint",
                payload={
                    "message": activity_summary(
                        summary=(
                            f"Recorded Level 1 checkpoint for completed issue "
                            f"{task.task_id}: {checkpoint_data.checkpoint_id}"
                        ),
                        severity="routine",
                        verb="checkpointed",
                        subject=task.task_id,
                        project=project_key,
                        checkpoint_id=checkpoint_data.checkpoint_id,
                    ),
                    "task_id": task.task_id,
                    "project": project_key,
                    "checkpoint_id": checkpoint_data.checkpoint_id,
                },
            )
        finally:
            close = getattr(msg_store, "close", None)
            if callable(close):
                close()

    def _checkpoint_launch_for_project(self, config, project_key: str) -> SessionLaunchSpec:
        project = config.projects[project_key]
        for session in config.sessions.values():
            if session.role == "worker" and session.project == project_key:
                account = config.accounts[session.account]
                return SessionLaunchSpec(
                    session=session,
                    account=account,
                    window_name=session.window_name or session.name,
                    log_path=config.project.logs_dir / f"{session.name}.log",
                    command="checkpoint",
                )

        session_name = f"worker_{project_key}"
        account_name = config.pollypm.controller_account
        account = config.accounts[account_name]
        session = SessionConfig(
            name=session_name,
            role="worker",
            provider=account.provider,
            account=account_name,
            cwd=project.path,
            project=project_key,
            window_name=f"worker-{project_key}",
        )
        return SessionLaunchSpec(
            session=session,
            account=account,
            window_name=session.window_name,
            log_path=config.project.logs_dir / f"{session.name}.log",
            command="checkpoint",
        )

    def enable_tracked_project(self, key: str) -> tuple[str, bool]:
        """Enable tracking for a known project and return the (key, toggled) result."""
        return enable_tracked_project(self.config_path, key)

    def remove_project(self, key: str) -> tuple[str, str]:
        """Remove a project from tracking; returns (key, message)."""
        return remove_project(self.config_path, key)

    def set_workspace_root(self, path: Path) -> Path:
        """Set the user's workspace root directory and return the resolved path."""
        return set_workspace_root(self.config_path, path)

    def stop_session(self, session_name: str) -> None:
        """Stop a worker session (kills its tmux window) without removing config."""
        stop_worker_session(self.config_path, session_name)

    def remove_session(self, session_name: str) -> None:
        """Stop and delete a worker session's config entry."""
        remove_worker_session(self.config_path, session_name)

    def _project_root(self, config, project_key: str) -> Path:
        project = config.projects.get(project_key)
        if project is not None:
            return project.path
        return config.project.root_dir

    def _task_backend(self, config, project_key: str):
        return get_task_backend(self._project_root(config, project_key))


def plan_launches_readonly(config, store) -> list:
    """Return the launches planned for ``config`` without mutating state.

    Thin wrapper used by read-only surfaces (dashboard, cockpit rail) that
    already hold an open state store and only need ``SessionLaunchSpec``
    objects. Avoids a second StateStore open by injecting the caller's
    store into a minimal Supervisor shell.
    """
    from pollypm.session_services import create_tmux_client

    supervisor = Supervisor.__new__(Supervisor)
    supervisor.config = config
    supervisor.store = store
    supervisor.readonly_state = True
    supervisor.tmux = create_tmux_client()
    supervisor.invalidate_launch_cache()
    return supervisor.plan_launches()


def render_json(data: object) -> str:
    """Render a value as pretty JSON, normalizing dataclasses / datetimes / Paths."""
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


__all__ = [
    "PollyPMService",
    "StatusSnapshot",
    "render_json",
]
