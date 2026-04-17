from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer

from pollypm.accounts import _account_logged_in
from pollypm.config import load_config, write_config
from pollypm.onboarding import default_session_args
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
    role: str = "worker",
    agent_profile: str | None = None,
) -> SessionConfig:
    config = load_config(config_path)
    project = config.projects.get(project_key)
    if project is None:
        registered = ", ".join(sorted(config.projects.keys())) or "<none>"
        raise typer.BadParameter(
            f"No project '{project_key}' registered.\n"
            f"\n"
            f"Why: `pm worker-start` needs a project key that's already "
            f"tracked in the PollyPM config.\n"
            f"\n"
            f"Fix: run `pm projects` to see registered projects "
            f"(currently: {registered}), or register a new one with\n"
            f"    pm add-project <path> --name {project_key}"
        )

    role = (role or "worker").strip() or "worker"

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
        if existing.role == role and existing.project == project_key and existing.enabled:
            raise typer.BadParameter(
                f"Project {project_key} already has {role} session {existing.name}"
            )

    session_key = session_name or _make_role_session_name(
        role, project_key, set(config.sessions)
    )
    # Workers use a ``pa`` worktree lane; non-worker roles (e.g. architect)
    # take a lane named after the role so they don't collide with workers.
    lane_kind = "pa" if role == "worker" else role
    worktree = ensure_worktree(
        config_path,
        project_key=project_key,
        lane_kind=lane_kind,
        lane_key=session_key,
        session_name=session_key,
    )
    window_prefix = "worker" if role == "worker" else role
    worker = SessionConfig(
        name=session_key,
        role=role,
        provider=account.provider,
        account=account_name,
        cwd=Path(worktree.path) if worktree is not None else project.path,
        project=project_key,
        window_name=f"{window_prefix}-{project_key}",
        prompt=prompt,
        agent_profile=agent_profile,
        args=default_session_args(account.provider, open_permissions=config.pollypm.open_permissions_by_default),
    )
    config.sessions[session_key] = worker
    try:
        write_config(config, config_path, force=True)
    except Exception as exc:
        del config.sessions[session_key]
        if worktree is not None and worktree.path and Path(worktree.path).exists():
            import shutil
            shutil.rmtree(worktree.path, ignore_errors=True)
        raise typer.BadParameter(f"Failed to save worker session config: {exc}") from exc
    return worker


def suggest_worker_prompt(config_path: Path, *, project_key: str) -> str:
    """Return an empty prompt -- workers wait for the heartbeat to assign work."""
    config = load_config(config_path)
    project = config.projects.get(project_key)
    if project is None:
        registered = ", ".join(sorted(config.projects.keys())) or "<none>"
        raise typer.BadParameter(
            f"No project '{project_key}' registered.\n"
            f"Fix: `pm projects` lists registered projects "
            f"(currently: {registered}); add with `pm add-project <path> "
            f"--name {project_key}`."
        )
    return ""


def launch_worker_session(
    config_path: Path,
    session_name: str,
    on_status: Callable[[str], None] | None = None,
    skip_stabilize: bool = False,
) -> SessionConfig:
    config = load_config(config_path)
    if session_name not in config.sessions:
        raise typer.BadParameter(f"Unknown session: {session_name}")

    supervisor = Supervisor(config)
    supervisor.ensure_layout()
    if skip_stabilize:
        supervisor.create_session_window(session_name, on_status=on_status)
    else:
        supervisor.launch_session(session_name, on_status=on_status)
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
    return _make_role_session_name("worker", project_key, existing)


def _make_role_session_name(role: str, project_key: str, existing: set[str]) -> str:
    """Return an unused ``<role>_<project>`` session name.

    Workers retain the historical ``worker_<project>`` convention. Other
    roles (e.g. ``architect``) follow the same ``<role>_<project>`` layout
    so the :func:`role_candidate_names` resolver (which probes both
    hyphen- and underscore-separated forms) can find them without
    bespoke per-role naming logic.
    """
    base = f"{role}_{project_key}"
    candidate = base
    index = 2
    while candidate in existing:
        candidate = f"{base}_{index}"
        index += 1
    return candidate
