from pathlib import Path

from pollypm.models import AccountConfig, ProjectSettings, ProviderKind, RuntimeKind, SessionConfig
from pollypm.providers.claude import ClaudeAdapter
from pollypm.providers.codex import CodexAdapter
from pollypm.runtimes.docker import DockerRuntimeAdapter
from pollypm.runtimes.local import LocalRuntimeAdapter
from pollypm.supervision.probe_runner import ProbeRunner


def test_local_runtime_wraps_home_and_command(tmp_path: Path) -> None:
    session = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=tmp_path,
        project="demo-project",
        prompt="hello",
    )
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        email="codex@example.com",
        runtime=RuntimeKind.LOCAL,
        home=tmp_path / "home",
    )

    command = CodexAdapter().build_launch_command(session, account)
    wrapped = LocalRuntimeAdapter().wrap_command(
        command,
        account,
        ProjectSettings(root_dir=tmp_path),
    )

    assert "CODEX_HOME" not in wrapped
    assert "runtime_launcher" in wrapped
    assert "pollypm.runtime_launcher" in wrapped


def test_local_runtime_payload_carries_round_start_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    round_start = "2026-05-04T15:14:55+00:00"
    monkeypatch.setenv("ROUND_START_ISO_TS", round_start)
    session = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=tmp_path,
        project="demo-project",
        prompt="hello",
    )
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        email="codex@example.com",
        runtime=RuntimeKind.LOCAL,
        home=tmp_path / "home",
    )

    command = CodexAdapter().build_launch_command(session, account)
    wrapped = LocalRuntimeAdapter().wrap_command(
        command,
        account,
        ProjectSettings(root_dir=tmp_path),
    )

    payload = _decoded_payload(wrapped)
    assert payload["env"]["ROUND_START_ISO_TS"] == round_start


def test_docker_runtime_mounts_workspace_and_home(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    cwd = project_root / "subdir"
    home = tmp_path / "home"
    cwd.mkdir(parents=True)
    home.mkdir(parents=True)

    session = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=cwd,
        project="demo-project",
        prompt="hello",
    )
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        email="codex@example.com",
        runtime=RuntimeKind.DOCKER,
        home=home,
        docker_image="ghcr.io/example/pollypm-agent:latest",
    )
    project = ProjectSettings(root_dir=project_root)

    command = CodexAdapter().build_launch_command(session, account)
    wrapped = DockerRuntimeAdapter().wrap_command(command, account, project)

    assert "docker run" in wrapped
    assert "ghcr.io/example/pollypm-agent:latest" in wrapped
    assert f"{project_root.resolve()}:/workspace" in wrapped
    assert f"{home.resolve()}:/home/pollypm" in wrapped
    assert "CODEX_HOME=/home/pollypm/.codex" in wrapped


def test_docker_runtime_exports_round_start_env(tmp_path: Path, monkeypatch) -> None:
    round_start = "2026-05-04T15:14:55+00:00"
    monkeypatch.setenv("ROUND_START_ISO_TS", round_start)
    project_root = tmp_path / "repo"
    cwd = project_root / "subdir"
    home = tmp_path / "home"
    cwd.mkdir(parents=True)
    home.mkdir(parents=True)

    session = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=cwd,
        project="demo-project",
        prompt="hello",
    )
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        email="codex@example.com",
        runtime=RuntimeKind.DOCKER,
        home=home,
        docker_image="ghcr.io/example/pollypm-agent:latest",
    )

    command = CodexAdapter().build_launch_command(session, account)
    wrapped = DockerRuntimeAdapter().wrap_command(
        command,
        account,
        ProjectSettings(root_dir=project_root),
    )

    assert f"ROUND_START_ISO_TS={round_start}" in wrapped


def test_claude_runtime_sets_provider_native_config_dir(tmp_path: Path) -> None:
    session = SessionConfig(
        name="operator",
        role="operator-pm",
        provider=ProviderKind.CLAUDE,
        account="claude_primary",
        cwd=tmp_path,
        project="pollypm",
        prompt="watch the project",
    )
    account = AccountConfig(
        name="claude_primary",
        provider=ProviderKind.CLAUDE,
        email="claude@example.com",
        runtime=RuntimeKind.LOCAL,
        home=tmp_path / "home",
    )

    command = ClaudeAdapter().build_launch_command(session, account)
    wrapped = LocalRuntimeAdapter().wrap_command(
        command,
        account,
        ProjectSettings(root_dir=tmp_path),
    )

    assert "CLAUDE_CONFIG_DIR" not in wrapped
    assert "pollypm.runtime_launcher" in wrapped


