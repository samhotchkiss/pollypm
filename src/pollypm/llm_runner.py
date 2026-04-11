"""Run LLM tasks using PollyPM's account system.

Background tasks (knowledge extraction, history import, doc updates)
should use the account with the most remaining capacity, not bare
`claude` on the host. This module picks the best account and runs
claude CLI with the correct CLAUDE_CONFIG_DIR.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pollypm.capacity import CapacityState, probe_capacity
from pollypm.config import PollyPMConfig, load_config
from pollypm.models import ProviderKind
from pollypm.runtime_env import claude_config_dir
from pollypm.storage.state import StateStore

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_CONFIG_PATH = Path.home() / ".pollypm" / "pollypm.toml"


def select_background_account(config: PollyPMConfig, store: StateStore) -> str | None:
    """Pick the Claude account with the most remaining capacity.

    Prefers non-controller accounts to avoid starving interactive sessions.
    Returns the account name, or None if no Claude accounts are available.
    """
    controller = config.pollypm.controller_account
    best_name: str | None = None
    best_remaining: int = -1
    best_is_controller = True

    for name, account in config.accounts.items():
        if account.provider != ProviderKind.CLAUDE:
            continue
        if account.home is None:
            continue

        probe = probe_capacity(config, store, name)
        if probe.state in (
            CapacityState.EXHAUSTED,
            CapacityState.AUTH_BROKEN,
            CapacityState.SIGNED_OUT,
            CapacityState.THROTTLED,
        ):
            continue

        remaining = probe.remaining_pct or 50  # assume 50% if unknown
        is_controller = name == controller

        # Prefer non-controller, then highest remaining
        if best_name is None:
            best_name, best_remaining, best_is_controller = name, remaining, is_controller
        elif best_is_controller and not is_controller:
            best_name, best_remaining, best_is_controller = name, remaining, is_controller
        elif is_controller == best_is_controller and remaining > best_remaining:
            best_name, best_remaining, best_is_controller = name, remaining, is_controller

    return best_name


def run_haiku(
    prompt: str,
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    model: str = HAIKU_MODEL,
    max_tokens: int | None = None,
) -> str | None:
    """Run a prompt through Haiku using the best available Claude account.

    Returns the raw stdout text, or None on failure.
    """
    if shutil.which("claude") is None:
        logger.warning("claude CLI not found, cannot run LLM task")
        return None

    config = load_config(config_path)
    store = StateStore(config.project.state_db)

    account_name = select_background_account(config, store)
    if account_name is None:
        logger.warning("No healthy Claude account available for background LLM task")
        return None

    account = config.accounts[account_name]
    config_dir = str(claude_config_dir(account.home))
    logger.info("Running Haiku task on account %s (%s)", account_name, config_dir)

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = config_dir

    cmd = ["claude", "-p", "--model", model]
    if max_tokens:
        cmd.extend(["--max-tokens", str(max_tokens)])

    # Pipe the prompt via stdin to avoid "Argument list too long" when the
    # prompt exceeds the OS command-line length limit (~256 KB on macOS).
    result = subprocess.run(
        cmd,
        check=False,
        text=True,
        capture_output=True,
        env=env,
        timeout=120,
        input=prompt,
    )

    if result.returncode != 0:
        logger.warning(
            "Haiku task failed (account=%s, rc=%d): %s",
            account_name, result.returncode, result.stderr[:200],
        )
        return None

    return result.stdout.strip() or None


def run_haiku_json(
    prompt: str,
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    model: str = HAIKU_MODEL,
) -> dict[str, Any] | None:
    """Run a prompt and parse the result as JSON."""
    raw = run_haiku(prompt, config_path=config_path, model=model)
    if raw is None:
        return None
    # Try to extract JSON from the response (may have markdown fences)
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first and last fence lines
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse LLM response as JSON: %s", text[:200])
        return None
    if not isinstance(parsed, dict):
        logger.warning("LLM returned non-dict JSON (got %s), ignoring", type(parsed).__name__)
        return None
    return parsed
