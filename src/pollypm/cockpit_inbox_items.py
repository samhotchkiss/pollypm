"""Shared inbox item adapters for the cockpit inbox screen.

Contract:
- Inputs: loaded config objects, Store message rows, and work-service tasks.
- Outputs: a single inbox-item surface that lets the Textual inbox render
  Store-backed notifications and task-backed inbox rows together.
- Side effects: opens and closes Store / SQLite readers while loading.
- Invariants: item ids are stable across refreshes, message rows stay
  read-only, and task-backed thread replies continue to flow through the
  work-service path unchanged.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from pollypm.cockpit_inbox import _inbox_db_sources, _row_is_dev_channel
from pollypm.rejection_feedback import (
    feedback_target_task_id,
    is_rejection_feedback_task,
)
from pollypm.store import SQLAlchemyStore
from pollypm.work.inbox_view import inbox_tasks
from pollypm.work.sqlite_service import SQLiteWorkService


_MARKDOWN_DECORATION_RE = re.compile(r"[*_`#>\[\]]+")

# A subject that begins with ``Digest:`` (or e.g. ``Action Digest:``,
# ``FYI Digest:`` after _plain_text strips the canonical [Tag]
# bracket) is a roll-up by definition. Match against the lowercased
# title — the optional leading word covers the dropped bracket tag.
_DIGEST_SUBJECT_RE = re.compile(
    r"^\s*(?:[A-Za-z]+\s+)?digest\b\s*[:—–-]",
    re.IGNORECASE,
)

# Regex triage is score-based: every matching rule contributes a candidate,
# the highest score wins, and exact ties use this documented intent priority.
_TRIAGE_KIND_PRIORITY = {
    "decision": 0,
    "blocker": 1,
    "action": 2,
    "review": 3,
    "completion": 4,
    "info": 5,
}

_TRIAGE_PATTERN_REGISTRY = (
    {
        "kind": "decision",
        "bucket": "action",
        "rank": 0,
        "label": "decision needed",
        "score": 3,
        "pattern": re.compile(
            r"\b(decision|triage|your call|need Polly's call|need your call|scope escalation)\b",
            re.IGNORECASE,
        ),
    },
    {
        "kind": "blocker",
        "bucket": "action",
        "rank": 0,
        "label": "blocked",
        "score": 3,
        "pattern": re.compile(
            r"\b(blocked|blocking|waiting on|on hold|stale review ping)\b",
            re.IGNORECASE,
        ),
    },
    {
        "kind": "action",
        "bucket": "action",
        "rank": 0,
        "label": "setup needed",
        "score": 3,
        "pattern": re.compile(
            r"\b(set up|setup|sign in|login|account access|access expired|"
            r"fly\.io|fly deploy|verification email|email click|click the link)\b",
            re.IGNORECASE,
        ),
    },
    {
        "kind": "review",
        "bucket": "action",
        "rank": 1,
        "label": "review needed",
        "score": 2,
        "pattern": re.compile(
            r"\b(review|approve|approval)\b",
            re.IGNORECASE,
        ),
    },
    {
        "kind": "completion",
        "bucket": "info",
        "rank": 2,
        "label": "completed update",
        "score": 2,
        "pattern": re.compile(
            r"\b(complete|completed|shipped|done|merged|deliverable)\b",
            re.IGNORECASE,
        ),
    },
    {
        "kind": "action",
        "bucket": "action",
        "rank": 1,
        "label": "action required",
        "score": 1,
        "pattern": re.compile(
            r"^(\[action\]|action)\b|"
            r"\b(action required|needs? your|need your|need Polly|question)\b",
            re.IGNORECASE,
        ),
    },
)


def message_item_id(source_key: str, row_id: object) -> str:
    """Stable local id for a Store-backed inbox item."""
    return f"msg:{source_key}:{row_id}"


class InboxEntry:
    """Thin adapter so tasks and Store messages share one inbox surface."""

    def __init__(self, *, raw=None, **values: Any) -> None:
        self._raw = raw
        for key, value in values.items():
            setattr(self, key, value)

    def __getattr__(self, name: str):
        raw = self.__dict__.get("_raw")
        if raw is not None:
            return getattr(raw, name)
        raise AttributeError(name)


def _plain_text(value: object | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _MARKDOWN_DECORATION_RE.sub("", text)
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def _is_orphaned_project(project: str, *, known_projects: set[str]) -> bool:
    project = (project or "").strip()
    if not project or project == "inbox":
        return False
    return project not in known_projects


def _triage_for_entry(
    item: InboxEntry,
    *,
    known_projects: set[str],
) -> tuple[str, int, str]:
    labels = {str(label) for label in list(getattr(item, "labels", []) or [])}
    project = (getattr(item, "project", "") or "").strip()
    title = _plain_text(getattr(item, "title", ""))
    body = _plain_text(getattr(item, "description", ""))
    text = " ".join(part for part in (title, body) if part).strip()

    if _is_orphaned_project(project, known_projects=known_projects):
        return "orphaned", 3, "deleted project"
    if "plan_review" in labels:
        return "action", 0, "plan review"
    if "blocking_question" in labels:
        return "action", 0, "worker blocked"
    if is_rejection_feedback_task(item):
        target = feedback_target_task_id(item)
        if target:
            return "info", 2, f"review feedback for {target}"
        return "info", 2, "review feedback"
    # Subject-level "Digest:" prefix means the message is a roll-up
    # by definition, regardless of whether the body mentions decisions
    # or blockers. Producers occasionally tier digest messages as
    # ``immediate`` (e.g. PMs that haven't migrated to the proper
    # tier yet), so we match the prefix even after the canonical
    # ``[Action]`` / ``[FYI]`` / ``[Info]`` bracket tag.
    title_lower = title.lower()
    if _DIGEST_SUBJECT_RE.search(title_lower):
        return "info", 2, "digest"
    matches = [
        rule
        for rule in _TRIAGE_PATTERN_REGISTRY
        if rule["pattern"].search(text)
    ]
    if matches:
        # When the title already announces a completion ("X shipped",
        # "Y complete", "Z done"), prefer the completion bucket even if
        # the body mentions ``approve`` or ``review`` — the title is the
        # user-visible summary and is a much stronger signal than a
        # mention deep in the body. Without this, "[Action] Calculator
        # CLI E2E complete" with body "approved by user" was bucketed as
        # ``review needed`` and pollutes the action lens for days.
        if any(
            rule["kind"] == "completion" and rule["pattern"].search(title)
            for rule in matches
        ):
            for rule in matches:
                if rule["kind"] == "completion":
                    return (
                        str(rule["bucket"]),
                        int(rule["rank"]),
                        str(rule["label"]),
                    )
        winner = min(
            matches,
            key=lambda rule: (
                -int(rule["score"]),
                _TRIAGE_KIND_PRIORITY.get(str(rule["kind"]), _TRIAGE_KIND_PRIORITY["info"]),
            ),
        )
        return str(winner["bucket"]), int(winner["rank"]), str(winner["label"])
    if getattr(item, "source", None) == "task":
        # Tasks the user has on their plate triage by work_status so
        # the inbox label reflects what the task actually needs:
        # review-stage tasks read "review needed", paused tasks read
        # "paused", blocked tasks read "blocked by deps". Without this
        # the operator sees "task assigned" for every task regardless
        # of state — they can't tell from the inbox whether the task
        # needs action now or is just sitting in their lane.
        status_obj = getattr(item, "work_status", None)
        status = str(getattr(status_obj, "value", status_obj) or "").lower()
        if status == "review":
            return "action", 1, "review needed"
        if status == "on_hold":
            return "info", 2, "paused"
        if status == "blocked":
            return "info", 2, "blocked by deps"
        return "action", 1, "task assigned"
    return "info", 2, "update"


def annotate_inbox_entry(
    item: InboxEntry,
    *,
    known_projects: set[str],
) -> InboxEntry:
    """Attach triage metadata used by the inbox UI."""
    bucket, rank, label = _triage_for_entry(item, known_projects=known_projects)
    item.triage_bucket = bucket
    item.triage_rank = rank
    item.triage_label = label
    item.is_orphaned = bucket == "orphaned"
    item.needs_action = bucket == "action"
    return item


def task_to_inbox_entry(task, *, db_path: Path | None) -> InboxEntry:
    """Wrap a work-service task in the common inbox-item surface."""
    return InboxEntry(
        raw=task,
        source="task",
        task_id=task.task_id,
        message_id=None,
        message_type=None,
        tier=None,
        state="open",
        sender=getattr(task, "sender", None),
        project=getattr(task, "project", "") or "",
        title=getattr(task, "title", "") or "",
        description=getattr(task, "description", "") or "",
        created_at=getattr(task, "created_at", None),
        updated_at=getattr(task, "updated_at", None),
        priority=getattr(task, "priority", None),
        labels=list(getattr(task, "labels", []) or []),
        roles=getattr(task, "roles", {}) or {},
        created_by=getattr(task, "created_by", "") or "",
        payload={},
        recipient="user",
        scope=getattr(task, "project", "") or "",
        db_path=db_path,
    )


def message_row_to_inbox_entry(
    row: dict[str, Any], *, source_key: str, db_path: Path,
) -> InboxEntry:
    """Project one Store message row into the common inbox-item surface."""
    payload = row.get("payload") or {}
    scope = (row.get("scope") or "").strip()
    project = (
        payload.get("project")
        or scope
        or ("inbox" if source_key == "__workspace__" else source_key)
    )
    tier = row.get("tier") or "immediate"
    message_type = row.get("type") or "notify"
    priority = "high" if tier == "immediate" and message_type == "alert" else "normal"
    return InboxEntry(
        source="message",
        task_id=message_item_id(source_key, row.get("id")),
        message_id=row.get("id"),
        message_type=message_type,
        tier=tier,
        state=row.get("state") or "open",
        sender=row.get("sender") or "polly",
        project=project,
        title=row.get("subject") or "(no subject)",
        description=row.get("body") or "",
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at") or row.get("created_at"),
        priority=priority,
        labels=list(row.get("labels", []) or []),
        roles={},
        created_by=row.get("sender") or "polly",
        payload=payload,
        recipient=row.get("recipient") or "",
        scope=scope,
        db_path=db_path,
    )


def is_task_inbox_entry(item) -> bool:
    """True when the inbox item is backed by a work-service task row."""
    return getattr(item, "source", "task") == "task"


def _entry_sort_value(item: InboxEntry) -> str:
    """Best-effort timestamp string for ordering two inbox entries."""
    for attr in ("updated_at", "created_at"):
        value = getattr(item, attr, None)
        if value is None:
            continue
        if hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except Exception:  # noqa: BLE001
                continue
        return str(value)
    return ""


def _dedupe_replayed_plan_reviews(items: list[InboxEntry]) -> list[InboxEntry]:
    """Collapse re-fired plan-review notifications.

    Architects that resend "[Action] Plan ready for review: <project>" on
    every retry — same plan, same labels, same recipient — pile up in
    the inbox as duplicate rows that all point at the same plan task.
    For the user, only the most recent matters: there is one plan to
    look at, and the older notifications add no information.

    Items not labelled ``plan_review`` or missing a ``plan_task:<ref>``
    label pass through untouched, since we can't safely identify them
    as duplicates of any other entry.
    """
    keep: dict[tuple[str, str], InboxEntry] = {}
    drop_ids: set[str] = set()
    for item in items:
        labels = {str(lbl) for lbl in (getattr(item, "labels", []) or [])}
        if "plan_review" not in labels:
            continue
        plan_task = ""
        for label in labels:
            if label.startswith("plan_task:"):
                plan_task = label.split(":", 1)[1].strip()
                break
        if not plan_task:
            continue
        project = str(getattr(item, "project", "") or "")
        key = (project, plan_task)
        existing = keep.get(key)
        if existing is None:
            keep[key] = item
            continue
        if _entry_sort_value(item) > _entry_sort_value(existing):
            drop_ids.add(str(getattr(existing, "task_id", "") or ""))
            keep[key] = item
        else:
            drop_ids.add(str(getattr(item, "task_id", "") or ""))
    if not drop_ids:
        return items
    return [
        item for item in items
        if str(getattr(item, "task_id", "") or "") not in drop_ids
    ]


def load_inbox_entries(
    config,
    *,
    session_read_ids: set[str] | None = None,
) -> tuple[list[InboxEntry], set[str], dict[str, list]]:
    """Load Store-backed messages and task-backed inbox items together."""
    session_read_ids = session_read_ids or set()
    items: list[InboxEntry] = []
    unread: set[str] = set()
    replies_by_task: dict[str, list] = {}
    seen_task_ids: set[str] = set()
    known_projects = set(getattr(config, "projects", {}).keys())
    for project_key, db_path, project_path in _inbox_db_sources(config):
        if not db_path.exists():
            continue
        source_key = project_key or "__workspace__"
        try:
            store = SQLAlchemyStore(f"sqlite:///{db_path}")
        except Exception:  # noqa: BLE001
            store = None
        if store is not None:
            try:
                try:
                    rows = store.query_messages(
                        recipient="user",
                        state="open",
                        type=["notify", "inbox_task", "alert"],
                    )
                except Exception:  # noqa: BLE001
                    rows = []
                for row in rows:
                    if _row_is_dev_channel(row.get("labels")):
                        continue
                    item = annotate_inbox_entry(
                        message_row_to_inbox_entry(
                            row,
                            source_key=source_key,
                            db_path=db_path,
                        ),
                        known_projects=known_projects,
                    )
                    items.append(item)
                    if item.task_id not in session_read_ids:
                        unread.add(item.task_id)
            finally:
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    pass
        try:
            svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
        except Exception:  # noqa: BLE001
            continue
        try:
            try:
                project_tasks = inbox_tasks(svc, project=project_key)
            except Exception:  # noqa: BLE001
                project_tasks = []
            for task in project_tasks:
                if task.task_id in seen_task_ids:
                    continue
                seen_task_ids.add(task.task_id)
                items.append(
                    annotate_inbox_entry(
                        task_to_inbox_entry(task, db_path=db_path),
                        known_projects=known_projects,
                    )
                )
                try:
                    rows = svc.get_context(task.task_id, entry_type="read", limit=1)
                except Exception:  # noqa: BLE001
                    rows = []
                if not rows:
                    unread.add(task.task_id)
                try:
                    replies = svc.list_replies(task.task_id)
                except Exception:  # noqa: BLE001
                    replies = []
                if replies:
                    replies_by_task[task.task_id] = replies
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
    items = _dedupe_replayed_plan_reviews(items)
    return items, unread, replies_by_task
