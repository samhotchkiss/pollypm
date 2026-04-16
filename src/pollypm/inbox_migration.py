"""One-shot migration of legacy inbox messages to tasks / archive.

Context
-------
The legacy inbox subsystem (``inbox_v2`` — folders under
``<project_root>/.pollypm/inbox/messages/``) is being retired. Existing
installs may have live messages in-flight on upgrade; this migration
rescues them so nothing is silently lost.

Policy
------
For each legacy message:

* If the recipient is an agent role (``to`` is neither empty nor
  ``"user"``/``"human"``) **and** the message has a non-trivial body,
  convert it into a ``flow=chat`` task in the work service. The original
  sender is recorded as the task's ``created_by``. The target project is
  the message's ``project`` field when it maps to a tracked project;
  otherwise the message is archived instead of lost.
* Otherwise, append the message (plus its full thread) to
  ``<base_dir>/inbox-archive.jsonl`` and delete it.

If any per-message conversion raises, the message is **left in place**
and a durable alert is raised pointing at its ID — the user can inspect
on the next boot.

Re-entrancy
-----------
A marker file at ``<base_dir>/inbox-migration-done.json`` is written
after a successful run; subsequent boots become a no-op.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


MIGRATION_MARKER_NAME = "inbox-migration-done.json"
ARCHIVE_NAME = "inbox-archive.jsonl"
MIGRATION_VERSION = 1


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InboxMigrationResult:
    migrated_to_tasks: int = 0
    archived: int = 0
    failed: int = 0
    failed_ids: list[str] = None  # type: ignore[assignment]
    skipped_already_done: bool = False

    def __post_init__(self) -> None:
        if self.failed_ids is None:
            self.failed_ids = []

    def summary(self) -> str:
        return (
            f"{self.migrated_to_tasks} migrated to tasks, "
            f"{self.archived} archived, {self.failed} failed"
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_inbox_migration_if_needed(config: Any) -> InboxMigrationResult:
    """Run the migration once per install.

    ``config`` must expose ``project.root_dir``, ``project.base_dir`` and
    ``project.state_db``; and ``projects`` (a dict of project key →
    project with a ``.path``).
    """
    base_dir: Path = config.project.base_dir
    marker_path = base_dir / MIGRATION_MARKER_NAME

    if marker_path.exists():
        return InboxMigrationResult(skipped_already_done=True)

    result = _run_migration(config)
    # Only write the marker if the migration completed cleanly for every
    # message we attempted. If any failed we leave the marker absent so the
    # next boot retries, but we still archive/task what we could.
    if result.failed == 0:
        _write_marker(marker_path, result)
    return result


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _write_marker(path: Path, result: InboxMigrationResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MIGRATION_VERSION,
        "completed_at": datetime.now(UTC).isoformat(),
        "migrated_to_tasks": result.migrated_to_tasks,
        "archived": result.archived,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _is_task_like(to: str, body: str) -> bool:
    """True iff this message should become a task.

    Task-like = (addressed to an agent role) AND (has non-empty body).
    A ``to`` of empty / ``user`` / ``human`` means the message is a
    user-facing notification and gets archived instead.
    """
    if not to or to in ("user", "human"):
        return False
    return bool(body and body.strip())


def _read_legacy_message(msg_dir: Path) -> dict[str, Any] | None:
    """Load a legacy message folder into a plain dict.

    Returns ``None`` if the folder is malformed and should be skipped.
    """
    state_path = msg_dir / "state.json"
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    entries: list[dict[str, str]] = []
    for entry_path in sorted(msg_dir.glob("[0-9]*.md")):
        try:
            text = entry_path.read_text()
        except OSError:
            continue
        sender = ""
        recipient = ""
        timestamp = ""
        body_lines: list[str] = []
        in_body = False
        for line in text.splitlines():
            if not in_body:
                if line.startswith("From: "):
                    sender = line[6:]
                elif line.startswith("To: "):
                    recipient = line[4:]
                elif line.startswith("Date: "):
                    timestamp = line[6:]
                elif line == "":
                    in_body = True
            else:
                body_lines.append(line)
        entries.append(
            {
                "sender": sender,
                "to": recipient,
                "timestamp": timestamp,
                "body": "\n".join(body_lines).strip(),
            }
        )
    return {
        "id": state.get("id", msg_dir.name),
        "subject": state.get("subject", ""),
        "status": state.get("status", "open"),
        "owner": state.get("owner", ""),
        "sender": state.get("sender", ""),
        "to": state.get("to", "") or (
            state.get("owner", "") if state.get("owner", "") != "user" else "user"
        ),
        "created_at": state.get("created_at", ""),
        "updated_at": state.get("updated_at", ""),
        "project": state.get("project", ""),
        "parent_id": state.get("parent_id", ""),
        "entries": entries,
        "path": str(msg_dir),
    }


def _first_body(message: dict[str, Any]) -> str:
    entries = message.get("entries") or []
    if not entries:
        return ""
    return entries[0].get("body", "") or ""


def _resolve_project_path(config: Any, project_key: str) -> Path | None:
    if not project_key:
        return None
    project = config.projects.get(project_key)
    if project is None:
        return None
    return project.path


def _append_archive(archive_path: Path, record: dict[str, Any]) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with archive_path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _raise_alert(config: Any, alert_type: str, message: str) -> None:
    """Raise a durable alert for a per-message failure.

    Tolerant of missing StateStore infrastructure (tests may run without
    a ``state.db`` present).
    """
    try:
        from pollypm.storage.state import StateStore

        store = StateStore(config.project.state_db)
        try:
            store.upsert_alert(
                "inbox_migration", alert_type, "warn", message,
            )
            store.record_event(
                "inbox_migration", "alert",
                f"Raised migration alert {alert_type}: {message[:160]}",
            )
        finally:
            store.close()
    except Exception:  # noqa: BLE001
        logger.warning("inbox-migration alert could not be persisted: %s", message)


def _record_event(config: Any, message: str) -> None:
    try:
        from pollypm.storage.state import StateStore

        store = StateStore(config.project.state_db)
        try:
            store.record_event("inbox_migration", "migration", message)
        finally:
            store.close()
    except Exception:  # noqa: BLE001
        logger.info("inbox-migration event could not be persisted: %s", message)


def _convert_to_task(config: Any, message: dict[str, Any]) -> bool:
    """Attempt to create a chat-flow task from ``message``.

    Returns ``True`` on success, ``False`` if the message's project isn't
    tracked (caller should archive instead).
    """
    project_key = message.get("project") or ""
    project_path = _resolve_project_path(config, project_key)
    if project_path is None:
        return False

    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    sender = (message.get("sender") or "").strip() or "unknown"
    subject = (message.get("subject") or "").strip() or "Migrated inbox message"
    body = _first_body(message)
    # Include the rest of the thread (if any) as additional context in the
    # description, newest-last, so the agent sees the full conversation.
    entries = message.get("entries") or []
    body_parts = [body] if body else []
    for entry in entries[1:]:
        ebody = (entry.get("body") or "").strip()
        if not ebody:
            continue
        esender = entry.get("sender") or "?"
        ets = entry.get("timestamp") or ""
        body_parts.append(f"\n\n---\nFrom: {esender}  Date: {ets}\n\n{ebody}")
    description = "".join(body_parts).strip()
    if not description:
        description = subject

    to = (message.get("to") or "").strip()
    # Roles: chat flow expects 'operator' (the replier) and an optional
    # 'requester' for provenance. Map ``to`` → operator when it names an
    # agent; keep 'requester' = original sender.
    roles = {
        "operator": to if to and to not in ("user", "human") else "polly",
        "requester": sender or "user",
    }

    with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
        task = svc.create(
            title=subject[:200],
            description=description,
            type="task",
            project=project_key,
            flow_template="chat",
            roles=roles,
            priority="normal",
            created_by=sender,
            labels=["migrated-from-inbox"],
        )
        # Queue it so the agent can pick it up / so it appears in inbox-view.
        try:
            svc.queue(task.task_id, actor="inbox_migration")
        except Exception:  # noqa: BLE001 - queueing is a best-effort polish
            logger.info("Could not auto-queue migrated task %s", task.task_id)
    return True


def _archive(config: Any, message: dict[str, Any]) -> None:
    archive_path = config.project.base_dir / ARCHIVE_NAME
    record = {
        "archived_at": datetime.now(UTC).isoformat(),
        "id": message.get("id", ""),
        "subject": message.get("subject", ""),
        "status": message.get("status", ""),
        "sender": message.get("sender", ""),
        "to": message.get("to", ""),
        "owner": message.get("owner", ""),
        "project": message.get("project", ""),
        "created_at": message.get("created_at", ""),
        "updated_at": message.get("updated_at", ""),
        "entries": message.get("entries", []),
    }
    _append_archive(archive_path, record)


def _delete_message_dir(msg_dir: Path) -> None:
    if msg_dir.exists():
        shutil.rmtree(msg_dir, ignore_errors=True)


def _iter_project_roots(config: Any) -> list[Path]:
    """All filesystem roots that may hold a legacy inbox on this install.

    The legacy subsystem wrote to ``<root>/.pollypm/inbox/messages/``; the
    primary write site is the host config's ``project.root_dir``, but
    historically a parallel inbox at ``.parent`` was also scanned. We
    cover both so upgrades don't leave orphaned folders.
    """
    seen: list[Path] = []
    primary = config.project.root_dir
    seen.append(primary)
    try:
        parent = primary.parent
        if parent != primary and parent.exists():
            seen.append(parent)
    except Exception:  # noqa: BLE001
        pass
    # Also scan tracked project paths — each project may have its own
    # ``.pollypm/inbox/messages/`` scaffold.
    for project in getattr(config, "projects", {}).values():
        path = getattr(project, "path", None)
        if path and path not in seen:
            seen.append(path)
    return seen


def _run_migration(config: Any) -> InboxMigrationResult:
    result = InboxMigrationResult()
    roots = _iter_project_roots(config)

    for root in roots:
        messages_root = root / ".pollypm" / "inbox" / "messages"
        if not messages_root.exists():
            continue
        for msg_dir in sorted(messages_root.iterdir()):
            if not msg_dir.is_dir():
                continue
            if msg_dir.name.startswith("_"):
                # Internal artefacts (dedup index, etc.) — skip.
                continue
            message = _read_legacy_message(msg_dir)
            if message is None:
                # Malformed folder — archive a stub pointer and move on.
                try:
                    _append_archive(
                        config.project.base_dir / ARCHIVE_NAME,
                        {
                            "archived_at": datetime.now(UTC).isoformat(),
                            "id": msg_dir.name,
                            "note": "malformed legacy message folder",
                            "path": str(msg_dir),
                        },
                    )
                    _delete_message_dir(msg_dir)
                    result.archived += 1
                except Exception:  # noqa: BLE001
                    result.failed += 1
                    result.failed_ids.append(msg_dir.name)
                continue

            try:
                body = _first_body(message)
                if _is_task_like(message.get("to") or "", body):
                    converted = _convert_to_task(config, message)
                    if converted:
                        _delete_message_dir(msg_dir)
                        result.migrated_to_tasks += 1
                        continue
                # Archive path — pure notifications, or task-like
                # messages whose project can't be resolved.
                _archive(config, message)
                _delete_message_dir(msg_dir)
                result.archived += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "inbox migration failed for %s: %s", msg_dir.name, exc,
                )
                result.failed += 1
                result.failed_ids.append(message.get("id") or msg_dir.name)
                _raise_alert(
                    config,
                    "inbox_migration_failed",
                    f"Could not migrate message {message.get('id') or msg_dir.name}: {exc}",
                )

    summary = (
        f"{result.migrated_to_tasks} migrated to tasks, "
        f"{result.archived} archived, {result.failed} lost"
    )
    _record_event(config, summary)
    return result
