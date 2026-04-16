"""Dedicated itsalive.co plugin for PollyPM."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pollypm.plugins_builtin.core_agent_profiles.profiles import StaticPromptProfile, heartbeat_prompt, polly_prompt, worker_prompt
from pollypm.itsalive import build_deploy_instructions, sweep_pending_deploys
from pollypm.plugin_api.v1 import Capability, HookContext, JobHandlerAPI, PluginAPI, PollyPMPlugin, RosterAPI

logger = logging.getLogger(__name__)


def _polly_prompt() -> str:
    return (
        f"{polly_prompt()}\n\n"
        "When the user wants to publish a site through itsalive, prefer PollyPM's `pm itsalive ...` "
        "commands over telling a worker to sit in a polling loop. If verification is needed, notify the "
        "user once and keep the lane moving; heartbeat will resume the deploy later.\n\n"
        f"{build_deploy_instructions()}"
    )


def _worker_prompt() -> str:
    return (
        f"{worker_prompt()}\n\n"
        "If a site should ship via itsalive, use the built-in PollyPM itsalive commands. Do not wait "
        "interactively for email verification when the wrapper has already persisted pending state.\n\n"
        f"{build_deploy_instructions()}"
    )


def _heartbeat_prompt() -> str:
    return (
        f"{heartbeat_prompt()}\n\n"
        "Heartbeat is also responsible for sweeping pending itsalive deploys and completing them once "
        "email verification finishes.\n\n"
        f"{build_deploy_instructions()}"
    )


def _on_session_after_launch(ctx: HookContext) -> None:
    logger.info("itsalive plugin active for %s", ctx.metadata.get("session_name", "session"))


def _resolve_project_root(payload: dict[str, Any]) -> Path:
    """Resolve the project root for a deploy sweep invocation.

    Order of precedence: explicit payload hint → config's project.root_dir
    → current working directory (best-effort fallback for unconfigured runs).
    """
    hint = payload.get("project_root") if isinstance(payload, dict) else None
    if hint:
        return Path(hint)
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path

        config_path = resolve_config_path(DEFAULT_CONFIG_PATH)
        if config_path.exists():
            return load_config(config_path).project.root_dir
    except Exception:  # noqa: BLE001
        pass
    return Path.cwd()


def itsalive_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Sweep pending itsalive deploys — migrated out of ``Supervisor.run_heartbeat``.

    For each pending deploy, the handler checks the verification status,
    completes deploys that are verified, and notifies the inbox on expiry.
    Results are returned as a small summary for observability.
    """
    project_root = _resolve_project_root(payload)
    outcomes = sweep_pending_deploys(project_root)
    summary: dict[str, int] = {}
    for outcome in outcomes:
        summary[outcome.status] = summary.get(outcome.status, 0) + 1
    return {"swept": len(outcomes), "by_status": summary}


def _register_handlers(api: JobHandlerAPI) -> None:
    api.register_handler(
        "itsalive.deploy_sweep", itsalive_sweep_handler,
        max_attempts=2, timeout_seconds=30.0,
    )


def _register_roster(api: RosterAPI) -> None:
    # Legacy registration path — kept for rail versions that still call
    # register_roster directly. The canonical registration now happens in
    # ``_initialize`` below via the unified PluginAPI.
    api.register_recurring("@every 60s", "itsalive.deploy_sweep", {})


def _initialize(api: PluginAPI) -> None:
    """Register itsalive's recurring deploy sweep via the unified plugin
    API (docs/plugin-discovery-spec.md §6).

    Inverting the direction: core no longer needs to know about
    sweep_pending_deploys — the plugin declares its own cadence. Matches
    the old 60s tick embedded in ``Supervisor.run_heartbeat``. See #118.
    """
    # ``register_roster`` already wires the schedule; this hook is safe
    # to call twice — the roster dedupes on (handler_name, payload). We
    # keep it here so plugins using the new initialize() lifecycle stay
    # readable and forward-compatible with rails that drop the older
    # register_roster hook.
    #
    # Gracefully skip if no roster is available (initialize may be
    # invoked in test harnesses that don't wire a heartbeat rail).
    try:
        api.roster.register_recurring("@every 60s", "itsalive.deploy_sweep", {})
    except RuntimeError:
        logger.debug("itsalive initialize skipped roster registration — no RosterAPI")


plugin = PollyPMPlugin(
    name="itsalive",
    version="0.3.0",
    description="itsalive.co deployment integration for PollyPM sessions.",
    capabilities=(
        Capability(kind="agent_profile", name="itsalive"),
        Capability(kind="agent_profile", name="polly", replaces=("polly",)),
        Capability(kind="agent_profile", name="worker", replaces=("worker",)),
        Capability(kind="agent_profile", name="heartbeat", replaces=("heartbeat",)),
        Capability(kind="hook", name="session.after_launch"),
        Capability(kind="job_handler", name="itsalive.deploy_sweep"),
        Capability(kind="roster_entry", name="itsalive.deploy_sweep"),
    ),
    agent_profiles={
        "itsalive": lambda: StaticPromptProfile(name="itsalive", prompt=build_deploy_instructions()),
        "polly": lambda: StaticPromptProfile(name="polly", prompt=_polly_prompt()),
        "worker": lambda: StaticPromptProfile(name="worker", prompt=_worker_prompt()),
        "heartbeat": lambda: StaticPromptProfile(name="heartbeat", prompt=_heartbeat_prompt()),
    },
    observers={
        "session.after_launch": [_on_session_after_launch],
    },
    register_handlers=_register_handlers,
    register_roster=_register_roster,
    initialize=_initialize,
)
