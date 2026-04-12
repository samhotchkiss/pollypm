from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pollypm.agent_profiles import get_agent_profile
from pollypm.agent_profiles.base import AgentProfileContext
from pollypm.checkpoints import record_checkpoint, snapshot_hash, write_mechanical_checkpoint
from pollypm.config import PollyPMConfig
from pollypm.heartbeats import get_heartbeat_backend
from pollypm.heartbeats.api import SupervisorHeartbeatAPI
from pollypm.knowledge_extract import EXTRACTION_INTERVAL_SECONDS
from pollypm.messaging import ensure_inbox
from pollypm.models import AccountConfig, ProviderKind, SessionConfig, SessionLaunchSpec
from pollypm.onboarding import _prime_claude_home, default_control_args, default_session_args
from pollypm.providers import get_provider
from pollypm.providers.base import LaunchCommand
from pollypm.projects import ensure_project_scaffold, ensure_session_lock
from pollypm.projects import project_checkpoints_dir, project_transcripts_dir, project_worktrees_dir, release_session_lock
from pollypm.runtimes import get_runtime
from pollypm.schedulers import ScheduledJob, get_scheduler_backend
from pollypm.transcript_ledger import sync_token_ledger_for_config
from pollypm.storage.state import AlertRecord, LeaseRecord, StateStore
from pollypm.tmux.client import TmuxClient, TmuxWindow


_OWNER_PREFIXES = {
    "heartbeat": "H:",
    "polly": "P:",
    "operator": "P:",
}


def _prefix_for_owner(owner: str, text: str) -> str:
    """Prepend an owner tag so recipients can identify who injected a message."""
    prefix = _OWNER_PREFIXES.get(owner)
    if prefix is None:
        return text
    return f"{prefix} {text}"


