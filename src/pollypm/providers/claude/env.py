"""Claude-specific environment helpers.

Phase B of #397 moved the Claude ``CLAUDE_CONFIG_DIR`` plumbing into the
provider package so callers that want "just the Claude env shim" can
reach for it without pulling in the cross-provider
``pollypm.runtime_env`` module.

No behavior change: ``isolated_env(home)`` returns the same dict shape
the legacy ``onboarding._isolated_env(ProviderKind.CLAUDE, home)`` call
used to produce. The legacy helper now delegates here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

# We still share ``claude_config_dir`` with the cross-provider helpers —
# the one-line definition lives in ``runtime_env`` because Docker paths
# depend on it too. Re-exporting it here keeps ``providers.claude`` a
# complete public surface for callers that only care about Claude.
from pollypm.runtime_env import claude_config_dir as _claude_config_dir


def claude_config_dir(home: Path) -> Path:
    """Return the Claude ``CLAUDE_CONFIG_DIR`` path for ``home``."""
    return _claude_config_dir(home)


def isolated_env(
    home: Path,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return env vars pinning the Claude CLI to ``home``.

    The return dict is layered onto whatever env the caller already has;
    callers that want a purely additive contribution (the Protocol
    contract) pass ``base_env=None`` and get back just the Claude-pinned
    keys. Callers that want a full env (the legacy ``_isolated_env``
    call site in ``onboarding``) pass ``base_env=os.environ``.
    """
    env = dict(base_env) if base_env is not None else {}
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir(home))
    return env


def isolated_env_with_os_environ(home: Path) -> dict[str, str]:
    """Legacy shape for ``onboarding._isolated_env(CLAUDE, home)``.

    Included so the back-compat shim in ``onboarding.py`` can call a
    single function and get identical behavior. Prefer
    :func:`isolated_env` in new code.
    """
    return isolated_env(home, base_env=os.environ)


__all__ = ["claude_config_dir", "isolated_env", "isolated_env_with_os_environ"]