def test_control_sessions_wrap_with_resume_fallback(tmp_path: Path) -> None:
    session = SessionConfig(
        name="operator",
        role="operator-pm",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=tmp_path,
        project="pollypm",
        prompt="watch the project",
    )
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        email="codex@example.com",
        runtime=RuntimeKind.LOCAL,
        home=tmp_path / "home",
    )

    command = CodexAdapter().build_launch_command(session, account)
    wrapped = LocalRuntimeAdapter().wrap_command(command, account, ProjectSettings(root_dir=tmp_path))

    assert "pollypm.runtime_launcher" in wrapped
    assert "session-markers/operator.resume" not in wrapped
    assert "history.jsonl" not in wrapped


def test_local_runtime_wrap_command_exec_returns_structured_argv(tmp_path: Path) -> None:
    session = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=tmp_path,
        project="demo-project",
        prompt="hello",
    )
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        email="codex@example.com",
        runtime=RuntimeKind.LOCAL,
        home=tmp_path / "home",
    )
    command = CodexAdapter().build_launch_command(session, account)

    wrapped = LocalRuntimeAdapter().wrap_command_exec(
        command,
        account,
        ProjectSettings(root_dir=tmp_path),
    )

    assert wrapped.argv[1:3] == ["-m", "pollypm.runtime_launcher"]
    assert wrapped.env is not None
    assert wrapped.env.get("CODEX_HOME", "").endswith("/home/.codex")
    assert wrapped.cwd == tmp_path


def test_docker_runtime_wrap_command_exec_is_non_tty(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    cwd = project_root / "subdir"
    home = tmp_path / "home"
    cwd.mkdir(parents=True)
    home.mkdir(parents=True)

    session = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=cwd,
        project="demo-project",
        prompt="hello",
    )
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        email="codex@example.com",
        runtime=RuntimeKind.DOCKER,
        home=home,
        docker_image="ghcr.io/example/pollypm-agent:latest",
    )
    command = CodexAdapter().build_launch_command(session, account)

    wrapped = DockerRuntimeAdapter().wrap_command_exec(
        command,
        account,
        ProjectSettings(root_dir=project_root),
    )

    assert wrapped.argv[:4] == ["docker", "run", "--rm", "-i"]
    assert "-it" not in wrapped.argv