class Supervisor:
    _CONTROL_ROLES = {"heartbeat-supervisor", "operator-pm"}
    _CONSOLE_WINDOW = "PollyPM"
    _STORAGE_CLOSET_SESSION_SUFFIX = "-storage-closet"
    _CONTROL_HOMES_DIR = "control-homes"
    _RECOVERY_WINDOW = timedelta(minutes=30)
    _RECOVERY_LIMIT = 5
    _RECOVERY_HARD_LIMIT = 20  # stop entirely after this many total attempts
    _STALL_NUDGE_MESSAGE = (
        "You appear stalled. State the remaining task in one sentence, "
        "execute the next step now."
    )
    _WORKER_BLOCKED_PM_COMMANDS = {
        "up",
        "down",
        "reset",
        "send",
        "console",
        "worker-start",
        "stop-session",
        "remove-session",
        "switch-session-account",
    }

    def __init__(self, config: PollyPMConfig) -> None:
        self.config = config
        self.tmux = TmuxClient()
        self.store = StateStore(config.project.state_db)
        self._cached_launches: list[SessionLaunchSpec] | None = None

    def _effective_session(self, session: SessionConfig, controller_account: str | None = None) -> SessionConfig:
        effective = session
        runtime = self.store.get_session_runtime(session.name)
        override_account: str | None = None
        override_applied = False
        if controller_account is not None and session.role in self._CONTROL_ROLES:
            override_account = controller_account
            override_applied = True
        elif runtime is not None and runtime.effective_account:
            override_account = runtime.effective_account
            override_applied = True
        if override_account is not None:
            account = self.config.accounts[override_account]
            effective = replace(effective, provider=account.provider, account=override_account)
        account = self.config.accounts[effective.account]
        profile_prompt = self._resolve_profile_prompt(effective, account)
        if effective.role in self._CONTROL_ROLES:
            effective = replace(
                effective,
                prompt=profile_prompt or effective.prompt,
                args=default_control_args(
                    account.provider,
                    open_permissions=self.config.pollypm.open_permissions_by_default,
                    role=effective.role,
                ),
            )
        else:
            if profile_prompt and not effective.prompt:
                effective = replace(effective, prompt=profile_prompt)
            if not effective.args:
                effective = replace(
                    effective,
                    args=default_session_args(
                        account.provider,
                        open_permissions=self.config.pollypm.open_permissions_by_default,
                        role=effective.role,
                    ),
                )
        return effective

    def _default_agent_profile(self, session: SessionConfig) -> str | None:
        if session.role == "heartbeat-supervisor":
            return "heartbeat"
        if session.role == "operator-pm":
            return "polly"
        if session.role == "worker":
            return "worker"
        return None

    def _resolve_profile_prompt(self, session: SessionConfig, account: AccountConfig) -> str | None:
        profile_name = session.agent_profile or self._default_agent_profile(session)
        if not profile_name:
            return None
        profile = get_agent_profile(profile_name, root_dir=self.config.project.root_dir)
        return profile.build_prompt(
            AgentProfileContext(
                config=self.config,
                session=session,
                account=account,
            )
        )

    def storage_closet_session_name(self) -> str:
        return f"{self.config.project.tmux_session}{self._STORAGE_CLOSET_SESSION_SUFFIX}"

    def heartbeat_tmux_session_name(self) -> str:
        return self.storage_closet_session_name()

    def _tmux_session_for_role(self, role: str) -> str:
        return self.storage_closet_session_name()

    def _tmux_session_for_launch(self, launch: SessionLaunchSpec) -> str:
        return self._tmux_session_for_role(launch.session.role)

    def _tmux_session_for_session(self, session_name: str) -> str:
        launch = self._launch_by_session(session_name)
        return self._tmux_session_for_launch(launch)

    def _all_tmux_session_names(self) -> list[str]:
        names = [self.config.project.tmux_session]
        storage = self.storage_closet_session_name()
        if storage not in names:
            names.append(storage)
        return names

    def plan_launches(self, *, controller_account: str | None = None) -> list[SessionLaunchSpec]:
        # Cache launches for the default (no controller override) case.
        # The launch plan only changes when config changes.
        if controller_account is None and self._cached_launches is not None:
            return self._cached_launches
        launches: list[SessionLaunchSpec] = []
        worker_projects: dict[str, str] = {}
        for session in self.config.sessions.values():
            effective = self._effective_session(session, controller_account)
            if not effective.enabled:
                continue
            if effective.role == "worker":
                existing = worker_projects.get(effective.project)
                if existing is not None:
                    raise ValueError(
                        f"Project {effective.project} is assigned to more than one worker session: "
                        f"{existing} and {effective.name}"
                    )
                worker_projects[effective.project] = effective.name
            account = self._effective_account(effective, self.config.accounts[effective.account])
            if account.provider is not effective.provider:
                raise ValueError(
                    f"Session {effective.name} uses provider {effective.provider.value} "
                    f"but account {account.name} is configured for {account.provider.value}"
                )
            provider = get_provider(effective.provider, root_dir=self.config.project.root_dir)
            launch = provider.build_launch_command(effective, account)
            launch = self._apply_role_launch_restrictions(effective, launch)
            if effective.provider is ProviderKind.CODEX and effective.role in self._CONTROL_ROLES and launch.initial_input:
                env = dict(launch.env)
                env["PM_CODEX_HOME_AGENTS_MD"] = launch.initial_input
                launch = replace(launch, env=env, initial_input=None)
            runtime = get_runtime(account.runtime, root_dir=self.config.project.root_dir)
            window_name = effective.window_name or effective.name
            log_dir = self.config.project.logs_dir / effective.name
            ensure_session_lock(log_dir, effective.name)
            log_path = log_dir / f"{window_name}.log"
            launches.append(
                SessionLaunchSpec(
                    session=effective,
                    account=account,
                    window_name=window_name,
                    log_path=log_path,
                    command=runtime.wrap_command(launch, account, self.config.project),
                    resume_marker=launch.resume_marker,
                    initial_input=launch.initial_input,
                    fresh_launch_marker=launch.fresh_launch_marker,
                )
            )
        if controller_account is None:
            self._cached_launches = launches
        return launches

    def bootstrap_tmux(
        self,
        *,
        skip_probe: bool = False,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        session_name = self.config.project.tmux_session
        existing = [name for name in self._all_tmux_session_names() if self.tmux.has_session(name)]
        if existing:
            # Sessions already running — reconcile instead of failing
            return self._reconcile_existing(session_name, on_status=on_status)

        failures: list[str] = []
        for controller_account in self._controller_candidates():
            launches = self.plan_launches(controller_account=controller_account)
            if not launches:
                raise RuntimeError("No enabled sessions found in config.")

            try:
                if skip_probe:
                    pass
                else:
                    self._probe_controller_account(controller_account)
                self._bootstrap_launches(session_name, launches, on_status=on_status)
                self.ensure_heartbeat_schedule()
                self.ensure_knowledge_extraction_schedule()
                self.store.record_event(
                    "pollypm",
                    "controller_selected",
                    f"Selected controller account {controller_account}",
                )
                return controller_account
            except RuntimeError as exc:
                failures.append(f"{controller_account}: {exc}")
                for tmux_session in self._all_tmux_session_names():
                    if self.tmux.has_session(tmux_session):
                        self.tmux.kill_session(tmux_session)

        raise RuntimeError("PollyPM could not launch any controller account: " + "; ".join(failures))

    def _bootstrap_clear_markers(self) -> None:
        """Clear stale session markers so all sessions start fresh."""
        for homes_dir in [self.config.project.base_dir / "homes", self.config.project.base_dir / "control-homes"]:
            if homes_dir.is_dir():
                for marker in homes_dir.glob("*/.pollypm-state/session-markers/*"):
                    marker.unlink(missing_ok=True)

    def _reconcile_existing(
        self,
        session_name: str,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        """Reconcile running state: create missing windows without killing existing ones."""
        _status = on_status or (lambda _: None)
        storage_session = self.storage_closet_session_name()
        launches = self.plan_launches()
        existing_windows: set[str] = set()
        if self.tmux.has_session(storage_session):
            for w in self.tmux.list_windows(storage_session):
                existing_windows.add(w.name)

        created = 0
        for launch in launches:
            if launch.window_name in existing_windows:
                continue
            _status(f"Recreating {launch.session.name}...")
            if not self.tmux.has_session(storage_session):
                self.tmux.create_session(storage_session, launch.window_name, launch.command)
            else:
                self.tmux.create_window(storage_session, launch.window_name, launch.command, detached=True)
            target = f"{storage_session}:{launch.window_name}"
            self.tmux.set_window_option(target, "allow-passthrough", "on")
            self.tmux.set_window_option(target, "focus-events", "on")
            self.tmux.pipe_pane(target, launch.log_path)
            self._record_launch(launch)
            created += 1

        if not self.tmux.has_session(session_name):
            self.tmux.create_session(session_name, self._CONSOLE_WINDOW, self._console_command(), remain_on_exit=False)

        self.ensure_heartbeat_schedule()
        self.ensure_knowledge_extraction_schedule()
        _status(f"Reconciled: {created} session(s) created, {len(existing_windows)} already running")
        return self.config.pollypm.controller_account

    def _bootstrap_launches(
        self,
        session_name: str,
        launches: list[SessionLaunchSpec],
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        import threading

        storage_session = self.storage_closet_session_name()
        (self.config.project.base_dir / "cockpit_state.json").unlink(missing_ok=True)
        self._bootstrap_clear_markers()


        # Phase 1: Create all tmux windows up front (fast, no blocking).
        targets: list[tuple[SessionLaunchSpec, str]] = []
        if launches:
            first = launches[0]
            if on_status:
                on_status(f"Creating {first.session.name}...")
            self.tmux.create_session(storage_session, first.window_name, first.command)
            target = f"{storage_session}:0"
            self.tmux.set_window_option(target, "allow-passthrough", "on")
            self.tmux.set_window_option(target, "focus-events", "on")
            self.tmux.pipe_pane(target, first.log_path)
            self._record_launch(first)
            targets.append((first, target))
            for launch in launches[1:]:
                if on_status:
                    on_status(f"Creating {launch.session.name}...")
                self.tmux.create_window(storage_session, launch.window_name, launch.command, detached=True)
                target = f"{storage_session}:{launch.window_name}"
                self.tmux.set_window_option(target, "allow-passthrough", "on")
                self.tmux.set_window_option(target, "focus-events", "on")
                self.tmux.pipe_pane(target, launch.log_path)
                self._record_launch(launch)
                targets.append((launch, target))

        # Phase 2: Create the cockpit session so the user can attach immediately.
        self.tmux.create_session(session_name, self._CONSOLE_WINDOW, self._console_command(), remain_on_exit=False)
        console_target = f"{session_name}:{self._CONSOLE_WINDOW}"
        self.tmux.set_window_option(console_target, "allow-passthrough", "on")
        self.tmux.set_window_option(console_target, "focus-events", "on")
        self.tmux.set_window_option(console_target, "window-size", "latest")
        self.tmux.set_window_option(console_target, "aggressive-resize", "on")
        self.focus_console()

        # Phase 3: Stabilize all sessions in parallel background threads.
        # Daemon threads so the CLI can attach to tmux immediately
        # without waiting for stabilization to complete.
        def _stabilize_one(launch: SessionLaunchSpec, tgt: str) -> None:
            try:
                self._stabilize_launch(launch, tgt)
            except Exception as exc:  # noqa: BLE001
                # Record the failure so the heartbeat can detect and recover.
                try:
                    self.store.record_event(
                        launch.session.name,
                        "stabilize_failed",
                        f"Bootstrap stabilization failed: {exc}",
                    )
                    self.store.upsert_alert(
                        launch.session.name,
                        "stabilize_failed",
                        "warn",
                        f"Session {launch.session.name} failed to stabilize during bootstrap: {exc}",
                    )
                except Exception:  # noqa: BLE001
                    pass  # store itself may be unavailable

        for launch, tgt in targets:
            threading.Thread(target=_stabilize_one, args=(launch, tgt), daemon=True).start()

    def shutdown_tmux(self) -> None:
        for session_name in reversed(self._all_tmux_session_names()):
            if self.tmux.has_session(session_name):
                self.tmux.kill_session(session_name)

    def _record_launch(self, launch: SessionLaunchSpec) -> None:
        self.store.upsert_session(
            name=launch.session.name,
            role=launch.session.role,
            project=launch.session.project,
            provider=launch.session.provider.value,
            account=launch.account.name,
            cwd=str(launch.session.cwd),
            window_name=launch.window_name,
        )
        self.store.clear_alert(launch.session.name, "missing_window")
        self.store.record_event(
            launch.session.name,
            "launch",
            f"Created tmux window {launch.window_name} with provider {launch.session.provider.value}",
        )

    def _session_name_by_window(self) -> dict[str, str]:
        return {
            (session.window_name or session.name): session.name
            for session in self.config.sessions.values()
            if session.enabled
        }

    def project_assignments(self) -> dict[str, list[SessionLaunchSpec]]:
        assignments: dict[str, list[SessionLaunchSpec]] = {}
        for launch in self.plan_launches():
            assignments.setdefault(launch.session.project, []).append(launch)
        return assignments

    def _window_map(self) -> dict[str, TmuxWindow]:
        windows: dict[str, TmuxWindow] = {}
        for session_name in self._all_tmux_session_names():
            if not self.tmux.has_session(session_name):
                continue
            for window in self.tmux.list_windows(session_name):
                windows[window.name] = window
        mounted = self._mounted_window_override()
        if mounted is not None:
            windows[mounted.name] = mounted
        return windows

    def _mounted_window_override(self) -> TmuxWindow | None:
        state_path = self.config.project.base_dir / "cockpit_state.json"
        if not state_path.exists():
            return None
        try:
            data = json.loads(state_path.read_text())
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, dict):
            return None
        mounted_session = data.get("mounted_session")
        if not isinstance(mounted_session, str) or not mounted_session:
            return None
        try:
            launch = self._launch_by_session(mounted_session)
        except KeyError:
            return None
        target = f"{self.config.project.tmux_session}:{self._CONSOLE_WINDOW}"
        try:
            panes = self.tmux.list_panes(target)
        except Exception:  # noqa: BLE001
            return None
        if len(panes) < 2:
            return None
        right_pane = max(panes, key=lambda pane: pane.pane_left)
        # Validate the mounted pane looks like the expected session.
        # A stale cockpit_state can claim a session is mounted when the
        # right pane is actually a static bash shell or a different session.
        pane_cmd = right_pane.pane_current_command or ""
        expected_provider = launch.session.provider.value
        if expected_provider == "claude" and pane_cmd in {"bash", "zsh", "sh", "fish"}:
            return None
        if expected_provider == "codex" and pane_cmd not in {"node"}:
            return None
        return TmuxWindow(
            session=self.config.project.tmux_session,
            index=0,
            name=launch.window_name,
            active=True,
            pane_id=right_pane.pane_id,
            pane_current_command=right_pane.pane_current_command,
            pane_current_path=right_pane.pane_current_path,
            pane_dead=right_pane.pane_dead,
        )

    def status(self) -> tuple[list[SessionLaunchSpec], list[TmuxWindow], list[AlertRecord], list[LeaseRecord], list[str]]:
        launches = self.plan_launches()
        errors: list[str] = []
        windows: list[TmuxWindow] = []

        try:
            for session_name in self._all_tmux_session_names():
                if self.tmux.has_session(session_name):
                    windows.extend(self.tmux.list_windows(session_name))
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

        try:
            alerts = self.store.open_alerts()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"store.open_alerts: {exc}")
            alerts = []
        try:
            leases = self.store.list_leases()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"store.list_leases: {exc}")
            leases = []
        return launches, windows, alerts, leases, errors

    def ensure_layout(self) -> Path:
        project = self.config.project
        project.base_dir.mkdir(parents=True, exist_ok=True)
        project.logs_dir.mkdir(parents=True, exist_ok=True)
        project.snapshots_dir.mkdir(parents=True, exist_ok=True)
        ensure_inbox(project.root_dir)
        ensure_project_scaffold(project.root_dir)
        for known_project in self.config.projects.values():
            ensure_project_scaffold(known_project.path)
        for account in self.config.accounts.values():
            if account.home is not None:
                account.home.mkdir(parents=True, exist_ok=True, mode=0o700)
                if account.provider is ProviderKind.CLAUDE:
                    _prime_claude_home(account.home)
                self._refresh_account_runtime_metadata(account.name)
        control_homes_root = self.config.project.base_dir / self._CONTROL_HOMES_DIR
        control_homes_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for session in self.config.sessions.values():
            if session.role not in self._CONTROL_ROLES:
                continue
            base_account = self.config.accounts.get(session.account)
            if base_account is None or base_account.home is None:
                continue
            self._sync_control_home(base_account, session.name)
        self.store.prune_sessions(
            {session.name for session in self.config.sessions.values() if session.enabled}
        )
        return project.base_dir

    def console_window_name(self) -> str:
        return self._CONSOLE_WINDOW

    def ensure_console_window(self) -> None:
        tmux_session = self.config.project.tmux_session
        if not self.tmux.has_session(tmux_session):
            return
        if self._CONSOLE_WINDOW in self._window_map():
            self._repair_console_if_broken(tmux_session)
            return
        self.tmux.create_window(tmux_session, self._CONSOLE_WINDOW, self._console_command(), detached=True)
        self.tmux.set_window_option(f"{tmux_session}:{self._CONSOLE_WINDOW}", "allow-passthrough", "on")
        self.tmux.set_window_option(f"{tmux_session}:{self._CONSOLE_WINDOW}", "window-size", "latest")
        self.tmux.set_window_option(f"{tmux_session}:{self._CONSOLE_WINDOW}", "aggressive-resize", "on")

    def _repair_console_if_broken(self, tmux_session: str) -> None:
        """Detect and repair a cockpit window where the rail pane died leaving only a worker."""
        target = f"{tmux_session}:{self._CONSOLE_WINDOW}"
        try:
            panes = self.tmux.list_panes(target)
        except Exception:  # noqa: BLE001
            return
        if len(panes) != 1:
            return
        pane = panes[0]
        # Check if the surviving pane is the right (worker) pane from cockpit state.
        # If so, the rail (left) pane died and we need to repair.
        state_path = self.config.project.base_dir / "cockpit_state.json"
        try:
            state_data = json.loads(state_path.read_text()) if state_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            state_data = {}
        saved_right_id = state_data.get("right_pane_id") if isinstance(state_data, dict) else None
        if not isinstance(saved_right_id, str) or saved_right_id != pane.pane_id:
            return
        # Try to park the worker pane back to storage-closet
        mounted_session = state_data.get("mounted_session") if isinstance(state_data, dict) else None
        if isinstance(mounted_session, str) and mounted_session:
            launch = next(
                (item for item in self.plan_launches() if item.session.name == mounted_session),
                None,
            )
            storage_session = self.storage_closet_session_name()
            if launch is not None and self.tmux.has_session(storage_session):
                try:
                    self.tmux.break_pane(pane.pane_id, storage_session, launch.window_name)
                except Exception:  # noqa: BLE001
                    pass
        # Clear stale cockpit state
        if isinstance(state_data, dict):
            state_data.pop("right_pane_id", None)
            state_data.pop("mounted_session", None)
            try:
                state_path.write_text(json.dumps(state_data, indent=2) + "\n")
            except OSError:
                pass
        # Recreate the cockpit.  If break_pane removed the last pane the
        # session itself is gone, so we need create_session instead of create_window.
        if not self.tmux.has_session(tmux_session):
            self.tmux.create_session(
                tmux_session, self._CONSOLE_WINDOW, self._console_command(), remain_on_exit=False,
            )
        else:
            try:
                remaining_panes = self.tmux.list_panes(target)
            except Exception:  # noqa: BLE001
                remaining_panes = []
            if len(remaining_panes) == 0:
                self.tmux.create_window(
                    tmux_session, self._CONSOLE_WINDOW, self._console_command(), detached=True,
                )
            else:
                self.tmux.respawn_pane(remaining_panes[0].pane_id, self._console_command())
        self.tmux.set_window_option(f"{tmux_session}:{self._CONSOLE_WINDOW}", "allow-passthrough", "on")
        self.tmux.set_window_option(f"{tmux_session}:{self._CONSOLE_WINDOW}", "window-size", "latest")
        self.tmux.set_window_option(f"{tmux_session}:{self._CONSOLE_WINDOW}", "aggressive-resize", "on")

    def focus_console(self) -> None:
        tmux_session = self.config.project.tmux_session
        if not self.tmux.has_session(tmux_session):
            return
        self.ensure_console_window()
        self.tmux.select_window(f"{tmux_session}:{self._CONSOLE_WINDOW}")

    def run_heartbeat(self, snapshot_lines: int = 200) -> list[AlertRecord]:
        # Phase 1: Fast synchronous pre-work (token sync uses SQLite, must stay on main thread)
        transcript_samples = sync_token_ledger_for_config(self.config)
        if transcript_samples:
            self.store.record_event(
                "heartbeat",
                "token_ledger",
                f"Synced {len(transcript_samples)} transcript token sample(s)",
            )
        self.release_expired_leases()

        # Phase 2: Fast synchronous sweep — capture + classify + alert
        backend = get_heartbeat_backend(
            self.config.pollypm.heartbeat_backend,
            root_dir=self.config.project.root_dir,
        )
        api = SupervisorHeartbeatAPI(self, snapshot_lines=snapshot_lines)
        alerts = backend.run(api, snapshot_lines=snapshot_lines)

        # Phase 3: Dispatch slow async jobs in parallel (fire-and-forget)
        # These must NOT touch self.store (SQLite is single-threaded).
        from pollypm.job_runner import submit_jobs_parallel
        submit_jobs_parallel(self, [
            ("version_check", {}),
            ("inbox_escalation", {}),
        ])
        return alerts

    def ensure_heartbeat_schedule(self) -> None:
        backend = get_scheduler_backend(
            self.config.pollypm.scheduler_backend,
            root_dir=self.config.project.root_dir,
        )
        self._dedup_recurring_jobs(backend, "heartbeat", 60)

    def ensure_knowledge_extraction_schedule(self) -> None:
        backend = get_scheduler_backend(
            self.config.pollypm.scheduler_backend,
            root_dir=self.config.project.root_dir,
        )
        self._dedup_recurring_jobs(backend, "knowledge_extract", EXTRACTION_INTERVAL_SECONDS, payload={"model": "haiku"})

    def _dedup_recurring_jobs(
        self,
        backend,
        kind: str,
        interval: int,
        payload: dict[str, object] | None = None,
    ) -> None:
        """Ensure exactly one pending recurring job of the given kind exists."""
        desired_payload = dict(payload or {})
        jobs = backend.list_jobs(self)
        matching = [job for job in jobs if job.kind == kind and job.interval_seconds == interval]
        pending = [job for job in matching if job.status == "pending"]
        exact_pending = [job for job in pending if job.payload == desired_payload]

        if len(matching) == 1 and len(exact_pending) == 1:
            return

        kept_job: ScheduledJob | None = None
        if exact_pending:
            kept_job = min(exact_pending, key=lambda job: job.run_at)
        elif pending:
            kept_job = min(pending, key=lambda job: job.run_at)
            kept_job.payload = desired_payload
            kept_job.last_error = None
        else:
            kept_job = ScheduledJob(
                job_id=uuid4().hex,
                kind=kind,
                run_at=datetime.now(UTC) + timedelta(seconds=interval),
                payload=desired_payload,
                interval_seconds=interval,
            )

        kept_job.status = "pending"
        remaining = [job for job in jobs if job not in matching]
        remaining.append(kept_job)

        save_jobs = getattr(backend, "_save_jobs", None)
        if callable(save_jobs):
            save_jobs(self, remaining)
            return

        if not matching:
            backend.schedule(
                self,
                kind=kind,
                run_at=kept_job.run_at,
                payload=kept_job.payload,
                interval_seconds=interval,
            )

    def _run_heartbeat_local(self, snapshot_lines: int = 200) -> list[AlertRecord]:
        window_map = self._window_map()
        name_by_window = self._session_name_by_window()

        for launch in self.plan_launches():
            window = window_map.get(launch.window_name)
            session_key = launch.session.name
            tmux_session = self._tmux_session_for_launch(launch)
            if window is None:
                self.store.upsert_alert(
                    session_key,
                    "missing_window",
                    "error",
                    f"Expected tmux window {launch.window_name} in session {tmux_session}",
                )
                self._maybe_recover_session(launch, failure_type="missing_window", failure_message="Expected tmux window is missing")
                continue

            snapshot_path, snapshot_content = self._write_snapshot(window, snapshot_lines)
            try:
                log_bytes = launch.log_path.stat().st_size
            except (FileNotFoundError, OSError):
                log_bytes = 0
            previous = self.store.latest_heartbeat(session_key)
            current_snapshot_hash = snapshot_hash(snapshot_content)

            self.store.record_heartbeat(
                session_name=session_key,
                tmux_window=window.name,
                pane_id=window.pane_id,
                pane_command=window.pane_current_command,
                pane_dead=window.pane_dead,
                log_bytes=log_bytes,
                snapshot_path=str(snapshot_path),
                snapshot_hash=current_snapshot_hash,
            )
            token_metrics = _extract_token_metrics(launch.session.provider, snapshot_content)
            if token_metrics is not None:
                delta = self.store.record_token_sample(
                    session_name=session_key,
                    account_name=launch.account.name,
                    provider=launch.session.provider.value,
                    model_name=token_metrics[0],
                    project_key=launch.session.project,
                    cumulative_tokens=token_metrics[1],
                )
                if delta > 0:
                    self.store.record_event(
                        session_key,
                        "token_usage",
                        f"Recorded {delta} tokens for {launch.session.project} on {launch.account.name} ({token_metrics[0]})",
                    )

            self.store.clear_alert(session_key, "missing_window")
            active_alerts = self._update_alerts(
                launch,
                window,
                pane_text=snapshot_content,
                previous_log_bytes=previous.log_bytes if previous else None,
                previous_snapshot_hash=previous.snapshot_hash if previous else None,
                current_log_bytes=log_bytes,
                current_snapshot_hash=current_snapshot_hash,
            )
            artifact = write_mechanical_checkpoint(
                self.config,
                launch,
                snapshot_path=snapshot_path,
                snapshot_content=snapshot_content,
                log_bytes=log_bytes,
                alerts=active_alerts,
            )
            record_checkpoint(
                self.store,
                launch,
                project_key=launch.session.project,
                level="level0",
                artifact=artifact,
                snapshot_path=snapshot_path,
                memory_backend_name=self.config.memory.backend,
            )
            failure = self._primary_failure(active_alerts)
            if failure is not None:
                self._maybe_recover_session(launch, failure_type=failure, failure_message=", ".join(active_alerts))

        for window_name, session_key in name_by_window.items():
            if window_name in window_map:
                continue
            self.store.upsert_alert(
                session_key,
                "missing_window",
                "error",
                f"Expected tmux window {window_name} in session {self._tmux_session_for_session(session_key)}",
            )

        self.store.record_event(
            "heartbeat",
            "heartbeat",
            f"Heartbeat sweep completed with {len(self.store.open_alerts())} open alerts",
        )
        return self.store.open_alerts()

    def _apply_role_launch_restrictions(
        self,
        session: SessionConfig,
        launch: LaunchCommand,
    ) -> LaunchCommand:
        if session.role != "worker":
            return launch
        env = dict(launch.env)
        shim_dir = self._worker_restriction_shim_dir()
        current_path = env.get("PATH") or os.environ.get("PATH", "")
        env["PATH"] = f"{shim_dir}{os.pathsep}{current_path}" if current_path else str(shim_dir)
        return replace(launch, env=env)

    def _worker_restriction_shim_dir(self) -> Path:
        shim_dir = self.config.project.base_dir / "role-shims" / "worker"
        shim_dir.mkdir(parents=True, exist_ok=True)
        tmux_shim = shim_dir / "tmux"
        tmux_shim.write_text(
            "#!/bin/sh\n"
            "echo 'PollyPM restriction: worker sessions may not manage tmux directly.' >&2\n"
            "exit 1\n"
        )
        tmux_shim.chmod(0o755)

        real_pm = shutil.which("pm")
        python = shlex.quote(sys.executable)
        if real_pm:
            fallback = f"exec {shlex.quote(real_pm)} \"$@\""
        else:
            fallback = f"exec {python} -m pollypm.cli \"$@\""
        pm_shim = shim_dir / "pm"
        pm_shim.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            + "".join(
                f"  {command}) echo 'PollyPM restriction: worker sessions may not manage sessions with pm {command}.' >&2; exit 1 ;;\n"
                for command in sorted(self._WORKER_BLOCKED_PM_COMMANDS)
            )
            + "esac\n"
            + fallback
            + "\n"
        )
        pm_shim.chmod(0o755)
        return shim_dir

    def schedule_job(
        self,
        *,
        kind: str,
        run_at: datetime,
        payload: dict[str, object] | None = None,
        interval_seconds: int | None = None,
    ) -> ScheduledJob:
        backend = get_scheduler_backend(
            self.config.pollypm.scheduler_backend,
            root_dir=self.config.project.root_dir,
        )
        return backend.schedule(
            self,
            kind=kind,
            run_at=run_at,
            payload=payload,
            interval_seconds=interval_seconds,
        )

    def list_scheduled_jobs(self) -> list[ScheduledJob]:
        backend = get_scheduler_backend(
            self.config.pollypm.scheduler_backend,
            root_dir=self.config.project.root_dir,
        )
        return backend.list_jobs(self)

    def run_scheduled_jobs(self) -> list[ScheduledJob]:
        backend = get_scheduler_backend(
            self.config.pollypm.scheduler_backend,
            root_dir=self.config.project.root_dir,
        )
        return backend.run_due(self)

    def _write_snapshot(self, window: TmuxWindow, snapshot_lines: int) -> tuple[Path, str]:
        target = window.pane_id or f"{window.session}:{window.name}"
        content = self.tmux.capture_pane(target, lines=snapshot_lines)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        snapshot_path = self.config.project.snapshots_dir / f"{window.name}-{stamp}.txt"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(content)
        return snapshot_path, content

    def _update_alerts(
        self,
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
            self.store.upsert_alert(
                session_name,
                "pane_dead",
                "error",
                f"Pane {window.pane_id} in window {window.name} has exited",
            )
            active_alerts.append("pane_dead")
        else:
            self.store.clear_alert(session_name, "pane_dead")

        if window.pane_current_command in shell_commands:
            self.store.upsert_alert(
                session_name,
                "shell_returned",
                "warn",
                f"Window {window.name} appears to be back at the shell prompt ({window.pane_current_command})",
            )
            active_alerts.append("shell_returned")
        else:
            self.store.clear_alert(session_name, "shell_returned")

        if previous_log_bytes is not None and current_log_bytes <= previous_log_bytes:
            self.store.upsert_alert(
                session_name,
                "idle_output",
                "warn",
                f"No new pane output since the previous heartbeat for window {window.name}",
            )
            active_alerts.append("idle_output")
        else:
            self.store.clear_alert(session_name, "idle_output")

        if previous_snapshot_hash and previous_snapshot_hash == current_snapshot_hash:
            history = self.store.recent_heartbeats(session_name, limit=3)
            recent_hashes = [item.snapshot_hash for item in history[:3]]
            if len(recent_hashes) == 3 and len(set(recent_hashes)) == 1:
                self.store.upsert_alert(
                    session_name,
                    "suspected_loop",
                    "warn",
                    f"Window {window.name} has produced effectively the same snapshot for 3 heartbeats",
                )
                active_alerts.append("suspected_loop")
                longer_history = self.store.recent_heartbeats(session_name, limit=5)
                longer_hashes = [item.snapshot_hash for item in longer_history[:5]]
                if len(longer_hashes) == 5 and len(set(longer_hashes)) == 1:
                    self._maybe_nudge_stalled_session(launch)
            else:
                self.store.clear_alert(session_name, "suspected_loop")
        else:
            self.store.clear_alert(session_name, "suspected_loop")

        lower_pane = pane_text.lower()
        if self._pane_has_auth_failure(lower_pane):
            self.store.upsert_alert(
                session_name,
                "auth_broken",
                "error",
                f"Window {window.name} reported authentication failure",
            )
            self.store.upsert_account_runtime(
                account_name=launch.account.name,
                provider=launch.account.provider.value,
                status="auth_broken",
                reason="live session reported authentication failure",
            )
            active_alerts.append("auth_broken")
        else:
            self.store.clear_alert(session_name, "auth_broken")

        if self._pane_has_capacity_failure(lower_pane):
            self.store.upsert_alert(
                session_name,
                "capacity_exhausted",
                "error",
                f"Window {window.name} reported a usage or quota limit",
            )
            self.store.upsert_account_runtime(
                account_name=launch.account.name,
                provider=launch.account.provider.value,
                status="exhausted",
                reason="live session reported capacity exhaustion",
            )
            active_alerts.append("capacity_exhausted")
        else:
            self.store.clear_alert(session_name, "capacity_exhausted")

        if self._pane_has_provider_outage(lower_pane):
            self.store.upsert_alert(
                session_name,
                "provider_outage",
                "warn",
                f"Window {window.name} appears to be hitting a provider outage",
            )
            self.store.upsert_account_runtime(
                account_name=launch.account.name,
                provider=launch.account.provider.value,
                status="provider_outage",
                reason="live session reported upstream provider instability",
                available_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            )
            active_alerts.append("provider_outage")
        else:
            self.store.clear_alert(session_name, "provider_outage")

        return active_alerts

    def _maybe_nudge_stalled_session(self, launch: SessionLaunchSpec) -> None:
        if launch.session.role != "worker":
            return
        lease = self.store.get_lease(launch.session.name)
        if lease is not None and lease.owner == "human":
            self.store.record_event(
                launch.session.name,
                "heartbeat_nudge_skipped",
                "Skipped stalled-worker nudge because session is leased to human",
            )
            return
        self.send_input(
            launch.session.name,
            self._STALL_NUDGE_MESSAGE,
            owner="heartbeat",
            force=lease is not None and lease.owner != "human",
        )

    def _lease_timeout(self) -> timedelta:
        return timedelta(minutes=self.config.pollypm.lease_timeout_minutes)

    def release_expired_leases(self, *, now: datetime | None = None) -> list[LeaseRecord]:
        current_time = now or datetime.now(UTC)
        released: list[LeaseRecord] = []
        for lease in self.store.list_leases():
            try:
                updated_at = datetime.fromisoformat(lease.updated_at)
            except ValueError:
                updated_at = current_time - self._lease_timeout() - timedelta(seconds=1)
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            age = current_time - updated_at.astimezone(UTC)
            if age < self._lease_timeout():
                continue
            self.store.clear_lease(lease.session_name)
            self.store.record_event(
                lease.session_name,
                "lease",
                (
                    f"Auto-released expired lease held by {lease.owner} "
                    f"after {self.config.pollypm.lease_timeout_minutes} minutes"
                ),
            )
            released.append(lease)
        return released

    def claim_lease(self, session_name: str, owner: str, note: str = "") -> None:
        self._require_session(session_name)
        self._assert_lease_available(
            session_name,
            owner=owner,
            action="claim a lease for",
        )
        self.store.set_lease(session_name, owner, note)
        message = f"Lease claimed by {owner}"
        if note:
            message = f"{message}: {note}"
        self.store.record_event(session_name, "lease", message)
        # Schedule auto-release after timeout
        try:
            backend = get_scheduler_backend(
                self.config.pollypm.scheduler_backend,
                root_dir=self.config.project.root_dir,
            )
            backend.schedule(
                self,
                kind="release_lease",
                run_at=datetime.now(UTC) + self._lease_timeout(),
                payload={"session_name": session_name, "owner": owner},
            )
        except Exception:  # noqa: BLE001
            pass  # Best-effort — lease still works without auto-release

    def release_lease(self, session_name: str, expected_owner: str | None = None) -> None:
        self._require_session(session_name)
        if expected_owner is not None:
            current = self.store.get_lease(session_name)
            if current is None or current.owner != expected_owner:
                return  # Lease was already released or reclaimed by someone else
        self.store.clear_lease(session_name)
        self.store.record_event(session_name, "lease", "Lease released")

    def send_input(
        self,
        session_name: str,
        text: str,
        *,
        owner: str = "pollypm",
        force: bool = False,
        press_enter: bool = True,
    ) -> None:
        launch = self._launch_by_session(session_name)
        self._assert_lease_available(session_name, owner=owner, force=force, action="send input to")

        target = f"{self._tmux_session_for_launch(launch)}:{launch.window_name}"
        prefixed = _prefix_for_owner(owner, text)
        self.tmux.send_keys(target, prefixed, press_enter=press_enter)
        # Codex CLI buffers input and requires a second Enter to submit.
        if press_enter and launch.session.provider is ProviderKind.CODEX:
            import time
            time.sleep(0.3)
            self.tmux.send_keys(target, "", press_enter=True)
        if owner == "human":
            self.store.set_lease(session_name, "human", "automatic lease from direct human input")
        self.store.record_event(session_name, "send_input", f"{owner} sent input: {text}")

    def open_alerts(self) -> list[AlertRecord]:
        return self.store.open_alerts()

    def leases(self) -> list[LeaseRecord]:
        return self.store.list_leases()

    def _pane_has_auth_failure(self, lowered_pane: str) -> bool:
        patterns = [
            "please run /login",
            "invalid authentication credentials",
            "authentication_error",
            "not authenticated",
        ]
        return any(pattern in lowered_pane for pattern in patterns)

    def _pane_has_capacity_failure(self, lowered_pane: str) -> bool:
        patterns = [
            "usage limit",
            "quota exceeded",
            "0% left",
            "out of credits",
            "credit balance is too low",
        ]
        return any(pattern in lowered_pane for pattern in patterns)

    def _pane_has_provider_outage(self, lowered_pane: str) -> bool:
        patterns = [
            "temporarily unavailable",
            "try again later",
            "server error",
            "overloaded",
            "service unavailable",
        ]
        return any(pattern in lowered_pane for pattern in patterns)

    def _primary_failure(self, alerts: list[str]) -> str | None:
        for alert_type in ["auth_broken", "capacity_exhausted", "provider_outage", "pane_dead", "shell_returned", "missing_window"]:
            if alert_type in alerts:
                return alert_type
        return None

    def _refresh_account_runtime_metadata(self, account_name: str) -> None:
        account = self.config.accounts[account_name]
        access_expires_at: str | None = None
        refresh_available = False
        if account.provider is ProviderKind.CLAUDE and account.home is not None:
            credentials_path = account.home / ".claude" / ".credentials.json"
            if credentials_path.exists():
                try:
                    import json

                    data = json.loads(credentials_path.read_text())
                    oauth = data.get("claudeAiOauth", {})
                    expires_at = oauth.get("expiresAt")
                    if isinstance(expires_at, (int, float)):
                        access_expires_at = datetime.fromtimestamp(expires_at / 1000, UTC).isoformat()
                    refresh_available = bool(oauth.get("refreshToken"))
                except Exception:  # noqa: BLE001
                    access_expires_at = None
        self.store.upsert_account_runtime(
            account_name=account_name,
            provider=account.provider.value,
            status="healthy",
            reason="local auth metadata loaded",
            access_expires_at=access_expires_at,
            refresh_available=refresh_available,
        )

    def _account_is_viable(self, account_name: str) -> bool:
        runtime = self.store.get_account_runtime(account_name)
        if runtime is not None and runtime.status in {"auth_broken", "exhausted", "provider_outage"}:
            return False
        account = self.config.accounts[account_name]
        if account.home is None:
            return False
        if account.provider is ProviderKind.CLAUDE:
            claude_dir = account.home / ".claude"
            # Claude Code may use .credentials.json (file auth) or macOS
            # Keychain (.claude.json contains the session config).  Accept
            # either as evidence the account has been set up.
            return (
                (claude_dir / ".credentials.json").exists()
                or (claude_dir / ".claude.json").exists()
            )
        if account.provider is ProviderKind.CODEX:
            return (account.home / ".codex" / "auth.json").exists()
        return False

    def _candidate_accounts(self, launch: SessionLaunchSpec, *, allow_same: bool) -> list[str]:
        preferred = []
        current = launch.account.name
        if allow_same:
            preferred.append(current)
        for name in self.config.pollypm.failover_accounts:
            if name not in preferred:
                preferred.append(name)
        controller = self.config.pollypm.controller_account
        if controller and controller not in preferred:
            preferred.append(controller)
        for name in self.config.accounts:
            if name not in preferred:
                preferred.append(name)
        same_provider = [name for name in preferred if self.config.accounts[name].provider is launch.session.provider]
        cross_provider = [name for name in preferred if self.config.accounts[name].provider is not launch.session.provider]
        ordered = same_provider + cross_provider
        return [name for name in ordered if self._account_is_viable(name)]

    def _record_recovery_attempt(self, session_name: str, *, status: str, failure_type: str, failure_message: str) -> tuple[bool, int]:
        now = datetime.now(UTC)
        runtime = self.store.get_session_runtime(session_name)
        attempts = 1
        started_at = now.isoformat()
        if runtime is not None and runtime.recovery_window_started_at:
            try:
                previous_start = datetime.fromisoformat(runtime.recovery_window_started_at)
            except ValueError:
                previous_start = now
            if now - previous_start <= self._RECOVERY_WINDOW:
                attempts = runtime.recovery_attempts + 1
                started_at = runtime.recovery_window_started_at
        self.store.upsert_session_runtime(
            session_name=session_name,
            status=status,
            effective_account=runtime.effective_account if runtime else None,
            effective_provider=runtime.effective_provider if runtime else None,
            recovery_attempts=attempts,
            recovery_window_started_at=started_at,
            last_failure_type=failure_type,
            last_failure_message=failure_message,
        )
        # Hard limit: stop entirely after too many total attempts across all windows
        if attempts > self._RECOVERY_HARD_LIMIT:
            return False, attempts
        return attempts <= self._RECOVERY_LIMIT, attempts

    # Failure types where the session is gone — no live interaction to protect.
    _DEAD_SESSION_FAILURES = frozenset({"missing_window", "pane_dead", "shell_returned"})

    def _maybe_recover_session(self, launch: SessionLaunchSpec, *, failure_type: str, failure_message: str) -> None:
        lease = self.store.get_lease(launch.session.name)
        if lease is not None and lease.owner != "pollypm":
            if failure_type in self._DEAD_SESSION_FAILURES:
                # Session is dead — the lease is protecting nothing.  Release it
                # so recovery can proceed immediately.
                self.store.clear_lease(launch.session.name)
                self.store.clear_alert(launch.session.name, "recovery_waiting_on_human")
                self.store.record_event(
                    launch.session.name,
                    "lease_override",
                    f"Auto-released stale lease (owner={lease.owner}) — session is {failure_type}",
                )
            else:
                self.store.upsert_alert(
                    launch.session.name,
                    "recovery_waiting_on_human",
                    "warn",
                    f"Recovery is queued behind lease owner {lease.owner} for {launch.window_name}",
                )
                return
        if failure_type == "provider_outage":
            self.store.upsert_session_runtime(
                session_name=launch.session.name,
                status="blocked",
                effective_account=launch.account.name,
                effective_provider=launch.account.provider.value,
                retry_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                last_failure_type=failure_type,
                last_failure_message=failure_message,
            )
            return

        allowed, attempts = self._record_recovery_attempt(
            launch.session.name,
            status="recovering",
            failure_type=failure_type,
            failure_message=failure_message,
        )
        if not allowed:
            msg = f"Automatic recovery paused after {attempts} rapid failures"
            if attempts >= self._RECOVERY_HARD_LIMIT:
                msg = (
                    f"Automatic recovery STOPPED after {attempts} total failures. "
                    f"Session requires manual intervention (pm up or account re-auth)."
                )
            self.store.upsert_alert(
                launch.session.name,
                "recovery_limit",
                "error",
                msg,
            )
            self.store.upsert_session_runtime(
                session_name=launch.session.name,
                status="degraded",
                last_failure_type=failure_type,
                last_failure_message=failure_message,
            )
            return

        allow_same = failure_type not in {"auth_broken", "capacity_exhausted"}
        candidates = self._candidate_accounts(launch, allow_same=allow_same)
        if not candidates:
            self.store.upsert_alert(
                launch.session.name,
                "blocked_no_capacity",
                "error",
                f"No viable account is currently available to recover {launch.window_name}",
            )
            self.store.upsert_session_runtime(
                session_name=launch.session.name,
                status="blocked",
                last_failure_type=failure_type,
                last_failure_message=failure_message,
                retry_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            )
            return

        last_error = ""
        for selected in candidates:
            try:
                self._restart_session(launch.session.name, selected, failure_type=failure_type)
                return
            except RuntimeError as exc:
                last_error = str(exc)
                candidate = self.config.accounts[selected]
                status = "auth_broken" if "authentication" in last_error.lower() or "/login" in last_error.lower() else "exhausted" if "usage limit" in last_error.lower() else "provider_outage" if "unavailable" in last_error.lower() or "server error" in last_error.lower() else "degraded"
                self.store.upsert_account_runtime(
                    account_name=selected,
                    provider=candidate.provider.value,
                    status=status,
                    reason=f"startup recovery failed: {last_error}",
                    available_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat() if status == "provider_outage" else None,
                )
                self.store.record_event(
                    launch.session.name,
                    "recovery_candidate_failed",
                    f"Recovery candidate {selected} failed: {last_error}",
                )

        self.store.upsert_alert(
            launch.session.name,
            "blocked_no_capacity",
            "error",
            f"Recovery failed for all viable accounts: {last_error or 'no candidate succeeded'}",
        )
        self.store.upsert_session_runtime(
            session_name=launch.session.name,
            status="blocked",
            last_failure_type=failure_type,
            last_failure_message=last_error or failure_message,
            retry_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        )

    def _restart_session(self, session_name: str, account_name: str, *, failure_type: str) -> None:
        launch = self._launch_by_session(session_name)
        self._assert_lease_available(
            session_name,
            owner="pollypm",
            action="restart",
        )
        tmux_session = self._tmux_session_for_launch(launch)
        previous_runtime = self.store.get_session_runtime(session_name)
        if self.tmux.has_session(tmux_session):
            window_map = self._window_map()
            if launch.window_name in window_map:
                self.tmux.kill_window(f"{tmux_session}:{launch.window_name}")
        account = self.config.accounts[account_name]
        self.store.upsert_session_runtime(
            session_name=session_name,
            status="recovering",
            effective_account=account_name,
            effective_provider=account.provider.value,
            last_failure_type=failure_type,
            last_failure_message=f"recovering on {account_name}",
        )
        try:
            self.launch_session(session_name)
        except RuntimeError as exc:
            # Retry once if window name collision
            if "already exists" in str(exc).lower() or "duplicate" in str(exc).lower():
                import time as _time
                _time.sleep(0.5)
                try:
                    self.launch_session(session_name)
                except Exception:
                    pass
        except Exception:
            self.store.upsert_session_runtime(
                session_name=session_name,
                status=previous_runtime.status if previous_runtime else "degraded",
                effective_account=previous_runtime.effective_account if previous_runtime else None,
                effective_provider=previous_runtime.effective_provider if previous_runtime else None,
                recovery_attempts=previous_runtime.recovery_attempts if previous_runtime else 0,
                recovery_window_started_at=previous_runtime.recovery_window_started_at if previous_runtime else None,
                last_failure_type=failure_type,
                last_failure_message=f"failed to recover on {account_name}",
                last_checkpoint_path=previous_runtime.last_checkpoint_path if previous_runtime else None,
                retry_at=previous_runtime.retry_at if previous_runtime else None,
                last_recovered_at=previous_runtime.last_recovered_at if previous_runtime else None,
            )
            raise
        self.store.record_event(
            session_name,
            "recovered",
            f"Recovered {launch.window_name} in place using {account_name}",
        )
        # If recovered on the config-default account, clear the override
        # so the session doesn't carry a stale effective_account that blocks
        # future recovery from choosing the correct provider.
        config_default = launch.session.account if hasattr(launch.session, "account") else None
        if account_name == config_default:
            eff_account = None
            eff_provider = None
        else:
            eff_account = account_name
            eff_provider = account.provider.value
        self.store.upsert_session_runtime(
            session_name=session_name,
            status="healthy",
            effective_account=eff_account,
            effective_provider=eff_provider,
            last_recovered_at=datetime.now(UTC).isoformat(),
        )
        for alert_type in [
            "pane_dead",
            "shell_returned",
            "auth_broken",
            "capacity_exhausted",
            "provider_outage",
            "missing_window",
            "recovery_waiting_on_human",
            "blocked_no_capacity",
        ]:
            self.store.clear_alert(session_name, alert_type)

    def _launch_by_session(self, session_name: str) -> SessionLaunchSpec:
        for launch in self.plan_launches():
            if launch.session.name == session_name:
                return launch
        raise KeyError(f"Unknown session: {session_name}")

    def _require_session(self, session_name: str) -> None:
        self._launch_by_session(session_name)

    def launch_session(
        self,
        session_name: str,
        on_status: Callable[[str], None] | None = None,
    ) -> SessionLaunchSpec:
        launch, target = self.create_session_window(session_name, on_status=on_status)
        if target is not None:
            self._stabilize_launch(launch, target, on_status=on_status)
        return launch

    def create_session_window(
        self,
        session_name: str,
        on_status: Callable[[str], None] | None = None,
    ) -> tuple[SessionLaunchSpec, str | None]:
        """Create the tmux window for a session without stabilizing it.

        Returns ``(launch, target)`` where *target* is the tmux pane
        address.  If the window already exists, *target* is ``None``.
        """
        launch = self._launch_by_session(session_name)
        tmux_session = self._tmux_session_for_launch(launch)
        window_map = self._window_map()
        if launch.window_name in window_map:
            return launch, None

        if on_status:
            on_status(f"Creating tmux window for {session_name}...")
        if not self.tmux.has_session(tmux_session):
            self.tmux.create_session(tmux_session, launch.window_name, launch.command)
            target = f"{tmux_session}:0"
            self.tmux.set_window_option(target, "allow-passthrough", "on")
        else:
            self.tmux.create_window(tmux_session, launch.window_name, launch.command, detached=True)
            target = f"{tmux_session}:{launch.window_name}"
            self.tmux.set_window_option(target, "allow-passthrough", "on")
        self.tmux.pipe_pane(target, launch.log_path)
        self._record_launch(launch)
        return launch, target

    def stop_session(self, session_name: str) -> None:
        launch = self._launch_by_session(session_name)
        self._assert_lease_available(
            session_name,
            owner="pollypm",
            action="stop",
        )
        tmux_session = self._tmux_session_for_launch(launch)
        if not self.tmux.has_session(tmux_session):
            return
        window_map = self._window_map()
        if launch.window_name not in window_map:
            return
        self.tmux.kill_window(f"{tmux_session}:{launch.window_name}")
        self._release_session_locks(launch)
        self.store.record_event(session_name, "stop", f"Stopped tmux window {launch.window_name}")

    def focus_session(self, session_name: str) -> None:
        launch = self._launch_by_session(session_name)
        tmux_session = self._tmux_session_for_launch(launch)
        if not self.tmux.has_session(tmux_session):
            raise RuntimeError(f"tmux session does not exist: {tmux_session}")
        self.tmux.select_window(f"{tmux_session}:{launch.window_name}")

    def switch_session_account(self, session_name: str, account_name: str) -> None:
        launch = self._launch_by_session(session_name)
        self._assert_lease_available(
            session_name,
            owner="pollypm",
            action="switch the account for",
        )
        account = self.config.accounts.get(account_name)
        if account is None:
            raise KeyError(f"Unknown account: {account_name}")
        self.store.upsert_session_runtime(
            session_name=session_name,
            status="recovering",
            effective_account=account_name,
            effective_provider=account.provider.value,
            last_failure_type="manual_switch",
            last_failure_message=f"manually switched to {account_name}",
        )
        self._restart_session(session_name, account_name, failure_type="manual_switch")
        self.store.record_event(
            session_name,
            "manual_switch",
            f"Switched {launch.window_name} to {account_name}",
        )

    def _assert_lease_available(
        self,
        session_name: str,
        *,
        owner: str,
        action: str,
        force: bool = False,
    ) -> None:
        lease = self.store.get_lease(session_name)
        if lease is None or lease.owner == owner or force:
            return
        raise RuntimeError(
            f"Cannot {action} {session_name}: session is currently leased to {lease.owner}; use --force to bypass"
        )

    def _release_session_locks(self, launch: SessionLaunchSpec) -> None:
        release_session_lock(self.config.project.logs_dir / launch.session.name, launch.session.name)
        project_path = self._project_path_for_session(launch.session.project)
        release_session_lock(project_checkpoints_dir(project_path) / launch.session.name, launch.session.name)
        release_session_lock(project_transcripts_dir(project_path) / launch.session.name, launch.session.name)
        worktrees_root = project_worktrees_dir(project_path)
        try:
            relative = launch.session.cwd.resolve().relative_to(worktrees_root.resolve())
        except ValueError:
            return
        parts = relative.parts
        if parts:
            release_session_lock(worktrees_root / parts[0], launch.session.name)

    def _project_path_for_session(self, project_key: str) -> Path:
        project = self.config.projects.get(project_key)
        if project is not None:
            return project.path
        return self.config.project.root_dir

    def _console_command(self) -> str:
        root = shlex.quote(str(self.config.project.root_dir))
        import shutil
        pm_path = shutil.which("pm")
        if pm_path:
            cockpit_cmd = f"{shlex.quote(pm_path)} cockpit"
        else:
            cockpit_cmd = f"cd {root} && uv run pm cockpit"
        # Wrap in a restart loop so the rail auto-recovers from crashes.
        # 2s cooldown prevents tight crash loops; exits cleanly on SIGTERM.
        return (
            f"sh -lc 'while true; do {cockpit_cmd}; "
            f"echo \"[Rail exited — restarting in 2s]\"; sleep 2; done'"
        )

    def _controller_candidates(self) -> list[str]:
        ordered = [self.config.pollypm.controller_account, *self.config.pollypm.failover_accounts]
        candidates: list[str] = []
        seen: set[str] = set()
        for name in ordered:
            if not name or name in seen or name not in self.config.accounts:
                continue
            seen.add(name)
            candidates.append(name)
        return candidates

    def _probe_controller_account(self, account_name: str) -> None:
        account = self.config.accounts[account_name]
        operator_session = self._effective_session(self.config.sessions["operator"], controller_account=account_name)
        account = self._effective_account(operator_session, self.config.accounts[operator_session.account])
        output = self._run_probe(account)
        lowered = output.lower()
        if account.provider is ProviderKind.CLAUDE:
            if "ok" in lowered and "authentication" not in lowered:
                return
            raise RuntimeError("Claude probe failed")
        if account.provider is ProviderKind.CODEX:
            if "usage limit" in lowered:
                raise RuntimeError("Codex account is out of credits")
            if "not logged" in lowered or "login" in lowered:
                raise RuntimeError("Codex account is not authenticated")
            if "error:" in lowered:
                raise RuntimeError("Codex probe failed")
            return
        raise RuntimeError(f"Unsupported controller provider: {account.provider.value}")

    def _run_probe(self, account: AccountConfig) -> str:
        if account.provider is ProviderKind.CLAUDE:
            probe = LaunchCommand(
                argv=["claude", "-p", "Reply with ok"],
                env=dict(account.env),
                cwd=self.config.project.root_dir,
            )
        elif account.provider is ProviderKind.CODEX:
            probe = LaunchCommand(
                argv=["codex", "exec", "--skip-git-repo-check", "Reply with ok and nothing else"],
                env=dict(account.env),
                cwd=self.config.project.root_dir,
            )
        else:
            raise RuntimeError(f"Unsupported controller provider: {account.provider.value}")

        runtime = get_runtime(account.runtime, root_dir=self.config.project.root_dir)
        command = runtime.wrap_command(probe, account, self.config.project)
        result = subprocess.run(
            command,
            check=False,
            shell=True,
            text=True,
            capture_output=True,
            timeout=90,
            executable="/bin/zsh",
        )
        return "\n".join(part for part in [result.stdout, result.stderr] if part)

    def _control_home(self, session_name: str) -> Path:
        return self.config.project.base_dir / self._CONTROL_HOMES_DIR / session_name

    def _effective_account(self, session: SessionConfig, account: AccountConfig) -> AccountConfig:
        if session.role not in self._CONTROL_ROLES or account.home is None:
            return account
        # Claude auth tokens live in the macOS Keychain, keyed to the CLAUDE_CONFIG_DIR path hash.
        # Using a different home (control-homes/) would lose the keychain entry.
        # For Claude accounts, use the original account home directly.
        if account.provider is ProviderKind.CLAUDE:
            _prime_claude_home(account.home)
            return account
        control_home = self._sync_control_home(account, session.name)
        return replace(account, home=control_home)

    def _sync_control_home(self, account: AccountConfig, session_name: str) -> Path:
        if account.home is None:
            raise RuntimeError(f"Account {account.name} has no home configured")
        source_home = account.home
        target_home = self._control_home(session_name)
        target_home.mkdir(parents=True, exist_ok=True, mode=0o700)

        if account.provider is ProviderKind.CLAUDE:
            self._sync_file(source_home / ".claude.json", target_home / ".claude.json")
            self._sync_file(source_home / ".claude" / ".credentials.json", target_home / ".claude" / ".credentials.json")
            self._sync_file(source_home / ".claude" / "settings.json", target_home / ".claude" / "settings.json")
            _prime_claude_home(target_home)
        elif account.provider is ProviderKind.CODEX:
            self._sync_file(source_home / ".codex" / ".codex-global-state.json", target_home / ".codex" / ".codex-global-state.json")
            self._sync_file(source_home / ".codex" / "auth.json", target_home / ".codex" / "auth.json")
            self._sync_file(source_home / ".codex" / "config.toml", target_home / ".codex" / "config.toml")

        return target_home

    def _sync_file(self, source: Path, target: Path) -> None:
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif target.exists() and not source.exists():
            # Reverse sync: if the control-home has the file but the
            # account home lost it (e.g. during a recovery reset),
            # copy it back to prevent auth loss.
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, source)

    def _stabilize_launch(
        self,
        launch: SessionLaunchSpec,
        target: str,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        name = launch.session.name

        def _prefixed_status(msg: str) -> None:
            if on_status:
                on_status(f"[{name}] {msg}")

        if launch.session.provider is ProviderKind.CLAUDE:
            self._stabilize_claude_launch(target, on_status=_prefixed_status)
        elif launch.session.provider is ProviderKind.CODEX:
            self._stabilize_codex_launch(target, on_status=_prefixed_status)
        _prefixed_status("Sending initial input...")
        self._send_initial_input_if_fresh(launch, target)
        self._mark_session_resume_ready(launch)

    def _mark_session_resume_ready(self, launch: SessionLaunchSpec) -> None:
        marker = launch.resume_marker
        if marker is None:
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now(UTC).isoformat().replace("+00:00", "Z") + "\n")

    def _send_initial_input_if_fresh(self, launch: SessionLaunchSpec, target: str) -> None:
        if launch.session.role not in self._CONTROL_ROLES:
            return
        initial_input = launch.initial_input
        fresh_marker = launch.fresh_launch_marker
        if not initial_input or fresh_marker is None or not fresh_marker.exists():
            return
        kickoff = self._prepare_initial_input(launch.session.name, initial_input)
        self.tmux.send_keys(target, kickoff)
        fresh_marker.unlink(missing_ok=True)

    def _prepare_initial_input(self, session_name: str, initial_input: str) -> str:
        if len(initial_input) <= 280:
            return initial_input
        prompts_dir = self.config.project.base_dir / "control-prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompts_dir / f"{session_name}.md"
        prompt_path.write_text(initial_input.rstrip() + "\n")
        try:
            display_path = prompt_path.relative_to(self.config.project.root_dir)
        except ValueError:
            display_path = prompt_path
        # Point to both SYSTEM.md (PollyPM reference) and the control prompt (role)
        instruct_path = self.config.project.root_dir / ".pollypm" / "docs" / "SYSTEM.md"
        if instruct_path.exists():
            try:
                instruct_display = instruct_path.relative_to(self.config.project.root_dir)
            except ValueError:
                instruct_display = instruct_path
            return (
                f'Read {instruct_display} for system context, then read {display_path} for your role. '
                f'Adopt both as your operating instructions, reply only "ready", then wait.'
            )
        return (
            f'Read {display_path}, adopt it as your operating instructions, reply only "ready", then wait.'
        )

    def _stabilize_claude_launch(
        self, target: str, on_status: Callable[[str], None] | None = None,
    ) -> None:
        timeout = 90
        start = time.monotonic()
        deadline = start + timeout
        last_action = ""
        _status = on_status or (lambda _msg: None)
        _status("Waiting for Claude Code to start...")
        while time.monotonic() < deadline:
            elapsed = int(time.monotonic() - start)
            try:
                pane = self.tmux.capture_pane(target, lines=320)
            except Exception:  # noqa: BLE001
                _status(f"Waiting for Claude Code to start... ({elapsed}s)")
                time.sleep(1)
                continue
            lowered = pane.lower()

            if "select login method:" in lowered or "paste code here if prompted" in lowered:
                _status("Login required — authenticate from the cockpit")
                return
            if "please run /login" in lowered or "invalid authentication credentials" in lowered:
                _status("Login required — re-authenticate interactively")
                return

            if "choose the text style that looks best with your terminal" in lowered:
                if last_action != "theme":
                    _status(f"Dismissing theme picker... ({elapsed}s)")
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "theme"
                time.sleep(1)
                continue

            if "quick safety check" in lowered and "yes, i trust this folder" in lowered:
                if last_action != "trust":
                    _status(f"Accepting trust prompt... ({elapsed}s)")
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "trust"
                time.sleep(1)
                continue

            if "warning: claude code running in bypass permissions mode" in lowered:
                if last_action != "bypass-confirm":
                    _status(f"Confirming bypass permissions mode... ({elapsed}s)")
                    self.tmux.send_keys(target, "2", press_enter=False)
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "bypass-confirm"
                time.sleep(1)
                continue

            if "we recommend medium effort for opus" in lowered:
                if last_action != "effort":
                    _status(f"Dismissing effort recommendation... ({elapsed}s)")
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "effort"
                time.sleep(1)
                continue

            if "❯" in pane and (
                "welcome back" in lowered
                or "0 tokens" in lowered
                or "/buddy" in pane
                or "bypass permissions" in lowered
                or "shift+tab" in lowered
            ):
                _status("Claude Code ready")
                return

            _status(f"Waiting for Claude Code to start... ({elapsed}s)")
            time.sleep(1)

        _status("Timed out waiting for Claude Code")
        return

    def _stabilize_codex_launch(
        self, target: str, on_status: Callable[[str], None] | None = None,
    ) -> None:
        timeout = 60
        start = time.monotonic()
        deadline = start + timeout
        last_action = ""
        ready_streak = 0
        _status = on_status or (lambda _msg: None)
        _status("Waiting for Codex to start...")
        while time.monotonic() < deadline:
            elapsed = int(time.monotonic() - start)
            try:
                pane = self.tmux.capture_pane(target, lines=260)
            except Exception:  # noqa: BLE001
                _status(f"Waiting for Codex to start... ({elapsed}s)")
                time.sleep(1)
                continue
            lowered = pane.lower()

            if "approaching rate limits" in lowered and "switch to gpt-5.1-codex-mini" in lowered:
                if last_action != "switch-mini":
                    _status(f"Switching to codex-mini due to rate limits... ({elapsed}s)")
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "switch-mini"
                time.sleep(1)
                continue
            if "usage limit" in lowered:
                raise RuntimeError("Codex account is out of credits")
            if "press enter to continue" in lowered:
                if last_action != "continue":
                    _status(f"Dismissing continue prompt... ({elapsed}s)")
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "continue"
                time.sleep(1)
                continue
            if "do you trust the contents of this directory" in lowered and "1. yes, continue" in lowered:
                if last_action != "trust":
                    _status(f"Accepting trust prompt... ({elapsed}s)")
                    self.tmux.send_keys(target, "", press_enter=True)
                    last_action = "trust"
                time.sleep(1)
                continue
            prompt_visible = "% left" in lowered or "›" in pane
            working = "working (" in lowered and "esc to interrupt" in lowered
            booting = "booting mcp server" in lowered
            if "openai codex" in lowered and (prompt_visible or working) and not booting:
                ready_streak += 1
                if ready_streak >= 2:
                    _status("Codex ready")
                    return
                time.sleep(1)
                continue
            ready_streak = 0
            _status(f"Waiting for Codex to start... ({elapsed}s)")
            time.sleep(1)

        _status("Timed out waiting for Codex")
        return


