from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path

from promptmaster.models import AccountConfig, ProjectSettings
from promptmaster.providers.base import LaunchCommand
from promptmaster.runtime_env import provider_profile_env


class LocalRuntimeAdapter:
    def _launcher_python(self, project: ProjectSettings) -> str:
        venv_python = project.root_dir / ".venv" / "bin" / "python"
        if venv_python.exists():
            return str(venv_python)
        return "python3"

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
        pythonpath = shlex.quote(str((project.root_dir / "src").resolve()))
        return (
            f"PYTHONPATH={pythonpath}${{PYTHONPATH:+:${{PYTHONPATH}}}} "
            f"exec {python} -m promptmaster.runtime_launcher {payload}"
        )
