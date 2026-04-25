from __future__ import annotations

import base64
import json
import os
import shlex

from pollypm.models import AccountConfig, ProjectSettings
from pollypm.providers.base import LaunchCommand
from pollypm.runtime_env import provider_profile_env
from pollypm.runtimes.base import WrappedRuntimeCommand


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
        env = provider_profile_env(account, base_env=command.env)
        payload = {
            "cwd": str(command.cwd),
            "env": env,
            "argv": command.argv,
            "resume_argv": command.resume_argv,
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
        env.update(provider_profile_env(account, base_env=command.env))
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
