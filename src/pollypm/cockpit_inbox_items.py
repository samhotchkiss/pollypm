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

import logging
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
from pollypm.work.inbox_plan_reviews import (
    PLAN_APPROVAL_NODE_ID,
    approved_plan_review_refs,
    is_plan_task_approved,
    task_user_approval_is_approved,
)
from pollypm.work.sqlite_service import SQLiteWorkService

logger = logging.getLogger(__name__)


# Plan-review notify rows reference the underlying user_approval task
# via ``plan_task:<project>/<number>``. Once that task's
# ``user_approval`` node-run is COMPLETED + APPROVED, the inbox row is
# phantom action — the approval already happened (#1103). The proper
# fix is an event listener that archives the row on approval; until
# that lands, filter at render time so the user's drain-action doesn't
# hit dead items. Imported as a constant so tests can pin the node id
# without depending on private flow internals.
_PLAN_APPROVAL_NODE_ID = PLAN_APPROVAL_NODE_ID


_MARKDOWN_DECORATION_RE = re.compile(r"[*_`#>\[\]]+")

# A subject that begins with ``Digest:`` (or e.g. ``Action Digest:``,
# ``FYI Digest:`` after _plain_text strips the canonical [Tag]
# bracket) is a roll-up by definition. Match against the lowercased
# title — the optional leading word covers the dropped bracket tag.
_DIGEST_SUBJECT_RE = re.compile(
    r"^\s*(?:[A-Za-z]+\s+)?digest\b\s*[:—–-]",
    re.IGNORECASE,
)

# System-health / inbox-machinery anomalies emitted by the
# heartbeat or by Polly's auditor (``Misrouted review ping``,
# ``Repeated stale review ping``, ``Stale planner tasks``,
# ``Review requested for missing task``, ``Second bogus review
# ping``). Producers should route these to ``--requester polly``
# (operator triage), but until they all migrate, demote them
# defensively at read time so they don't pile up in the user's
# action lens. The patterns are deliberately conservative:
# anchor at the start of the title (post-bracket-strip) and only
# match phrases that name the system surface, not user decisions
# that happen to mention "stale" or "missing" in the body.
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
        # Labelled "needs unblock" rather than "blocked" — when the
        # triage bucket is "action", the user *is* the unblock, so
        # "Action Required · blocked" reads contradictory in the
        # right-pane banner. "Needs unblock" tells the user that
        # they're the unblock without that mismatch.
        "label": "needs unblock",
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
    if _OPS_ANOMALY_SUBJECT_RE.search(title_lower):
        return "info", 2, "operations alert"
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
        description=str(row.get("body") or "").replace("\\n", "\n"),
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


def _dedupe_message_vs_task_plan_reviews(
    items: list[InboxEntry],
) -> list[InboxEntry]:
    """Collapse a notify-message + task-entry pair for the same plan.

    Two streams feed the inbox: notify messages produced by the
    operator/architect's ``pm notify`` calls, and task-backed entries
    surfaced from the work-service for any task at ``user_approval``
    on the ``plan_project`` flow. Both surface the booktalk plan as a
    distinct row — Sam (2026-04-26) saw them as ``Plan ready for
    review: booktalk`` (message) and ``Plan project booktalk`` (task)
    side by side, indistinguishable noise pointing at the same action.

    The notify message carries the operator's user-friendly copy and
    (eventually) the structured user_prompt payload, so it's the
    higher-fidelity surface — drop the bare task row when a message
    with ``plan_task:<task_id>`` already covers it.
    """
    # Build refs to plan tasks already covered by a richer handoff row.
    message_covered_task_ids: dict[str, set[str]] = {}
    for item in items:
        labels = {str(lbl) for lbl in (getattr(item, "labels", []) or [])}
        if "plan_review" not in labels:
            continue
        # Task-backed notify rows can also carry ``plan_task:<id>``.
        # Track who provides the coverage so a self-referential row
        # never dedupes itself out of the rendered inbox.
        for label in labels:
            if label.startswith("plan_task:"):
                ref = label.split(":", 1)[1].strip()
                if ref:
                    coverer_id = str(getattr(item, "task_id", "") or "")
                    message_covered_task_ids.setdefault(ref, set()).add(coverer_id)
                break
    if not message_covered_task_ids:
        return items
    kept: list[InboxEntry] = []
    for item in items:
        # Only drop task-based entries when a different row covers the
        # same plan task — message rows and self-referential task rows
        # remain visible.
        if is_task_inbox_entry(item):
            task_id = str(getattr(item, "task_id", "") or "")
            coverers = message_covered_task_ids.get(task_id, set())
            if any(coverer_id != task_id for coverer_id in coverers):
                continue
        kept.append(item)
    return kept


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


