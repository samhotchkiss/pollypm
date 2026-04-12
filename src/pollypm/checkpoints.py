from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pollypm.models import PollyPMConfig, SessionLaunchSpec
from pollypm.memory_backends import get_memory_backend
from pollypm.projects import ensure_project_scaffold, ensure_session_lock, project_checkpoints_dir, session_scoped_dir
from pollypm.storage.state import StateStore


HAIKU_MODEL = "claude-3-5-haiku-latest"
TRANSCRIPT_CAP_CHARS = 16000  # ~4000 tokens

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CheckpointArtifact:
    json_path: Path
    summary_path: Path
    summary_text: str


@dataclass(slots=True)
class CheckpointData:
    """Unified checkpoint data structure for all levels."""

    # Metadata
    checkpoint_id: str = ""
    session_name: str = ""
    project: str = ""
    role: str = ""
    level: int = 0
    trigger: str = ""
    created_at: str = ""
    parent_checkpoint_id: str = ""
    is_canonical: bool = True

    # Level 0 fields
    transcript_tail: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    git_branch: str = ""
    git_status: str = ""
    git_diff_stat: str = ""
    commands_observed: list[str] = field(default_factory=list)
    test_results: dict[str, int] = field(default_factory=dict)
    worktree_path: str = ""
    provider: str = ""
    account: str = ""
    lease_holder: str = ""
    snapshot_hash: str = ""

    # Level 1 fields
    objective: str = ""
    sub_step: str = ""
    work_completed: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    recommended_next_step: str = ""
    confidence: str = ""

    # Level 2 fields
    progress_pct: int = 0
    approach_assessment: str = ""
    drift_analysis: str = ""
    risk_factors: list[str] = field(default_factory=list)
    alternative_approaches: list[str] = field(default_factory=list)
    cross_session_context: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "checkpoint_id": self.checkpoint_id,
            "session_name": self.session_name,
            "project": self.project,
            "role": self.role,
            "level": self.level,
            "trigger": self.trigger,
            "created_at": self.created_at,
            "parent_checkpoint_id": self.parent_checkpoint_id,
            "is_canonical": self.is_canonical,
            "transcript_tail": self.transcript_tail,
            "files_changed": self.files_changed,
            "git_branch": self.git_branch,
            "git_status": self.git_status,
            "git_diff_stat": self.git_diff_stat,
            "commands_observed": self.commands_observed,
            "test_results": self.test_results,
            "worktree_path": self.worktree_path,
            "provider": self.provider,
            "account": self.account,
            "lease_holder": self.lease_holder,
            "snapshot_hash": self.snapshot_hash,
            "objective": self.objective,
            "sub_step": self.sub_step,
            "work_completed": self.work_completed,
            "blockers": self.blockers,
            "unresolved_questions": self.unresolved_questions,
            "recommended_next_step": self.recommended_next_step,
            "confidence": self.confidence,
            "progress_pct": self.progress_pct,
            "approach_assessment": self.approach_assessment,
            "drift_analysis": self.drift_analysis,
            "risk_factors": self.risk_factors,
            "alternative_approaches": self.alternative_approaches,
            "cross_session_context": self.cross_session_context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CheckpointData":
        """Deserialize from dictionary."""
        return cls(
            checkpoint_id=str(data.get("checkpoint_id", "")),
            session_name=str(data.get("session_name", "")),
            project=str(data.get("project", "")),
            role=str(data.get("role", "")),
            level=int(data.get("level", 0)),
            trigger=str(data.get("trigger", "")),
            created_at=str(data.get("created_at", "")),
            parent_checkpoint_id=str(data.get("parent_checkpoint_id", "")),
            is_canonical=bool(data.get("is_canonical", True)),
            transcript_tail=list(data.get("transcript_tail", [])),
            files_changed=list(data.get("files_changed", [])),
            git_branch=str(data.get("git_branch", "")),
            git_status=str(data.get("git_status", "")),
            git_diff_stat=str(data.get("git_diff_stat", "")),
            commands_observed=list(data.get("commands_observed", [])),
            test_results=dict(data.get("test_results", {})),
            worktree_path=str(data.get("worktree_path", "")),
            provider=str(data.get("provider", "")),
            account=str(data.get("account", "")),
            lease_holder=str(data.get("lease_holder", "")),
            snapshot_hash=str(data.get("snapshot_hash", "")),
            objective=str(data.get("objective", "")),
            sub_step=str(data.get("sub_step", "")),
            work_completed=list(data.get("work_completed", [])),
            blockers=list(data.get("blockers", [])),
            unresolved_questions=list(data.get("unresolved_questions", [])),
            recommended_next_step=str(data.get("recommended_next_step", "")),
            confidence=str(data.get("confidence", "")),
            progress_pct=int(data.get("progress_pct", 0)),
            approach_assessment=str(data.get("approach_assessment", "")),
            drift_analysis=str(data.get("drift_analysis", "")),
            risk_factors=list(data.get("risk_factors", [])),
            alternative_approaches=list(data.get("alternative_approaches", [])),
            cross_session_context=str(data.get("cross_session_context", "")),
        )


