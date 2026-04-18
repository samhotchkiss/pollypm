from pathlib import Path

from pollypm.config import write_config
from pollypm.models import (
    AccountConfig,
    ProjectKind,
    ProjectSettings,
    PollyPMConfig,
    PollyPMSettings,
    ProviderKind,
    RuntimeKind,
    SessionConfig,
    KnownProject,
)
from pollypm.storage.state import StateStore
from pollypm.plugins_builtin.core_agent_profiles.profiles import polly_prompt as operator_prompt
from pollypm.workers import auto_select_worker_account, suggest_worker_prompt
from pollypm.plugins_builtin.core_agent_profiles.profiles import worker_prompt


def _config(tmp_path: Path) -> tuple[PollyPMConfig, Path]:
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(
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
                home=tmp_path / ".pollypm/homes/claude_controller",
            ),
            "codex_backup": AccountConfig(
                name="codex_backup",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                runtime=RuntimeKind.LOCAL,
                home=tmp_path / ".pollypm/homes/codex_backup",
            ),
            "claude_worker": AccountConfig(
                name="claude_worker",
                provider=ProviderKind.CLAUDE,
                email="worker@example.com",
                runtime=RuntimeKind.LOCAL,
                home=tmp_path / ".pollypm/homes/claude_worker",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-heartbeat",
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-operator",
            ),
        },
        projects={
            "pollypm": KnownProject(
                key="pollypm",
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
    config_path = tmp_path / "pollypm.toml"
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

    monkeypatch.setattr("pollypm.workers._account_logged_in", lambda account: True)

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

    monkeypatch.setattr("pollypm.workers._account_logged_in", lambda account: True)

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

    monkeypatch.setattr("pollypm.workers._account_logged_in", lambda account: True)

    selected = auto_select_worker_account(config_path)

    assert selected == "codex_backup"


def test_suggest_worker_prompt_returns_empty(tmp_path: Path) -> None:
    _config_data, config_path = _config(tmp_path)

    prompt = suggest_worker_prompt(config_path, project_key="pollypm")

    assert prompt == ""


def test_worker_prompt_requires_core_identity() -> None:
    prompt = worker_prompt()

    assert "<identity>" in prompt
    assert "worker" in prompt.lower()
    assert "<principles>" in prompt


def test_operator_prompt_requires_delegation_instructions() -> None:
    prompt = operator_prompt()

    assert "<identity>" in prompt
    assert "delegate" in prompt.lower()
    assert "pm" in prompt  # references pm commands
    assert "<principles>" in prompt