def _plan_task_ref(item: InboxEntry) -> str:
    """Return the ``plan_task:<project/number>`` ref payload, or ``""``."""
    labels = list(getattr(item, "labels", []) or [])
    for label in labels:
        text = str(label)
        if text.startswith("plan_task:"):
            return text.split(":", 1)[1].strip()
    return ""


def _task_user_approval_is_approved(task) -> bool:
    """Return True when ``task``'s user_approval node-run is APPROVED.

    Inspects the task's already-loaded ``executions`` list (no DB
    fetch) for the canonical ``user_approval`` node and reports whether
    the latest completed visit was APPROVED. Latest-wins matches the
    binding-decision semantics used elsewhere. Returns False on any
    structural surprise — fail-open so a transient anomaly never
    silently hides a real action-needed row.
    """
    return task_user_approval_is_approved(task)


def _is_plan_task_approved(svc, project: str, task_number: int) -> bool:
    """Return True when ``project/task_number`` has user_approval = APPROVED.

    Walks the task's executions for the canonical ``user_approval``
    node, takes the latest completed visit, and reports whether its
    decision is APPROVED. Latest-wins matches the binding-decision
    semantics already used by ``plan_presence._find_approved_plan_task``.
    Returns False on any lookup failure — fail-open so a transient DB
    error never silently hides a real action-needed row.
    """
    return is_plan_task_approved(svc, project, task_number)


# Sentinel key for the workspace-root state.db inside ``project_db_paths``.
# The root db is the fallback DB for any project whose own
# ``<project_path>/.pollypm/state.db`` doesn't exist on disk — in many
# real workspaces every project shares the workspace-root db, so the
# per-project ``plan_task:<project/number>`` ref must resolve there
# (#1103 follow-up).
_WORKSPACE_DB_KEY = "__workspace__"


