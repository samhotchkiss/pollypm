"""Fast read-only inbox action preview for cockpit first paint.

This helper exists for the ``python -m pollypm cockpit-pane inbox`` hot path.
It reads only Store-backed ``messages`` rows with stdlib modules, avoiding the
SQLAlchemy/Textual/work-service imports that the full interactive Inbox needs.
If it cannot prove there are action rows, callers fall back to the full loader.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import tomllib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


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
    config: "PollyPMConfig | None" = None,
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
        # #1355: previously silent. A malformed config silently disables
        # the fast preview path; log so config corruption is debuggable
        # instead of "the cockpit just feels slow".
        logger.warning(
            "inbox_action_preview: failed to read config at %s",
            config_path,
            exc_info=True,
        )
        return None

    # Postgres backend: one unified ``messages`` table — single query
    # against the shared pool replaces the per-state.db scan.
    projects_raw = raw.get("projects")
    projects = projects_raw if isinstance(projects_raw, dict) else {}
    known_projects = {str(key) for key in projects}
    rows_per_source = max(preview_limit * 4, 48)
    pg_rows = _pg_query_message_rows(limit=rows_per_source, config=config)
    items: list[FastInboxPreviewEntry] = []
    for row in pg_rows:
        item = _row_to_entry(
            row,
            source_key=WORKSPACE_DB_KEY,
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


def _pg_query_message_rows(
    *,
    limit: int,
    config: "PollyPMConfig | None",
) -> list[dict[str, object]]:
    """Query qualifying ``messages`` rows from the pg RO pool.

    Returns one dict per row keyed on the column names so
    ``_row_to_entry`` is backend-agnostic.
    """
    try:
        from pollypm.storage.pg_pool import get_ro_pool
    except Exception as exc:  # noqa: BLE001
        logger.debug("inbox_action_preview: pg_pool import failed: %s", exc)
        return []
    try:
        pool = get_ro_pool(config)
    except Exception as exc:  # noqa: BLE001
        logger.debug("inbox_action_preview: get_ro_pool failed: %s", exc)
        return []
    sql = (
        "SELECT id, scope, type, tier, recipient, sender, state, parent_id, "
        "       subject, body, payload_json, labels, "
        "       created_at, updated_at, closed_at "
        "FROM messages "
        "WHERE recipient = %s AND state = %s "
        "  AND type IN (%s, %s, %s) "
        "ORDER BY created_at DESC, id DESC "
        "LIMIT %s"
    )
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                ("user", "open", "notify", "inbox_task", "alert", int(limit)),
            )
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "inbox_action_preview: pg query failed: %s", exc, exc_info=True,
        )
        return []
    return [dict(zip(cols, row, strict=False)) for row in rows]


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