def test_probe_runner_uses_shell_false(monkeypatch, tmp_path: Path) -> None:
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        email="codex@example.com",
        runtime=RuntimeKind.LOCAL,
        home=tmp_path / "home",
    )
    calls: dict[str, object] = {}

    class _StubRuntime:
        def wrap_command_exec(self, command, account, project):
            calls["wrapped"] = {
                "argv": command.argv,
                "account": account.name,
                "project": str(project.root_dir),
            }
            from pollypm.runtimes.base import WrappedRuntimeCommand

            return WrappedRuntimeCommand(
                argv=["echo", "ok"],
                env={"TEST_ENV": "1"},
                cwd=project.root_dir,
            )

    def _fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs

        class _Result:
            stdout = "ok"
            stderr = ""

        return _Result()

    monkeypatch.setattr(
        "pollypm.supervision.probe_runner.get_runtime",
        lambda runtime, root_dir=None: _StubRuntime(),
    )
    monkeypatch.setattr(
        "pollypm.supervision.probe_runner.subprocess.run",
        _fake_run,
    )

    output = ProbeRunner(ProjectSettings(root_dir=tmp_path)).run_probe(account)

    assert output == "ok"
    assert calls["argv"] == ["echo", "ok"]
    kwargs = calls["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == tmp_path
    assert kwargs["env"] == {"TEST_ENV": "1"}


# ---------------------------------------------------------------------------
# #965 — agent binary resolution before runtime_launcher serialization
# ---------------------------------------------------------------------------


def _decoded_payload(wrapped_cmd: str) -> dict:
    """Pull the base64 payload out of the ``sh -lc`` wrap_command output."""
    import base64
    import json
    import shlex

    parts = shlex.split(wrapped_cmd)
    inner = parts[-1]
    inner_parts = shlex.split(inner)
    b64 = inner_parts[-1]
    padded = b64 + "=" * (-len(b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))


def test_resolve_argv_binary_finds_codex_in_npm_global(tmp_path: Path, monkeypatch) -> None:
    """#965 — ``resolve_argv_binary`` resolves a bare binary name to an
    absolute path using the enriched PATH, even when the binary lives
    in a non-default location like ``~/.npm-global/bin``.
    """
    from pollypm.runtimes import local as _local_module

    fake_npm_global = tmp_path / "npm-global" / "bin"
    fake_npm_global.mkdir(parents=True)
    fake_codex = fake_npm_global / "codex"
    fake_codex.write_text("#!/bin/sh\n")
    fake_codex.chmod(0o755)

    # Reset and seed the cached enriched PATH so the test is hermetic.
    monkeypatch.setattr(_local_module, "_CACHED_RESOLVE_PATH", str(fake_npm_global))

    resolved = _local_module.resolve_argv_binary(["codex", "--flag"])
    assert resolved == [str(fake_codex), "--flag"]


def test_resolve_argv_binary_leaves_absolute_argv_unchanged(tmp_path: Path) -> None:
    """When ``argv[0]`` is already absolute, no PATH search happens."""
    from pollypm.runtimes import local as _local_module

    abs_path = tmp_path / "already-absolute"
    resolved = _local_module.resolve_argv_binary([str(abs_path), "--x"])
    assert resolved == [str(abs_path), "--x"]


def test_resolve_argv_binary_returns_unchanged_when_missing(tmp_path: Path, monkeypatch) -> None:
    """Missing binary → return argv unchanged so the launcher can surface
    a clear, named error rather than swallowing the failure here.
    """
    from pollypm.runtimes import local as _local_module

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setattr(_local_module, "_CACHED_RESOLVE_PATH", str(empty_dir))
    resolved = _local_module.resolve_argv_binary(["nonexistent-agent-bin", "--x"])
    assert resolved == ["nonexistent-agent-bin", "--x"]


def test_local_runtime_payload_uses_absolute_argv_for_codex(tmp_path: Path, monkeypatch) -> None:
    """#965 — the launcher payload built by ``LocalRuntimeAdapter`` carries
    an absolute path for the codex binary, so ``runtime_launcher.os.execvpe``
    never has to search a sanitized child-process PATH.
    """
    from pollypm.runtimes import local as _local_module

    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()
    fake_codex = fake_bin_dir / "codex"
    fake_codex.write_text("#!/bin/sh\n")
    fake_codex.chmod(0o755)
    monkeypatch.setattr(_local_module, "_CACHED_RESOLVE_PATH", str(fake_bin_dir))

    session = SessionConfig(
        name="worker",
        role="worker",
        provider=ProviderKind.CODEX,
        account="codex_primary",
        cwd=tmp_path,
        project="demo-project",
    )
    account = AccountConfig(
        name="codex_primary",
        provider=ProviderKind.CODEX,
        email="codex@example.com",
        runtime=RuntimeKind.LOCAL,
        home=tmp_path / "home",
    )
    command = CodexAdapter().build_launch_command(session, account)
    wrapped = LocalRuntimeAdapter().wrap_command(
        command,
        account,
        ProjectSettings(root_dir=tmp_path),
    )

    payload = _decoded_payload(wrapped)
    assert payload["argv"][0] == str(fake_codex), (
        f"expected absolute path to codex binary, got {payload['argv'][0]!r}"
    )
    assert Path(payload["argv"][0]).is_absolute()


def test_local_runtime_payload_uses_absolute_argv_for_claude(tmp_path: Path, monkeypatch) -> None:
    """#965 sibling-coverage — the same resolution applies to Claude
    launches so a future tmux PATH-stripping change cannot break Claude
    while leaving Codex working.
    """
    from pollypm.runtimes import local as _local_module

    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()
    fake_claude = fake_bin_dir / "claude"
    fake_claude.write_text("#!/bin/sh\n")
    fake_claude.chmod(0o755)
    monkeypatch.setattr(_local_module, "_CACHED_RESOLVE_PATH", str(fake_bin_dir))

    session = SessionConfig(
        name="operator",
        role="operator-pm",
        provider=ProviderKind.CLAUDE,
        account="claude_primary",
        cwd=tmp_path,
        project="pollypm",
    )
    account = AccountConfig(
        name="claude_primary",
        provider=ProviderKind.CLAUDE,
        email="claude@example.com",
        runtime=RuntimeKind.LOCAL,
        home=tmp_path / "home",
    )
    command = ClaudeAdapter().build_launch_command(session, account)
    wrapped = LocalRuntimeAdapter().wrap_command(
        command,
        account,
        ProjectSettings(root_dir=tmp_path),
    )

    payload = _decoded_payload(wrapped)
    assert payload["argv"][0] == str(fake_claude)
    assert Path(payload["argv"][0]).is_absolute()