def snapshot_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Checkpoint root helpers
# ---------------------------------------------------------------------------


def _checkpoint_root(config: PollyPMConfig, session_name: str, project_key: str) -> Path:
    project_path = _project_root(config, project_key)
    ensure_project_scaffold(project_path)
    root = session_scoped_dir(project_checkpoints_dir(project_path), session_name)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_checkpoint_files(
    checkpoint_root: Path,
    data: CheckpointData,
) -> CheckpointArtifact:
    """Write checkpoint JSON and summary markdown to disk."""
    json_path = checkpoint_root / f"{data.checkpoint_id}.json"
    summary_path = checkpoint_root / f"{data.checkpoint_id}.md"

    json_path.write_text(json.dumps(data.to_dict(), indent=2) + "\n")

    summary_text = _render_checkpoint_summary(data)
    summary_path.write_text(summary_text)

    # Update latest.json as canonical recovery point
    if data.is_canonical:
        latest_path = checkpoint_root / "latest.json"
        latest_path.write_text(json.dumps(data.to_dict(), indent=2) + "\n")

    return CheckpointArtifact(
        json_path=json_path,
        summary_path=summary_path,
        summary_text=summary_text,
    )


def _render_checkpoint_summary(data: CheckpointData) -> str:
    """Render human-readable checkpoint summary."""
    lines = [
        f"# Checkpoint `{data.session_name}` (Level {data.level})",
        "",
        f"- Project: `{data.project}`",
        f"- Role: `{data.role}`",
        f"- Provider/Account: `{data.provider}` / `{data.account}`",
        f"- Trigger: `{data.trigger}`",
        f"- Created: `{data.created_at}`",
        "",
    ]

    if data.git_branch:
        lines.append(f"- Branch: `{data.git_branch}`")
    if data.git_status:
        lines.append(f"- Git status: {data.git_status}")
    if data.files_changed:
        lines.append(f"- Files changed: {len(data.files_changed)}")
    lines.append("")

    if data.level >= 1:
        if data.objective:
            lines.extend(["## Objective", "", data.objective, ""])
        if data.sub_step:
            lines.extend(["## Current Step", "", data.sub_step, ""])
        if data.work_completed:
            lines.append("## Work Completed")
            lines.append("")
            for item in data.work_completed:
                lines.append(f"- {item}")
            lines.append("")
        if data.recommended_next_step:
            lines.extend(["## Next Step", "", data.recommended_next_step, ""])
        if data.blockers:
            lines.append("## Blockers")
            lines.append("")
            for item in data.blockers:
                lines.append(f"- {item}")
            lines.append("")

    if data.level >= 2:
        if data.progress_pct:
            lines.append(f"- Progress: {data.progress_pct}%")
        if data.approach_assessment:
            lines.extend(["## Approach Assessment", "", data.approach_assessment, ""])
        if data.drift_analysis:
            lines.extend(["## Drift Analysis", "", data.drift_analysis, ""])
        if data.risk_factors:
            lines.append("## Risk Factors")
            lines.append("")
            for item in data.risk_factors:
                lines.append(f"- {item}")
            lines.append("")

    if data.transcript_tail:
        lines.append("## Transcript Tail")
        lines.append("")
        lines.append("```text")
        lines.extend(data.transcript_tail[-12:])
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Level 0: Mechanical Snapshot
# ---------------------------------------------------------------------------


