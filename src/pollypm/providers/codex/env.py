"""Environment helpers for the Codex provider — Phase C of #397.

The runtime pins ``codex`` to an isolated ``CODEX_HOME`` so parallel
accounts on the same host don't share auth state. This module owns the
two helpers that answer "where does Codex keep its profile?" and
"what env does a subprocess need to see to find it?".

Both helpers delegate to ``pollypm.runtime_env`` — the provider package
owns the public API but does not re-implement the logic. Moving the
string literal ``"CODEX_HOME"`` through a single module makes it easy
to spot every call site if the variable name ever changes upstream.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pollypm.models import ProviderKind
from pollypm.runtime_env import codex_home_dir, provider_profile_env_for_provider


def isolated_env(
    home: Path,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the env a Codex subprocess needs to use ``home`` as ``CODEX_HOME``.

    Mirrors the semantics of ``onboarding._isolated_env`` for Codex: the
    returned dict starts from ``base_env`` (default: ``os.environ``) and
    layers ``CODEX_HOME=<home>/.codex`` on top. Callers that want only
    the additive contribution (no ``os.environ`` leak) should pass
    ``base_env={}``.
    """
    env = dict(base_env if base_env is not None else os.environ)
    return provider_profile_env_for_provider(ProviderKind.CODEX, home, base_env=env)


def codex_profile_dir(home: Path) -> Path:
    """Return the ``.codex`` directory inside ``home``.

    Thin wrapper over ``pollypm.runtime_env.codex_home_dir`` so the
    Codex provider package has a public symbol for the auth-file
    location. The returned path is *not* created on disk — callers that
    need the directory to exist must ``mkdir`` it themselves.
    """
    return codex_home_dir(home)


__all__ = ["codex_profile_dir", "isolated_env"]
