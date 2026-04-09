from __future__ import annotations

from pathlib import Path

import typer

from pollypm.accounts import _account_logged_in
from pollypm.config import load_config, write_config
from pollypm.models import ProviderKind, SessionConfig
from pollypm.supervisor import Supervisor
from pollypm.storage.state import StateStore
from pollypm.task_backends import get_task_backend
from pollypm.worktrees import ensure_worktree


def _effective_control_accounts(config_path: Path) -> set[str]:
    config = load_config(config_path)
    store = StateStore(config.project.state_db)
    accounts = {config.pollypm.controller_account}
    for session_name in ("heartbeat", "operator"):
        runtime = store.get_session_runtime(session_name)
        if runtime is not None and runtime.effective_account:
            accounts.add(runtime.effective_account)
    return {name for name in accounts if name}


def _account_is_available(config_path: Path, account_name: str) -> bool:
    config = load_config(config_path)
    account = config.accounts[account_name]
    if not _account_logged_in(account):
        return False
    store = StateStore(config.project.state_db)
    runtime = store.get_account_runtime(account_name)
    if runtime is not None and runtime.status in {"auth-broken", "exhausted", "provider_outage", "blocked"}:
        return False
    return True


def auto_select_worker_account(
    config_path: Path,
    *,
    provider: ProviderKind | None = None,
) -> str:
    config = load_config(config_path)
    control_accounts = _effective_control_accounts(config_path)
    candidates: list[str] = []
    for name, account in config.accounts.items():
        if provider is not None and account.provider is not provider:
            continue
        if _account_is_available(config_path, name):
            candidates.append(name)

    if not candidates:
        raise typer.BadParameter("No healthy logged-in account is available for a new worker session.")

    provider_rank = {
        ProviderKind.CODEX: 0,
        ProviderKind.CLAUDE: 1,
    }
    controller = config.pollypm.controller_account

    def _tier(name: str) -> int:
        if name == controller:
            return 2
        if name in control_accounts:
            return 1
        return 0

    candidates.sort(
        key=lambda item: (
            _tier(item),
            provider_rank.get(config.accounts[item].provider, 9),
            item,
        )
    )
    return candidates[0]


def create_worker_session(
    config_path: Path,
    *,
    project_key: str,
    prompt: str | None,
    account_name: str | None = None,
    provider: ProviderKind | None = None,
    session_name: str | None = None,
) -> SessionConfig:
    config = load_config(config_path)
    project = config.projects.get(project_key)
    if project is None:
        raise typer.BadParameter(f"Unknown project: {project_key}")

    if account_name is None:
        account_name = auto_select_worker_account(config_path, provider=provider)
    if account_name not in config.accounts:
        raise typer.BadParameter(f"Unknown account: {account_name}")
    account = config.accounts[account_name]
    if provider is not None and account.provider is not provider:
        raise typer.BadParameter(
            f"Account {account_name} uses provider {account.provider.value}, not {provider.value}"
        )
    if not prompt or not prompt.strip():
        prompt = suggest_worker_prompt(config_path, project_key=project_key)

    for existing in config.sessions.values():
        if existing.role == "worker" and existing.project == project_key and existing.enabled:
            raise typer.BadParameter(
                f"Project {project_key} already has worker session {existing.name}"
            )

    session_key = session_name or _make_worker_session_name(project_key, set(config.sessions))
    worktree = ensure_worktree(
        config_path,
        project_key=project_key,
        lane_kind="pa",
        lane_key=session_key,
        session_name=session_key,
    )
    worker = SessionConfig(
        name=session_key,
        role="worker",
        provider=account.provider,
        account=account_name,
        cwd=Path(worktree.path) if worktree is not None else project.path,
        project=project_key,
        window_name=f"worker-{project_key}",
        prompt=prompt,
    )
    config.sessions[session_key] = worker
    write_config(config, config_path, force=True)
    return worker


def suggest_worker_prompt(config_path: Path, *, project_key: str) -> str:
    config = load_config(config_path)
    project = config.projects.get(project_key)
    if project is None:
        raise typer.BadParameter(f"Unknown project: {project_key}")

    task_backend = get_task_backend(project.path)
    if project.tracked and task_backend.exists():
        active_tasks = task_backend.list_tasks(states=["02-in-progress"])
        ready_tasks = task_backend.list_tasks(states=["01-ready"])
        selected_task = active_tasks[0] if active_tasks else (ready_tasks[0] if ready_tasks else None)
        if selected_task is not None:
            state_instruction = (
                "Resume the active issue"
                if selected_task.state == "02-in-progress"
                else "Start the next ready issue"
            )
            return (
                f"{state_instruction} {selected_task.task_id} ({selected_task.title}) for project "
                f"{project.name or project.key} at {project.path}. "
                f"If the issue is not already in 02-in-progress, move it there first. "
                "Keep the work scoped to one small, testable, modular slice. "
                "Run the relevant checks before stopping, then move the issue to 03-needs-review "
                "with a concise handoff for Polly."
            )

    return (
        f"Work on the {project.name or project.key} project at {project.path}. "
        "First identify the single highest-leverage next slice that can be completed and verified in one lane. "
        "State that slice in one sentence, then execute only that slice end-to-end, run the relevant checks, "
        "and stop with a concrete handoff for Polly."
    )


def launch_worker_session(config_path: Path, session_name: str) -> SessionConfig:
    config = load_config(config_path)
    if session_name not in config.sessions:
        raise typer.BadParameter(f"Unknown session: {session_name}")

    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    supervisor.launch_session(session_name)
    return config.sessions[session_name]


def stop_worker_session(config_path: Path, session_name: str) -> None:
    config = load_config(config_path)
    session = config.sessions.get(session_name)
    if session is None:
        raise typer.BadParameter(f"Unknown session: {session_name}")
    if session.role != "worker":
        raise typer.BadParameter("Only worker sessions can be stopped from the worker manager.")

    supervisor = Supervisor(config)
    supervisor.stop_session(session_name)


def remove_worker_session(config_path: Path, session_name: str) -> None:
    config = load_config(config_path)
    session = config.sessions.get(session_name)
    if session is None:
        raise typer.BadParameter(f"Unknown session: {session_name}")
    if session.role != "worker":
        raise typer.BadParameter("Only worker sessions can be removed from the worker manager.")

    del config.sessions[session_name]
    write_config(config, config_path, force=True)


def _make_worker_session_name(project_key: str, existing: set[str]) -> str:
    base = f"worker_{project_key}"
    candidate = base
    index = 2
    while candidate in existing:
        candidate = f"{base}_{index}"
        index += 1
    return candidate
