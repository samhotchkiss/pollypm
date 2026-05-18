"""Per-project categorization + operator dashboard view model (#1572).

This module is the single source of truth for "what is this project
doing right now?" Both surfaces of the epic — the operator dashboard
app and the cockpit rail glyph — read from :func:`categorize_project`
so the rail and the dashboard can never paint a project into
different buckets (the exact split-brain that motivated #1564).

Four categories, priority-ordered:

* :attr:`ProjectState.WAITING` — at least one inbox item with
  ``awaits_user(item) == True``. The user owes a decision.
* :attr:`ProjectState.WORKING` — at least one live worker session or
  an in-flight task (``in_progress`` / ``review``), and no waiting
  item. The system is acting.
* :attr:`ProjectState.IDLE` — neither of the above. The project is
  quiet.
* :attr:`ProjectState.PAUSED` — the project is opted out
  (``tracked=False``). Paused projects with waiting items still
  surface as :attr:`WAITING`; the per-project tracked-filter
  asymmetry (cycle 87, ``feedback_pollypm_testing_priorities``) is
  honoured by the caller building the project list, not by this
  function.

The module is a leaf in the import graph: it depends only on the
canonical inbox predicate, the ``InboxItemKind`` enum, and the
structural shape of the work service. No cockpit, no Supervisor, no
sqlite3. That keeps it importable from every surface (rail, dashboard
app, CLI, tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Protocol

from pollypm.inbox import awaits_user
from pollypm.inbox.kind import InboxItemKind, coerce_kind


class ProjectState(Enum):
    """Mutually-exclusive operator-facing project category."""

    WAITING = "waiting"
    WORKING = "working"
    IDLE = "idle"
    PAUSED = "paused"


# Display glyphs. Pinned at module scope so both the dashboard and the
# rail import the same constant — if the spec ever ships new symbols
# the change lands here and propagates to both surfaces.
_GLYPHS: dict[ProjectState, str] = {
    ProjectState.WAITING: "◆",   # ◆
    ProjectState.WORKING: "●",   # ●
    ProjectState.IDLE:    "○",   # ○
    ProjectState.PAUSED:  "⏸",   # ⏸
}


def glyph_for_project_state(state: ProjectState) -> str:
    """Return the display glyph for ``state``.

    Imported by both ``cockpit_rail`` (project row indicator) and the
    operator dashboard app (per-row glyph). Co-locating the table here
    is the load-bearing invariant the issue calls out: the section a
    project lands in MUST match the glyph the rail draws for it.
    """
    return _GLYPHS[state]


# ---------------------------------------------------------------------------
# Inputs — structural shapes
# ---------------------------------------------------------------------------


class _InboxItem(Protocol):
    """Anything with a ``kind`` and a project-key field.

    Matches :class:`pollypm.cockpit_inbox_items.InboxEntry` plus any
    test double. ``project`` is the canonical project key; ``scope``
    is the legacy alias so message-row entries (which carry ``scope``)
    work without an adapter.
    """

    kind: object


class _Task(Protocol):
    """Work-service task shape — only the fields the categorizer reads."""

    work_status: object
    project: str


class _WorkerSession(Protocol):
    """Worker-session row shape — ``task_project`` + activity timestamps."""

    task_project: str


class _WorkServiceLike(Protocol):
    """The narrow slice of ``WorkService`` the categorizer needs.

    Both :class:`pollypm.work.sqlite_service.SQLiteWorkService` and
    :class:`pollypm.work.mock_service.MockWorkService` satisfy this
    shape, so tests can pass either implementation. Method signatures
    mirror the public protocol — adding methods here would tighten the
    coupling without buying anything.
    """

    def list_tasks(self, *, project: str | None = None, **kwargs: object) -> list: ...

    def list_worker_sessions(
        self, *, project: str | None = None, active_only: bool = True,
    ) -> list: ...


# Task statuses that indicate active automated work for the WORKING
# category. Tasks parked on user-waiting statuses
# (``waiting_on_user`` / ``on_hold`` / ``blocked`` / ``review``) do
# NOT count as working — they're either user-waiting (covered by the
# inbox predicate) or stalled (which is not "system is acting").
_WORKING_TASK_STATUSES: frozenset[str] = frozenset({"in_progress", "rework"})


def _project_of(item: _InboxItem) -> str:
    """Best-effort project key for an inbox item."""
    project = str(getattr(item, "project", "") or "").strip()
    if project and project != "inbox":
        return project
    scope = str(getattr(item, "scope", "") or "").strip()
    if scope and scope != "inbox":
        return scope
    return ""


def _task_status(task: object) -> str:
    value = getattr(task, "work_status", getattr(task, "status", ""))
    return str(getattr(value, "value", value) or "")


def categorize_project(
    project_key: str,
    *,
    work_service: _WorkServiceLike,
    inbox_items: Iterable[_InboxItem] = (),
    tracked: bool = True,
) -> ProjectState:
    """Return the single user-facing category for ``project_key``.

    Priority order, top to bottom:

    1. If ANY inbox item for the project has ``awaits_user(item) == True``,
       the project is :attr:`ProjectState.WAITING`. Waiting wins over
       Working — a project with a live worker AND a user-waiting item
       reads as Waiting because the user is the next-action owner.
    2. If a live worker session is bound to a task in the project, OR
       a task is in an active automated status
       (:attr:`WorkStatus.IN_PROGRESS` / :attr:`WorkStatus.REWORK`),
       the project is :attr:`ProjectState.WORKING`.
    3. If ``tracked`` is ``False`` AND nothing waiting AND nothing
       working, the project is :attr:`ProjectState.PAUSED`.
    4. Otherwise :attr:`ProjectState.IDLE`.

    ``tracked`` defaults to ``True`` so a caller that doesn't know the
    project's tracked-state (e.g. a test) gets the "normal" path; pass
    the real flag from the project config to honour paused-project
    semantics.
    """
    waiting_items = [item for item in inbox_items if awaits_user(item)]
    project_waiting_items = [
        item for item in waiting_items if _project_of(item) == project_key
    ]
    if project_waiting_items:
        return ProjectState.WAITING

    try:
        live_workers = work_service.list_worker_sessions(
            project=project_key, active_only=True,
        )
    except Exception:  # noqa: BLE001
        live_workers = []
    if live_workers:
        return ProjectState.WORKING

    try:
        tasks = work_service.list_tasks(project=project_key)
    except Exception:  # noqa: BLE001
        tasks = []
    for task in tasks:
        if _task_status(task) in _WORKING_TASK_STATUSES:
            return ProjectState.WORKING

    if not tracked:
        return ProjectState.PAUSED
    return ProjectState.IDLE


# ---------------------------------------------------------------------------
# why_waiting — per-row "why" derivation for the Waiting section
# ---------------------------------------------------------------------------


# Highest priority first. ``plan_review_pending`` reads as the most
# urgent because a stuck plan blocks every downstream task; approval
# requests come next (user explicitly asked); watchdog escalations
# above PM questions because they represent the watchdog cascade
# escalating up; manual_decision is the lowest because it's a generic
# bucket. LEGACY rows that the predicate treats as awaits_user fall
# through to the legacy copy.
_KIND_PRIORITY: tuple[InboxItemKind, ...] = (
    InboxItemKind.PLAN_REVIEW_PENDING,
    InboxItemKind.APPROVAL_REQUEST,
    InboxItemKind.WATCHDOG_OPERATOR_DISPATCH,
    InboxItemKind.PM_QUESTION_UNANSWERED,
    InboxItemKind.MANUAL_DECISION,
)

_KIND_COPY: dict[InboxItemKind, str] = {
    InboxItemKind.PLAN_REVIEW_PENDING: "Needs your review of the new plan",
    InboxItemKind.APPROVAL_REQUEST: "Needs your approval",
    InboxItemKind.PM_QUESTION_UNANSWERED: "PM is waiting on your reply",
    InboxItemKind.MANUAL_DECISION: "Polly needs your decision",
}

_LEGACY_COPY = "Needs your attention"


def _item_subject(item: _InboxItem) -> str:
    title = str(getattr(item, "title", "") or "").strip()
    if title:
        return title
    return str(getattr(item, "subject", "") or "").strip()


def why_waiting(items: Iterable[_InboxItem]) -> str:
    """Return a one-line "why this project is waiting" string.

    Picks the highest-priority item per :data:`_KIND_PRIORITY` and
    maps its kind to the user-facing copy from the issue spec. A
    watchdog dispatch interpolates the item subject ("Watchdog
    escalated: <subject>") because the subject IS the dispatch; every
    other kind has a static copy because the kind alone is enough to
    explain what the user needs to do.

    Empty input or no recognised kinds returns the legacy fallback so
    the dashboard row always renders a useful line.
    """
    candidates = [
        (item, coerce_kind(getattr(item, "kind", None)))
        for item in items
    ]
    if not candidates:
        return _LEGACY_COPY

    by_priority: dict[InboxItemKind, _InboxItem] = {}
    for item, kind in candidates:
        if kind in by_priority:
            continue
        by_priority[kind] = item

    for kind in _KIND_PRIORITY:
        if kind not in by_priority:
            continue
        if kind is InboxItemKind.WATCHDOG_OPERATOR_DISPATCH:
            subject = _item_subject(by_priority[kind])
            if subject:
                return f"Watchdog escalated: {subject}"
            return "Watchdog escalation"
        return _KIND_COPY[kind]

    return _LEGACY_COPY


# ---------------------------------------------------------------------------
# what_working — per-row "what's it doing" derivation
# ---------------------------------------------------------------------------


_WORKING_LINE_CAP = 80


def _started_at_sort_key(record: object) -> str:
    """Most-recent worker session wins ties — sort by ``started_at`` desc."""
    return str(getattr(record, "started_at", "") or "")


def _task_title(task: object) -> str:
    return str(getattr(task, "title", "") or "").strip()


def _truncate(text: str, *, cap: int = _WORKING_LINE_CAP) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 1].rstrip() + "…"


def what_working(
    project_key: str,
    *,
    work_service: _WorkServiceLike,
) -> str:
    """Return a one-line "what's this project doing" string.

    Initial form per the issue: ``<worker_name>: <task title>``. When
    multiple workers are live the most-recently-started wins. When no
    worker is live but a task is in an automated status the line is
    ``<status>: <task title>`` so the dashboard still says something
    useful instead of going blank. Falls back to ``"Active"`` when
    neither query yields a name.

    Output is capped at :data:`_WORKING_LINE_CAP` so the terminal
    render never wraps mid-name.
    """
    try:
        workers = list(work_service.list_worker_sessions(
            project=project_key, active_only=True,
        ))
    except Exception:  # noqa: BLE001
        workers = []

    if workers:
        workers.sort(key=_started_at_sort_key, reverse=True)
        latest = workers[0]
        agent = str(getattr(latest, "agent_name", "") or "worker").strip() or "worker"
        task_number = getattr(latest, "task_number", None)
        title = ""
        if task_number is not None:
            try:
                task = work_service.get(f"{project_key}/{task_number}")
                title = _task_title(task)
            except Exception:  # noqa: BLE001
                title = ""
        if title:
            return _truncate(f"{agent}: {title}")
        return _truncate(agent)

    try:
        tasks = list(work_service.list_tasks(project=project_key))
    except Exception:  # noqa: BLE001
        tasks = []
    for task in tasks:
        status = _task_status(task)
        if status in _WORKING_TASK_STATUSES:
            title = _task_title(task)
            label = status.replace("_", " ")
            if title:
                return _truncate(f"{label}: {title}")
            return _truncate(label)
    return "Active"


# ---------------------------------------------------------------------------
# View model — what the dashboard renders
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperatorDashboardRow:
    """One row in the operator dashboard.

    ``detail`` is the per-section one-liner: "why waiting" for
    Waiting rows, "what working" for Working rows, last-activity
    string for Idle rows.
    """

    project_key: str
    state: ProjectState
    glyph: str
    detail: str
    last_activity: str = ""


@dataclass(frozen=True, slots=True)
class OperatorDashboardView:
    """All three sections, computed in a single pass."""

    waiting: tuple[OperatorDashboardRow, ...] = field(default_factory=tuple)
    working: tuple[OperatorDashboardRow, ...] = field(default_factory=tuple)
    idle: tuple[OperatorDashboardRow, ...] = field(default_factory=tuple)
    paused: tuple[OperatorDashboardRow, ...] = field(default_factory=tuple)


def _format_last_activity(value: object) -> str:
    if not value:
        return ""
    text = str(value)
    if "T" in text:
        return text.split("T", 1)[0]
    return text


def _project_last_activity(
    project_key: str, *, work_service: _WorkServiceLike,
) -> str:
    try:
        tasks = list(work_service.list_tasks(project=project_key))
    except Exception:  # noqa: BLE001
        tasks = []
    best = ""
    for task in tasks:
        for attr in ("updated_at", "created_at"):
            value = getattr(task, attr, None)
            if value is None:
                continue
            text = str(value)
            if text > best:
                best = text
                break
    return _format_last_activity(best)


def build_operator_dashboard_view(
    project_keys: Iterable[str],
    *,
    work_service: _WorkServiceLike,
    inbox_items_by_project: dict[str, list[_InboxItem]] | None = None,
    tracked_by_project: dict[str, bool] | None = None,
) -> OperatorDashboardView:
    """Categorize every project and pack the rows into the view model.

    A single pass over ``project_keys`` so a project lands in exactly
    one section. Sorting:

    * Waiting: alphabetical by project key (deterministic; the
      "highest-priority kind" within a project drives the row copy).
    * Working: alphabetical.
    * Idle: most-recent activity first (per the issue spec).
    * Paused: alphabetical.

    ``inbox_items_by_project`` and ``tracked_by_project`` default to
    empty so a caller that doesn't have those signals still gets a
    sensible (Idle/Working) categorization.
    """
    inbox_by_project = inbox_items_by_project or {}
    tracked_lookup = tracked_by_project or {}

    waiting: list[OperatorDashboardRow] = []
    working: list[OperatorDashboardRow] = []
    idle: list[OperatorDashboardRow] = []
    paused: list[OperatorDashboardRow] = []

    for project_key in project_keys:
        items = inbox_by_project.get(project_key, [])
        tracked = tracked_lookup.get(project_key, True)
        state = categorize_project(
            project_key,
            work_service=work_service,
            inbox_items=items,
            tracked=tracked,
        )
        glyph = glyph_for_project_state(state)
        if state is ProjectState.WAITING:
            waiting.append(
                OperatorDashboardRow(
                    project_key=project_key,
                    state=state,
                    glyph=glyph,
                    detail=why_waiting(items),
                )
            )
        elif state is ProjectState.WORKING:
            working.append(
                OperatorDashboardRow(
                    project_key=project_key,
                    state=state,
                    glyph=glyph,
                    detail=what_working(project_key, work_service=work_service),
                )
            )
        elif state is ProjectState.PAUSED:
            paused.append(
                OperatorDashboardRow(
                    project_key=project_key,
                    state=state,
                    glyph=glyph,
                    detail="Paused",
                    last_activity=_project_last_activity(
                        project_key, work_service=work_service,
                    ),
                )
            )
        else:
            last = _project_last_activity(project_key, work_service=work_service)
            detail = f"Last activity {last}" if last else "Quiet"
            idle.append(
                OperatorDashboardRow(
                    project_key=project_key,
                    state=state,
                    glyph=glyph,
                    detail=detail,
                    last_activity=last,
                )
            )

    waiting.sort(key=lambda r: r.project_key.lower())
    working.sort(key=lambda r: r.project_key.lower())
    idle.sort(key=lambda r: (r.last_activity, r.project_key.lower()), reverse=True)
    paused.sort(key=lambda r: r.project_key.lower())

    return OperatorDashboardView(
        waiting=tuple(waiting),
        working=tuple(working),
        idle=tuple(idle),
        paused=tuple(paused),
    )
