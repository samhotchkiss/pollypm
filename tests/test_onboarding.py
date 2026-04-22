from pathlib import Path

from typer.testing import CliRunner

from pollypm.cli import app as cli_app
import pytest
from click.exceptions import Exit as ClickExit
from pollypm.config import load_config, write_config
from pollypm.doctor import decode_setup_tag, setup_tag_line
from pollypm.models import KnownProject, ProviderKind
from pollypm.projects import DEFAULT_WORKSPACE_ROOT
from pollypm.onboarding import (
    ConnectedAccount,
    OnboardingResult,
    _scan_recent_projects,
    _seeded_demo_route,
    build_onboarded_config,
    demo_project_fallback_destination,
    provision_demo_project_fallback,
    seed_demo_project_task,
)


def test_build_onboarded_config_uses_controller_for_pollypm_sessions(tmp_path: Path) -> None:
    accounts = {
        "codex_1": ConnectedAccount(
            provider=ProviderKind.CODEX,
            email="codex@example.com",
            account_name="codex_1",
            home=tmp_path / ".pollypm" / "homes" / "codex_1",
        ),
        "claude_1": ConnectedAccount(
            provider=ProviderKind.CLAUDE,
            email="claude@example.com",
            account_name="claude_1",
            home=tmp_path / ".pollypm" / "homes" / "claude_1",
        ),
    }

    config = build_onboarded_config(
        root_dir=tmp_path,
        accounts=accounts,
        controller_account="claude_1",
        failover_enabled=True,
        failover_accounts=["codex_1"],
        projects={
            "wire": KnownProject(
                key="wire",
                path=tmp_path / "wire",
                name="Wire",
            )
        },
    )

    assert config.pollypm.controller_account == "claude_1"
    assert config.pollypm.open_permissions_by_default is True
    assert config.pollypm.failover_accounts == ["codex_1"]
    assert config.project.workspace_root == DEFAULT_WORKSPACE_ROOT
    assert config.sessions["heartbeat"].account == "claude_1"
    assert config.sessions["heartbeat"].provider is ProviderKind.CLAUDE
    assert config.sessions["heartbeat"].project == "pollypm"
    assert "<identity>" in config.sessions["operator"].prompt
    assert "delegate" in config.sessions["operator"].prompt.lower()
    assert "<identity>" in config.sessions["heartbeat"].prompt
    assert config.projects["wire"].name == "Wire"


def test_rendered_onboarding_config_round_trips(tmp_path: Path) -> None:
    accounts = {
        "codex_1": ConnectedAccount(
            provider=ProviderKind.CODEX,
            email="codex@example.com",
            account_name="codex_1",
            home=tmp_path / ".pollypm" / "homes" / "codex_1",
        ),
    }
    config = build_onboarded_config(
        root_dir=tmp_path,
        accounts=accounts,
        controller_account="codex_1",
        failover_enabled=False,
        failover_accounts=[],
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path)

    loaded = load_config(config_path)
    assert loaded.pollypm.controller_account == "codex_1"
    assert loaded.pollypm.open_permissions_by_default is True
    assert loaded.pollypm.failover_enabled is False
    assert loaded.accounts["codex_1"].email == "codex@example.com"
    assert set(loaded.sessions) == {"heartbeat", "operator"}


def test_build_onboarded_config_can_disable_open_permissions(tmp_path: Path) -> None:
    accounts = {
        "claude_1": ConnectedAccount(
            provider=ProviderKind.CLAUDE,
            email="claude@example.com",
            account_name="claude_1",
            home=tmp_path / ".pollypm" / "homes" / "claude_1",
        ),
    }
    config = build_onboarded_config(
        root_dir=tmp_path,
        accounts=accounts,
        controller_account="claude_1",
        open_permissions_by_default=False,
        failover_enabled=False,
        failover_accounts=[],
    )

    assert config.pollypm.open_permissions_by_default is False
    # Control roles always carry their role restrictions regardless of permissions.
    assert "--allowedTools" in config.sessions["heartbeat"].args
    assert "--disallowedTools" in config.sessions["heartbeat"].args
    assert "--allowedTools" in config.sessions["operator"].args
    assert "--disallowedTools" in config.sessions["operator"].args
    assert "--dangerously-skip-permissions" not in config.sessions["heartbeat"].args
    assert "--dangerously-skip-permissions" not in config.sessions["operator"].args


