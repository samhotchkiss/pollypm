from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path

import typer

from pollypm.accounts import is_account_runtime_unavailable
from pollypm.acct import detect_logged_in
from pollypm.config import config_rmw_lock, load_config, write_config
from pollypm.onboarding import default_session_args
from pollypm.models import ProviderKind, SessionConfig
from pollypm.role_routing import resolved_provider_kind, resolve_role_assignment
from pollypm.supervisor import Supervisor
from pollypm.worktrees import ensure_worktree


_log = logging.getLogger(__name__)


def _effective_control_accounts(config_path: Path) -> set[str]:
    config = load_config(config_path)
    accounts = {config.pollypm.controller_account}
    # Backend-aware read: pg installs route through the cluster-A
    # facade; sqlite installs read session_runtime from the per-project
    # StateStore.
    from pollypm.storage._backend_dispatch import is_pg_backend

    if is_pg_backend(config):
        from pollypm.storage.pg_sessions import get_session_runtime

        for session_name in ("heartbeat", "operator"):
            runtime = get_session_runtime(session_name)
            if runtime is not None and runtime.effective_account:
                accounts.add(runtime.effective_account)
    else:
        from pollypm.storage.state import StateStore

        with StateStore(config.project.state_db) as store:
            for session_name in ("heartbeat", "operator"):
                runtime = store.get_session_runtime(session_name)
                if runtime is not None and runtime.effective_account:
                    accounts.add(runtime.effective_account)
    return {name for name in accounts if name}