def _filter_approved_plan_reviews(
    items: list[InboxEntry],
    *,
    project_db_paths: dict[str, tuple[Path, Path]],
) -> list[InboxEntry]:
    """Drop ``plan_review`` rows whose user_approval is already APPROVED.

    Per-render filter — the proper fix is an event listener that
    archives the inbox row on approval-completion (#1103). Until that
    lands, this keeps stale "Plan ready for review" rows from sitting
    in the action lens for days after the approval has happened at the
    work-service layer.

    Logs the number of phantom rows filtered so we can measure inbox
    cleanup and decide when the proper sweeper is needed.

    Task-backed plan-review rows are checked against their loaded task
    state directly. Message-backed rows still resolve their
    ``plan_task:<project/number>`` labels through the DB map.

    DB resolution: ``project_db_paths`` may carry both per-project
    entries (``project_key -> (db, project_path)``) AND a workspace-root
    entry under the ``__workspace__`` sentinel key. When a plan_review's
    referenced project has no per-project DB, the workspace-root DB is
    consulted as a fallback — most users keep all task state in the
    workspace-root db rather than per-project ones (#1103 tick-5
    follow-up: filter previously found zero phantoms because the
    smoketest ref's project had no per-project db).
    """
    if not items or not project_db_paths:
        return items
    workspace_db = project_db_paths.get(_WORKSPACE_DB_KEY)
    # Group plan_task refs by the DB we'll consult so each db's svc is
    # opened at most once per render. ``db_key`` is either the project
    # key (when a per-project db exists) or ``__workspace__`` (fallback).
    refs_by_db: dict[str, set[tuple[str, int]]] = {}
    considered = 0
    refs_unparsed = 0
    has_task_backed_plan_reviews = False
    for item in items:
        labels = {str(lbl) for lbl in (getattr(item, "labels", []) or [])}
        if "plan_review" not in labels:
            continue
        considered += 1
        if getattr(item, "source", None) == "task":
            has_task_backed_plan_reviews = True
            continue
        ref = _plan_task_ref(item)
        if not ref or "/" not in ref:
            refs_unparsed += 1
            continue
        project, _, number_text = ref.partition("/")
        try:
            number = int(number_text)
        except (TypeError, ValueError):
            refs_unparsed += 1
            continue
        if project in project_db_paths:
            db_key = project
        elif workspace_db is not None:
            db_key = _WORKSPACE_DB_KEY
        else:
            continue
        refs_by_db.setdefault(db_key, set()).add((project, number))
    if not refs_by_db:
        if considered:
            # Log even when no refs resolved, so a producer-side
            # regression (plan_review rows missing the plan_task label)
            # is visible in cockpit_debug.log instead of silently
            # piling phantoms in the action lens.
            logger.info(
                "inbox: plan_review filter considered %d row(s), dropped 0 "
                "(no resolvable plan_task refs), refs_unparsed=%d",
                considered,
                refs_unparsed,
            )
        if not has_task_backed_plan_reviews:
            return items
        approved_refs = set()
    else:
        approved_refs = approved_plan_review_refs(
            refs_by_db=refs_by_db,
            project_db_paths=project_db_paths,
            service_factory=SQLiteWorkService,
        )
    kept: list[InboxEntry] = []
    dropped = 0
    for item in items:
        labels = {str(lbl) for lbl in (getattr(item, "labels", []) or [])}
        if "plan_review" in labels:
            if getattr(item, "source", None) == "task":
                if _task_user_approval_is_approved(item):
                    dropped += 1
                    continue
                kept.append(item)
                continue
            ref = _plan_task_ref(item)
            if ref and ref in approved_refs:
                dropped += 1
                continue
        kept.append(item)
    # Always log "considered N, dropped M" when we examined any rows so a
    # future regression (filter wired but nothing dropped) is obvious in
    # the cockpit debug log instead of silently surfacing phantom rows.
    if considered:
        logger.info(
            "inbox: plan_review filter considered %d row(s), dropped %d "
            "(user_approval APPROVED) across %d ref(s), refs_unparsed=%d",
            considered,
            dropped,
            len(approved_refs),
            refs_unparsed,
        )
    return kept


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
    # ``plan_task:<project/number>`` refs may point at a project other
    # than the one that hosts the notify message — collect db paths for
    # every project so the approval filter can resolve any ref. The
    # workspace-root db lands under the ``__workspace__`` sentinel so
    # the filter can fall back to it when a referenced project has no
    # per-project state.db (#1103 follow-up).
    project_db_paths: dict[str, tuple[Path, Path]] = {}
    for project_key, db_path, project_path in _inbox_db_sources(config):
        if not db_path.exists():
            continue
        source_key = project_key or _WORKSPACE_DB_KEY
        if project_key:
            project_db_paths[project_key] = (db_path, project_path)
        else:
            project_db_paths[_WORKSPACE_DB_KEY] = (db_path, project_path)
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
            # N+1 escape: pre-fetch read-markers and replies for every
            # task in this project in one query each, then bucket
            # locally. Was 2 roundtrips per task on the 8s refresh tick.
            project_for_query = project_key if project_key else "inbox"
            try:
                read_marker_numbers = svc.task_numbers_with_context_entry(
                    project=project_for_query, entry_type="read",
                )
            except Exception:  # noqa: BLE001
                read_marker_numbers = set()
            try:
                replies_by_number = svc.bulk_list_replies(project=project_for_query)
            except Exception:  # noqa: BLE001
                replies_by_number = {}
            for task in project_tasks:
                if task.task_id in seen_task_ids:
                    continue
                seen_task_ids.add(task.task_id)
                # Synth-source filter for plan_review (#1103, 4th
                # attempt). Check the task's own user_approval
                # execution directly: if it's COMPLETED + APPROVED,
                # the plan review is already done and emitting the row
                # would be a phantom action.
                task_labels = {str(lbl) for lbl in (getattr(task, "labels", []) or [])}
                if "plan_review" in task_labels:
                    emit = not _task_user_approval_is_approved(task)
                    logger.warning(
                        "PLAN_REVIEW_SYNTH project=%s task_id=%s emit=%s "
                        "reason=%s",
                        getattr(task, "project", "") or "",
                        task.task_id,
                        emit,
                        "user_approval_pending" if emit else "user_approval_approved",
                    )
                    if not emit:
                        continue
                items.append(
                    annotate_inbox_entry(
                        task_to_inbox_entry(task, db_path=db_path),
                        known_projects=known_projects,
                    )
                )
                if task.task_number not in read_marker_numbers:
                    unread.add(task.task_id)
                replies = replies_by_number.get(task.task_number, [])
                if replies:
                    replies_by_task[task.task_id] = replies
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
    items = _dedupe_replayed_plan_reviews(items)
    items = _dedupe_message_vs_task_plan_reviews(items)
    items = _filter_approved_plan_reviews(
        items, project_db_paths=project_db_paths,
    )
    return items, unread, replies_by_task


