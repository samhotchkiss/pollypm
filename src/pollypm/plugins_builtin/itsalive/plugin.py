"""Dedicated itsalive.co plugin for PollyPM."""

from __future__ import annotations

import logging

from pollypm.plugins_builtin.core_agent_profiles.profiles import StaticPromptProfile, heartbeat_prompt, polly_prompt, worker_prompt
from pollypm.itsalive import build_deploy_instructions
from pollypm.plugin_api.v1 import HookContext, PollyPMPlugin

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


plugin = PollyPMPlugin(
    name="itsalive",
    version="0.2.0",
    description="itsalive.co deployment integration for PollyPM sessions.",
    capabilities=("agent_profile", "hook"),
    agent_profiles={
        "itsalive": lambda: StaticPromptProfile(name="itsalive", prompt=build_deploy_instructions()),
        "polly": lambda: StaticPromptProfile(name="polly", prompt=_polly_prompt()),
        "worker": lambda: StaticPromptProfile(name="worker", prompt=_worker_prompt()),
        "heartbeat": lambda: StaticPromptProfile(name="heartbeat", prompt=_heartbeat_prompt()),
    },
    observers={
        "session.after_launch": [_on_session_after_launch],
    },
)
