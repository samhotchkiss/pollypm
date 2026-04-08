from pathlib import Path

from promptmaster.config import write_config
from promptmaster.models import (
    AccountConfig,
    ProjectKind,
    ProjectSettings,
    PromptMasterConfig,
    PromptMasterSettings,
    ProviderKind,
    RuntimeKind,
    SessionConfig,
    KnownProject,
)
from promptmaster.storage.state import StateStore
from promptmaster.workers import auto_select_worker_account, suggest_worker_prompt


def _config(tmp_path: Path) -> tuple[PromptMasterConfig, Path]:
    config = PromptMasterConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".promptmaster",
            logs_dir=tmp_path / ".promptmaster/logs",
            snapshots_dir=tmp_path / ".promptmaster/snapshots",
            state_db=tmp_path / ".promptmaster/state.db",
        ),
        promptmaster=PromptMasterSettings(
            controller_account="claude_controller",
            failover_enabled=True,
            failover_accounts=["codex_backup"],
        ),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                runtime=RuntimeKind.LOCAL,
                home=tmp_path / ".promptmaster/homes/claude_controller",
            ),
            "codex_backup": AccountConfig(
                name="codex_backup",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                runtime=RuntimeKind.LOCAL,
                home=tmp_path / ".promptmaster/homes/codex_backup",
            ),
            "claude_worker": AccountConfig(
                name="claude_worker",
                provider=ProviderKind.CLAUDE,
                email="worker@example.com",
                runtime=RuntimeKind.LOCAL,
                home=tmp_path / ".promptmaster/homes/claude_worker",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="promptmaster",
                window_name="pm-heartbeat",
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="promptmaster",
                window_name="pm-operator",
            ),
        },
        projects={
            "promptmaster": KnownProject(
                key="promptmaster",
                path=tmp_path,
                name="PollyPM",
                kind=ProjectKind.FOLDER,
            )
        },
    )
    for account in config.accounts.values():
        if account.home is not None:
            account.home.mkdir(parents=True, exist_ok=True)
            if account.provider is ProviderKind.CLAUDE:
                (account.home / ".claude").mkdir(parents=True, exist_ok=True)
                (account.home / ".claude" / ".credentials.json").write_text("{}")
            else:
                (account.home / ".codex").mkdir(parents=True, exist_ok=True)
                (account.home / ".codex" / "auth.json").write_text("{}")
    config_path = tmp_path / "promptmaster.toml"
    write_config(config, config_path)
    return config, config_path


def test_auto_select_worker_avoids_effective_live_controller(tmp_path: Path, monkeypatch) -> None:
    config, config_path = _config(tmp_path)
    store = StateStore(config.project.state_db)
    store.upsert_session_runtime(
        session_name="operator",
        status="healthy",
        effective_account="codex_backup",
        effective_provider=ProviderKind.CODEX.value,
    )

    monkeypatch.setattr("promptmaster.workers._account_logged_in", lambda account: True)

    selected = auto_select_worker_account(config_path)

    assert selected == "claude_worker"


def test_auto_select_worker_skips_runtime_unhealthy_account(tmp_path: Path, monkeypatch) -> None:
    config, config_path = _config(tmp_path)
    store = StateStore(config.project.state_db)
    store.upsert_account_runtime(
        account_name="codex_backup",
        provider=ProviderKind.CODEX.value,
        status="auth-broken",
        reason="failed auth",
    )

    monkeypatch.setattr("promptmaster.workers._account_logged_in", lambda account: True)

    selected = auto_select_worker_account(config_path)

    assert selected == "claude_worker"


def test_auto_select_worker_uses_control_plane_account_before_controller_last_resort(
    tmp_path: Path, monkeypatch
) -> None:
    config, config_path = _config(tmp_path)
    store = StateStore(config.project.state_db)
    store.upsert_session_runtime(
        session_name="operator",
        status="healthy",
        effective_account="codex_backup",
        effective_provider=ProviderKind.CODEX.value,
    )
    del config.accounts["claude_worker"]
    write_config(config, config_path, force=True)

    monkeypatch.setattr("promptmaster.workers._account_logged_in", lambda account: True)

    selected = auto_select_worker_account(config_path)

    assert selected == "codex_backup"


def test_suggest_worker_prompt_uses_tracked_ready_issue(tmp_path: Path) -> None:
    config, config_path = _config(tmp_path)
    project = config.projects["promptmaster"]
    project.tracked = True
    write_config(config, config_path, force=True)

    issues_root = project.path / "issues"
    (issues_root / "01-ready").mkdir(parents=True, exist_ok=True)
    (issues_root / "01-ready" / "0017-build-launch-flow.md").write_text(
        "# 0017 Build Launch Flow\n\nTighten launch behavior.\n"
    )

    prompt = suggest_worker_prompt(config_path, project_key="promptmaster")

    assert "0017" in prompt
    assert "Build Launch Flow" in prompt
    assert "03-needs-review" in prompt


def test_suggest_worker_prompt_for_untracked_project_is_concrete(tmp_path: Path) -> None:
    _config_data, config_path = _config(tmp_path)

    prompt = suggest_worker_prompt(config_path, project_key="promptmaster")

    assert "single highest-leverage next slice" in prompt
    assert "concrete handoff for Polly" in prompt
