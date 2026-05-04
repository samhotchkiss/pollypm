"""Identify today's top priorities across all tracked projects.

Returns a :class:`PriorityList`:

* ``top_tasks`` — the N tasks most worth the user's attention now,
  ordered by ``(priority desc, stale-in-current-state desc)``.
* ``blockers`` — tasks in the ``blocked`` state, with blocker refs.
* ``awaiting_approval`` — inbox items with kinds in
  ``{advisor_insight, downtime_result, plan_approval}`` that have sat
  open for more than 24 hours.

Each per-project query is one SELECT (no per-task round trips). The
helper runs in <2 s for ≤10 projects in the benchmark suite.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from pollypm.models import KnownProject
from pollypm.plugins_builtin.morning_briefing.handlers.gather_yesterday import (
    iter_tracked_projects,
    project_state_db_paths,
)


logger = logging.getLogger(__name__)


# Status buckets that are eligible for "top priority" surfacing.
_OPEN_STATUSES = ("queued", "in_progress", "review", "blocked")

# Priority ordering. "critical" > "high" > "normal" > "low".
_PRIORITY_RANK = {"critical": 3, "high": 2, "normal": 1, "low": 0}

_INBOX_AWAITING_KINDS = frozenset({
    "advisor_insight", "downtime_result", "plan_approval",
})


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PriorityEntry:
    project: str
    task_id: str             # ``project/task_number``
    title: str
    priority: str            # lowercase name — "critical" etc.
    state: str
    assignee: str
    age_seconds: float       # time since last state change (or creation)


@dataclass(slots=True)
class BlockerEntry:
    project: str
    task_id: str
    title: str
    blocked_by: list[str] = field(default_factory=list)   # task_ids
    unresolved_blockers: list[str] = field(default_factory=list)


@dataclass(slots=True)
class InboxItemSummary:
    id: str
    subject: str
    kind: str
    owner: str
    opened_at: str           # ISO
    age_hours: float


@dataclass(slots=True)
class PriorityList:
    top_tasks: list[PriorityEntry] = field(default_factory=list)
    blockers: list[BlockerEntry] = field(default_factory=list)
    awaiting_approval: list[InboxItemSummary] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.top_tasks or self.blockers or self.awaiting_approval)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt


def _age_seconds(row: Mapping[str, Any], *, now_utc: datetime) -> float:
    """Time since the task last changed state (or was created)."""
    # Prefer updated_at (indexed), fall back to created_at.
    ref = _parse_iso(row["updated_at"]) or _parse_iso(row["created_at"])
    if ref is None:
        return 0.0
    return max(0.0, (now_utc - ref).total_seconds())


# ---------------------------------------------------------------------------
# Top-N tasks
# ---------------------------------------------------------------------------


def _gather_top_tasks_for_project(
    project: KnownProject,
    *,
    now_utc: datetime,
    limit: int,
    config=None,
) -> list[PriorityEntry]:
    """Query the top candidates from one project's state.db.

    We pull more rows than the eventual cap so the global sort across
    projects has enough to rank from. ``limit * 2`` is the safety margin.

    Tries per-project then workspace-root state.db (post-#339), filtering
    by ``project = project.key`` so a shared workspace DB only surfaces
    this project's rows. Duplicate ``(project, task_number)`` pairs from
    overlapping DBs are deduped (per-project DB wins on ordering).
    """
    from pollypm.storage.morning_briefing_queries import priority_task_rows

    rows = priority_task_rows(
        project_state_db_paths(project, config),
        project_key=project.key,
        open_statuses=_OPEN_STATUSES,
        limit=limit,
    )
    return [
        PriorityEntry(
            project=str(row["project"] or ""),
            task_id=f"{row['project']}/{row['task_number']}",
            title=str(row["title"] or ""),
            priority=str(row["priority"] or "normal").lower(),
            state=str(row["work_status"] or ""),
            assignee=str(row["assignee"] or ""),
            age_seconds=_age_seconds(row, now_utc=now_utc),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Blockers
# ---------------------------------------------------------------------------


def _gather_blockers_for_project(
    project: KnownProject,
    *,
    config=None,
) -> list[BlockerEntry]:
    """Tasks in ``blocked`` state with their blocker links.

    Tries per-project then workspace-root state.db (post-#339), filtering
    by ``project = project.key`` so a shared workspace DB only surfaces
    this project's blocked tasks. Duplicate ``(project, task_number)``
    pairs from overlapping DBs are deduped.
    """
    from pollypm.storage.morning_briefing_queries import blocker_rows

    rows = blocker_rows(project_state_db_paths(project, config), project_key=project.key)
    return [
        BlockerEntry(
            project=str(row["project"] or ""),
            task_id=f"{row['project']}/{row['task_number']}",
            title=str(row["title"] or ""),
            blocked_by=list(row.get("blocked_by") or []),
            unresolved_blockers=list(row.get("unresolved_blockers") or []),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Awaiting-approval inbox items
# ---------------------------------------------------------------------------


def _iter_inbox_message_states(project_root: Path):
    """Yield ``(msg_id, state_dict)`` for each inbox v2 message.

    Safe against missing directory / corrupt state.json.
    """
    root = project_root / ".pollypm" / "inbox" / "messages"
    if not root.exists():
        return
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        state_file = entry / "state.json"
        if not state_file.exists():
            continue
        try:
            data = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        yield entry.name, data


def _infer_inbox_kind(state: dict) -> str:
    """Best-effort derivation of a message 'kind' for filtering.

    Inbox v2 state doesn't carry an explicit ``kind`` today (see #218
    notes). We infer from the ``subject`` / ``labels`` / ``sender``
    fields — approximate but sufficient for the three buckets the
    briefing cares about.
    """
    raw_kind = state.get("kind")
    if isinstance(raw_kind, str) and raw_kind.strip():
        return raw_kind.strip()
    subject = str(state.get("subject") or "").lower()
    sender = str(state.get("sender") or "").lower()
    if "advisor" in sender or "advisor" in subject:
        return "advisor_insight"
    if "downtime" in sender or "downtime" in subject:
        return "downtime_result"
    if "plan" in subject and ("approv" in subject or "review" in subject):
        return "plan_approval"
    return ""


def _gather_awaiting_approval(
    project_root: Path,
    *,
    now_utc: datetime,
    min_age_hours: float = 24.0,
) -> list[InboxItemSummary]:
    """Inbox v2 items in the awaiting-approval buckets aged >= ``min_age_hours``."""
    out: list[InboxItemSummary] = []
    for msg_id, state in _iter_inbox_message_states(project_root):
        if state.get("status") not in (None, "", "open", "waiting"):
            continue
        kind = _infer_inbox_kind(state)
        if kind not in _INBOX_AWAITING_KINDS:
            continue
        created = _parse_iso(str(state.get("created_at") or ""))
        if created is None:
            continue
        age_hours = (now_utc - created).total_seconds() / 3600.0
        if age_hours < min_age_hours:
            continue
        out.append(
            InboxItemSummary(
                id=msg_id,
                subject=str(state.get("subject") or ""),
                kind=kind,
                owner=str(state.get("owner") or ""),
                opened_at=str(state.get("created_at") or ""),
                age_hours=age_hours,
            )
        )
    # Oldest first — they've been waiting longest.
    out.sort(key=lambda item: item.age_hours, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def identify_priorities(
    config,
    *,
    now_local: datetime,
    priorities_count: int = 5,
    project_root: Path | None = None,
) -> PriorityList:
    """Build today's priority list across every tracked project."""
    if now_local.tzinfo is None:
        raise ValueError("now_local must be timezone-aware")
    now_utc = now_local.astimezone(ZoneInfo("UTC"))

    candidates: list[PriorityEntry] = []
    blockers: list[BlockerEntry] = []
    for project in iter_tracked_projects(config):
        try:
            candidates.extend(
                _gather_top_tasks_for_project(
                    project, now_utc=now_utc, limit=priorities_count,
                    config=config,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("briefing: top-tasks failed for %s: %s", project.key, exc)
        try:
            blockers.extend(_gather_blockers_for_project(project, config=config))
        except Exception as exc:  # noqa: BLE001
            logger.debug("briefing: blockers failed for %s: %s", project.key, exc)

    # Global sort: priority desc, then age_seconds desc (older = more stale).
    def _sort_key(p: PriorityEntry) -> tuple[int, float]:
        return (_PRIORITY_RANK.get(p.priority, 1), p.age_seconds)

    candidates.sort(key=_sort_key, reverse=True)
    top_tasks = candidates[: max(1, priorities_count)]

    root = Path(project_root) if project_root is not None else Path(config.project.root_dir)
    awaiting = _gather_awaiting_approval(root, now_utc=now_utc)

    return PriorityList(
        top_tasks=top_tasks,
        blockers=blockers,
        awaiting_approval=awaiting,
    )