_TOKEN_RE = re.compile(r"(\d[\d,]*)\s+tokens\b", re.IGNORECASE)
_CLAUDE_MODEL_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9 .+-]*?(?:\([^)]+\))?)\s+·\s+Claude\b")
_CODEX_FOOTER_MODEL_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*(?:\s+[A-Za-z0-9._-]+)?)\s+·\s+\d+% left\b")
_CODEX_BANNER_MODEL_RE = re.compile(r"model:\s*([^\n]+)", re.IGNORECASE)


def _extract_token_metrics(provider: ProviderKind, pane_text: str) -> tuple[str, int] | None:
    model = _extract_claude_model_name(pane_text) if provider is ProviderKind.CLAUDE else _extract_codex_model_name(pane_text)
    if not model:
        return None
    match = None
    for candidate in _TOKEN_RE.finditer(pane_text):
        match = candidate
    if match is None:
        return None
    return (model, int(match.group(1).replace(",", "")))


def _extract_claude_model_name(pane_text: str) -> str | None:
    for line in pane_text.splitlines():
        match = _CLAUDE_MODEL_RE.search(line)
        if match:
            return " ".join(match.group(1).split())
    return None


def _extract_codex_model_name(pane_text: str) -> str | None:
    for line in reversed(pane_text.splitlines()):
        footer = _CODEX_FOOTER_MODEL_RE.search(line)
        if footer:
            return " ".join(footer.group(1).split())
        banner = _CODEX_BANNER_MODEL_RE.search(line)
        if banner:
            return " ".join(banner.group(1).split())
    return None
