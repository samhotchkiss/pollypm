"""Default LaunchPlanner implementation.

This module preserves the historical Supervisor behavior for planning
session launches. The bodies of ``plan_launches``, ``effective_session``,
``tmux_session_for_launch``, and ``launch_by_session`` are lifted from
``Supervisor`` verbatim — no logic changes.

The planner depends on a handful of Supervisor-owned helpers
(``_effective_account``, ``_apply_role_launch_restrictions``,
``_resolve_profile_prompt``, and the storage-closet naming) because
those concerns (auth sync, sandboxing, agent profiles) live elsewhere
in Supervisor today and aren't part of this step's extraction. They're
passed in as callables via a small context object so the planner
stays a clean seam we can swap later.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from typing import TYPE_CHECKING, Callable

from pollypm.models import AccountConfig, ProviderKind, SessionConfig, SessionLaunchSpec
from pollypm.projects import ensure_session_lock
from pollypm.providers import get_provider
from pollypm.providers.args import sanitize_provider_args
from pollypm.providers.base import LaunchCommand
from pollypm.role_routing import resolved_provider_kind, resolve_role_assignment
from pollypm.runtimes import get_runtime

if TYPE_CHECKING:
    from pollypm.config import PollyPMConfig
    from pollypm.storage.state import StateStore


_CONTROL_ROLES = frozenset({"heartbeat-supervisor", "operator-pm", "triage", "reviewer"})
_ROUTED_ROLES = frozenset({"operator-pm", "architect", "worker", "reviewer"})
_log = logging.getLogger(__name__)


def _preferred_account_names(
    config: "PollyPMConfig",
    *,
    session: SessionConfig,
    override_account: str | None,
) -> list[str]:
    names: list[str] = []
    for name in (
        override_account,
        session.account,
        config.pollypm.controller_account,
        *config.pollypm.failover_accounts,
        *config.accounts,
    ):
        if name and name not in names:
            names.append(name)
    return names


def _first_account_for_provider(
    config: "PollyPMConfig",
    *,
    provider: ProviderKind,
    session: SessionConfig,
    override_account: str | None,
) -> str | None:
    for name in _preferred_account_names(
        config,
        session=session,
        override_account=override_account,
    ):
        account = config.accounts.get(name)
        if account is not None and account.provider is provider:
            return name
    return None


@dataclass(slots=True)
class DefaultLaunchPlannerContext:
    """Callables the default planner needs from its host.

    The planner doesn't own auth-sync, worker sandboxing, or agent
    profile resolution — those live elsewhere (Supervisor today). The
    context threads the relevant callables through so the planner can
    call them without a hard Supervisor dependency.
    """

    config: "PollyPMConfig"
    store: "StateStore"
    readonly_state: bool
    effective_account: Callable[[SessionConfig, AccountConfig], AccountConfig]
    apply_role_launch_restrictions: Callable[[SessionConfig, LaunchCommand], LaunchCommand]
    resolve_profile_prompt: Callable[[SessionConfig, AccountConfig], str | None]
    storage_closet_session_name: Callable[[], str]


class DefaultLaunchPlanner:
    """Default launch planner — preserves historical Supervisor semantics."""

    name = "default"

    def __init__(self, context: DefaultLaunchPlannerContext) -> None:
        self._ctx = context
        self._cached_launches: list[SessionLaunchSpec] | None = None

    # ── Public protocol surface ───────────────────────────────────────────

    def plan_launches(
        self, *, controller_account: str | None = None
    ) -> list[SessionLaunchSpec]:
        """Return the launch plan for every enabled session.

        Verbatim lift from ``Supervisor.plan_launches``.
        """
        ctx = self._ctx
        # Cache launches for the default (no controller override) case.
        # The launch plan only changes when config changes.
        if controller_account is None and self._cached_launches is not None:
            return self._cached_launches
        launches: list[SessionLaunchSpec] = []
        worker_projects: dict[str, str] = {}
        for session in ctx.config.sessions.values():
            effective = self.effective_session(session, controller_account)
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
            if effective.account not in ctx.config.accounts:
                continue  # skip sessions with missing accounts
            account = ctx.effective_account(effective, ctx.config.accounts[effective.account])
            if account.provider is not effective.provider:
                raise ValueError(
                    f"Session {effective.name} uses provider {effective.provider.value} "
                    f"but account {account.name} is configured for {account.provider.value}"
                )
            provider = get_provider(effective.provider, root_dir=ctx.config.project.root_dir)
            launch = provider.build_launch_command(effective, account)
            launch = ctx.apply_role_launch_restrictions(effective, launch)
            # Architect warm-resume: when an architect was previously
            # idle-closed by the heartbeat sweep, swap its argv to the
            # provider's resume incantation so the new pane comes back
            # warm with its prior project context. Token is left in
            # place until the resumed architect produces output (the
            # heartbeat sweep clears it on first non-empty snapshot).
            if effective.role == "architect" and effective.project:
                resume_token = ctx.store.get_architect_resume_token(effective.project)
                if resume_token is not None and resume_token.provider == account.provider.value:
                    from pollypm.acct.registry import get_provider as _get_acct_provider
                    acct_adapter = _get_acct_provider(account.provider.value)
                    # Strip the binary from existing argv (acct adapter
                    # prepends its own with the resume incantation).
                    extra_args = list(launch.argv[1:]) if launch.argv else []
                    new_argv = acct_adapter.resume_launch_cmd(
                        account, resume_token.session_id, extra_args,
                    )
                    launch = replace(launch, argv=new_argv)
            if effective.provider is ProviderKind.CODEX and effective.role in _CONTROL_ROLES and launch.initial_input:
                env = dict(launch.env)
                env["PM_CODEX_HOME_AGENTS_MD"] = launch.initial_input
                launch = replace(launch, env=env, initial_input=None)
            runtime = get_runtime(account.runtime, root_dir=ctx.config.project.root_dir)
            window_name = effective.window_name or effective.name
            log_dir = ctx.config.project.logs_dir / effective.name
            ensure_session_lock(log_dir, effective.name)
            log_path = log_dir / f"{window_name}.log"
            launches.append(
                SessionLaunchSpec(
                    session=effective,
                    account=account,
                    window_name=window_name,
                    log_path=log_path,
                    command=runtime.wrap_command(launch, account, ctx.config.project),
                    resume_marker=launch.resume_marker,
                    initial_input=launch.initial_input,
                    fresh_launch_marker=launch.fresh_launch_marker,
                )
            )
        if controller_account is None:
            self._cached_launches = launches
        return launches

    def effective_session(
        self,
        session: SessionConfig,
        controller_account: str | None = None,
    ) -> SessionConfig:
        """Return ``session`` with runtime account overrides applied.

        Verbatim lift from ``Supervisor.effective_session``.
        """
        ctx = self._ctx
        # Local imports match the original Supervisor module's dependencies
        # without pulling them into this module's top-level import set.
        from pollypm.onboarding import default_control_args, default_session_args

        effective = session
        routed_assignment = None
        try:
            runtime = ctx.store.get_session_runtime(session.name)
        except Exception:  # noqa: BLE001
            runtime = None
        override_account: str | None = None
        override_applied = False
        if controller_account is not None and session.role in _CONTROL_ROLES:
            override_account = controller_account
            override_applied = True
        elif runtime is not None and runtime.effective_account:
            override_account = runtime.effective_account
            override_applied = True
        if override_account is not None:
            if override_account in ctx.config.accounts:
                account = ctx.config.accounts[override_account]
                effective = replace(effective, provider=account.provider, account=override_account)
            else:
                # Stale account ref in state DB — clear it and fall back to config default
                if runtime is not None and not override_applied:
                    try:
                        ctx.store.set_session_runtime(session.name, effective_account="")
                    except Exception:  # noqa: BLE001
                        pass
        if session.role in _ROUTED_ROLES:
            project_key = None if session.role == "operator-pm" else session.project
            routed_assignment = resolve_role_assignment(
                session.role,
                project_key,
                config=ctx.config,
            )
            try:
                routed_provider = resolved_provider_kind(routed_assignment)
            except ValueError:
                _log.warning(
                    "Ignoring invalid routed provider %r for %s session %s.",
                    routed_assignment.provider,
                    session.role,
                    session.name,
                )
            else:
                if routed_assignment.source != "fallback":
                    account_name = _first_account_for_provider(
                        ctx.config,
                        provider=routed_provider,
                        session=effective,
                        override_account=override_account,
                    )
                    if account_name is None:
                        _log.warning(
                            "Role routing resolved %s session %s to %s/%s from %s, but no compatible account is configured; "
                            "keeping the existing provider/account.",
                            session.role,
                            session.name,
                            routed_assignment.provider,
                            routed_assignment.model,
                            routed_assignment.source,
                        )
                        routed_assignment = None
                    else:
                        effective = replace(
                            effective,
                            provider=routed_provider,
                            account=account_name,
                        )
                        _log.info(
                            "Role routing resolved %s session %s to %s/%s from %s.",
                            session.role,
                            session.name,
                            routed_assignment.provider,
                            routed_assignment.model,
                            routed_assignment.source,
                        )
                elif effective.provider is routed_provider:
                    _log.info(
                        "Role routing resolved %s session %s to %s/%s from %s.",
                        session.role,
                        session.name,
                        routed_assignment.provider,
                        routed_assignment.model,
                        routed_assignment.source,
                    )
                else:
                    routed_assignment = None
        if effective.account not in ctx.config.accounts:
            # Fall back to controller account if the session's account is missing
            if controller_account and controller_account in ctx.config.accounts:
                effective = replace(effective, account=controller_account)
            else:
                return effective
        if ctx.readonly_state:
            return effective
        account = ctx.config.accounts[effective.account]
        profile_prompt = ctx.resolve_profile_prompt(effective, account)
        if effective.role in _CONTROL_ROLES:
            effective = replace(
                effective,
                prompt=profile_prompt or effective.prompt,
                args=default_control_args(
                    account.provider,
                    open_permissions=ctx.config.pollypm.open_permissions_by_default,
                    role=effective.role,
                    model=routed_assignment.model if routed_assignment is not None else None,
                ),
            )
        else:
            if profile_prompt and not effective.prompt:
                effective = replace(effective, prompt=profile_prompt)
            elif profile_prompt and effective.prompt:
                # Worker has a custom prompt — append the profile's task
                # management section so the worker knows how to use the
                # task system regardless of what the custom prompt says.
                effective = replace(
                    effective,
                    prompt=effective.prompt.rstrip() + "\n\n" + profile_prompt,
                )
            if not effective.args:
                effective = replace(
                    effective,
                    args=default_session_args(
                        account.provider,
                        open_permissions=ctx.config.pollypm.open_permissions_by_default,
                        role=effective.role,
                        model=routed_assignment.model if routed_assignment is not None else None,
                    ),
                )
            elif routed_assignment is not None:
                effective = replace(
                    effective,
                    args=default_session_args(
                        account.provider,
                        open_permissions=ctx.config.pollypm.open_permissions_by_default,
                        role=effective.role,
                        model=routed_assignment.model,
                    ),
                )
            else:
                # Sanitize: strip provider-incompatible flags from project-local configs
                effective = replace(
                    effective,
                    args=sanitize_provider_args(effective.args, account.provider),
                )
        return effective

    def tmux_session_for_launch(self, launch: SessionLaunchSpec) -> str:
        """Return the tmux session name that should host ``launch``.

        Verbatim lift from ``Supervisor.tmux_session_for_launch`` /
        ``Supervisor._tmux_session_for_role``. Today every role lands
        in the storage-closet session; the indirection is preserved so
        future planners can override placement.
        """
        # launch.session.role is currently unused (all roles go to the
        # same tmux session) but kept in the signature to match the
        # Supervisor method it replaces.
        _ = launch.session.role
        return self._ctx.storage_closet_session_name()

    def launch_by_session(self, session_name: str) -> SessionLaunchSpec:
        """Return the ``SessionLaunchSpec`` for ``session_name``.

        Verbatim lift from ``Supervisor.launch_by_session``.
        """
        for launch in self.plan_launches():
            if launch.session.name == session_name:
                return launch
        raise KeyError(f"Unknown session: {session_name}")

    def invalidate_cache(self) -> None:
        """Drop the cached launch plan."""
        self._cached_launches = None
