"""Fast read-only inbox action preview for cockpit first paint.

This helper exists for the ``python -m pollypm cockpit-pane inbox`` hot path.
It reads only Store-backed ``messages`` rows with stdlib modules, avoiding the
SQLAlchemy/Textual/work-service imports that the full interactive Inbox needs.
If it cannot prove there are action rows, callers fall back to the full loader.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import tomllib
from typing import Any


WORKSPACE_DB_KEY = "__workspace__"

_MARKDOWN_DECORATION_RE = re.compile(r"[*_`#>\[\]]+")
_DIGEST_SUBJECT_RE = re.compile(
    r"^\s*(?:[A-Za-z]+\s+)?digest\b\s*[:—–-]",
    re.IGNORECASE,
)
_OPS_ANOMALY_SUBJECT_RE = re.compile(
    r"^\s*(?:[A-Za-z]+\s+)?(?:"
    r"misrouted\s+review\s+ping"
    r"|repeated\s+stale\s+review\s+ping"
    r"|second\s+bogus\s+review\s+ping"
    r"|bogus\s+review\s+ping"
    r"|stale\s+planner\s+tasks?"
    r"|review\s+requested\s+for\s+missing\s+task"
    r"|review-needed\s+notifications?\s+(?:contain|missing)"
    r")\b",
    re.IGNORECASE,
)
_COMPLETION_RE = re.compile(
    r"\b(complete|completed|shipped|done|merged|deliverable)\b",
    re.IGNORECASE,
)
_ACTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "decision needed",
        re.compile(
            r"\b(decision|triage|your call|need Polly's call|need your call|scope escalation)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "needs unblock",
        re.compile(
            r"\b(blocked|blocking|waiting on|on hold|stale review ping)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "setup needed",
        re.compile(
            r"\b(set up|setup|sign in|login|account access|access expired|"
            r"fly\.io|fly deploy|verification email|email click|click the link)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "review needed",
        re.compile(r"\b(review|approve|approval)\b", re.IGNORECASE),
    ),
    (
        "action required",
        re.compile(
            r"^(\[action\]|action)\b|"
            r"\b(action required|needs? your|need your|need Polly|question)\b",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(slots=True)
class FastInboxPreviewEntry:
    task_id: str = ""
    title: str = ""
    project: str = ""
    triage_label: str = "action required"
    labels: tuple[str, ...] = ()
    created_at: object | None = None
    updated_at: object | None = None
    needs_action: bool = True


def load_fast_inbox_action_preview(
    config_path: Path,
    *,
    project: str | None = None,
    limit: int = 12,
) -> tuple[list[FastInboxPreviewEntry], set[str], int] | None:
    """Return Store-backed action rows without importing the full inbox stack.

    ``None`` means "not enough information"; callers should use the normal
    loader. A non-empty tuple means actual action rows were found and are safe
    to prepaint.
    """
    preview_limit = max(int(limit), 1)
    try:
        raw = tomllib.loads(config_path.read_text())
    except Exception:  # noqa: BLE001
        return None
    sources, known_projects = _message_sources(raw, config_path=config_path)
    if not sources:
        return None

    items: list[FastInboxPreviewEntry] = []
    rows_per_source = max(preview_limit * 4, 48)
    for source_key, db_path in sources:
        if not db_path.exists():
            continue
        for row in _query_message_rows(db_path, limit=rows_per_source):
            item = _row_to_entry(
                row,
                source_key=source_key,
                known_projects=known_projects,
            )
            if item is None:
                continue
            if project and item.project != project:
                continue
            items.append(item)

    if not items:
        return None

    items = _dedupe_replayed_plan_reviews(items)
    items.sort(key=_entry_sort_value, reverse=True)
    preview = items[:preview_limit]
    return preview, {item.task_id for item in preview}, len(items)


def _message_sources(
    raw: dict[str, Any], *, config_path: Path,
) -> tuple[list[tuple[str, Path]], set[str]]:
    base = config_path.parent
    projects_raw = raw.get("projects")
    projects = projects_raw if isinstance(projects_raw, dict) else {}
    known_projects = {str(key) for key in projects}
    sources: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    def _add(source_key: str, db_path: Path) -> None:
        resolved = db_path.resolve() if db_path.exists() else db_path
        if resolved in seen:
            return
        seen.add(resolved)
        sources.append((source_key, db_path))

    for project_key, project_raw in projects.items():
        if not isinstance(project_raw, dict):
            continue
        path_raw = project_raw.get("path")
        if not isinstance(path_raw, str) or not path_raw.strip():
            continue
        project_path = Path(path_raw).expanduser()
        if not project_path.is_absolute():
            project_path = base / project_path
        _add(str(project_key), project_path / ".pollypm" / "state.db")

    project_settings = raw.get("project")
    if isinstance(project_settings, dict):
        workspace_raw = project_settings.get("workspace_root")
        if isinstance(workspace_raw, str) and workspace_raw.strip():
            workspace = Path(workspace_raw).expanduser()
            if not workspace.is_absolute():
                workspace = base / workspace
            _add(WORKSPACE_DB_KEY, workspace / ".pollypm" / "state.db")
    return sources, known_projects


def _query_message_rows(db_path: Path, *, limit: int) -> list[dict[str, object]]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
    except Exception:  # noqa: BLE001
        return []
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                """
                SELECT
                    id, scope, type, tier, recipient, sender, state, parent_id,
                    subject, body, payload_json, labels, created_at, updated_at,
                    closed_at
                FROM messages
                WHERE recipient = ?
                  AND state = ?
                  AND type IN (?, ?, ?)
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                ("user", "open", "notify", "inbox_task", "alert", int(limit)),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _row_to_entry(
    row: dict[str, object],
    *,
    source_key: str,
    known_projects: set[str],
) -> FastInboxPreviewEntry | None:
    labels = _labels(row.get("labels"))
    if "channel:dev" in labels:
        return None
    payload = _payload(row.get("payload_json"))
    scope = str(row.get("scope") or "").strip()
    project = str(
        payload.get("project")
        or scope
        or ("inbox" if source_key == WORKSPACE_DB_KEY else source_key)
    )
    if _is_orphaned_project(project, known_projects=known_projects):
        return None
    title = str(row.get("subject") or "(no subject)")
    body = str(row.get("body") or "").replace("\\n", "\n")
    triage_label = _fast_action_label(title=title, body=body, labels=labels)
    if triage_label is None:
        return None
    row_id = row.get("id")
    return FastInboxPreviewEntry(
        task_id=f"msg:{source_key}:{row_id}",
        title=title,
        project=project,
        triage_label=triage_label,
        labels=tuple(labels),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at") or row.get("created_at"),
    )


