from pathlib import Path

from promptmaster.checkpoints import record_checkpoint, write_mechanical_checkpoint
from promptmaster.models import (
    AccountConfig,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PromptMasterConfig,
    PromptMasterSettings,
    ProviderKind,
    SessionConfig,
    SessionLaunchSpec,
)
from promptmaster.memory_backends import get_memory_backend
from promptmaster.storage.state import StateStore


def test_mechanical_checkpoint_persists_files_and_state(tmp_path: Path) -> None:
    config = PromptMasterConfig(
        project=ProjectSettings(root_dir=tmp_path, base_dir=tmp_path / ".promptmaster", logs_dir=tmp_path / ".promptmaster/logs", snapshots_dir=tmp_path / ".promptmaster/snapshots", state_db=tmp_path / ".promptmaster/state.db"),
        promptmaster=PromptMasterSettings(controller_account="codex_primary"),
        accounts={
            "codex_primary": AccountConfig(
                name="codex_primary",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                home=tmp_path / ".promptmaster/homes/codex_primary",
            )
        },
        sessions={
            "worker": SessionConfig(
                name="worker",
                role="worker",
                provider=ProviderKind.CODEX,
                account="codex_primary",
                cwd=tmp_path,
                project="demo",
                window_name="worker-demo",
            )
        },
        projects={
            "demo": KnownProject(
                key="demo",
                path=tmp_path / "demo",
                name="Demo",
                kind=ProjectKind.FOLDER,
            )
        },
    )
    (tmp_path / "demo").mkdir()
    launch = SessionLaunchSpec(
        session=config.sessions["worker"],
        account=config.accounts["codex_primary"],
        window_name="worker-demo",
        log_path=tmp_path / ".promptmaster/logs/worker-demo.log",
        command="codex",
    )
    store = StateStore(config.project.state_db)
    artifact = write_mechanical_checkpoint(
        config,
        launch,
        snapshot_path=tmp_path / ".promptmaster/snapshots/worker-demo.txt",
        snapshot_content="Line 1\nLine 2\n",
        log_bytes=42,
        alerts=["idle_output"],
    )
    assert artifact.json_path.exists()
    assert artifact.summary_path.exists()

    record_checkpoint(
        store,
        launch,
        project_key="demo",
        level="level0",
        artifact=artifact,
        snapshot_path=tmp_path / ".promptmaster/snapshots/worker-demo.txt",
        memory_backend_name="file",
    )
    latest = store.latest_checkpoint("worker")
    assert latest is not None
    assert latest.level == "level0"
    assert latest.project_key == "demo"

    memory_backend = get_memory_backend(tmp_path, "file")
    entries = memory_backend.list_entries(scope="demo", kind="checkpoint")
    assert len(entries) == 1
    assert entries[0].title == "Checkpoint worker"
