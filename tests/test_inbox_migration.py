"""Tests for the one-shot legacy inbox migration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pollypm.inbox_migration import (
    ARCHIVE_NAME,
    InboxMigrationResult,
    MIGRATION_MARKER_NAME,
    run_inbox_migration_if_needed,
)
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Test helpers — build a config shape the migration expects.
# ---------------------------------------------------------------------------


@dataclass
class _Project:
    key: str
    path: Path


@dataclass
class _ProjectSettings:
    root_dir: Path
    base_dir: Path
    state_db: Path


@dataclass
class _Config:
    project: _ProjectSettings
    projects: dict = field(default_factory=dict)


def _make_config(tmp_path: Path, *, projects: dict[str, Path] | None = None) -> _Config:
    root = tmp_path / "root"
    base = root / ".pollypm-state"
    root.mkdir(parents=True, exist_ok=True)
    base.mkdir(parents=True, exist_ok=True)
    projects_dict: dict[str, _Project] = {}
    for key, path in (projects or {}).items():
        path.mkdir(parents=True, exist_ok=True)
        projects_dict[key] = _Project(key=key, path=path)
    return _Config(
        project=_ProjectSettings(
            root_dir=root,
            base_dir=base,
            state_db=base / "state.db",
        ),
        projects=projects_dict,
    )


def _write_legacy_message(
    project_root: Path,
    *,
    msg_id: str,
    subject: str,
    sender: str,
    to: str,
    body: str,
    owner: str | None = None,
    project: str = "",
    status: str = "open",
    created_at: str = "2026-04-01T00:00:00+00:00",
) -> Path:
    """Create a legacy inbox message folder as ``inbox_v2`` would."""
    msg_dir = project_root / ".pollypm" / "inbox" / "messages" / msg_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "id": msg_id,
        "subject": subject,
        "status": status,
        "owner": owner or to or "polly",
        "sender": sender,
        "to": to,
        "project": project,
        "created_at": created_at,
        "updated_at": created_at,
        "message_count": 1,
    }
    (msg_dir / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    (msg_dir / "0001-msg.md").write_text(
        f"From: {sender}\nTo: {to}\nDate: {created_at}\nSubject: {subject}\n\n{body}\n"
    )
    return msg_dir


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------


def test_task_like_message_becomes_chat_task(tmp_path: Path) -> None:
    project_path = tmp_path / "camptown"
    config = _make_config(tmp_path, projects={"camptown": project_path})

    _write_legacy_message(
        config.project.root_dir,
        msg_id="20260401T000000Z-fix-bug",
        subject="Please fix the nav bug",
        sender="user",
        to="worker_camptown",
        body="The nav bar is broken on mobile — please fix.",
        project="camptown",
    )

    result = run_inbox_migration_if_needed(config)

    assert result.migrated_to_tasks == 1
    assert result.archived == 0
    assert result.failed == 0

    # Task was created in the project's work DB with the chat flow.
    db_path = project_path / ".pollypm" / "state.db"
    assert db_path.exists()
    with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
        tasks = svc.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.title == "Please fix the nav bug"
    assert task.flow_template_id == "chat"
    assert "migrated-from-inbox" in task.labels
    assert task.created_by == "user"

    # Legacy folder is gone.
    assert not (
        config.project.root_dir / ".pollypm" / "inbox" / "messages"
        / "20260401T000000Z-fix-bug"
    ).exists()


def test_user_facing_notification_is_archived(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    _write_legacy_message(
        config.project.root_dir,
        msg_id="20260401T000001Z-deploy",
        subject="Deploy complete",
        sender="itsalive",
        to="user",
        body="https://example.itsalive.co is live.",
    )

    result = run_inbox_migration_if_needed(config)

    assert result.archived == 1
    assert result.migrated_to_tasks == 0

    archive_path = config.project.base_dir / ARCHIVE_NAME
    assert archive_path.exists()
    lines = archive_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["subject"] == "Deploy complete"
    assert record["sender"] == "itsalive"
    assert record["to"] == "user"


def test_marker_prevents_rerun(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    _write_legacy_message(
        config.project.root_dir,
        msg_id="20260401T000002Z-ping",
        subject="Ping",
        sender="system",
        to="user",
        body="A ping.",
    )

    first = run_inbox_migration_if_needed(config)
    assert first.archived == 1
    assert not first.skipped_already_done

    # Second call must be a no-op even though a fresh message now exists.
    _write_legacy_message(
        config.project.root_dir,
        msg_id="20260401T000003Z-second",
        subject="Second",
        sender="system",
        to="user",
        body="Not migrated because marker is set.",
    )

    second = run_inbox_migration_if_needed(config)
    assert second.skipped_already_done
    assert second.archived == 0
    assert second.migrated_to_tasks == 0

    # The new message is still on disk (marker short-circuited us).
    assert (
        config.project.root_dir
        / ".pollypm"
        / "inbox"
        / "messages"
        / "20260401T000003Z-second"
    ).exists()


def test_task_like_without_known_project_falls_back_to_archive(tmp_path: Path) -> None:
    # The message targets an agent but its ``project`` key isn't in
    # config.projects — we can't build a task DB for it, so we archive.
    config = _make_config(tmp_path)  # no projects registered

    _write_legacy_message(
        config.project.root_dir,
        msg_id="20260401T000004Z-unknown",
        subject="Work for a ghost project",
        sender="polly",
        to="worker_ghost",
        body="Investigate X.",
        project="ghost",
    )

    result = run_inbox_migration_if_needed(config)

    assert result.migrated_to_tasks == 0
    assert result.archived == 1
    archive_path = config.project.base_dir / ARCHIVE_NAME
    assert archive_path.exists()


def test_partial_failure_leaves_message_and_raises_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path = tmp_path / "camptown"
    config = _make_config(tmp_path, projects={"camptown": project_path})

    _write_legacy_message(
        config.project.root_dir,
        msg_id="20260401T000005Z-bad",
        subject="Bad one",
        sender="user",
        to="worker_camptown",
        body="Investigate the failure.",
        project="camptown",
    )
    _write_legacy_message(
        config.project.root_dir,
        msg_id="20260401T000006Z-good",
        subject="Good notification",
        sender="system",
        to="user",
        body="Just an FYI.",
    )

    # Force _convert_to_task to blow up for the task-like message.
    from pollypm import inbox_migration

    original = inbox_migration._convert_to_task

    def _boom(cfg, message):
        if message["id"].endswith("bad"):
            raise RuntimeError("kaboom")
        return original(cfg, message)

    monkeypatch.setattr(inbox_migration, "_convert_to_task", _boom)

    # Also stub out the alert persistence so tests don't need StateStore.
    raised: list[tuple[str, str]] = []

    def _capture(cfg, alert_type, message):
        raised.append((alert_type, message))

    monkeypatch.setattr(inbox_migration, "_raise_alert", _capture)
    monkeypatch.setattr(inbox_migration, "_record_event", lambda *a, **k: None)

    result = run_inbox_migration_if_needed(config)

    assert result.failed == 1
    assert result.archived == 1
    # The failing message stays on disk.
    bad_path = (
        config.project.root_dir
        / ".pollypm"
        / "inbox"
        / "messages"
        / "20260401T000005Z-bad"
    )
    assert bad_path.exists()

    # An alert was raised pointing at the failing message.
    assert raised, "expected at least one alert to be raised"
    assert any("20260401T000005Z-bad" in msg for _kind, msg in raised)

    # No marker is written when anything failed — so the next boot retries.
    assert not (config.project.base_dir / MIGRATION_MARKER_NAME).exists()


# ---------------------------------------------------------------------------
# Integration: seeded inbox with 3 messages → tasks + archive + event record
# ---------------------------------------------------------------------------


def test_integration_three_messages(tmp_path: Path) -> None:
    project_path = tmp_path / "camptown"
    config = _make_config(tmp_path, projects={"camptown": project_path})

    _write_legacy_message(
        config.project.root_dir,
        msg_id="20260401T000010Z-one",
        subject="Task one for the worker",
        sender="user",
        to="worker_camptown",
        body="First task body.",
        project="camptown",
    )
    _write_legacy_message(
        config.project.root_dir,
        msg_id="20260401T000011Z-two",
        subject="Task two for Polly",
        sender="user",
        to="polly",
        body="Second task body.",
        project="camptown",
    )
    _write_legacy_message(
        config.project.root_dir,
        msg_id="20260401T000012Z-notice",
        subject="Nightly summary",
        sender="system",
        to="user",
        body="3 commits, 1 deploy.",
    )

    # Prevent the alert/event helpers from touching a real state.db.
    from pollypm import inbox_migration

    def _noop(*_a, **_k):
        return None

    result = run_inbox_migration_if_needed(config)

    assert result.migrated_to_tasks == 2
    assert result.archived == 1
    assert result.failed == 0

    # Both tasks exist in the camptown DB.
    db_path = project_path / ".pollypm" / "state.db"
    with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
        tasks = svc.list_tasks()
    titles = {t.title for t in tasks}
    assert "Task one for the worker" in titles
    assert "Task two for Polly" in titles

    # The one user-facing notification landed in the archive.
    archive_path = config.project.base_dir / ARCHIVE_NAME
    records = [json.loads(l) for l in archive_path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["subject"] == "Nightly summary"

    # Marker written.
    assert (config.project.base_dir / MIGRATION_MARKER_NAME).exists()
