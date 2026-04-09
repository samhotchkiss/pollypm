from pathlib import Path

from pollypm.config import load_config, write_config
from pollypm.models import KnownProject, ProviderKind
from pollypm.projects import DEFAULT_WORKSPACE_ROOT
from pollypm.onboarding import ConnectedAccount, build_onboarded_config


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
    assert "true interactive CLI session" in config.sessions["operator"].prompt
    assert "kick off, resume, and oversee a work session" in config.sessions["operator"].prompt
    assert "supervision, not implementation" in config.sessions["heartbeat"].prompt
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
    assert config.sessions["heartbeat"].args == []
    assert config.sessions["operator"].args == []
