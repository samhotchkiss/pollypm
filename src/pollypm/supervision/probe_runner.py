"""Provider/runtime-neutral controller probe execution.

Contract:
- Inputs: an :class:`AccountConfig` and the project settings that own the
  runtime environment.
- Outputs: the combined stdout/stderr text from the provider-native probe.
- Side effects: launches one short-lived subprocess via the selected
  runtime adapter.
- Invariants: probe execution always uses structured argv with
  ``shell=False``; provider selection stays limited to Claude and Codex.
- Allowed dependencies: runtime adapters, provider launch models, and the
  shared error helpers. No tmux or Supervisor state.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from pollypm.models import AccountConfig, ProjectSettings, ProviderKind
from pollypm.providers.base import LaunchCommand
from pollypm.runtimes import get_runtime


def build_controller_probe(account: AccountConfig, project: ProjectSettings) -> LaunchCommand:
    """Build the provider-native probe command for ``account``."""
    if account.provider is ProviderKind.CLAUDE:
        return LaunchCommand(
            argv=["claude", "-p", "Reply with ok"],
            env=dict(account.env),
            cwd=project.root_dir,
        )
    if account.provider is ProviderKind.CODEX:
        return LaunchCommand(
            argv=["codex", "exec", "--skip-git-repo-check", "Reply with ok and nothing else"],
            env=dict(account.env),
            cwd=project.root_dir,
        )
    raise RuntimeError(f"Unsupported controller provider: {account.provider.value}")


@dataclass(slots=True)
class ProbeRunner:
    """Execute provider probes through the configured runtime adapter."""

    project: ProjectSettings

    def run_probe(self, account: AccountConfig) -> str:
        probe = build_controller_probe(account, self.project)
        runtime = get_runtime(account.runtime, root_dir=self.project.root_dir)
        wrapped = runtime.wrap_command_exec(probe, account, self.project)
        result = subprocess.run(
            wrapped.argv,
            check=False,
            shell=False,
            text=True,
            capture_output=True,
            timeout=90,
            cwd=wrapped.cwd,
            env=wrapped.env,
        )
        return "\n".join(part for part in [result.stdout, result.stderr] if part)