def _fast_action_label(
    *,
    title: str,
    body: str,
    labels: list[str],
) -> str | None:
    if "plan_review" in labels:
        return "plan review"
    if "blocking_question" in labels:
        return "worker blocked"
    title_plain = _plain_text(title)
    title_lower = title_plain.lower()
    if _DIGEST_SUBJECT_RE.search(title_lower):
        return None
    if _OPS_ANOMALY_SUBJECT_RE.search(title_lower):
        return None
    if _COMPLETION_RE.search(title_plain):
        return None
    text = " ".join(
        part for part in (title_plain, _plain_text(body)) if part
    ).strip()
    for label, pattern in _ACTION_RULES:
        if pattern.search(text):
            return label
    return None


def _plain_text(value: object | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _MARKDOWN_DECORATION_RE.sub("", text)
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def _labels(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(label) for label in value if str(label).strip()]
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            return [str(label) for label in parsed if str(label).strip()]
    return []


def _payload(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _is_orphaned_project(project: str, *, known_projects: set[str]) -> bool:
    project = (project or "").strip()
    if not project or project == "inbox":
        return False
    return project not in known_projects


def _entry_sort_value(item: FastInboxPreviewEntry) -> str:
    for value in (item.updated_at, item.created_at):
        if value is None:
            continue
        if hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except Exception:  # noqa: BLE001
                continue
        return str(value)
    return ""


def _dedupe_replayed_plan_reviews(
    items: list[FastInboxPreviewEntry],
) -> list[FastInboxPreviewEntry]:
    keep: dict[tuple[str, str], FastInboxPreviewEntry] = {}
    drop_ids: set[str] = set()
    for item in items:
        labels = set(item.labels)
        if "plan_review" not in labels:
            continue
        plan_task = ""
        for label in labels:
            if label.startswith("plan_task:"):
                plan_task = label.split(":", 1)[1].strip()
                break
        if not plan_task:
            continue
        key = (item.project, plan_task)
        existing = keep.get(key)
        if existing is None:
            keep[key] = item
            continue
        if _entry_sort_value(item) > _entry_sort_value(existing):
            drop_ids.add(existing.task_id)
            keep[key] = item
        else:
            drop_ids.add(item.task_id)
    if not drop_ids:
        return items
    return [item for item in items if item.task_id not in drop_ids]
