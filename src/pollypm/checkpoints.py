from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pollypm.models import PollyPMConfig, SessionLaunchSpec
from pollypm.memory_backends import get_memory_backend
from pollypm.projects import ensure_project_scaffold, project_checkpoints_dir
from pollypm.storage.state import StateStore


@dataclass(slots=True)
class CheckpointArtifact:
    json_path: Path
    summary_path: Path
    summary_text: str


def snapshot_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def write_mechanical_checkpoint(
    config: PollyPMConfig,
    launch: SessionLaunchSpec,
    *,
    snapshot_path: Path,
    snapshot_content: str,
    log_bytes: int,
    alerts: list[str],
) -> CheckpointArtifact:
    project_path = _project_root(config, launch.session.project)
    ensure_project_scaffold(project_path)
    checkpoint_root = project_checkpoints_dir(project_path) / launch.session.name
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = checkpoint_root / f"{stamp}.json"
    summary_path = checkpoint_root / f"{stamp}.md"
    tail_lines = [line.rstrip() for line in snapshot_content.splitlines()[-20:] if line.strip()]
    payload = {
        "session_name": launch.session.name,
        "project": launch.session.project,
        "role": launch.session.role,
        "provider": launch.session.provider.value,
        "account": launch.account.name,
        "window_name": launch.window_name,
        "cwd": str(launch.session.cwd),
        "snapshot_path": str(snapshot_path),
        "snapshot_hash": snapshot_hash(snapshot_content),
        "log_bytes": log_bytes,
        "alerts": alerts,
        "created_at": datetime.now(UTC).isoformat(),
        "transcript_tail": tail_lines,
    }
    json_path.write_text(json.dumps(payload, indent=2) + "\n")
    summary_text = "\n".join(
        [
            f"# Checkpoint `{launch.session.name}`",
            "",
            f"- Project: `{launch.session.project}`",
            f"- Role: `{launch.session.role}`",
            f"- Provider/Account: `{launch.session.provider.value}` / `{launch.account.email or launch.account.name}`",
            f"- Window: `{launch.window_name}`",
            f"- Snapshot: `{snapshot_path.name}`",
            f"- Alerts: {', '.join(alerts) if alerts else 'none'}",
            "",
            "## Transcript Tail",
            "",
            "```text",
            *tail_lines[-12:],
            "```",
            "",
        ]
    )
    summary_path.write_text(summary_text)
    return CheckpointArtifact(json_path=json_path, summary_path=summary_path, summary_text=summary_text)


def record_checkpoint(
    store: StateStore,
    launch: SessionLaunchSpec,
    *,
    project_key: str,
    level: str,
    artifact: CheckpointArtifact,
    snapshot_path: Path,
    memory_backend_name: str = "file",
) -> None:
    store.record_checkpoint(
        session_name=launch.session.name,
        project_key=project_key,
        level=level,
        json_path=str(artifact.json_path),
        summary_path=str(artifact.summary_path),
        snapshot_path=str(snapshot_path),
        summary_text=artifact.summary_text,
    )
    store.upsert_session_runtime(
        session_name=launch.session.name,
        status="healthy",
        last_checkpoint_path=str(artifact.summary_path),
    )
    try:
        memory_backend = get_memory_backend(launch.session.cwd, memory_backend_name)
        memory_backend.write_entry(
            scope=launch.session.project,
            title=f"Checkpoint {launch.session.name}",
            body=artifact.summary_text,
            kind="checkpoint",
            tags=[launch.session.name, launch.session.role, launch.session.provider.value],
            source="checkpoint",
        )
    except Exception:  # noqa: BLE001
        pass


def _project_root(config: PollyPMConfig, project_key: str) -> Path:
    if project_key == "pollypm":
        return config.project.root_dir
    project = config.projects.get(project_key)
    if project is None:
        return config.project.root_dir
    return project.path
