from __future__ import annotations

import base64
import json
import logging
import os
import shlex
import shutil
import subprocess

from pollypm.models import AccountConfig, ProjectSettings
from pollypm.providers.base import LaunchCommand
from pollypm.runtime_env import launch_context_env, provider_profile_env
from pollypm.runtimes.base import WrappedRuntimeCommand

logger = logging.getLogger(__name__)


# Cached enriched PATH used to resolve agent binaries to absolute paths
# before they are serialized into the runtime_launcher payload (#965).
# The first call seeds the cache by harvesting the user's interactive
# shell PATH (``zsh -lic 'echo $PATH'``); subsequent calls reuse it.
# When the shell harvest fails we fall back to ``os.environ['PATH']``
# concatenated with a known set of canonical user bin directories so
# the resolver still finds binaries installed in non-default locations
# (npm-global, pipx, cargo) on a fresh machine.
_CACHED_RESOLVE_PATH: str | None = None

# Canonical fallback bin dirs to consider when no shell PATH is
# available. These cover the common cases on macOS and Linux user
# accounts (npm-global, pipx, cargo, homebrew, system bins) so the
# resolver finds agent binaries that a sanitized tmux child env may
# have stripped from PATH.
_FALLBACK_BIN_DIRS = (
    "~/.npm-global/bin",
    "~/.local/bin",
    "~/.cargo/bin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def _harvest_shell_path() -> str | None:
    """Run the user's login shell and capture its ``$PATH``.

    Returns ``None`` on any failure — callers should fall back to the
    canonical bin-dir list. Tries ``zsh`` first (the macOS default),
    then ``bash``; both with ``-lic`` so login + interactive rc files
    contribute their PATH additions.
    """
    shell = os.environ.get("SHELL") or "/bin/zsh"
    candidates: list[str] = []
    if shell:
        candidates.append(shell)
    for fallback in ("/bin/zsh", "/bin/bash"):
        if fallback not in candidates:
            candidates.append(fallback)
    for candidate in candidates:
        try:
            result = subprocess.run(
                [candidate, "-lic", "echo $PATH"],
                check=True,
                text=True,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        path = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if path and ":" in path:
            return path
    return None


def _resolve_path() -> str:
    """Return the cached enriched PATH used to locate agent binaries.

    Composition (deduplicated, order-preserving):
    1. The user's interactive shell ``$PATH`` (``zsh -lic`` harvest).
    2. The cockpit process' ``os.environ['PATH']``.
    3. Canonical user bin-dirs (npm-global, .local/bin, homebrew, ...).

    Cached after the first call so the shell harvest only runs once
    per process.
    """
    global _CACHED_RESOLVE_PATH
    if _CACHED_RESOLVE_PATH is not None:
        return _CACHED_RESOLVE_PATH
    parts: list[str] = []
    seen: set[str] = set()

    def _add(piece: str | None) -> None:
        if not piece:
            return
        for entry in piece.split(os.pathsep):
            entry = entry.strip()
            if not entry or entry in seen:
                continue
            seen.add(entry)
            parts.append(entry)

    _add(_harvest_shell_path())
    _add(os.environ.get("PATH"))
    for raw in _FALLBACK_BIN_DIRS:
        _add(os.path.expanduser(raw))
    _CACHED_RESOLVE_PATH = os.pathsep.join(parts)
    return _CACHED_RESOLVE_PATH


def resolve_argv_binary(argv: list[str]) -> list[str]:
    """Return ``argv`` with ``argv[0]`` resolved to an absolute path.

    Locates the binary against the enriched PATH (shell + os.environ
    + canonical fallbacks) so ``os.execvpe`` in the runtime launcher
    does not need to search a sanitized child-process PATH (#965).
    Returns the original argv unchanged when:

    * ``argv`` is empty.
    * ``argv[0]`` is already an absolute path.
    * ``argv[0]`` cannot be resolved (the launcher will fail loudly
      with a clear error rather than the bare ``execvpe`` traceback).
    """
    if not argv:
        return argv
    binary = argv[0]
    if not binary or os.path.isabs(binary):
        return argv
    resolved = shutil.which(binary, path=_resolve_path())
    if not resolved:
        logger.warning(
            "resolve_argv_binary: %r not found on enriched PATH; "
            "leaving argv unchanged so the launcher can surface a clear error",
            binary,
        )
        return argv
    return [resolved, *argv[1:]]


class LocalRuntimeAdapter:
    def _launcher_python(self, project: ProjectSettings) -> str:
        venv_python = project.root_dir / ".venv" / "bin" / "python"
        if venv_python.exists():
            return str(venv_python)
        # When installed globally (uv tool install), use the current interpreter
        import sys
        return sys.executable

    def _encode_payload(
        self,
        command: LaunchCommand,
        account: AccountConfig,
        project: ProjectSettings,
    ) -> str:
        env = provider_profile_env(
            account,
            base_env=launch_context_env(base_env=command.env),
        )
        # #965 — resolve agent binary to an absolute path so the
        # runtime_launcher's ``os.execvpe`` does not need to search a
        # sanitized child-process PATH (npm-global stripped under tmux).
        argv = resolve_argv_binary(list(command.argv))
        resume_argv = (
            resolve_argv_binary(list(command.resume_argv))
            if command.resume_argv
            else command.resume_argv
        )
        payload = {
            "cwd": str(command.cwd),
            "env": env,
            "argv": argv,
            "resume_argv": resume_argv,
            "resume_marker": str(command.resume_marker) if command.resume_marker is not None else None,
            "fresh_launch_marker": str(command.fresh_launch_marker) if command.fresh_launch_marker is not None else None,
            "home": str(account.home) if account.home is not None else None,
            "codex_home": env.get("CODEX_HOME"),
            "claude_config_dir": env.get("CLAUDE_CONFIG_DIR"),
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def wrap_command(
        self,
        command: LaunchCommand,
        account: AccountConfig,
        project: ProjectSettings,
    ) -> str:
        python = shlex.quote(self._launcher_python(project))
        payload = shlex.quote(self._encode_payload(command, account, project))
        src_dir = (project.root_dir / "src").resolve()
        if src_dir.is_dir():
            pythonpath = shlex.quote(str(src_dir))
            prefix = f"PYTHONPATH={pythonpath}${{PYTHONPATH:+:${{PYTHONPATH}}}} "
        else:
            prefix = ""
        inner = f"{prefix}exec {python} -m pollypm.runtime_launcher {payload}"
        return f"sh -lc {shlex.quote(inner)}"

    def wrap_command_exec(
        self,
        command: LaunchCommand,
        account: AccountConfig,
        project: ProjectSettings,
    ) -> WrappedRuntimeCommand:
        env = os.environ.copy()
        env.update(
            provider_profile_env(
                account,
                base_env=launch_context_env(base_env=command.env),
            )
        )
        src_dir = (project.root_dir / "src").resolve()
        if src_dir.is_dir():
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                f"{src_dir}:{existing}" if existing else str(src_dir)
            )
        return WrappedRuntimeCommand(
            argv=[
                self._launcher_python(project),
                "-m",
                "pollypm.runtime_launcher",
                self._encode_payload(command, account, project),
            ],
            env=env,
            cwd=command.cwd,
        )
