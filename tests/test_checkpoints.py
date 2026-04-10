from pathlib import Path

from pollypm.checkpoints import record_checkpoint, write_mechanical_checkpoint
from pollypm.models import (
    AccountConfig,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PollyPMConfig,
    PollyPMSettings,
    ProviderKind,
    SessionConfig,
    SessionLaunchSpec,
)
from pollypm.memory_backends import get_memory_backend
from pollypm.storage.state import StateStore


def test_mechanical_checkpoint_persists_files_and_state(tmp_path: Path) -> None:
    config = PollyPMConfig(
        project=ProjectSettings(root_dir=tmp_path, base_dir=tmp_path / ".pollypm-state", logs_dir=tmp_path / ".pollypm-state/logs", snapshots_dir=tmp_path / ".pollypm-state/snapshots", state_db=tmp_path / ".pollypm-state/state.db"),
        pollypm=PollyPMSettings(controller_account="codex_primary"),
        accounts={
            "codex_primary": AccountConfig(
                name="codex_primary",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                home=tmp_path / ".pollypm-state/homes/codex_primary",
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
        log_path=tmp_path / ".pollypm-state/logs/worker-demo.log",
        command="codex",
    )
    store = StateStore(config.project.state_db)
    artifact = write_mechanical_checkpoint(
        config,
        launch,
        snapshot_path=tmp_path / ".pollypm-state/snapshots/worker-demo.txt",
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
        snapshot_path=tmp_path / ".pollypm-state/snapshots/worker-demo.txt",
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


def test_record_checkpoint_preserves_existing_runtime_status(tmp_path: Path) -> None:
    config = PollyPMConfig(
        project=ProjectSettings(root_dir=tmp_path, base_dir=tmp_path / ".pollypm-state", logs_dir=tmp_path / ".pollypm-state/logs", snapshots_dir=tmp_path / ".pollypm-state/snapshots", state_db=tmp_path / ".pollypm-state/state.db"),
        pollypm=PollyPMSettings(controller_account="codex_primary"),
        accounts={
            "codex_primary": AccountConfig(
                name="codex_primary",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                home=tmp_path / ".pollypm-state/homes/codex_primary",
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
        log_path=tmp_path / ".pollypm-state/logs/worker-demo.log",
        command="codex",
    )
    store = StateStore(config.project.state_db)
    store.upsert_session_runtime(
        session_name="worker",
        status="needs_followup",
        last_failure_message="Last turn suggests additional work remains",
    )
    artifact = write_mechanical_checkpoint(
        config,
        launch,
        snapshot_path=tmp_path / ".pollypm-state/snapshots/worker-demo.txt",
        snapshot_content="Line 1\nLine 2\n",
        log_bytes=42,
        alerts=["needs_followup"],
    )

    record_checkpoint(
        store,
        launch,
        project_key="demo",
        level="level0",
        artifact=artifact,
        snapshot_path=tmp_path / ".pollypm-state/snapshots/worker-demo.txt",
        memory_backend_name="file",
    )

    runtime = store.get_session_runtime("worker")
    assert runtime is not None
    assert runtime.status == "needs_followup"
    assert runtime.last_checkpoint_path == str(artifact.summary_path)
