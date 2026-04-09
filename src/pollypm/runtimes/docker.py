from __future__ import annotations

import shlex
from pathlib import Path

from pollypm.models import AccountConfig, ProjectSettings
from pollypm.providers.base import LaunchCommand
from pollypm.runtime_env import container_runtime_env_for_provider


class DockerRuntimeAdapter:
    workspace_mount = "/workspace"
    home_mount = "/home/pollypm"

    def _fresh_codex_cleanup(self, codex_home: str, resume_marker: str) -> str:
        home = shlex.quote(codex_home)
        return (
            f"if [ ! -f {resume_marker} ]; then "
            f"rm -f {home}/history.jsonl; "
            f"rm -rf {home}/sessions {home}/shell_snapshots; "
            f"find {home} -maxdepth 1 \\( -name 'logs_*.sqlite*' -o -name 'state_*.sqlite*' \\) -delete; "
            f"fi"
        )

    def _container_path_for_home_file(self, account: AccountConfig, path: Path) -> Path:
        if account.home is None:
            raise ValueError(f"Account {account.name} needs a persistent home for docker runtime")
        relative = path.resolve().relative_to(account.home.resolve())
        return Path(self.home_mount) / relative

    def wrap_command(
        self,
        command: LaunchCommand,
        account: AccountConfig,
        project: ProjectSettings,
    ) -> str:
        if not account.docker_image:
            raise ValueError(f"Account {account.name} is missing docker_image for docker runtime")
        if account.home is None:
            raise ValueError(f"Account {account.name} needs a persistent home for docker runtime")

        try:
            relative_cwd = command.cwd.resolve().relative_to(project.root_dir.resolve())
        except ValueError as exc:
            raise ValueError(
                f"Session cwd {command.cwd} must live under project root {project.root_dir} for docker runtime"
            ) from exc

        account.home.mkdir(parents=True, exist_ok=True)

        env = container_runtime_env_for_provider(account.provider, Path(self.home_mount), base_env=command.env)

        inner_parts = [
            "mkdir -p \"$HOME\" \"$XDG_CONFIG_HOME\" \"$XDG_DATA_HOME\" \"$XDG_STATE_HOME\"",
            f"cd {shlex.quote(str(Path(self.workspace_mount) / relative_cwd))}",
        ]
        if "CODEX_HOME" in env:
            inner_parts.append("mkdir -p \"$CODEX_HOME\"")
        if "CLAUDE_CONFIG_DIR" in env:
            inner_parts.append("mkdir -p \"$CLAUDE_CONFIG_DIR\"")
        if command.resume_marker is not None:
            inner_parts.append(
                f"mkdir -p {shlex.quote(str(self._container_path_for_home_file(account, command.resume_marker).parent))}"
            )
        if command.fresh_launch_marker is not None:
            inner_parts.append(
                f"mkdir -p {shlex.quote(str(self._container_path_for_home_file(account, command.fresh_launch_marker).parent))}"
            )
        for key, value in env.items():
            inner_parts.append(f"export {key}={shlex.quote(value)}")
        if command.resume_argv and command.resume_marker is not None:
            resume_marker = self._container_path_for_home_file(account, command.resume_marker)
            quoted_resume_marker = shlex.quote(str(resume_marker))
            if command.fresh_launch_marker is not None:
                inner_parts.append(f"rm -f {shlex.quote(str(self._container_path_for_home_file(account, command.fresh_launch_marker)))}")
            if "CODEX_HOME" in env:
                inner_parts.append(self._fresh_codex_cleanup(env["CODEX_HOME"], quoted_resume_marker))
            inner_parts.append(
                f"if [ -f {quoted_resume_marker} ]; then {shlex.join(command.resume_argv)}; "
                f"_pm_status=$?; if [ $_pm_status -eq 0 ]; then exit 0; fi; fi"
            )
            if command.fresh_launch_marker is not None:
                inner_parts.append(f"touch {shlex.quote(str(self._container_path_for_home_file(account, command.fresh_launch_marker)))}")
            inner_parts.append(f"exec {shlex.join(command.argv)}")
        else:
            if command.fresh_launch_marker is not None:
                inner_parts.append(f"touch {shlex.quote(str(self._container_path_for_home_file(account, command.fresh_launch_marker)))}")
            inner_parts.append(f"exec {shlex.join(command.argv)}")
        inner = " && ".join(inner_parts)

        docker_parts = [
            "docker",
            "run",
            "--rm",
            "-it",
            "-v",
            f"{project.root_dir.resolve()}:{self.workspace_mount}",
            "-v",
            f"{account.home.resolve()}:{self.home_mount}",
            "-w",
            str(Path(self.workspace_mount) / relative_cwd),
        ]
        for key, value in env.items():
            docker_parts.extend(["-e", f"{key}={value}"])
        docker_parts.extend(account.docker_extra_args)
        docker_parts.append(account.docker_image)
        docker_parts.extend(["sh", "-lc", inner])
        return shlex.join(docker_parts)