def load_inbox_action_preview(
    config,
    *,
    project: str | None = None,
    limit: int = 12,
) -> tuple[list[InboxEntry], set[str], int]:
    """Load a small Store-backed action preview for first Inbox paint.

    The interactive Inbox still does the full ``load_inbox_entries`` pass.
    This helper is only for the cockpit-pane prepaint path, where showing the
    first action rows quickly matters more than reply counts or task-backed
    rows that will arrive with the Textual app's normal refresh.
    """
    preview_limit = max(int(limit), 1)
    rows_per_source = max(preview_limit * 4, 48)
    known_projects = set(getattr(config, "projects", {}).keys())
    items: list[InboxEntry] = []

    for project_key, db_path, _project_path in _inbox_db_sources(config):
        if not db_path.exists():
            continue
        try:
            store = SQLAlchemyStore(f"sqlite:///{db_path}")
        except Exception:  # noqa: BLE001
            continue
        try:
            try:
                rows = store.query_messages(
                    recipient="user",
                    state="open",
                    type=["notify", "inbox_task", "alert"],
                    limit=rows_per_source,
                )
            except Exception:  # noqa: BLE001
                rows = []
            for row in rows:
                if _row_is_dev_channel(row.get("labels")):
                    continue
                item = annotate_inbox_entry(
                    message_row_to_inbox_entry(
                        row,
                        source_key=project_key or _WORKSPACE_DB_KEY,
                        db_path=db_path,
                    ),
                    known_projects=known_projects,
                )
                if project and (getattr(item, "project", "") or "") != project:
                    continue
                if getattr(item, "is_orphaned", False):
                    continue
                if not getattr(item, "needs_action", False):
                    continue
                items.append(item)
        finally:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass

    if not items:
        return [], set(), 0

    items = _dedupe_replayed_plan_reviews(items)
    items.sort(key=_entry_sort_value, reverse=True)
    preview = items[:preview_limit]
    unread = {item.task_id for item in preview}
    return preview, unread, len(items)