def test_setup_tag_round_trips(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("pollypm.doctor.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "pollypm.doctor._tool_version",
        lambda binary, timeout=2.0: {"claude": "2.1.0", "codex": "0.1.0"}.get(binary),
    )
    monkeypatch.setattr("pollypm.doctor._tool_major", lambda binary, timeout=2.0: {"tmux": 3, "git": 2, "node": 20}.get(binary))
    monkeypatch.setattr(
        "pollypm.doctor._setup_fingerprint",
        lambda config_path=None: {
            "platform": "darwin-arm64",
            "pollypm_version": "1.2.3",
            "claude_version": "2.1.0",
            "claude_home_mode": "default-profile",
            "codex_version": "0.1.0",
            "codex_home_mode": "isolated",
            "tmux_major": 3,
            "git_major": 2,
            "node_major": 20,
            "accounts": 2,
            "projects": 4,
        },
    )

    line = setup_tag_line(tmp_path / "pollypm.toml")
    tag = line.split()[2]

    assert decode_setup_tag(tag)["accounts"] == 2


def test_decode_setup_tag_cli_round_trips(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("pollypm.doctor.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "pollypm.doctor._setup_fingerprint",
        lambda config_path=None: {
            "platform": "darwin-arm64",
            "pollypm_version": "1.2.3",
            "claude_version": "2.1.0",
            "claude_home_mode": "default-profile",
            "codex_version": "0.1.0",
            "codex_home_mode": "isolated",
            "tmux_major": 3,
            "git_major": 2,
            "node_major": 20,
            "accounts": 2,
            "projects": 4,
        },
    )

    line = setup_tag_line(tmp_path / "pollypm.toml")
    tag = line.split()[2]

    result = CliRunner().invoke(cli_app, ["debug", "decode-setup-tag", tag])

    assert result.exit_code == 0
    assert '"accounts": 2' in result.output


def test_provision_demo_project_fallback_creates_self_contained_repo(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    config_path = tmp_path / "pollypm.toml"
    git_calls: list[list[str]] = []

    def _fake_run(command: list[str], **_kwargs):
        git_calls.append(command)
        (workspace_root / "pollypm-demo" / ".git").mkdir(parents=True, exist_ok=True)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("pollypm.onboarding.DEFAULT_WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr("pollypm.onboarding.shutil.which", lambda name: "/usr/bin/git" if name == "git" else None)
    monkeypatch.setattr("pollypm.onboarding.subprocess.run", _fake_run)

    target = provision_demo_project_fallback(config_path)

    assert target == demo_project_fallback_destination(config_path)
    assert (target / ".pollypm-demo-fallback").exists()
    assert (target / "README.md").exists()
    assert (target / "demo_app.py").exists()
    assert (target / "TASK.md").exists()
    assert (target / "tests" / "test_demo_app.py").exists()
    assert (target / ".pollypm").exists()
    assert git_calls == [["git", "init", "-q", str(target)]]

    same_target = provision_demo_project_fallback(config_path)
    assert same_target == target
    assert git_calls == [["git", "init", "-q", str(target)]]


def test_seed_demo_project_task_creates_a_visible_queue_item(tmp_path: Path) -> None:
    project_path = tmp_path / "pollypm-demo"
    project_path.mkdir()

    task_id = seed_demo_project_task(project_path, project_key="pollypm_demo")

    from pollypm.work.sqlite_service import SQLiteWorkService

    with SQLiteWorkService(db_path=project_path / ".pollypm" / "state.db", project_path=project_path) as svc:
        task = svc.get(task_id)
        assert task.project == "pollypm_demo"
        assert task.work_status.value == "queued"
        assert task.title == "Fix the demo queue estimate bug"


def test_scan_recent_projects_offers_demo_repo_when_discovery_is_empty(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "pollypm.toml"
    demo_path = tmp_path / "workspace" / "pollypm-demo"
    messages: list[str] = []

    monkeypatch.setattr(
        "pollypm.onboarding.discover_recent_project_candidates",
        lambda _config_path: [],
    )
    monkeypatch.setattr(
        "pollypm.onboarding.demo_project_fallback_destination",
        lambda _config_path: demo_path,
    )
    monkeypatch.setattr(
        "pollypm.onboarding.provision_demo_project_fallback",
        lambda _config_path: demo_path,
    )
    monkeypatch.setattr(
        "pollypm.onboarding.add_selected_projects",
        lambda _config_path, selected_paths: [
            KnownProject(key="pollypm_demo", path=selected_paths[0], name="PollyPM Demo")
        ],
    )
    monkeypatch.setattr("pollypm.onboarding.typer.confirm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("pollypm.onboarding.typer.echo", lambda message="": messages.append(message))

    projects = _scan_recent_projects(config_path)

    assert [project.path for project in projects] == [demo_path]
    assert any("demo repo" in message.lower() for message in messages)


def test_onboarding_result_keeps_path_compatibility(tmp_path: Path) -> None:
    result = OnboardingResult(
        config_path=tmp_path / "pollypm.toml",
        launch_requested=True,
        seeded_demo_project_key="pollypm_demo",
        seeded_demo_task_id="demo/1",
    )

    assert result.parent == tmp_path
    assert result.resolve() == (tmp_path / "pollypm.toml").resolve()
    assert str(result) == str(tmp_path / "pollypm.toml")


def test_run_onboarding_launches_seeded_demo_experience(monkeypatch, tmp_path: Path) -> None:
    result = OnboardingResult(
        config_path=tmp_path / "pollypm.toml",
        launch_requested=True,
        seeded_demo_project_key="pollypm_demo",
        seeded_demo_task_id="demo/1",
    )
    launched: list[OnboardingResult] = []

    monkeypatch.setattr(
        "pollypm.onboarding_tui.run_onboarding_app",
        lambda config_path, force=False: result,
    )
    monkeypatch.setattr(
        "pollypm.onboarding._launch_onboarding_experience",
        lambda launch_result: launched.append(launch_result) or True,
    )

    with pytest.raises(ClickExit):
        __import__("pollypm.onboarding", fromlist=["run_onboarding"]).run_onboarding(
            config_path=result.config_path,
            force=True,
        )

    assert launched == [result]


def test_seeded_demo_route_parses_project_and_task_number(tmp_path: Path) -> None:
    result = OnboardingResult(
        config_path=tmp_path / "pollypm.toml",
        launch_requested=True,
        seeded_demo_project_key="pollypm_demo",
        seeded_demo_task_id="demo/17",
    )

    assert _seeded_demo_route(result) == "project:pollypm_demo:task:17"