def create_level0_checkpoint(
    config: PollyPMConfig,
    launch: SessionLaunchSpec,
    *,
    snapshot_content: str,
    trigger: str = "heartbeat",
    parent_checkpoint_id: str = "",
) -> tuple[CheckpointData, CheckpointArtifact]:
    """Create a Level 0 mechanical checkpoint (no LLM call)."""
    checkpoint_root = _checkpoint_root(config, launch.session.name, launch.session.project)
    ensure_session_lock(checkpoint_root, launch.session.name)

    tail_lines = [line.rstrip() for line in snapshot_content.splitlines()[-100:] if line.strip()]

    # Gather git state
    cwd = str(launch.session.cwd)
    git_branch = _git_output(cwd, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    git_status = _git_output(cwd, ["git", "status", "--porcelain"])
    git_diff_stat = _git_output(cwd, ["git", "diff", "--stat"])

    # Files changed since last checkpoint
    files_changed: list[str] = []
    if git_status:
        for line in git_status.strip().splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2:
                files_changed.append(parts[1])

    # Extract commands from transcript tail
    commands_observed = _extract_commands(tail_lines)

    # Detect test results
    test_results = _extract_test_results(tail_lines)

    data = CheckpointData(
        checkpoint_id=_checkpoint_id(),
        session_name=launch.session.name,
        project=launch.session.project,
        role=launch.session.role,
        level=0,
        trigger=trigger,
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        parent_checkpoint_id=parent_checkpoint_id,
        is_canonical=True,
        transcript_tail=tail_lines,
        files_changed=files_changed,
        git_branch=git_branch,
        git_status=git_status,
        git_diff_stat=git_diff_stat,
        commands_observed=commands_observed,
        test_results=test_results,
        provider=launch.session.provider.value,
        account=launch.account.name,
        snapshot_hash=snapshot_hash(snapshot_content),
    )

    artifact = _write_checkpoint_files(checkpoint_root, data)
    return data, artifact


# ---------------------------------------------------------------------------
# Level 1: Compact Summary
# ---------------------------------------------------------------------------


def has_meaningful_work(
    current_l0: CheckpointData,
    previous_l1: CheckpointData | None,
) -> bool:
    """Determine if meaningful work has occurred since the last Level 1."""
    if previous_l1 is None:
        # First Level 1 — always meaningful if there's any content
        return bool(current_l0.files_changed or current_l0.git_diff_stat or current_l0.test_results)

    # Check for changes
    if current_l0.files_changed != previous_l1.files_changed:
        return True
    if current_l0.git_status != previous_l1.git_status:
        return True
    if current_l0.snapshot_hash != previous_l1.snapshot_hash:
        return True
    if current_l0.test_results != previous_l1.test_results:
        return True

    return False


def create_level1_checkpoint(
    config: PollyPMConfig,
    launch: SessionLaunchSpec,
    *,
    level0: CheckpointData,
    previous_l1: CheckpointData | None = None,
    trigger: str = "turn_end",
    transcript_since_last: str = "",
) -> tuple[CheckpointData, CheckpointArtifact]:
    """Create a Level 1 compact summary checkpoint.

    Attempts LLM summarization, falls back to heuristic.
    Delta-based: unchanged fields are copied from previous_l1.
    """
    checkpoint_root = _checkpoint_root(config, launch.session.name, launch.session.project)
    ensure_session_lock(checkpoint_root, launch.session.name)

    # Attempt LLM extraction
    l1_fields = _extract_l1_with_llm(level0, transcript_since_last)
    if l1_fields is None:
        l1_fields = _extract_l1_heuristic(level0, transcript_since_last)

    # Delta-based: copy unchanged fields from previous checkpoint
    if previous_l1 is not None:
        if not l1_fields.get("objective"):
            l1_fields["objective"] = previous_l1.objective
        # Carry forward blockers only if still relevant
        if not l1_fields.get("blockers") and previous_l1.blockers:
            l1_fields["blockers"] = previous_l1.blockers

    data = CheckpointData(
        checkpoint_id=_checkpoint_id(),
        session_name=launch.session.name,
        project=launch.session.project,
        role=launch.session.role,
        level=1,
        trigger=trigger,
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        parent_checkpoint_id=level0.checkpoint_id,
        is_canonical=True,
        # Copy Level 0 fields
        transcript_tail=level0.transcript_tail,
        files_changed=level0.files_changed,
        git_branch=level0.git_branch,
        git_status=level0.git_status,
        git_diff_stat=level0.git_diff_stat,
        commands_observed=level0.commands_observed,
        test_results=level0.test_results,
        worktree_path=level0.worktree_path,
        provider=level0.provider,
        account=level0.account,
        lease_holder=level0.lease_holder,
        snapshot_hash=level0.snapshot_hash,
        # Level 1 fields
        objective=l1_fields.get("objective", ""),
        sub_step=l1_fields.get("sub_step", ""),
        work_completed=l1_fields.get("work_completed", []),
        blockers=l1_fields.get("blockers", []),
        unresolved_questions=l1_fields.get("unresolved_questions", []),
        recommended_next_step=l1_fields.get("recommended_next_step", ""),
        confidence=l1_fields.get("confidence", ""),
    )

    artifact = _write_checkpoint_files(checkpoint_root, data)
    return data, artifact


def create_issue_completion_checkpoint(
    config: PollyPMConfig,
    launch: SessionLaunchSpec,
    *,
    task_title: str,
    task_path: Path,
    review_summary: str = "",
    verification: str = "",
) -> tuple[CheckpointData, CheckpointArtifact]:
    """Create a Level 1 checkpoint when a task transitions to completed."""
    checkpoint_root = _checkpoint_root(config, launch.session.name, launch.session.project)
    ensure_session_lock(checkpoint_root, launch.session.name)

    cwd = str(launch.session.cwd)
    git_branch = _git_output(cwd, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    git_status = _git_output(cwd, ["git", "status", "--porcelain"])
    git_diff_stat = _git_output(cwd, ["git", "diff", "--stat"])
    if not git_diff_stat:
        git_diff_stat = _git_output(cwd, ["git", "diff", "--cached", "--stat"])

    files_changed: list[str] = []
    seen_files: set[str] = set()
    file_lists = [
        _git_output(cwd, ["git", "diff", "--name-only"]),
        _git_output(cwd, ["git", "diff", "--cached", "--name-only"]),
        _git_output(cwd, ["git", "ls-files", "--others", "--exclude-standard"]),
    ]
    for output in file_lists:
        for line in output.splitlines():
            path_text = line.strip()
            if path_text and path_text not in seen_files:
                seen_files.add(path_text)
                files_changed.append(path_text)
    if not files_changed and git_status:
        for line in git_status.strip().splitlines():
            if not line.strip():
                continue
            path_text = line[3:].strip() if len(line) >= 4 else ""
            if path_text and path_text not in seen_files:
                seen_files.add(path_text)
                files_changed.append(path_text)

    test_files = [
        path for path in files_changed
        if "/test" in path or path.startswith("test") or "/tests/" in path or path.startswith("tests/")
    ]
    work_completed: list[str] = []
    if files_changed:
        work_completed.append(
            f"Modified {len(files_changed)} file(s): {', '.join(files_changed[:5])}"
        )
    if test_files:
        work_completed.append(
            f"Tests added or updated: {', '.join(test_files[:5])}"
        )
    if review_summary.strip():
        work_completed.append(f"Review summary: {review_summary.strip()}")
    if verification.strip():
        work_completed.append(f"Verification: {verification.strip()}")
    if not work_completed:
        work_completed.append("Issue transitioned to completed.")

    transcript_tail = [line for line in task_path.read_text().splitlines()[-40:] if line.strip()] if task_path.exists() else []
    data = CheckpointData(
        checkpoint_id=_checkpoint_id(),
        session_name=launch.session.name,
        project=launch.session.project,
        role=launch.session.role,
        level=1,
        trigger="issue_completed",
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        is_canonical=True,
        transcript_tail=transcript_tail,
        files_changed=files_changed,
        git_branch=git_branch,
        git_status=git_status,
        git_diff_stat=git_diff_stat,
        test_results={},
        worktree_path=str(launch.session.cwd),
        provider=launch.session.provider.value,
        account=launch.account.name,
        objective=task_title,
        work_completed=work_completed,
        recommended_next_step="Pick up the next ready issue.",
        snapshot_hash=snapshot_hash(task_path.read_text() if task_path.exists() else task_title),
    )

    artifact = _write_checkpoint_files(checkpoint_root, data)
    return data, artifact


# ---------------------------------------------------------------------------
# Level 2: Strategic Summary
# ---------------------------------------------------------------------------


def create_level2_checkpoint(
    config: PollyPMConfig,
    launch: SessionLaunchSpec,
    *,
    level1: CheckpointData,
    trigger: str = "pm_request",
    transcript_since_last: str = "",
) -> tuple[CheckpointData, CheckpointArtifact]:
    """Create a Level 2 strategic summary checkpoint."""
    checkpoint_root = _checkpoint_root(config, launch.session.name, launch.session.project)
    ensure_session_lock(checkpoint_root, launch.session.name)

    l2_fields = _extract_l2_with_llm(level1, transcript_since_last)
    if l2_fields is None:
        l2_fields = _extract_l2_heuristic(level1)

    data = CheckpointData(
        checkpoint_id=_checkpoint_id(),
        session_name=level1.session_name,
        project=level1.project,
        role=level1.role,
        level=2,
        trigger=trigger,
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        parent_checkpoint_id=level1.checkpoint_id,
        is_canonical=True,
        # Copy Level 0 + Level 1 fields
        transcript_tail=level1.transcript_tail,
        files_changed=level1.files_changed,
        git_branch=level1.git_branch,
        git_status=level1.git_status,
        git_diff_stat=level1.git_diff_stat,
        commands_observed=level1.commands_observed,
        test_results=level1.test_results,
        worktree_path=level1.worktree_path,
        provider=level1.provider,
        account=level1.account,
        lease_holder=level1.lease_holder,
        snapshot_hash=level1.snapshot_hash,
        objective=level1.objective,
        sub_step=level1.sub_step,
        work_completed=level1.work_completed,
        blockers=level1.blockers,
        unresolved_questions=level1.unresolved_questions,
        recommended_next_step=level1.recommended_next_step,
        confidence=level1.confidence,
        # Level 2 fields
        progress_pct=l2_fields.get("progress_pct", 0),
        approach_assessment=l2_fields.get("approach_assessment", ""),
        drift_analysis=l2_fields.get("drift_analysis", ""),
        risk_factors=l2_fields.get("risk_factors", []),
        alternative_approaches=l2_fields.get("alternative_approaches", []),
        cross_session_context=l2_fields.get("cross_session_context", ""),
    )

    artifact = _write_checkpoint_files(checkpoint_root, data)
    return data, artifact


# ---------------------------------------------------------------------------
# Load canonical checkpoint
# ---------------------------------------------------------------------------


def load_canonical_checkpoint(
    config: PollyPMConfig,
    session_name: str,
    project_key: str,
) -> CheckpointData | None:
    """Load the canonical (latest) checkpoint for a session."""
    project_path = _project_root(config, project_key)
    checkpoint_root = session_scoped_dir(project_checkpoints_dir(project_path), session_name)
    latest_path = checkpoint_root / "latest.json"
    if not latest_path.exists():
        return None
    try:
        data = json.loads(latest_path.read_text())
        return CheckpointData.from_dict(data)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Legacy compatibility wrapper
# ---------------------------------------------------------------------------


def write_mechanical_checkpoint(
    config: PollyPMConfig,
    launch: SessionLaunchSpec,
    *,
    snapshot_path: Path,
    snapshot_content: str,
    log_bytes: int,
    alerts: list[str],
) -> CheckpointArtifact:
    """Legacy wrapper: creates a Level 0 checkpoint and returns the artifact."""
    _data, artifact = create_level0_checkpoint(
        config,
        launch,
        snapshot_content=snapshot_content,
        trigger="heartbeat",
    )
    return artifact


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
    current = store.get_session_runtime(launch.session.name)
    store.upsert_session_runtime(
        session_name=launch.session.name,
        status=current.status if current is not None else "healthy",
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


# ---------------------------------------------------------------------------
# LLM extraction helpers
# ---------------------------------------------------------------------------


def _extract_l1_with_llm(
    level0: CheckpointData,
    transcript: str,
) -> dict[str, Any] | None:
    """Use Haiku to generate Level 1 summary fields."""
    if shutil.which("claude") is None:
        return None

    capped = transcript[:TRANSCRIPT_CAP_CHARS] if transcript else ""
    context = {
        "files_changed": level0.files_changed[:20],
        "git_branch": level0.git_branch,
        "git_status": level0.git_status[:500],
        "commands": level0.commands_observed[:10],
        "test_results": level0.test_results,
        "transcript_tail": capped,
    }

    prompt_lines = [
        "Generate a compact handoff summary for an agent session checkpoint.",
        "Return JSON with: objective, sub_step, work_completed (array), blockers (array),",
        "unresolved_questions (array), recommended_next_step, confidence.",
        "Be concise. No secrets or tokens.",
        json.dumps(context),
    ]

    result = subprocess.run(
        ["claude", "-p", "\n".join(prompt_lines), "--model", HAIKU_MODEL],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "objective": str(payload.get("objective", "")),
        "sub_step": str(payload.get("sub_step", "")),
        "work_completed": [str(i) for i in payload.get("work_completed", []) if i],
        "blockers": [str(i) for i in payload.get("blockers", []) if i],
        "unresolved_questions": [str(i) for i in payload.get("unresolved_questions", []) if i],
        "recommended_next_step": str(payload.get("recommended_next_step", "")),
        "confidence": str(payload.get("confidence", "")),
    }


def _extract_l1_heuristic(
    level0: CheckpointData,
    transcript: str,
) -> dict[str, Any]:
    """Heuristic Level 1 extraction from Level 0 data."""
    objective = ""
    work_completed: list[str] = []
    recommended_next_step = ""

    if level0.files_changed:
        work_completed.append(f"Modified {len(level0.files_changed)} file(s): {', '.join(level0.files_changed[:5])}")
    if level0.test_results:
        passed = level0.test_results.get("passed", 0)
        failed = level0.test_results.get("failed", 0)
        work_completed.append(f"Tests: {passed} passed, {failed} failed")
    if level0.commands_observed:
        work_completed.append(f"Commands: {', '.join(level0.commands_observed[:3])}")

    # Try to infer objective from transcript tail
    for line in reversed(level0.transcript_tail):
        lowered = line.lower()
        if any(kw in lowered for kw in ("implement", "fix", "add", "create", "build", "write")):
            objective = line.strip()[:200]
            break

    return {
        "objective": objective,
        "sub_step": "",
        "work_completed": work_completed,
        "blockers": [],
        "unresolved_questions": [],
        "recommended_next_step": recommended_next_step,
        "confidence": "",
    }


def _extract_l2_with_llm(
    level1: CheckpointData,
    transcript: str,
) -> dict[str, Any] | None:
    """Use Haiku to generate Level 2 strategic summary."""
    if shutil.which("claude") is None:
        return None

    capped = transcript[:TRANSCRIPT_CAP_CHARS] if transcript else ""
    context = {
        "objective": level1.objective,
        "sub_step": level1.sub_step,
        "work_completed": level1.work_completed,
        "blockers": level1.blockers,
        "files_changed": level1.files_changed[:20],
        "test_results": level1.test_results,
        "transcript_excerpt": capped,
    }

    prompt_lines = [
        "Generate a strategic checkpoint summary for a PM reviewing session progress.",
        "Return JSON with: progress_pct (int 0-100), approach_assessment, drift_analysis,",
        "risk_factors (array), alternative_approaches (array), cross_session_context.",
        "Be analytical and concise. No secrets.",
        json.dumps(context),
    ]

    result = subprocess.run(
        ["claude", "-p", "\n".join(prompt_lines), "--model", HAIKU_MODEL],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "progress_pct": int(payload.get("progress_pct", 0)),
        "approach_assessment": str(payload.get("approach_assessment", "")),
        "drift_analysis": str(payload.get("drift_analysis", "")),
        "risk_factors": [str(i) for i in payload.get("risk_factors", []) if i],
        "alternative_approaches": [str(i) for i in payload.get("alternative_approaches", []) if i],
        "cross_session_context": str(payload.get("cross_session_context", "")),
    }


def _extract_l2_heuristic(level1: CheckpointData) -> dict[str, Any]:
    """Heuristic Level 2 extraction from Level 1 data."""
    risk_factors: list[str] = []
    if level1.blockers:
        risk_factors.extend(f"Blocker: {b}" for b in level1.blockers)
    if level1.test_results.get("failed", 0) > 0:
        risk_factors.append(f"Failing tests: {level1.test_results['failed']}")

    return {
        "progress_pct": 0,
        "approach_assessment": "",
        "drift_analysis": "",
        "risk_factors": risk_factors,
        "alternative_approaches": [],
        "cross_session_context": "",
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _checkpoint_id() -> str:
    """Generate a unique checkpoint ID."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    short_uuid = uuid.uuid4().hex[:8]
    return f"{stamp}-{short_uuid}"


def _git_output(cwd: str, command: list[str]) -> str:
    """Run a git command and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _extract_commands(lines: list[str]) -> list[str]:
    """Extract shell commands from transcript lines."""
    commands: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("$ ") or stripped.startswith("% "):
            cmd = stripped[2:].strip()
            if cmd and cmd not in commands:
                commands.append(cmd)
    return commands[-10:]  # Keep last 10


def _extract_test_results(lines: list[str]) -> dict[str, int]:
    """Extract test results from transcript lines."""
    import re
    for line in reversed(lines):
        # pytest format: "X passed, Y failed in Z.Zs"
        match = re.search(r"(\d+) passed", line)
        if match:
            results: dict[str, int] = {"passed": int(match.group(1))}
            failed = re.search(r"(\d+) failed", line)
            if failed:
                results["failed"] = int(failed.group(1))
            return results
    return {}


def _project_root(config: PollyPMConfig, project_key: str) -> Path:
    if project_key == "pollypm":
        return config.project.root_dir
    project = config.projects.get(project_key)
    if project is None:
        return config.project.root_dir
    return project.path
