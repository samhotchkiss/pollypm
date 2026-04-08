from datetime import UTC, datetime, timedelta
from pathlib import Path

from promptmaster.models import (
    AccountConfig,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PromptMasterConfig,
    PromptMasterSettings,
    ProviderKind,
    SessionConfig,
)
from promptmaster.schedulers import get_scheduler_backend
from promptmaster.supervisor import Supervisor


def _config(tmp_path: Path) -> PromptMasterConfig:
    return PromptMasterConfig(
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
                home=tmp_path / ".promptmaster/homes/claude_controller",
            ),
            "codex_backup": AccountConfig(
                name="codex_backup",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                home=tmp_path / ".promptmaster/homes/codex_backup",
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


def test_inline_scheduler_round_trip(tmp_path: Path) -> None:
    supervisor = Supervisor(_config(tmp_path))
    supervisor.ensure_layout()
    backend = get_scheduler_backend("inline", root_dir=tmp_path)

    job = backend.schedule(
        supervisor,
        kind="send_input",
        run_at=datetime.now(UTC) + timedelta(hours=1),
        payload={"session_name": "operator", "text": "hello"},
    )
    jobs = backend.list_jobs(supervisor)

    assert len(jobs) == 1
    assert jobs[0].job_id == job.job_id
    assert jobs[0].kind == "send_input"


def test_inline_scheduler_runs_due_jobs(monkeypatch, tmp_path: Path) -> None:
    supervisor = Supervisor(_config(tmp_path))
    supervisor.ensure_layout()
    sent: dict[str, str] = {}
    monkeypatch.setattr(
        supervisor,
        "send_input",
        lambda session_name, text, owner="pm-bot": sent.update(
            {"session": session_name, "text": text, "owner": owner}
        ),
    )
    backend = get_scheduler_backend("inline", root_dir=tmp_path)
    backend.schedule(
        supervisor,
        kind="send_input",
        run_at=datetime.now(UTC) - timedelta(seconds=1),
        payload={"session_name": "operator", "text": "hello", "owner": "human"},
    )

    ran = backend.run_due(supervisor)

    assert len(ran) == 1
    assert sent == {"session": "operator", "text": "hello", "owner": "human"}
