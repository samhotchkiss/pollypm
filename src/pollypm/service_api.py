from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
from pollypm.config import load_config
from pollypm.models import ProviderKind
from pollypm.projects import (
    enable_tracked_project,
    register_project,
    remove_project,
    set_workspace_root,
)
from pollypm.schedulers.base import ScheduledJob
from pollypm.supervisor import Supervisor
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


class PollyPMService:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    def load_supervisor(self) -> Supervisor:
        return Supervisor(load_config(self.config_path))

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

    def list_account_statuses(self) -> list[AccountStatus]:
        return list_account_statuses(self.config_path)

    def create_and_launch_worker(self, *, project_key: str, prompt: str | None):
        session = create_worker_session(
            self.config_path,
            project_key=project_key,
            prompt=prompt,
        )
        supervisor = self.load_supervisor()
        if supervisor.tmux.has_session(supervisor.config.project.tmux_session):
            launch_worker_session(self.config_path, session.name)
        return session

    def suggest_worker_prompt(self, *, project_key: str) -> str:
        return suggest_worker_prompt(self.config_path, project_key=project_key)

    def focus_session(self, session_name: str) -> None:
        self.load_supervisor().focus_session(session_name)

    def send_input(self, session_name: str, text: str, *, owner: str = "human") -> None:
        self.load_supervisor().send_input(session_name, text, owner=owner)

    def run_heartbeat(self) -> None:
        self.load_supervisor().run_heartbeat()

    def ensure_pollypm(self) -> str:
        supervisor = self.load_supervisor()
        session_name = supervisor.config.project.tmux_session
        if not supervisor.tmux.has_session(session_name):
            return supervisor.bootstrap_tmux()
        supervisor.ensure_console_window()
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
