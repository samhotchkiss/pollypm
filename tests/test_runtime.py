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