def _account_is_available(config_path: Path, account_name: str) -> bool:
    config = load_config(config_path)
    account = config.accounts[account_name]
    if not detect_logged_in(account):
        return False
    from pollypm.storage._backend_dispatch import is_pg_backend

    if is_pg_backend(config):
        from pollypm.storage.pg_accounts import get_account_runtime

        runtime = get_account_runtime(account_name)
    else:
        from pollypm.storage.state import StateStore

        with StateStore(config.project.state_db) as store:
            runtime = store.get_account_runtime(account_name)
    # Accept both ``auth_broken`` (canonical, written by heartbeats/api.py
    # and supervisor.py) and the legacy hyphenated ``auth-broken`` form
    # so a runtime row written by either side correctly excludes the
    # account from worker selection (#1437).
    if runtime is not None and is_account_runtime_unavailable(runtime.status):
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
    # #2063 round 12: pre-lock work is DISCOVERY ONLY — read-only helpers
    # whose results are NOT committed to disk. The legacy implementation
    # resolved the worker ``account``, role routing, ``active_provider``,
    # ``active_model``, and ``prompt`` from the pre-lock snapshot and then
    # committed those values inside the lock. If an account or project edit
    # landed between pre-lock validation and the locked commit, the
    # committed ``SessionConfig`` could reference an account no longer in
    # ``fresh.accounts`` — load_config's invariant guard then raised
    # ``ValueError: Session references unknown account`` on the next read.
    #
    # Codex reproduction: patch ``suggest_worker_prompt`` to remove the
    # selected worker account from disk between pre-lock account validation
    # and the locked commit; the legacy path persisted an invalid session.
    #
    # Fix shape: pre-lock keeps only cheap helpers — project existence
    # (early-error UX), role routing (pure function, re-derived inside the
    # lock anyway), and a prompt suggestion when the operator passed none.
    # Every field that ends up in the committed ``SessionConfig`` is
    # re-derived from ``fresh`` inside the lock.

    role = (role or "worker").strip() or "worker"

    # PRE-LOCK DISCOVERY (results are NOT committed; the locked block
    # rebuilds everything against ``fresh``):
    # - early project-existence check for a friendlier error than the
    #   locked block would emit on a missing project_key;
    # - prompt suggestion when the caller passed none (the suggestion is
    #   currently empty-string so it can't go stale, but we still
    #   call it pre-lock to avoid the suggester re-reading inside the
    #   critical section).
    discovery_config = load_config(config_path)
    if project_key not in discovery_config.projects:
        registered = ", ".join(sorted(discovery_config.projects.keys())) or "<none>"
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
    if not prompt or not prompt.strip():
        prompt_suggestion = suggest_worker_prompt(
            config_path, project_key=project_key
        )
    else:
        prompt_suggestion = None
    del discovery_config

    # Workers use a ``pa`` worktree lane; non-worker roles (e.g. architect)
    # take a lane named after the role so they don't collide with workers.
    lane_kind = "pa" if role == "worker" else role
    window_prefix = "worker" if role == "worker" else role

    # LOCKED RMW: every field committed to disk is derived from ``fresh``.
    # Round 11 moved the duplicate-role check + session_key allocation +
    # worktree creation + commit inside the lock. Round 12 extends that
    # invariant: account selection, route resolution, ``active_provider``,
    # ``active_model``, and the final ``prompt`` are ALSO derived from
    # ``fresh`` — so an account / project edit between pre-lock discovery
    # and the locked commit can never persist a session referencing
    # something that's no longer in the locked config snapshot.
    worktree = None
    try:
        with config_rmw_lock(config_path):
            fresh = load_config(config_path)

            # Re-validate the project against ``fresh`` — an external
            # ``pm projects remove`` could have landed between discovery
            # and lock acquire.
            project = fresh.projects.get(project_key)
            if project is None:
                registered = ", ".join(sorted(fresh.projects.keys())) or "<none>"
                raise typer.BadParameter(
                    f"No project '{project_key}' registered.\n"
                    f"\n"
                    f"Why: project disappeared between worker-start "
                    f"validation and the config write lock.\n"
                    f"\n"
                    f"Fix: re-register with `pm add-project <path> "
                    f"--name {project_key}` (currently registered: "
                    f"{registered})."
                )

            # Role routing is a pure function of ``fresh`` — re-resolve so
            # an external role_assignments edit between pre-lock discovery
            # and the locked commit is honoured.
            routed_assignment = resolve_role_assignment(
                role, project_key, config=fresh
            )
            routed_provider: ProviderKind | None
            try:
                routed_provider = resolved_provider_kind(routed_assignment)
            except ValueError:
                _log.warning(
                    "Ignoring invalid routed provider %r for %s on %s.",
                    routed_assignment.provider,
                    role,
                    project_key,
                )
                routed_provider = None

            # Account selection is derived from ``fresh``. The auto-select
            # helper reloads config_path itself; under the held lock that
            # reload sees the same ``fresh`` snapshot (load_config caches
            # on mtime; no other writer can advance mtime while we hold
            # the lock).
            resolved_account = account_name
            if resolved_account is None:
                preferred_provider = (
                    routed_provider
                    if routed_provider is not None
                    and routed_assignment.source != "fallback"
                    else provider
                )
                try:
                    resolved_account = auto_select_worker_account(
                        config_path,
                        provider=preferred_provider,
                    )
                except typer.BadParameter:
                    if (
                        routed_provider is None
                        or provider is not None
                        or routed_assignment.source == "fallback"
                    ):
                        raise
                    _log.warning(
                        "Role routing resolved %s for %s to %s/%s from %s, but no matching account is available; "
                        "falling back to the legacy worker account selection.",
                        role,
                        project_key,
                        routed_assignment.provider,
                        routed_assignment.model,
                        routed_assignment.source,
                    )
                    resolved_account = auto_select_worker_account(
                        config_path, provider=provider
                    )

            if resolved_account not in fresh.accounts:
                # Round-12 invariant: never commit a session whose
                # ``account`` is missing from the locked snapshot.
                known = ", ".join(sorted(fresh.accounts.keys())) or "<none>"
                raise typer.BadParameter(
                    f"Unknown account: {resolved_account} "
                    f"(known in current config: {known})"
                )
            account = fresh.accounts[resolved_account]
            if provider is not None and account.provider is not provider:
                raise typer.BadParameter(
                    f"Account {resolved_account} uses provider "
                    f"{account.provider.value}, not {provider.value}"
                )
            active_provider = account.provider
            active_model: str | None = None
            if routed_provider is not None and (
                routed_assignment.source != "fallback"
                or account.provider is routed_provider
            ):
                if account.provider is not routed_provider:
                    _log.warning(
                        "Role routing resolved %s for %s to %s/%s from %s, but account %s uses %s; "
                        "keeping the session on the account provider.",
                        role,
                        project_key,
                        routed_assignment.provider,
                        routed_assignment.model,
                        routed_assignment.source,
                        resolved_account,
                        account.provider.value,
                    )
                else:
                    active_provider = routed_provider
                    active_model = routed_assignment.model
                    _log.info(
                        "Role routing resolved %s for %s to %s/%s from %s.",
                        role,
                        project_key,
                        routed_assignment.provider,
                        routed_assignment.model,
                        routed_assignment.source,
                    )
            elif routed_provider is not None and routed_assignment.source != "fallback":
                _log.warning(
                    "Role routing resolved %s for %s to %s/%s from %s, but account %s uses %s; "
                    "keeping the session on the account provider.",
                    role,
                    project_key,
                    routed_assignment.provider,
                    routed_assignment.model,
                    routed_assignment.source,
                    resolved_account,
                    account.provider.value,
                )

            for existing in fresh.sessions.values():
                if (
                    existing.role == role
                    and existing.project == project_key
                    and existing.enabled
                ):
                    raise typer.BadParameter(
                        f"Project {project_key} already has {role} session {existing.name}"
                    )
            if session_name is not None and session_name in fresh.sessions:
                # Explicit session name collision — surface a clear error
                # rather than overwriting the existing entry.
                raise typer.BadParameter(
                    f"Session name {session_name!r} is already in use"
                )
            session_key = session_name or _make_role_session_name(
                role, project_key, set(fresh.sessions)
            )
            worktree = ensure_worktree(
                config_path,
                project_key=project_key,
                lane_kind=lane_kind,
                lane_key=session_key,
                session_name=session_key,
            )
            final_prompt = prompt if (prompt and prompt.strip()) else (
                prompt_suggestion or ""
            )
            worker = SessionConfig(
                name=session_key,
                role=role,
                provider=active_provider,
                account=resolved_account,
                cwd=Path(worktree.path) if worktree is not None else project.path,
                project=project_key,
                window_name=f"{window_prefix}-{project_key}",
                prompt=final_prompt,
                agent_profile=agent_profile,
                args=default_session_args(
                    active_provider,
                    open_permissions=fresh.pollypm.open_permissions_by_default,
                    role=role,
                    model=active_model,
                ),
            )
            fresh.sessions[session_key] = worker
            write_config(fresh, config_path, force=True)
    except typer.BadParameter:
        # Uniqueness / validation failures and operator-input errors leave
        # the worktree state alone — either we never created one (the
        # check fired first) or the worktree belongs to a sibling session
        # that legitimately won the race.
        raise
    except Exception as exc:
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
    # #2063 round 7: hold the shared RMW lock across the full
    # load → mutate → write.
    with config_rmw_lock(config_path):
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
