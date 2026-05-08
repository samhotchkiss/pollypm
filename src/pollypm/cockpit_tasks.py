from __future__ import annotations

import difflib
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from rich.syntax import Syntax
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static, TabbedContent, TabPane

from pollypm.approval_notifications import notify_task_approved
from pollypm.cockpit_interaction import (
    ActionKind,
    CockpitBinding,
    FocusKind,
    REGISTRY as _COCKPIT_INTERACTION_REGISTRY,
    ScreenContract,
    cancel_arming,
    destructive_action_safe,
)
from pollypm.cockpit_task_priority import (
    priority_glyph,
    priority_label,
    priority_rank,
)
from pollypm.cockpit_task_review import (
    ReviewDiffBundle,
    collapse_review_diff_file,
    extract_confidence_score,
    load_task_review_diff,
    load_task_review_artifact,
    render_task_review_artifact,
    review_diff_hidden_line_count,
)
from pollypm.cockpit_formatting import format_event_time
from pollypm.cockpit_formatting import format_relative_age as _format_relative_age
from pollypm.config import load_config, project_config_path
from pollypm.plugins_builtin.project_planning.plan_presence import (
    plan_approval_task,
    plan_blocked_task_ids,
)
from pollypm.plugins_builtin.task_assignment_notify.api import (
    # #939: pull the sweeper-ping window + context-entry type from the
    # plugin's public surface, not its handler module. Lets the plugin
    # refactor ``handlers.sweep`` without breaking cockpit rendering.
    RECENT_SWEEPER_PING_SECONDS,
    SWEEPER_PING_CONTEXT_ENTRY_TYPE,
)
from pollypm.notify_task import is_notify_inbox_task
from pollypm.rejection_feedback import (
    RejectionFeedbackNotice,
    is_rejection_feedback_task,
    unread_rejection_feedback,
)
from pollypm.session_services import create_tmux_client
from pollypm.task_review_summary import PLAIN_SUMMARY_ENTRY_TYPE, REVIEW_SUMMARY_ACTOR
from pollypm.tz import format_time as _fmt_time

_TASK_STATUS_ORDER = {
    "in_progress": 0,
    "review": 1,
    "waiting_on_plan": 2,
    "queued": 2,
    "blocked": 3,
    "on_hold": 4,
    "draft": 5,
    "done": 6,
    "cancelled": 7,
}

_FILTER_LABELS = {
    "all": "All",
    "active": "Active",
    "review": "Review",
    "blocked": "Blocked",
    "done": "Done",
}


def _project_label_and_aliases(
    config: object, project_key: str,
) -> tuple[str, list[str]]:
    """Return ``(display_label, [storage_aliases])`` for a project key.

    The work-service stores tasks under the project's display name
    (``blackjack-trainer``) but the cockpit receives the slugified
    config key (``blackjack_trainer``). ``list_tasks(project=...)`` is
    an exact match, so a Tasks pane that only queries with the config
    key silently shows zero rows for projects whose key and name
    differ — issue #920. Build the alias list once so both the label
    and the queries stay in sync.
    """
    project = (getattr(config, "projects", None) or {}).get(project_key)
    aliases: list[str] = []

    def _add(value: object) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text or text in aliases:
            return
        aliases.append(text)

    _add(project_key)
    if project is not None:
        _add(getattr(project, "name", None))
        path = getattr(project, "path", None)
        if path is not None:
            try:
                _add(Path(path).name)
            except TypeError:
                pass
    _add(project_key.replace("_", "-"))
    _add(project_key.replace("-", "_"))

    label = aliases[0]
    if project is not None:
        candidate = (
            project.display_label()
            if hasattr(project, "display_label")
            else (getattr(project, "name", None) or project_key)
        )
        if isinstance(candidate, str) and candidate.strip():
            label = candidate
    return label, aliases


def _dedupe_tasks_by_id(task_lists: list[list]) -> list:
    """Concatenate ``list_tasks`` results, deduping by ``task_id``."""
    seen: set[str] = set()
    out: list = []
    for tasks in task_lists:
        for task in tasks:
            tid = getattr(task, "task_id", None)
            if not tid or tid in seen:
                continue
            seen.add(tid)
            out.append(task)
    return out

_PENDING_UNDO_SECONDS = 5.0


@dataclass(slots=True)
class _PendingReviewAction:
    task_ids: tuple[str, ...]
    task_numbers: tuple[int, ...]
    decision: str
    reason: str | None
    deadline: float


class _TaskLiveScroll(VerticalScroll):
    """Live-pane scroll container that pauses tailing when the user scrolls up."""

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        app = getattr(self, "app", None)
        if not isinstance(app, PollyTasksApp):
            return
        if new_value >= self.max_scroll_y:
            app._set_live_tail_paused(False)
        elif new_value < old_value:
            app._set_live_tail_paused(True)

    def action_scroll_up(self) -> None:
        self.scroll_up(animate=False, force=True)
        app = getattr(self, "app", None)
        if isinstance(app, PollyTasksApp):
            app._set_live_tail_paused(True)

    @on(events.MouseScrollUp)
    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        self.action_scroll_up()


def _execution_completed_output_lines(execution) -> list[str]:
    work_output = getattr(execution, "work_output", None)
    if work_output is None:
        return []
    lines = [f"Summary: {getattr(work_output, 'summary', '') or '(empty)'}"]
    for artifact in getattr(work_output, "artifacts", []) or []:
        kind = getattr(getattr(artifact, "kind", None), "value", None) or getattr(
            artifact, "kind", None
        )
        bits = [str(kind or "artifact")]
        description = getattr(artifact, "description", None)
        if description:
            bits.append(str(description))
        ref = getattr(artifact, "ref", None)
        if ref:
            bits.append(f"ref={ref}")
        path = getattr(artifact, "path", None)
        if path:
            bits.append(f"path={path}")
        external_ref = getattr(artifact, "external_ref", None)
        if external_ref:
            bits.append(f"external={external_ref}")
        lines.append(" | ".join(bits))
    return lines


def _execution_snapshot_label(execution) -> str:
    node_id = getattr(execution, "node_id", "submission")
    visit = getattr(execution, "visit", "?")
    completed_at = getattr(execution, "completed_at", None) or getattr(
        execution, "started_at", None
    )
    if completed_at is None:
        return f"{node_id} v{visit}"
    return f"{node_id} v{visit} · {_format_event_time(completed_at)}"


def _review_submission_executions(task) -> list:
    submissions: list = []
    for execution in list(getattr(task, "executions", None) or []):
        if getattr(execution, "work_output", None) is not None:
            submissions.append(execution)
    submissions.sort(
        key=lambda execution: (
            _timestamp_sort_value(
                getattr(execution, "completed_at", None)
                or getattr(execution, "started_at", None)
            ),
            int(getattr(execution, "visit", 0) or 0),
        )
    )
    return submissions


def _render_review_submission_diff(task) -> str:
    submissions = _review_submission_executions(task)
    if len(submissions) < 2:
        return "No resubmission diff yet."
    before = submissions[-2]
    after = submissions[-1]
    before_lines = _execution_completed_output_lines(before)
    after_lines = _execution_completed_output_lines(after)
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=_execution_snapshot_label(before),
            tofile=_execution_snapshot_label(after),
            lineterm="",
        )
    )
    if not diff_lines:
        return "No changes between the latest two submissions."
    rendered = ["[b]Resubmission Diff[/b]"]
    for line in diff_lines:
        if line.startswith(("---", "+++")):
            rendered.append(f"[dim]{line}[/dim]")
        elif line.startswith("@@"):
            rendered.append(f"[yellow]{line}[/yellow]")
        elif line.startswith("+"):
            rendered.append(f"[green]{line}[/green]")
        elif line.startswith("-"):
            rendered.append(f"[red]{line}[/red]")
        else:
            rendered.append(line)
    return "\n".join(rendered)


def _task_has_resubmission_diff(task) -> bool:
    submissions = _review_submission_executions(task)
    if len(submissions) < 2:
        return False
    return any(
        getattr(getattr(execution, "decision", None), "value", None) == "rejected"
        for execution in getattr(task, "executions", None) or []
    )


def _review_confidence_score(task, review_artifact) -> int | None:
    if review_artifact is not None and getattr(review_artifact, "confidence", None) is not None:
        return int(review_artifact.confidence)
    executions = list(getattr(task, "executions", None) or [])
    executions.sort(
        key=lambda execution: (
            _timestamp_sort_value(
                getattr(execution, "completed_at", None)
                or getattr(execution, "started_at", None)
            ),
            int(getattr(execution, "visit", 0) or 0),
        ),
        reverse=True,
    )
    for execution in executions:
        score = extract_confidence_score(getattr(execution, "decision_reason", None))
        if score is not None:
            return score
        work_output = getattr(execution, "work_output", None)
        if work_output is None:
            continue
        score = extract_confidence_score(getattr(work_output, "summary", None))
        if score is not None:
            return score
        for artifact in getattr(work_output, "artifacts", []) or []:
            score = extract_confidence_score(getattr(artifact, "description", None))
            if score is not None:
                return score
    return None


def _review_confidence_markup(score: int | None) -> str:
    if score is None:
        return ""
    if score >= 8:
        style = "black on #2f9e44"
    elif score >= 5:
        style = "black on #f4b942"
    else:
        style = "black on #d94841"
    return f"[{style}] Russell: {score}/10 [/] "


def _format_event_time(value) -> str:
    return format_event_time(value, formatter=_fmt_time)


def _timestamp_sort_value(value) -> float:
    """Sortable timestamp helper tolerant of missing / malformed inputs."""
    if not value:
        return 0.0
    try:
        if hasattr(value, "timestamp"):
            return float(value.timestamp())
        from datetime import datetime as _dt

        return float(_dt.fromisoformat(str(value)).timestamp())
    except Exception:  # noqa: BLE001
        return 0.0


def _task_status_value(task) -> str:
    return getattr(getattr(task, "work_status", None), "value", None) or str(
        getattr(task, "work_status", "") or ""
    )


_STATUS_DISPLAY_LABELS = {
    "autoreview": "Auto review",
    "blocked": "Blocked",
    "cancelled": "Cancelled",
    "done": "Done",
    "draft": "Draft",
    "in_progress": "In progress",
    "on_hold": "On hold",
    "queued": "Queued",
    "review": "In review",
    "rework": "Rework needed",
    "user-review": "Needs review",
    "waiting_on_plan": "Waiting on plan",
}


def _display_label(value: object, *, fallback: str = "—") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    label = text.replace("_", " ").replace("-", " ")
    return label[:1].upper() + label[1:]


def _status_display_label(status: str) -> str:
    return _STATUS_DISPLAY_LABELS.get(status, _display_label(status))


def _task_sort_key(
    task,
    *,
    owner: str | None = None,
    plan_blocked: bool = False,
) -> tuple[int, int, int, float, int]:
    """Sort tasks by status, review owner, priority, then newest activity."""
    status = "waiting_on_plan" if plan_blocked else _task_status_value(task)
    updated = getattr(task, "updated_at", None) or getattr(task, "created_at", None)
    review_owner_rank = 0 if status != "review" or owner == "human" else 1
    return (
        _TASK_STATUS_ORDER.get(status, 99),
        review_owner_rank,
        priority_rank(task),
        -_timestamp_sort_value(updated),
        int(getattr(task, "task_number", 0) or 0),
    )


def _format_stage_label(task, flow) -> str:
    node_id = getattr(task, "current_node_id", None)
    if not node_id:
        return "—"
    if flow is None:
        return _display_label(node_id)
    node = getattr(flow, "nodes", {}).get(node_id)
    if node is None:
        return _display_label(node_id)
    parts = [_display_label(getattr(node, "name", None) or node_id)]
    node_type = getattr(getattr(node, "type", None), "value", None) or getattr(
        node, "type", None
    )
    if node_type:
        parts.append(_display_label(node_type))
    actor = (
        getattr(node, "actor_role", None)
        or getattr(node, "agent_name", None)
        or getattr(getattr(node, "actor_type", None), "value", None)
        or getattr(node, "actor_type", None)
    )
    if actor:
        parts.append(_display_label(actor))
    return " · ".join(parts)


def _peek_session_tail(pane_id: str | None) -> list[str]:
    if not pane_id:
        return []
    try:
        pane_text = create_tmux_client().capture_pane(pane_id, lines=12)
    except Exception:  # noqa: BLE001
        return []
    lines = [line.rstrip() for line in pane_text.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-8:]


def _project_auto_merge_on_approve_enabled(project_path: Path | None) -> bool:
    """Read the per-project auto-merge flag, defaulting on for review flow."""

    if project_path is None:
        return True
    config_file = project_config_path(project_path)
    if not config_file.exists():
        return True
    try:
        raw = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return True
    for section_name in ("project", "cockpit", "task_ui"):
        section = raw.get(section_name)
        if isinstance(section, dict) and "auto_merge_on_approve" in section:
            return bool(section["auto_merge_on_approve"])
    return True


def _task_pr_number(task) -> str | None:
    refs = getattr(task, "external_refs", {}) or {}
    for key in ("github_pr", "github_pr_number", "pull_request", "pr_number"):
        value = refs.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if not text:
            continue
        if text.startswith("http"):
            text = text.rstrip("/").rsplit("/", 1)[-1]
        if text.isdigit():
            return text
    return None


def _task_counts(
    tasks: list,
    *,
    plan_blocked_task_ids: set[str] | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    plan_blocked_task_ids = plan_blocked_task_ids or set()
    for task in tasks:
        status = (
            "waiting_on_plan"
            if task.task_id in plan_blocked_task_ids
            else _task_status_value(task)
        )
        counts[status] = counts.get(status, 0) + 1
    return counts


def _task_matches_status(
    task,
    status_filter: str,
    *,
    plan_blocked: bool = False,
) -> bool:
    status = "waiting_on_plan" if plan_blocked else _task_status_value(task)
    if status_filter == "all":
        return True
    if status_filter == "active":
        return status not in {"done", "cancelled"}
    if status_filter == "review":
        return status == "review"
    if status_filter == "blocked":
        return status in {"blocked", "on_hold", "waiting_on_plan"}
    if status_filter == "done":
        return status in {"done", "cancelled"}
    return True


def _task_matches_query(task, owner: str | None, query: str) -> bool:
    query = (query or "").strip().lower()
    if not query:
        return True
    haystack = " ".join(
        [
            getattr(task, "task_id", "") or "",
            getattr(task, "title", "") or "",
            getattr(task, "description", "") or "",
            getattr(task, "assignee", "") or "",
            owner or "",
            getattr(task, "current_node_id", "") or "",
            getattr(task, "flow_template_id", "") or "",
        ]
    ).lower()
    return query in haystack


def _plain_english_summary(task) -> str | None:
    """Return a plain-English takeaway for ``task`` if one exists.

    Stored as a ``plain_summary``-typed context entry (same mechanism
    as ``plan_approved`` — see ``work_context_entries.entry_type``).
    When the architect's emit step writes one, it shows up at the top
    of every view of the task: cockpit overview, inbox, pm task get.
    Returns None when no summary has been stored.
    """
    status = _task_status_value(task)
    candidates = []
    for index, entry in enumerate(getattr(task, "context", None) or []):
        if getattr(entry, "entry_type", "") != PLAIN_SUMMARY_ENTRY_TYPE:
            continue
        if (
            getattr(entry, "actor", "") == REVIEW_SUMMARY_ACTOR
            and status not in {"review", "on_hold"}
        ):
            continue
        text = getattr(entry, "text", "") or ""
        if text.strip():
            candidates.append((getattr(entry, "timestamp", None), index, text.strip()))
    candidates.sort(key=lambda item: (_summary_timestamp_key(item[0]), item[1]), reverse=True)
    if candidates:
        return candidates[0][2]
    return None


def _summary_timestamp_key(value: object | None) -> float:
    if value is None:
        return 0.0
    try:
        if hasattr(value, "timestamp"):
            return float(value.timestamp())
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _plain_line(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def _clip_plain_text(text: str, *, limit: int = 220) -> str:
    clean = _plain_line(text)
    if len(clean) <= limit:
        return clean
    cut = clean.rfind(" ", 0, limit)
    if cut <= 0 or cut < limit - 40:
        cut = limit - 1
    return clean[:cut].rstrip() + "…"


def _review_plain_english_fallback(
    task,
    *,
    status_label: str,
    owner: str | None,
    rejection_feedback: RejectionFeedbackNotice | None,
) -> str | None:
    """Generate a short top-of-pane summary when no stored summary exists.

    Review/on-hold tasks are exactly where the user needs a plain-language
    first read. If the worker/reviewer did not persist a `plain_summary`
    context entry, synthesize the minimum useful summary from current state
    and any linked rejection-feedback artifact.
    """
    if rejection_feedback is not None:
        if status_label not in {"review", "user-review", "on_hold"}:
            return (
                "This task has unread reviewer feedback in the inbox.\n"
                "The current owner may still be reworking it. Use the Inbox "
                "Feedback section below for context."
            )
        return (
            "Review needed: the plain-English approval summary has not been "
            "generated yet.\n"
            "Use the Review tab and Inbox Feedback below to decide whether "
            "the current submission matches what you want. Approve only if it "
            "does; otherwise reject with the specific change needed."
        )

    if status_label == "user-review":
        if getattr(task, "flow_template_id", "") == "plan_project":
            return (
                "Review needed: the project plan is waiting for your "
                "approval. Read the Review tab, then approve it or reject "
                "with the changes you need.\n"
                "What to do: approve if the plan is ready to become work; "
                "otherwise reject with the concrete changes needed."
            )
        return (
            "Review needed: this task is waiting for your approval. Check "
            "the Review tab or artifact, then approve it or reject with the "
            "changes you need.\n"
            "What to do: approve if the submitted work meets the task; "
            "otherwise reject with the concrete changes needed."
        )

    if status_label == "review":
        return (
            "Review needed: this task is parked in review. Decide whether "
            "the submitted work is acceptable or send it back with specific "
            "changes."
        )

    if status_label == "on_hold":
        return (
            "Action needed: this task is on hold. Read the blocker below, "
            "then decide whether to resume it, approve it, or send it back."
        )

    if status_label == "autoreview":
        reviewer = owner or "the reviewer"
        return (
            f"Review in progress: {reviewer} is checking this task. You do "
            "not need to act unless it is rejected or asks for approval."
        )

    return None


def _status_label(
    task,
    owner: str | None,
    *,
    plan_blocked: bool = False,
) -> str:
    """Render a human-meaningful status chip.

    The DB stores a single ``review`` status regardless of whether the
    human or Russell owes the next action. The cockpit splits them —
    ``user-review`` means the task is parked at a human-actor node and
    is waiting on you; ``autoreview`` means Russell's queue. User
    feedback 2026-04-23: "we need to differentiate between waiting on
    feedback from russell and from human."
    """
    if plan_blocked:
        return "waiting_on_plan"
    status = task.work_status.value
    if status != "review":
        return status
    if (owner or "") == "human":
        return "user-review"
    return "autoreview"


def _recent_sweeper_ping(task) -> bool:
    status = _task_status_value(task)
    if status not in {"queued", "review", "in_progress"}:
        return False
    now = datetime.now(timezone.utc)
    for entry in reversed(list(getattr(task, "context", None) or [])):
        if getattr(entry, "entry_type", "") != SWEEPER_PING_CONTEXT_ENTRY_TYPE:
            continue
        stamp = getattr(entry, "timestamp", None)
        if stamp is None:
            return False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (now - stamp).total_seconds() <= RECENT_SWEEPER_PING_SECONDS
    return False


def _rejection_feedback_label(
    rejection_feedback: RejectionFeedbackNotice,
    *,
    include_inbox_task_id: bool = True,
) -> str:
    label = "Unread rejection feedback"
    if include_inbox_task_id:
        label += f" ({rejection_feedback.inbox_task_id})"
    return label


def _render_overview(
    task,
    *,
    owner: str | None,
    flow,
    active_session,
    rejection_feedback: RejectionFeedbackNotice | None = None,
    task_index: dict[str, "object"] | None = None,
    plan_blocked: bool = False,
    plan_approval_task_id: str | None = None,
    recently_pinged: bool = False,
) -> str:
    status_label = _status_label(task, owner, plan_blocked=plan_blocked)
    icon = PollyTasksApp._STATUS_ICONS.get(status_label, "·")
    lines = [
        f"{icon} #{task.task_number} {priority_glyph(task)} {task.title}",
    ]
    # Recovery action (#1016) — when this task is stuck, the FIRST
    # thing the detail view shows is "here is exactly what to do
    # next", BEFORE the plain-English summary or status block. The
    # original bug (#1016) was that the detail view buried the next
    # step under prose; this hoists it.
    try:
        from pollypm.recovery_actions import (
            recovery_action_for,
            render_recovery_action_block,
        )
        recovery = recovery_action_for(task)
    except Exception:  # noqa: BLE001
        recovery = None
    if recovery is not None:
        lines.append("")
        lines.extend(render_recovery_action_block(recovery))
    # Plain-English takeaway first — before any of the technical
    # metadata — so a human reviewer doesn't have to decode the
    # description to know what's actually happening.
    summary = _plain_english_summary(task) or _review_plain_english_fallback(
        task,
        status_label=status_label,
        owner=owner,
        rejection_feedback=rejection_feedback,
    )
    if summary:
        lines.extend([
            "",
            "In plain English",
            "",
            summary,
        ])
    lines.extend([
        "",
        f"Status     {_status_display_label(status_label)}",
        f"Priority   {priority_label(task)}",
        f"Flow       {task.flow_template_id}",
        f"Stage      {_format_stage_label(task, flow)}",
        f"Owner      {owner or '—'}",
    ])
    if recently_pinged:
        lines.append("Sweeper    pinged in the last minute")
    if plan_blocked:
        lines.extend([
            "",
            "Plan Gate",
            "",
            "Waiting   approve the project plan before worker pickup",
        ])
        if plan_approval_task_id:
            lines.append(
                f"Approve    {plan_approval_task_id} · plan_project/user_approval"
            )
        else:
            lines.append("Approve    start or finish the plan_project flow")
    if task.assignee:
        lines.append(f"Assignee   {task.assignee}")
    if task.roles:
        roles = ", ".join(f"{key}={value}" for key, value in task.roles.items())
        lines.append(f"Roles      {roles}")
    tokens_in = getattr(task, "total_input_tokens", 0) or 0
    tokens_out = getattr(task, "total_output_tokens", 0) or 0
    session_count = getattr(task, "session_count", 0) or 0
    if tokens_in or tokens_out or session_count:
        lines.append(
            f"Tokens     in={tokens_in}  out={tokens_out}  sessions={session_count}"
        )
    updated = _format_event_time(getattr(task, "updated_at", None))
    updated_rel = _format_relative_age(getattr(task, "updated_at", None))
    if updated:
        line = f"Updated    {updated}"
        if updated_rel:
            line += f" · {updated_rel}"
        lines.append(line)
    if active_session is not None:
        lines.append(f"Session    {active_session.agent_name}")
    if rejection_feedback is not None:
        lines.extend(
            [
                "",
                "Inbox Feedback",
                "",
                f"Artifact   {_rejection_feedback_label(rejection_feedback)}",
                f"Preview    {rejection_feedback.preview}",
            ]
        )
    # User-reported gap (morning 2026-04-23): blocked tasks didn't
    # surface WHY they were blocked. Render blocked_by with task
    # titles and statuses so the user can see the dependency chain
    # without running a separate query.
    #
    # #1024 — when the task itself is no longer blocked, the heading
    # "Blocked by" lies (in_progress + done dependencies). Switch the
    # label to "Depends on" and tag resolved edges with a check so the
    # dep graph stays visible without contradicting the status row.
    blocked_by = list(getattr(task, "blocked_by", None) or [])
    if blocked_by:
        own_status = getattr(getattr(task, "work_status", None), "value", "") or ""
        is_currently_blocked = own_status == "blocked"
        resolved_statuses = {"done", "accepted", "cancelled", "canceled"}
        if is_currently_blocked:
            heading = "Blocked by"
        else:
            heading = "Depends on"
        rendered_rows: list[str] = []
        for project_key, number in blocked_by:
            blocker_task_id = f"{project_key}/{number}"
            sibling = (task_index or {}).get(blocker_task_id)
            if sibling is None:
                # Without a sibling lookup we can't tell whether the dep
                # is resolved. When the task isn't blocked, render the
                # row plainly under "Depends on"; when blocked, keep the
                # bare ID under "Blocked by" (best effort).
                rendered_rows.append(f"- {blocker_task_id}")
                continue
            title = getattr(sibling, "title", "") or ""
            status_enum = getattr(sibling, "work_status", None)
            status = getattr(status_enum, "value", "") if status_enum else ""
            is_resolved = status in resolved_statuses
            if is_currently_blocked and is_resolved:
                # The task is blocked but THIS edge is resolved — skip
                # it. "Blocked by" should only list un-resolved edges.
                continue
            if not is_currently_blocked and is_resolved:
                check = "✓ "
            else:
                check = ""
            status_suffix = f" [{status}]" if status else ""
            rendered_rows.append(
                f"- {check}{blocker_task_id} — {title}{status_suffix}"
            )
        if rendered_rows:
            lines.extend(["", heading, ""])
            lines.extend(rendered_rows)
    if task.description:
        lines.extend(["", "Description", "", task.description])
    if task.acceptance_criteria:
        lines.extend(["", "Acceptance Criteria", "", task.acceptance_criteria])
    if task.constraints:
        lines.extend(["", "Constraints", "", task.constraints])
    if task.relevant_files:
        lines.extend(["", "Relevant Files", ""])
        lines.extend(f"- {path}" for path in task.relevant_files)
    return "\n".join(lines)


def _render_context(task) -> str:
    if not getattr(task, "context", None):
        return "No context entries yet."
    lines: list[str] = []
    for entry in task.context:
        if getattr(entry, "entry_type", "") == SWEEPER_PING_CONTEXT_ENTRY_TYPE:
            continue
        ts = _format_event_time(getattr(entry, "timestamp", None))
        age = _format_relative_age(getattr(entry, "timestamp", None))
        meta = [getattr(entry, "actor", "") or "system"]
        if ts:
            meta.append(ts)
        if age:
            meta.append(age)
        lines.append(" · ".join(meta))
        text = getattr(entry, "text", "") or ""
        lines.append(text if text else "(empty)")
        lines.append("")
    if not lines:
        return "No context entries yet."
    return "\n".join(lines).rstrip()


def _render_live(task, active_session) -> str:
    if active_session is None:
        return "No active worker session is currently attached to this task."
    lines = [f"Session    {active_session.agent_name}"]
    if active_session.branch_name:
        lines.append(f"Branch     {active_session.branch_name}")
    if active_session.worktree_path:
        lines.append(f"Worktree   {active_session.worktree_path}")
    started = _format_event_time(active_session.started_at)
    started_rel = _format_relative_age(active_session.started_at)
    if started:
        line = f"Started    {started}"
        if started_rel:
            line += f" · {started_rel}"
        lines.append(line)
    peek_lines = _peek_session_tail(active_session.pane_id)
    lines.extend(["", "Peek", ""])
    if peek_lines:
        lines.extend(peek_lines)
    else:
        lines.append("unavailable")
    transcript_root = Path.home() / ".pollypm"
    transcript_dir = transcript_root / "transcripts" / "tasks" / task.task_id
    if transcript_dir.exists():
        lines.extend(["", "Transcript", "", str(transcript_dir)])
    return "\n".join(lines)


class _TaskRejectFixModal(ModalScreen[str | None]):
    """Follow-up modal for the "write fix instructions" reject path."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    CSS = """
    Screen {
        align: center middle;
        background: rgba(8, 12, 15, 0.65);
    }
    #task-reject-fix-modal {
        width: 72;
        height: auto;
        padding: 1 2;
        border: round #394754;
        background: #111920;
    }
    #task-reject-fix-actions {
        height: auto;
        padding-top: 1;
    }
    #task-reject-fix-actions Button {
        margin-right: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.reason_input = Input(
            placeholder="Write the fix instructions...",
            id="task-reject-fix-reason",
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="task-reject-fix-modal"):
            yield Static("Write Fix Instructions", id="task-reject-fix-title")
            yield Static(
                "Be specific enough that the next pass can move immediately.",
                id="task-reject-fix-copy",
            )
            yield self.reason_input
            with Horizontal(id="task-reject-fix-actions"):
                yield Button("Save", id="task-reject-fix-submit", variant="warning")
                yield Button("Cancel", id="task-reject-fix-cancel")

    def on_mount(self) -> None:
        self.reason_input.focus()

    def on_key(self, event: events.Key) -> None:
        key = event.key
        if key == "1":
            event.stop()
            self.action_pick_tests()
        elif key == "2":
            event.stop()
            self.action_pick_scope()
        elif key == "3":
            event.stop()
            self.action_pick_fix()
        elif key == "4":
            event.stop()
            self.action_pick_other()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#task-reject-fix-reason")
    def _on_submit(self, event: Input.Submitted) -> None:
        reason = (event.value or "").strip()
        self.dismiss(reason or None)

    @on(Button.Pressed, "#task-reject-fix-submit")
    def _on_press_submit(self) -> None:
        reason = (self.reason_input.value or "").strip()
        self.dismiss(reason or None)

    @on(Button.Pressed, "#task-reject-fix-cancel")
    def _on_press_cancel(self) -> None:
        self.dismiss(None)


class _TaskRejectReasonModal(ModalScreen[str | None]):
    """Quick-pick reject modal with a free-text escape hatch."""

    BINDINGS = [
        Binding("1", "pick_tests", "Tests missing or broken", show=False, priority=True),
        Binding("2", "pick_scope", "Scope drift", show=False, priority=True),
        Binding("3", "pick_fix", "Write fix instructions", show=False, priority=True),
        Binding("4", "pick_other", "Other", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False),
    ]
    CSS = """
    Screen {
        align: center middle;
        background: rgba(8, 12, 15, 0.65);
    }
    #task-reject-modal {
        width: 78;
        height: auto;
        padding: 1 2;
        border: round #394754;
        background: #111920;
    }
    #task-reject-reasons {
        height: auto;
        padding: 1 0;
        color: #b7c4d1;
    }
    #task-reject-actions {
        height: auto;
        padding-top: 1;
    }
    #task-reject-actions Button {
        margin-right: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.reason_input = Input(
            placeholder="Type a custom rejection reason, then press Enter",
            id="task-reject-reason",
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="task-reject-modal"):
            yield Static("Reject Review Task", id="task-reject-title")
            yield Static(
                "[1] Tests missing or broken\n"
                "[2] Scope drift\n"
                "[3] Write fix instructions\n"
                "[4] Other (free text)",
                id="task-reject-reasons",
            )
            yield self.reason_input
            with Horizontal(id="task-reject-actions"):
                yield Button("1 Tests", id="task-reject-tests", variant="default")
                yield Button("2 Scope", id="task-reject-scope", variant="default")
                yield Button("3 Fix", id="task-reject-fix", variant="default")
                yield Button("4 Other", id="task-reject-other", variant="default")

    def on_mount(self) -> None:
        self.reason_input.focus()

    def _dismiss_if_current(self, result: str | None) -> None:
        screen_stack = self.app.screen_stack
        if len(screen_stack) <= 1 or screen_stack[-1] is not self:
            return
        self.dismiss(result)

    def action_cancel(self) -> None:
        self._dismiss_if_current(None)

    def action_pick_tests(self) -> None:
        self._dismiss_if_current("Tests missing or broken")

    def action_pick_scope(self) -> None:
        self._dismiss_if_current("Scope drift")

    def action_pick_fix(self) -> None:
        def _after(reason: str | None) -> None:
            self.app.call_after_refresh(
                lambda: self._dismiss_if_current(reason or None)
            )

        self.app.push_screen(_TaskRejectFixModal(), _after)

    def action_pick_other(self) -> None:
        reason = (self.reason_input.value or "").strip()
        if reason:
            self._dismiss_if_current(reason)
            return
        self.reason_input.focus()

    @on(Input.Submitted, "#task-reject-reason")
    def _on_submit(self, event: Input.Submitted) -> None:
        reason = (event.value or "").strip()
        self._dismiss_if_current(reason or None)

    @on(Button.Pressed, "#task-reject-tests")
    def _on_press_tests(self) -> None:
        self.action_pick_tests()

    @on(Button.Pressed, "#task-reject-scope")
    def _on_press_scope(self) -> None:
        self.action_pick_scope()

    @on(Button.Pressed, "#task-reject-fix")
    def _on_press_fix(self) -> None:
        self.action_pick_fix()

    @on(Button.Pressed, "#task-reject-other")
    def _on_press_other(self) -> None:
        self.action_pick_other()


class PollyTasksApp(App[None]):
    """Interactive task board for a single project.

    Contract:
    - Input: a Polly config path and one project key.
    - Data source: one project-local WorkService rooted at that project's
      `.pollypm/state.db`.
    - Output: a split-pane Textual UI with replaceable modules:
      task filtering/table, overview rendering, timeline rendering, context
      rendering, and live-session rendering.
    """

    TITLE = "PollyPM"
    SUB_TITLE = "Tasks"
    BINDINGS = [
        Binding("slash", "focus_search", "Search", show=False),
        Binding("r", "refresh", "Refresh"),
        # #1016 — capital ``R`` runs (or shows) the recovery action for
        # the selected stuck task. We keep lowercase ``r`` on refresh
        # so existing muscle memory survives; ``R`` is unbound elsewhere
        # in this pane.
        Binding("R", "recovery_action", "Recovery"),
        Binding("a", "approve_task", "Approve"),
        Binding("x", "reject_task", "Reject"),
        Binding("c", "clear_filters", "Clear Filters", show=False),
        Binding("space", "toggle_task_selection", "Select", show=False),
        Binding("A", "bulk_approve_selected", "Approve Selected", show=False),
        Binding("X", "bulk_reject_selected", "Reject Selected", show=False),
        Binding("d", "toggle_resubmission_diff", "Review Diff", show=False),
        Binding("z", "undo_pending_review", "Undo", show=False),
        Binding("G", "resume_live_tail", "Resume Live", show=False),
        Binding("end", "resume_live_tail", "Resume Live", show=False),
        Binding("o", "refresh_live", "Refresh Live", show=False),
        Binding("escape", "back", "Back"),
    ]
    CSS = """
    Screen { background: #10161b; color: #eef2f4; }
    #tasks-root { height: 1fr; }
    #tasks-toolbar {
        height: auto;
        padding: 1 1 0 1;
        background: #0d1419;
    }
    #tasks-heading {
        width: 18;
        padding: 1 1 0 0;
        color: #8db7ff;
        text-style: bold;
    }
    #tasks-search {
        width: 1fr;
        height: 1;
        min-height: 1;
        margin: 0 1 0 0;
        padding: 0 1;
        border: none;
        background: #111820;
        color: #d6dee5;
    }
    #tasks-search:focus {
        border: none;
        background: #14202a;
    }
    #tasks-filters {
        width: auto;
        height: auto;
        padding-top: 0;
    }
    #tasks-filters Button {
        margin-left: 1;
        min-width: 8;
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
    }
    #tasks-filter-chips {
        height: auto;
        padding: 0 1 1 1;
        background: #0d1419;
    }
    #tasks-filter-chips Button {
        margin-right: 1;
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
    }
    #tasks-body { height: 1fr; }
    #tasks-list-pane {
        width: 46;
        min-width: 38;
        padding: 0 2 0 1;
        border-right: none;
    }
    #tasks-summary {
        height: auto;
        color: #97a6b2;
        padding: 1 0;
    }
    #tasks-table {
        height: 1fr;
    }
    #tasks-status {
        height: auto;
        color: #6b7a88;
        padding: 1 0;
    }
    #tasks-detail-pane {
        width: 1fr;
        padding: 0 1 0 2;
    }
    #task-header {
        height: auto;
        padding: 1 0 0 0;
        color: #eef6ff;
    }
    #task-actions {
        height: auto;
        padding: 1 0;
    }
    #task-actions Button {
        margin-right: 1;
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
    }
    /* #767 — when a review action is pending undo, the morphed
       button gets a strong success tint so the click target reads
       as "this committed; press again to undo". CSS variable goes
       through the Textual class system; the runtime adds/removes
       the class via _morph_review_action_buttons. */
    #task-actions Button.-undo {
        background: #1f4d2a;
        color: #d7f4dd;
        border: tall #3fb950;
        text-style: bold;
    }
    #task-actions Button.-undo:focus,
    #task-actions Button.-undo:hover {
        background: #266a39;
    }
    #task-review-panel {
        height: 1fr;
    }
    #task-review-confidence-chip {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        border: round #3d4d60;
        background: #18232d;
        color: #cfe1ff;
    }
    #task-review-inline-diff-header {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        border-left: thick #36536b;
        color: #d7e8ff;
    }
    #task-review-inline-diff-expand {
        width: auto;
        margin: 0 0 1 1;
    }
    #task-review-inline-diff {
        height: auto;
        margin-top: 1;
        padding: 0 1;
        border: round #3d4d60;
        background: #0f161d;
        color: #d8e4ef;
    }
    #task-review-diff {
        height: auto;
        margin-top: 1;
        padding: 1 0 0 0;
    }
    #tasks-banner {
        height: 3;
        padding: 0 1;
        color: #0d1419;
        background: #3ddc84;
        text-style: bold;
    }
    #tasks-banner.-reject {
        color: #ffd7d9;
        background: #d93f3a;
    }
    #task-tabs { height: 1fr; }
    #task-tabs ContentTabs {
        height: 1;
    }
    #task-tabs Underline {
        display: none;
    }
    #task-detail-scroll,
    #task-review-scroll,
    #task-context-scroll,
    #task-live-scroll {
        height: 1fr;
        overflow-y: auto;
    }
    #task-live-header {
        height: auto;
        padding: 0 0 1 0;
    }
    #task-live-header-fill {
        width: 1fr;
    }
    #task-live-end-pill {
        width: auto;
        padding: 0 1;
        border: round #3d4d60;
        background: #18232d;
        color: #cfe1ff;
    }
    #task-detail,
    #task-review,
    #task-context,
    #task-live {
        padding: 0 1 1 1;
    }
    #task-timeline {
        height: 1fr;
    }
    """

    _STATUS_ICONS = {
        "draft": "◌",
        "queued": "○",
        "waiting_on_plan": "◇",
        "in_progress": "⟳",
        "blocked": "⊘",
        "on_hold": "⏸",
        "review": "◉",
        "done": "✓",
        "cancelled": "✗",
    }

    def __init__(
        self,
        config_path: Path,
        project_key: str,
        *,
        initial_task_id: str | None = None,
    ) -> None:
        super().__init__()
        self.config_path = config_path
        self.project_key = project_key
        self._initial_task_id = initial_task_id
        self._tasks: list = []
        self._owner_by_task_id: dict[str, str | None] = {}
        self._rejection_feedback_by_task_id: dict[str, RejectionFeedbackNotice] = {}
        self._plan_blocked_task_ids: set[str] = set()
        self._plan_approval_task_id: str | None = None
        self._recent_sweeper_ping_task_ids: set[str] = set()
        self._selected_task_id: str | None = None
        self._status_filter = "active"
        self._search_query = ""
        self.search_input = Input(
            placeholder="Search task, owner, stage, assignee…",
            id="tasks-search",
        )
        self.summary = Static("", id="tasks-summary")
        self.status = Static("", id="tasks-status")
        self.task_table = DataTable(id="tasks-table", zebra_stripes=True)
        self.detail_header = Static("", id="task-header")
        self.detail_overview = Static("", id="task-detail")
        self.detail_review = Static("", id="task-review")
        self.review_confidence = Static("", id="task-review-confidence-chip")
        self.review_inline_diff_header = Static("", id="task-review-inline-diff-header")
        self.review_inline_diff_expand = Button(
            "Expand",
            id="task-review-inline-diff-expand",
            variant="default",
        )
        self.review_inline_diff = Static("", id="task-review-inline-diff")
        self.review_diff_toggle = Button(
            "Diff Since Rejection: Off [D]",
            id="task-review-diff-toggle",
            variant="default",
        )
        self.review_diff = Static("", id="task-review-diff")
        self.detail_context = Static("", id="task-context")
        self.detail_live = Static("", id="task-live")
        self.timeline = DataTable(id="task-timeline", zebra_stripes=True)
        self.filter_chips_status = Button("", id="tasks-chip-status")
        self.filter_chips_search = Button("", id="tasks-chip-search")
        self.filter_chips_clear = Button("×clear", id="tasks-chip-clear-all", variant="default")
        self.filter_buttons = {
            key: Button(label, id=f"tasks-filter-{key}")
            for key, label in _FILTER_LABELS.items()
        }
        self.approve_button = Button("Approve", id="task-approve", variant="success")
        self.reject_button = Button("Reject", id="task-reject", variant="warning")
        self.bulk_approve_button = Button(
            "Approve Selected", id="task-bulk-approve", variant="success"
        )
        self.refresh_live_button = Button("Refresh Live", id="task-refresh-live")
        self.live_tail_fill = Static("", id="task-live-header-fill")
        self.live_tail_pill = Static("[End]", id="task-live-end-pill")
        self.banner = Static("", id="tasks-banner", markup=True)
        self._pending_review_action: _PendingReviewAction | None = None
        self._pending_commit_timer = None
        self._pending_countdown_timer = None
        self._live_tail_paused = False
        self._active_reject_modal: _TaskRejectReasonModal | None = None
        self._selected_task_ids: set[str] = set()
        self._show_resubmission_diff = False
        self._review_inline_diff: ReviewDiffBundle | None = None
        self._review_inline_diff_file_index = 0
        self._review_inline_diff_expanded = False
        self._review_nav_prefix: str | None = None
        # #1017 — within-pane click responsiveness mirrors the rail
        # responsiveness machinery from #974: bump a request id on every
        # click, paint the row highlight + a placeholder header
        # synchronously, and let the slow DB / review-artifact load
        # happen in a worker thread. ``_apply_detail_result`` is gated
        # on this seq so a click during a still-loading prior click
        # cannot get clobbered when the older load finally returns.
        self._detail_request_seq: int = 0
        self._pending_detail_task_id: str | None = None

    def compose(self) -> ComposeResult:
        # #920 — header must use the project's display label
        # (``blackjack-trainer``), not the slugified config key
        # (``blackjack_trainer``), so the rail / CLI / Tasks-pane
        # heading agree on one project name.
        try:
            _config = load_config(self.config_path)
            display_label, _ = _project_label_and_aliases(
                _config, self.project_key,
            )
        except Exception:  # noqa: BLE001
            display_label = self.project_key
        with Vertical(id="tasks-root"):
            with Horizontal(id="tasks-toolbar"):
                yield Static(f"Tasks · {display_label}", id="tasks-heading")
                yield self.search_input
                with Horizontal(id="tasks-filters"):
                    for button in self.filter_buttons.values():
                        yield button
            with Horizontal(id="tasks-filter-chips"):
                yield self.filter_chips_status
                yield self.filter_chips_search
                yield self.filter_chips_clear
            with Horizontal(id="tasks-body"):
                with Vertical(id="tasks-list-pane"):
                    yield self.summary
                    yield self.task_table
                    yield self.status
                with Vertical(id="tasks-detail-pane"):
                    yield self.detail_header
                    with Horizontal(id="task-actions"):
                        yield self.approve_button
                        yield self.reject_button
                        yield self.bulk_approve_button
                        yield self.refresh_live_button
                    with TabbedContent(initial="task-tab-overview", id="task-tabs"):
                        with TabPane("Overview", id="task-tab-overview"):
                            with VerticalScroll(id="task-detail-scroll"):
                                yield self.detail_overview
                        with TabPane("Review", id="task-tab-review"):
                            with Vertical(id="task-review-panel"):
                                yield self.review_confidence
                                yield self.review_inline_diff_header
                                yield self.review_inline_diff_expand
                                yield self.review_diff_toggle
                                with VerticalScroll(id="task-review-scroll"):
                                    yield self.detail_review
                                    yield self.review_inline_diff
                                    yield self.review_diff
                        with TabPane("Timeline", id="task-tab-timeline"):
                            yield self.timeline
                        with TabPane("Context", id="task-tab-context"):
                            with VerticalScroll(id="task-context-scroll"):
                                yield self.detail_context
                        with TabPane("Live", id="task-tab-live"):
                            with Vertical(id="task-live-panel"):
                                with Horizontal(id="task-live-header"):
                                    yield self.live_tail_fill
                                    yield self.live_tail_pill
                                with _TaskLiveScroll(id="task-live-scroll"):
                                    yield self.detail_live
            yield self.banner

    def on_mount(self) -> None:
        self._configure_tables()
        self._sync_filter_buttons()
        self._sync_filter_chips()
        self._sync_live_tail_indicator()
        self._sync_banner()
        self._set_detail_empty("No task selected.")
        self._refresh_list(select_first=True)
        self.set_interval(8, self._background_refresh)
        self.task_table.focus()

    def _configure_tables(self) -> None:
        self.task_table.cursor_type = "row"
        self.task_table.add_columns("ID", "Status", "Title", "Owner", "Stage", "Updated")
        self.timeline.cursor_type = "row"
        self.timeline.add_columns("Stage", "State", "Started", "Completed", "Details")

    def _get_svc(self):
        """Return the work service whose DB actually holds this project's tasks.

        #919 / #920 — tasks may live either in ``<project>/.pollypm/state.db``
        or in ``<workspace>/.pollypm/state.db`` depending on how they
        were created. Pick the first candidate DB that has at least
        one row matching any project alias so detail views (``svc.get``,
        ``svc.get_context``) hit the same DB ``_load_tasks`` listed
        from. Fall back to the project-local DB so the legacy contract
        still holds when neither DB has a row yet.
        """
        from pollypm.work import create_work_service

        try:
            config = load_config(self.config_path)
        except Exception:  # noqa: BLE001
            return None
        _, aliases = _project_label_and_aliases(config, self.project_key)
        candidates = self._candidate_dbs()
        if not candidates:
            return None
        for db_path, project_path in candidates:
            try:
                svc = create_work_service(db_path=db_path, project_path=project_path)
            except Exception:  # noqa: BLE001
                continue
            try:
                for alias in aliases:
                    try:
                        rows = svc.list_tasks(project=alias, limit=1)
                    except Exception:  # noqa: BLE001
                        continue
                    if rows:
                        return svc
            except Exception:  # noqa: BLE001
                pass
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        # No DB has rows yet — fall back to the project-local DB so
        # the rest of the pane still has a working service handle.
        first_db, first_project_path = candidates[0]
        try:
            return create_work_service(
                db_path=first_db, project_path=first_project_path,
            )
        except Exception:  # noqa: BLE001
            return None

    def _candidate_dbs(self) -> list[tuple[Path, Path]]:
        """Return ``[(db_path, project_path)]`` for every DB that may
        hold tasks for this project.

        Mirrors :func:`pollypm.cockpit_ui._dashboard_task_db_paths` so
        the Tasks pane reads the same set of state databases the
        dashboard does. Without this, projects whose tasks live in the
        workspace-root ``.pollypm/state.db`` (the default for tasks
        created via ``pm task ...`` with no project DB seeded) render
        with zero rows even though ``pm task counts`` reports them
        — issue #919/#920.

        Order (#1004): the workspace-root canonical DB is preferred
        first because the work-DB resolver collapsed to workspace-root
        as the single source of truth. The per-project
        ``<project>/.pollypm/state.db`` is retained as a legacy
        fallback so unmigrated projects still render, but workspace
        wins when both DBs hold rows for the project — otherwise the
        Tasks pane (which picks the FIRST candidate with any matching
        rows in :meth:`_get_svc`) latches onto a stale per-project DB
        and shows split-brain results vs. the live worker session.
        """
        config = load_config(self.config_path)
        project = config.projects.get(self.project_key)
        if not project:
            return []
        candidates: list[tuple[Path, Path]] = []
        seen: set[Path] = set()

        def _add(path: object) -> None:
            try:
                candidate = Path(path)
            except TypeError:
                return
            if candidate in seen or not candidate.exists():
                return
            seen.add(candidate)
            candidates.append((candidate, project.path))

        project_settings = getattr(config, "project", None)
        workspace_root = getattr(project_settings, "workspace_root", None)
        if workspace_root is not None:
            _add(Path(workspace_root) / ".pollypm" / "state.db")
        _add(project.path / ".pollypm" / "state.db")
        state_db = getattr(project_settings, "state_db", None)
        if state_db is not None:
            _add(state_db)
        return candidates

    def _open_svcs(self):
        """Yield work-service handles to query for this project's tasks.

        Routes through :meth:`_get_svc` so existing tests that
        monkeypatch ``_get_svc`` to return a fake service continue to
        work. ``_get_svc`` already picks the candidate DB that holds
        the matching rows (project-local first, then workspace-root)
        so a single yielded service usually covers every alias.
        """
        svc = self._get_svc()
        if svc is None:
            return
        yield svc

    def _load_tasks(
        self,
    ) -> tuple[
        list,
        dict[str, str | None],
        dict[str, RejectionFeedbackNotice],
        set[str],
        str | None,
        set[str],
    ]:
        try:
            config = load_config(self.config_path)
        except Exception:  # noqa: BLE001
            return [], {}, {}, set(), None, set()
        _, aliases = _project_label_and_aliases(config, self.project_key)
        # Iterate every candidate DB (project-local + workspace-root)
        # and every alias (config key, display name, hyphen/underscore
        # swap) so the Tasks pane stays in sync with both the dashboard
        # and ``pm task counts``. Without this, tasks created via
        # ``pm task ...`` against the workspace-root state.db never
        # surface for projects whose key/name differ — issue #920.
        services = list(self._open_svcs())
        if not services:
            return [], {}, {}, set(), None, set()
        plan_blocked: set[str] = set()
        approval_task_id: str | None = None
        owners: dict[str, str | None] = {}
        feedback: dict[str, RejectionFeedbackNotice] = {}
        try:
            tasks_by_id: dict[str, object] = {}
            owner_svc: dict[str, object] = {}
            for svc in services:
                for alias in aliases:
                    try:
                        candidates = svc.list_tasks(project=alias)
                    except Exception:  # noqa: BLE001
                        continue
                    for candidate in candidates:
                        if is_rejection_feedback_task(candidate):
                            continue
                        # ``pm notify --priority immediate`` materialises a
                        # ``chat``-flow task so the cockpit inbox can render
                        # the architect's plan_review (and other operator
                        # announcements) with structured action affordances.
                        # That row has no node-level transition the user
                        # can act on from the Tasks pane, so we filter it
                        # out here and keep it inbox-only (#1003).
                        if is_notify_inbox_task(candidate):
                            continue
                        tid = getattr(candidate, "task_id", None)
                        if not tid or tid in tasks_by_id:
                            continue
                        tasks_by_id[tid] = candidate
                        owner_svc[tid] = svc
            tasks = list(tasks_by_id.values())
            owners = {
                tid: owner_svc[tid].derive_owner(task)
                for tid, task in tasks_by_id.items()
            }
            for svc in services:
                for alias in aliases:
                    try:
                        more = unread_rejection_feedback(svc, project=alias)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(more, dict):
                        for k, v in more.items():
                            feedback.setdefault(k, v)
            try:
                project = config.projects.get(self.project_key)
                if project is not None:
                    # Resolve effective enforce_plan with the same
                    # project-overrides-global precedence the rail
                    # rollup and sweeper use. Skipping this here was
                    # the third (and last) gate site that ignored the
                    # per-project bypass — Sam saw the task list still
                    # render ``waiting_on_plan`` even after media's
                    # bypass shipped because this code path didn't
                    # check the override.
                    planner = getattr(config, "planner", None)
                    global_enforce = bool(
                        getattr(planner, "enforce_plan", True)
                    )
                    project_enforce = getattr(project, "enforce_plan", None)
                    enforce_plan = (
                        project_enforce
                        if project_enforce is not None
                        else global_enforce
                    )
                    primary_svc = services[0]
                    if enforce_plan:
                        plan_blocked = plan_blocked_task_ids(
                            self.project_key,
                            project.path,
                            primary_svc,
                            plan_dir=getattr(
                                planner,
                                "plan_dir",
                                "docs/plan",
                            ),
                        )
                    approval_task = plan_approval_task(primary_svc, self.project_key)
                    if approval_task is not None:
                        approval_task_id = getattr(approval_task, "task_id", None)
            except Exception:  # noqa: BLE001
                plan_blocked = set()
                approval_task_id = None
            recent_sweeper_pings = {
                task.task_id for task in tasks if _recent_sweeper_ping(task)
            }
        finally:
            for svc in services:
                try:
                    svc.close()
                except Exception:  # noqa: BLE001
                    pass
        tasks.sort(
            key=lambda task: _task_sort_key(
                task,
                owner=owners.get(task.task_id),
                plan_blocked=task.task_id in plan_blocked,
            )
        )
        return tasks, owners, feedback, plan_blocked, approval_task_id, recent_sweeper_pings

    def _filtered_tasks(self) -> list:
        visible: list = []
        for task in self._tasks:
            plan_blocked = task.task_id in self._plan_blocked_task_ids
            if not _task_matches_status(
                task,
                self._status_filter,
                plan_blocked=plan_blocked,
            ):
                continue
            owner = self._owner_by_task_id.get(task.task_id)
            if not _task_matches_query(task, owner, self._search_query):
                continue
            visible.append(task)
        return visible

    def _done_task_count(self) -> int:
        return sum(
            1
            for task in self._tasks
            if _task_matches_status(
                task,
                "done",
                plan_blocked=task.task_id in self._plan_blocked_task_ids,
            )
        )

    def _active_task_count(self) -> int:
        return sum(
            1
            for task in self._tasks
            if _task_matches_status(
                task,
                "active",
                plan_blocked=task.task_id in self._plan_blocked_task_ids,
            )
        )

    def _use_done_filter_for_done_only_default(self) -> None:
        if self._status_filter != "active" or self._search_query.strip():
            return
        if (
            self._tasks
            and self._active_task_count() == 0
            and self._done_task_count() > 0
        ):
            self._status_filter = "done"
            self._sync_filter_buttons()

    def _empty_filter_message(self) -> str:
        if self._status_filter == "active" and not self._search_query.strip():
            done_count = self._done_task_count()
            if done_count:
                task_word = "task" if done_count == 1 else "tasks"
                return f"No active tasks. {done_count} done {task_word} available."
        return "No tasks match the current filter."

    def _summary_text(self, visible: list) -> str:
        counts = _task_counts(
            self._tasks,
            plan_blocked_task_ids=self._plan_blocked_task_ids,
        )
        parts: list[str] = []
        for status in (
            "in_progress",
            "review",
            "waiting_on_plan",
            "queued",
            "blocked",
            "on_hold",
            "done",
        ):
            count = counts.get(status, 0)
            if count:
                icon = self._STATUS_ICONS.get(status, "·")
                parts.append(f"{icon} {count} {_status_display_label(status)}")
        total = len(self._tasks)
        shown = len(visible)
        if shown != total:
            prefix = f"{shown} of {total} shown"
        else:
            task_word = "task" if total == 1 else "tasks"
            prefix = f"{total} {task_word}"
        return prefix if not parts else prefix + "  ·  " + "  ·  ".join(parts)

    def _current_task_key(self) -> str | None:
        if self.task_table.row_count == 0 or self.task_table.cursor_row < 0:
            return None
        try:
            row_key = self.task_table.coordinate_to_cell_key(
                (self.task_table.cursor_row, 0)
            ).row_key
        except Exception:  # noqa: BLE001
            return None
        return str(row_key.value) if row_key is not None else None

    def _sync_filter_buttons(self) -> None:
        for key, button in self.filter_buttons.items():
            button.variant = "primary" if key == self._status_filter else "default"

    def _sync_filter_chips(self) -> None:
        chips: list[tuple[Button, str, bool]] = [
            (
                self.filter_chips_status,
                f"Status: {_FILTER_LABELS.get(self._status_filter, self._status_filter)} ×",
                self._status_filter != "all",
            ),
            (
                self.filter_chips_search,
                f"Search: {self._search_query.strip()} ×",
                bool(self._search_query.strip()),
            ),
        ]
        any_visible = False
        for button, label, visible in chips:
            button.label = label
            button.display = visible
            any_visible = any_visible or visible
        self.filter_chips_clear.display = any_visible
        try:
            chips_row = self.query_one("#tasks-filter-chips", Horizontal)
        except Exception:  # noqa: BLE001
            return
        chips_row.display = any_visible

    def _sync_live_tail_indicator(self) -> None:
        self.live_tail_pill.display = self._live_tail_paused
        try:
            header_row = self.query_one("#task-live-header", Horizontal)
        except Exception:  # noqa: BLE001
            return
        header_row.display = self._live_tail_paused

    def _sync_banner(self) -> None:
        """Render the approve/reject commit banner with an undo countdown.

        #767: the old banner was a single dim gray line tucked below
        the task table — users reported missing the confirmation entirely
        and not realizing the action had registered. The new banner is:
        - Full-width, tall (3 rows), high-contrast (success green on
          dark, or error red for reject).
        - Bold "APPROVED" / "REJECTED" label so the click is obviously
          confirmed the instant it fires.
        - Visible ticking countdown (``Undo (3s)``) at the end plus a
          shrinking progress bar underneath so the decaying window is
          tangible without reading numbers.
        """
        if self._pending_review_action is None:
            self.banner.update("")
            self.banner.display = False
            self.banner.remove_class("-reject")
            return

        pending = self._pending_review_action
        total_window = float(_PENDING_UNDO_SECONDS)
        remaining = max(0.0, pending.deadline - monotonic())
        seconds_left = max(0, int(remaining + 0.999))

        if pending.decision == "approve":
            label = "APPROVED"
            self.banner.remove_class("-reject")
        else:
            label = "REJECTED"
            self.banner.add_class("-reject")

        if len(pending.task_numbers) == 1:
            target = f"#{pending.task_numbers[0]}"
        else:
            target = f"{len(pending.task_numbers)} tasks"

        bar_total = 30
        filled = int(round(bar_total * (remaining / total_window)))
        filled = max(0, min(bar_total, filled))
        progress_bar = "█" * filled + "░" * (bar_total - filled)

        line_one = f"✓ {label} · {target}   [b]Undo[/b] press [b]Z[/b] ({seconds_left}s)"
        line_two = progress_bar
        self.banner.update(f"{line_one}\n{line_two}")
        self.banner.display = True
        # #767 — keep the morphed button label in sync with the
        # countdown so the click-target affordance ticks down at
        # the same rhythm as the banner.
        self._morph_review_action_buttons()

    def _set_detail_empty(self, message: str) -> None:
        self._selected_task_id = None
        self.detail_header.update(message)
        self.detail_overview.update("")
        self.detail_review.update("")
        self.review_confidence.update("")
        self.review_confidence.display = False
        self.review_inline_diff_header.update("")
        self.review_inline_diff_header.display = False
        self.review_inline_diff_expand.display = False
        self.review_inline_diff.update("")
        self.review_inline_diff.display = False
        self.review_diff_toggle.display = False
        self.review_diff.update("")
        self.review_diff.display = False
        self.detail_context.update("")
        self.detail_live.update("")
        self.timeline.clear()
        self.approve_button.disabled = True
        self.reject_button.disabled = True
        self.bulk_approve_button.disabled = True
        self.refresh_live_button.disabled = True
        self._set_live_tail_paused(False)
        self._review_inline_diff = None
        self._review_inline_diff_file_index = 0
        self._review_inline_diff_expanded = False
        self._review_nav_prefix = None

    def _render_table(self, *, select_first: bool) -> None:
        visible = self._filtered_tasks()
        visible_ids = {task.task_id for task in visible}
        self._selected_task_ids.intersection_update(visible_ids)
        self.summary.update(self._summary_text(visible))
        self.status.update(
            f"Filter: {_FILTER_LABELS.get(self._status_filter, self._status_filter)}"
            + (
                f"  ·  Search: {self._search_query} · c clear"
                if self._search_query.strip()
                else "  ·  / search · c clear · space select · a approve · x reject"
            )
        )
        self._sync_filter_chips()
        previous = self._selected_task_id
        self.task_table.clear()
        if not visible:
            self._set_detail_empty(self._empty_filter_message())
            return
        for task in visible:
            owner = self._owner_by_task_id.get(task.task_id) or "—"
            updated = _format_event_time(
                getattr(task, "updated_at", None) or getattr(task, "created_at", None)
            )
            feedback = self._rejection_feedback_by_task_id.get(task.task_id)
            plan_blocked = task.task_id in self._plan_blocked_task_ids
            recently_pinged = task.task_id in self._recent_sweeper_ping_task_ids
            status_label = _status_label(task, owner, plan_blocked=plan_blocked)
            status = _status_display_label(status_label)
            title = f"{priority_glyph(task)} {task.title}"
            stage = _display_label(task.current_node_id)
            task_number = f"#{task.task_number}"
            if task.task_id in self._selected_task_ids:
                task_number = f"◉ {task_number}"
            if recently_pinged:
                title = f"↻ {title}"
            if feedback is not None:
                title = f"🔄 {title}"
            # Recovery glyph (#1016 scope-expansion comment) — stuck
            # tasks get a leading "→R" marker in the row so the
            # operator can see at a glance which rows have a known
            # recovery affordance available. Press R on a selected
            # stuck row to view / run the recovery (handled by
            # ``action_recovery``).
            try:
                from pollypm.recovery_actions import recovery_action_for
                row_recovery = recovery_action_for(task)
            except Exception:  # noqa: BLE001
                row_recovery = None
            if row_recovery is not None:
                title = f"→R {title}"
            self.task_table.add_row(
                task_number,
                status,
                title,
                owner,
                stage,
                updated or "—",
                key=task.task_id,
            )
        target_id = None
        if self._initial_task_id and any(
            t.task_id == self._initial_task_id for t in visible
        ):
            target_id = self._initial_task_id
            self._initial_task_id = None
        if target_id is None:
            target_id = previous if any(t.task_id == previous for t in visible) else None
        if target_id is None and (select_first or previous is None):
            target_id = visible[0].task_id
        if target_id is None:
            target_id = visible[0].task_id
        row_index = next(
            (index for index, task in enumerate(visible) if task.task_id == target_id),
            0,
        )
        self.task_table.move_cursor(row=row_index, column=0, animate=False, scroll=True)
        selected_review_count = len(self._selected_review_task_ids(visible=visible))
        self.bulk_approve_button.label = (
            f"Approve Selected ({selected_review_count})"
            if selected_review_count
            else "Approve Selected"
        )
        self.bulk_approve_button.disabled = selected_review_count == 0
        self._show_detail(target_id)

    def _selected_review_task_ids(self, *, visible: list | None = None) -> list[str]:
        tasks = visible if visible is not None else self._filtered_tasks()
        return [
            task.task_id
            for task in tasks
            if task.task_id in self._selected_task_ids
            and getattr(getattr(task, "work_status", None), "value", None) == "review"
        ]

    def _refresh_list(self, *, select_first: bool = False) -> None:
        (
            self._tasks,
            self._owner_by_task_id,
            self._rejection_feedback_by_task_id,
            self._plan_blocked_task_ids,
            self._plan_approval_task_id,
            self._recent_sweeper_ping_task_ids,
        ) = self._load_tasks()
        if select_first:
            self._use_done_filter_for_done_only_default()
        self._render_table(select_first=select_first)

    def _render_timeline(self, executions: list) -> None:
        self.timeline.clear()
        if not executions:
            return
        for execution in executions:
            status = getattr(getattr(execution, "status", None), "value", None) or str(
                getattr(execution, "status", "") or ""
            )
            if status == "active":
                marker = "⟳"
            elif getattr(execution, "decision", None) is not None:
                decision_value = getattr(
                    getattr(execution, "decision", None),
                    "value",
                    getattr(execution, "decision", None),
                )
                marker = "✓" if decision_value == "approved" else "✗"
            else:
                marker = "●"
            started = _format_event_time(getattr(execution, "started_at", None)) or "—"
            completed = _format_event_time(getattr(execution, "completed_at", None)) or "—"
            details: list[str] = []
            age = _format_relative_age(
                getattr(execution, "completed_at", None)
                or getattr(execution, "started_at", None)
            )
            if age:
                details.append(age)
            decision = getattr(execution, "decision", None)
            if decision is not None:
                decision_value = getattr(decision, "value", decision)
                details.append(str(decision_value))
            if getattr(execution, "decision_reason", None):
                details.append(str(execution.decision_reason))
            output = getattr(execution, "work_output", None)
            if output is not None and getattr(output, "summary", None):
                details.append(str(output.summary))
            self.timeline.add_row(
                f"{marker} {execution.node_id}",
                status,
                started,
                completed,
                " · ".join(details) if details else "—",
                key=f"{execution.node_id}:{execution.visit}",
            )

    def _render_selected_task(self, task, *, owner: str | None, flow, active_session) -> None:
        feedback = self._rejection_feedback_by_task_id.get(task.task_id)
        plan_blocked = task.task_id in self._plan_blocked_task_ids
        recently_pinged = task.task_id in self._recent_sweeper_ping_task_ids
        status_label = _status_label(task, owner, plan_blocked=plan_blocked)
        icon = self._STATUS_ICONS.get(status_label, "·")
        header_bits = [
            f"{'🔄 ' if feedback is not None else ''}{icon} #{task.task_number} {priority_glyph(task)} {task.title}",
            (
                f"{_status_display_label(status_label)} · "
                f"{_format_relative_age(task.updated_at or task.created_at)}"
            ),
        ]
        if recently_pinged:
            header_bits.append("Sweeper pinged in the last minute")
        if feedback is not None:
            header_bits.append(
                f"{_rejection_feedback_label(feedback, include_inbox_task_id=False)} in inbox"
            )
        self.detail_header.update("\n".join(header_bits))
        task_index = {t.task_id: t for t in self._tasks}
        self.detail_overview.update(
            _render_overview(
                task,
                owner=owner,
                flow=flow,
                active_session=active_session,
                rejection_feedback=feedback,
                task_index=task_index,
                plan_blocked=plan_blocked,
                plan_approval_task_id=self._plan_approval_task_id,
                recently_pinged=recently_pinged,
            )
        )
        self.detail_context.update(_render_context(task))
        self.detail_live.update(_render_live(task, active_session))
        self._render_timeline(list(getattr(task, "executions", None) or []))
        in_review = task.work_status.value == "review"
        self.approve_button.disabled = not in_review
        self.reject_button.disabled = not in_review
        self.bulk_approve_button.disabled = len(self._selected_review_task_ids()) == 0
        self.refresh_live_button.disabled = active_session is None
        if active_session is None:
            self._set_live_tail_paused(False)
        self._sync_live_tail_indicator()
        if active_session is not None and not self._live_tail_paused:
            self.call_after_refresh(self._tail_live_scroll_to_end)

    def _sync_review_panel(self, task, review_artifact, review_diff_bundle) -> None:
        self.detail_review.update(render_task_review_artifact(review_artifact))
        score = _review_confidence_score(task, review_artifact)
        if score is None:
            self.review_confidence.update("")
            self.review_confidence.display = False
        else:
            self.review_confidence.update(_review_confidence_markup(score))
            self.review_confidence.display = True
        self._review_inline_diff = review_diff_bundle
        self._sync_inline_review_diff()
        diff_available = _task_has_resubmission_diff(task)
        self.review_diff_toggle.display = diff_available
        self.review_diff_toggle.disabled = not diff_available
        if not diff_available:
            self._show_resubmission_diff = False
            self.review_diff.update("")
            self.review_diff.display = False
            return
        self.review_diff_toggle.label = (
            "Diff Since Rejection: On [D]"
            if self._show_resubmission_diff
            else "Diff Since Rejection: Off [D]"
        )
        if self._show_resubmission_diff:
            self.review_diff.update(_render_review_submission_diff(task))
        else:
            self.review_diff.update(
                "[dim]Press [D] to compare this submission against the last rejected attempt.[/dim]"
            )
        self.review_diff.display = True

    def _sync_inline_review_diff(self) -> None:
        bundle = self._review_inline_diff
        if bundle is None or not bundle.files:
            self.review_inline_diff_header.update("")
            self.review_inline_diff_header.display = False
            self.review_inline_diff_expand.display = False
            self.review_inline_diff.update("")
            self.review_inline_diff.display = False
            return
        self._review_inline_diff_file_index = max(
            0,
            min(self._review_inline_diff_file_index, len(bundle.files) - 1),
        )
        diff_file = bundle.files[self._review_inline_diff_file_index]
        current_file = self._review_inline_diff_file_index + 1
        self.review_inline_diff_header.update(
            "\n".join(
                [
                    (
                        f"[b]Code Diff[/b] · {bundle.source_label}"
                        f" · {current_file}/{len(bundle.files)} files"
                    ),
                    (
                        f"{diff_file.path}"
                        + (
                            " [dim]· [f prev · ]f next[/dim]"
                            if len(bundle.files) > 1
                            else ""
                        )
                    ),
                ]
            )
        )
        self.review_inline_diff_header.display = True
        hidden_lines = review_diff_hidden_line_count(diff_file)
        collapsed = hidden_lines > 0 and not self._review_inline_diff_expanded
        if hidden_lines > 0:
            line_word = "line" if hidden_lines == 1 else "lines"
            self.review_inline_diff_expand.label = (
                "Collapse"
                if self._review_inline_diff_expanded
                else f"Expand (+{hidden_lines} {line_word})"
            )
            self.review_inline_diff_expand.display = True
        else:
            self.review_inline_diff_expand.display = False
        self.review_inline_diff.update(
            Syntax(
                diff_file.patch if not collapsed else collapse_review_diff_file(diff_file),
                "diff",
                line_numbers=True,
                word_wrap=False,
                background_color="default",
            )
        )
        self.review_inline_diff.display = True

    def _review_tab_is_active(self) -> bool:
        try:
            tabs = self.query_one("#task-tabs", TabbedContent)
        except Exception:  # noqa: BLE001
            return False
        return tabs.active == "task-tab-review"

    def _scroll_review_panel_home(self) -> None:
        try:
            review_scroll = self.query_one("#task-review-scroll", VerticalScroll)
        except Exception:  # noqa: BLE001
            return
        review_scroll.scroll_home(animate=False)

    def _jump_review_diff_file(self, step: int) -> None:
        bundle = self._review_inline_diff
        if bundle is None or len(bundle.files) < 2:
            return
        self._review_inline_diff_file_index = (
            self._review_inline_diff_file_index + step
        ) % len(bundle.files)
        self._review_inline_diff_expanded = False
        self._sync_inline_review_diff()
        self.call_after_refresh(self._scroll_review_panel_home)

    def _project_path(self) -> Path | None:
        try:
            config = load_config(self.config_path)
        except Exception:  # noqa: BLE001
            return None
        project = getattr(config, "projects", {}).get(self.project_key)
        if project is None:
            return None
        return getattr(project, "path", None)

    def _show_detail(self, task_id: str) -> None:
        """Synchronously acknowledge a task click; load detail off the UI thread.

        #1017 — clicking a row used to do every DB read, review-artifact
        scan, and git-diff resolution synchronously on the UI thread.
        That left the click feeling unresponsive: profiling on a real
        bikepath/8 click measured 60ms-1s of stalling on a typical
        click and ~1s on the cold first click thanks to gate plugin
        discovery in :class:`SQLiteWorkService`'s constructor.

        The fix mirrors the #974 rail-click pattern:

        1. Bump :attr:`_detail_request_seq` so any in-flight prior load
           is logically superseded.
        2. Stamp ``_selected_task_id`` and paint a "Loading…"
           placeholder synchronously so the click is visibly
           acknowledged.
        3. Hand the slow work to a worker thread; the worker calls back
           via ``call_from_thread`` to apply the rendered detail, but
           only if its captured seq still matches.
        """

        previous_task_id = self._selected_task_id
        self._detail_request_seq += 1
        seq = self._detail_request_seq
        self._pending_detail_task_id = task_id

        # Optimistic UI: stamp the selected row immediately so cursor
        # highlight, button-disabled state, and downstream actions
        # (``approve_task`` etc.) all point at what the user clicked
        # even before the DB load finishes.
        self._selected_task_id = task_id

        if task_id != previous_task_id:
            self._set_live_tail_paused(False)
            self._show_resubmission_diff = False
            self._review_inline_diff_file_index = 0
            self._review_inline_diff_expanded = False

        # Render a fast placeholder so the user sees their click
        # land. Use any cached row data we already have from
        # ``_tasks`` so the loading hint is more than a bare task id.
        cached = next(
            (t for t in self._tasks if t.task_id == task_id),
            None,
        )
        if cached is not None:
            placeholder = (
                f"#{cached.task_number} {priority_glyph(cached)} {cached.title}"
                f"\n[dim]Loading…[/dim]"
            )
        else:
            placeholder = f"[dim]Loading task {task_id}…[/dim]"
        self.detail_header.update(placeholder)

        self._dispatch_detail_load(task_id, seq, previous_task_id)

    def _dispatch_detail_load(
        self,
        task_id: str,
        seq: int,
        previous_task_id: str | None,
    ) -> None:
        """Run :meth:`_load_detail_data` on a worker thread.

        Falls back to inline execution when Textual's worker pool is
        unavailable (e.g. unit tests that bypass ``App.__init__`` and
        have no running event loop). Production always has a loop.
        """

        try:
            self.run_worker(
                lambda: self._detail_worker(task_id, seq, previous_task_id),
                thread=True,
                exclusive=True,
                group="task_detail_load",
            )
        except Exception:  # noqa: BLE001
            # No loop — execute inline so unit tests still see the
            # detail rendered. Production always has a loop.
            self._detail_worker(task_id, seq, previous_task_id)

    def _detail_worker(
        self,
        task_id: str,
        seq: int,
        previous_task_id: str | None,
    ) -> None:
        """Worker-thread body: load detail, then apply on the UI thread.

        #1017 — gated on ``seq`` so a slow load whose click was already
        superseded by a newer click cannot overwrite the user's most
        recent intent when it eventually returns.
        """

        if seq != self._detail_request_seq:
            return
        result = self._load_detail_data(task_id)

        def _apply() -> None:
            if seq != self._detail_request_seq:
                return
            self._apply_detail_result(task_id, previous_task_id, result)

        try:
            self.call_from_thread(_apply)
        except Exception:  # noqa: BLE001
            # No running app (unit tests) — apply directly.
            _apply()

    def _load_detail_data(self, task_id: str) -> dict:
        """Pure data-fetch step: run on a worker thread.

        Returns a dict the UI thread consumes — ``error`` is set when
        the work-service handle could not be opened or when ``svc.get``
        raised. The UI thread translates that into a
        ``_set_detail_empty`` call without itself touching the DB.
        """

        svc = self._get_svc()
        if svc is None:
            return {"error": "Could not open the project work service."}
        try:
            task = svc.get(task_id)
            task.context = svc.get_context(task_id, limit=15)
            task.executions = svc.get_execution(task_id)
            owner = svc.derive_owner(task)
            try:
                flow = svc.get_flow(task.flow_template_id, project=task.project)
            except Exception:  # noqa: BLE001
                flow = None
            try:
                active_session = svc.get_worker_session(
                    task_project=task.project,
                    task_number=task.task_number,
                    active_only=True,
                )
            except Exception:  # noqa: BLE001
                active_session = None
        except Exception as exc:  # noqa: BLE001
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
            return {"error": f"Error loading task: {exc}"}
        try:
            svc.close()
        except Exception:  # noqa: BLE001
            pass
        project_path = self._project_path()
        review_artifact = load_task_review_artifact(task, project_path)
        review_diff_bundle = load_task_review_diff(
            task,
            project_path,
            review_artifact=review_artifact,
            active_branch=getattr(active_session, "branch_name", None),
        )
        return {
            "task": task,
            "owner": owner,
            "flow": flow,
            "active_session": active_session,
            "review_artifact": review_artifact,
            "review_diff_bundle": review_diff_bundle,
        }

    def _apply_detail_result(
        self,
        task_id: str,
        previous_task_id: str | None,
        result: dict,
    ) -> None:
        """Render the loaded detail. Runs on the UI thread.

        Caller (`_detail_worker`) has already verified the seq is
        still current, so this method is free to mutate widget state
        without re-checking.
        """

        if "error" in result:
            self._set_detail_empty(str(result["error"]))
            return
        task = result["task"]
        owner = result["owner"]
        flow = result["flow"]
        active_session = result["active_session"]
        review_artifact = result["review_artifact"]
        review_diff_bundle = result["review_diff_bundle"]

        self._owner_by_task_id[task.task_id] = owner
        if review_diff_bundle is None:
            self._review_inline_diff_file_index = 0
            self._review_inline_diff_expanded = False
        elif self._review_inline_diff_file_index >= len(review_diff_bundle.files):
            self._review_inline_diff_file_index = 0
            self._review_inline_diff_expanded = False
        self._render_selected_task(
            task,
            owner=owner,
            flow=flow,
            active_session=active_session,
        )
        self._sync_review_panel(task, review_artifact, review_diff_bundle)
        try:
            tabs = self.query_one("#task-tabs", TabbedContent)
        except Exception:  # noqa: BLE001
            tabs = None
        if tabs is not None and task_id != previous_task_id:
            tabs.active = (
                "task-tab-review"
                if task.work_status.value == "review"
                and (review_artifact is not None or review_diff_bundle is not None)
                else "task-tab-overview"
            )
        if active_session is not None and not self._live_tail_paused:
            try:
                self.call_after_refresh(self._tail_live_scroll_to_end)
            except Exception:  # noqa: BLE001
                pass

    def _background_refresh(self) -> None:
        try:
            self._refresh_list(select_first=False)
        except Exception:  # noqa: BLE001
            pass

    def action_focus_search(self) -> None:
        self.search_input.focus()
        self.search_input.cursor_position = len(self.search_input.value)

    def _has_search_filter(self) -> bool:
        return bool((self._search_query or "").strip() or self.search_input.value)

    def _clear_search_filter(self, *, focus_search: bool = False) -> None:
        self._search_query = ""
        self.search_input.value = ""
        self._render_table(select_first=True)
        if focus_search:
            self.action_focus_search()

    def action_back(self) -> None:
        if self.search_input.has_focus:
            if self.search_input.value:
                self._clear_search_filter(focus_search=True)
                return
            self.task_table.focus()
            return
        if self._has_search_filter():
            self._clear_search_filter(focus_search=True)
            return
        self.exit()

    def action_clear_filters(self) -> None:
        self._status_filter = "all"
        self._search_query = ""
        self.search_input.value = ""
        self._sync_filter_buttons()
        self._render_table(select_first=True)
        self.task_table.focus()

    def action_refresh(self) -> None:
        self._refresh_list(select_first=False)

    def action_recovery_action(self) -> None:
        """#1016 — surface the recovery affordance for the selected
        stuck task.

        Behaviour:

        * If no task is selected, no-op.
        * If the selected task isn't stuck, post a transient toast
          ("no recovery action — task is healthy").
        * Otherwise, emit a notification with the recovery title +
          first CLI step. Running the steps for the operator would
          require a confirmation modal we haven't built yet (it can
          ``git commit`` or merge, both irreversible) — for now we
          surface the action prominently so the operator can copy /
          paste with one keystroke. The detail view already shows the
          full block.
        """
        if not self._selected_task_id:
            return
        task = next(
            (t for t in self._tasks if t.task_id == self._selected_task_id),
            None,
        )
        if task is None:
            return
        try:
            from pollypm.recovery_actions import (
                recovery_action_for,
                render_recovery_action_block,
            )
            action = recovery_action_for(task)
        except Exception:  # noqa: BLE001
            action = None
        if action is None:
            try:
                self.notify(
                    "No recovery action — this task is not stuck.",
                    severity="information",
                )
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            block = "\n".join(render_recovery_action_block(action))
            self.notify(block, title="Recovery action", timeout=12)
        except Exception:  # noqa: BLE001
            pass

    def action_refresh_live(self) -> None:
        if not self._selected_task_id:
            return
        tabs = self.query_one("#task-tabs", TabbedContent)
        tabs.active = "task-tab-live"
        self._show_detail(self._selected_task_id)

    def action_resume_live_tail(self) -> None:
        self._set_live_tail_paused(False)
        self._sync_live_tail_indicator()
        if self._selected_task_id:
            self.call_after_refresh(self._tail_live_scroll_to_end)

    def action_undo_pending_review(self) -> None:
        if self._pending_review_action is None:
            return
        self._clear_pending_review_action()

    def _set_live_tail_paused(self, paused: bool) -> None:
        if self._live_tail_paused == paused:
            return
        self._live_tail_paused = paused
        self._sync_live_tail_indicator()

    def _tail_live_scroll_to_end(self) -> None:
        if self._live_tail_paused:
            return
        try:
            live_scroll = self.query_one("#task-live-scroll", _TaskLiveScroll)
        except Exception:  # noqa: BLE001
            return
        live_scroll.scroll_end(animate=False, force=True)

    def _clear_pending_review_action(self) -> None:
        if self._pending_commit_timer is not None:
            self._pending_commit_timer.stop()
            self._pending_commit_timer = None
        if self._pending_countdown_timer is not None:
            self._pending_countdown_timer.stop()
            self._pending_countdown_timer = None
        self._pending_review_action = None
        # #767 — restore the in-place button morph so the next
        # review action starts from the regular Approve / Reject
        # affordance, not the lingering Undo state.
        self._restore_review_action_buttons()
        self._sync_banner()

    def _start_pending_review_action(
        self,
        *,
        task_ids: tuple[str, ...],
        task_numbers: tuple[int, ...],
        decision: str,
        reason: str | None = None,
    ) -> None:
        if self._pending_review_action is not None:
            self._commit_pending_review_action()
        self._pending_review_action = _PendingReviewAction(
            task_ids=task_ids,
            task_numbers=task_numbers,
            decision=decision,
            reason=reason,
            deadline=monotonic() + _PENDING_UNDO_SECONDS,
        )
        self._pending_commit_timer = self.set_timer(
            _PENDING_UNDO_SECONDS,
            self._commit_pending_review_action,
        )
        self._pending_countdown_timer = self.set_interval(1, self._sync_banner)
        # #767 — morph the click target itself: the button the user
        # just pressed becomes ``Undo (5s)`` in-place, with a tinted
        # row classifier so the commit is obviously confirmed at the
        # gaze location instead of relying on the user noticing a
        # separate banner. The countdown ticks via ``_sync_banner``
        # which now also refreshes the morphed button label.
        self._morph_review_action_buttons()
        self._sync_banner()

    def _morph_review_action_buttons(self) -> None:
        """Replace the active review button with an Undo affordance.

        Approve becomes ``Undo (5s)`` (success-tinted), Reject
        becomes ``Undo (5s)`` (warning-tinted). The other button
        disables so the user can't fire two pending actions at once.
        Called from :meth:`_start_pending_review_action` and
        :meth:`_sync_banner` (the latter ticks the countdown
        in-place without re-mounting the widget).
        """
        pending = self._pending_review_action
        if pending is None:
            return
        seconds_left = max(
            0, int(max(0.0, pending.deadline - monotonic()) + 0.999),
        )
        undo_label = f"Undo ({seconds_left}s)"
        if pending.decision == "approve":
            self.approve_button.label = undo_label
            self.approve_button.add_class("-undo")
            self.reject_button.disabled = True
        else:
            self.reject_button.label = undo_label
            self.reject_button.add_class("-undo")
            self.approve_button.disabled = True

    def _restore_review_action_buttons(self) -> None:
        """Undo the morph from :meth:`_morph_review_action_buttons`.

        Called when the pending action clears (committed, undone,
        or pre-empted by another review). Returns both buttons to
        their default Approve / Reject labels and re-enables them.
        """
        self.approve_button.label = "Approve"
        self.approve_button.remove_class("-undo")
        self.approve_button.disabled = False
        self.reject_button.label = "Reject"
        self.reject_button.remove_class("-undo")
        self.reject_button.disabled = False

    def _merge_task_pr(self, task) -> None:
        pr_number = _task_pr_number(task)
        if pr_number is None:
            return
        if not _project_auto_merge_on_approve_enabled(self._project_path()):
            return
        project_path = self._project_path()
        if project_path is None:
            return
        try:
            subprocess.run(
                ["gh", "pr", "merge", pr_number, "--squash"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=45,
                check=True,
            )
            self.notify(f"Merged PR #{pr_number}", severity="information")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Auto-merge failed: {exc}", severity="error")

    def _commit_pending_review_action(self) -> None:
        pending = self._pending_review_action
        if pending is None:
            return
        self._clear_pending_review_action()
        svc = self._get_svc()
        if svc is None:
            self.notify("Could not open project database.", severity="error")
            return
        try:
            processed_ids: list[str] = []
            approve_count = 0
            reject_count = 0
            for task_id in pending.task_ids:
                task = svc.get(task_id)
                if task.work_status.value != "review":
                    self.notify(f"{task_id} is not in review state.", severity="warning")
                    continue
                if pending.decision == "approve":
                    approved = svc.approve(
                        task_id,
                        "user",
                        pending.reason or "Approved from task cockpit",
                    )
                    notify_task_approved(approved, notify=self.notify)
                    self._merge_task_pr(approved)
                    approve_count += 1
                else:
                    svc.reject(
                        task_id,
                        "user",
                        pending.reason or "Rejected from task cockpit",
                    )
                    reject_count += 1
                processed_ids.append(task_id)
            if len(processed_ids) > 1:
                if approve_count:
                    self.notify(
                        f"Approved {approve_count} selected task"
                        + ("s" if approve_count != 1 else ""),
                        severity="information",
                    )
                if reject_count:
                    self.notify(
                        f"Rejected {reject_count} selected task"
                        + ("s" if reject_count != 1 else ""),
                        severity="information",
                    )
            self._selected_task_ids.difference_update(processed_ids)
        except Exception as exc:  # noqa: BLE001
            action = "Approve" if pending.decision == "approve" else "Reject"
            self.notify(f"{action} failed: {exc}", severity="error")
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._refresh_list(select_first=False)
        except Exception:  # noqa: BLE001
            pass

    def _review_task(self, task_id: str, *, decision: str, reason: str | None = None) -> None:
        task = None
        svc = self._get_svc()
        if svc is None:
            self.notify("Could not open project database.", severity="error")
            return
        try:
            task = svc.get(task_id)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Review failed: {exc}", severity="error")
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        if task is None:
            return
        if task.work_status.value != "review":
            self.notify("Task is not in review state.", severity="warning")
            return
        self._start_pending_review_action(
            task_ids=(task_id,),
            task_numbers=(task.task_number,),
            decision=decision,
            reason=reason,
        )

    def _notify_arm_hint(self, label: str) -> None:
        """Surface a transient hint after a destructive-action arm.

        ``destructive_action_safe`` returns ``False`` on the first
        press without an explicit selection. The user has to know
        that — silent refusal would feel like a frozen UI. The hint
        also tells them exactly how to confirm.
        """
        try:
            self.notify(
                f"Press the same key again within 3s to {label}.",
                severity="warning",
                timeout=3.0,
            )
        except Exception:  # noqa: BLE001 — notify is best-effort
            pass

    def _fire_approve_task(self) -> None:
        """Submit approval for the currently-selected task.

        The fire method is the post-arming, post-search-redirect
        path. Keyboard ``a`` (``action_approve_task``) runs the
        arming + search-redirect gate first; the on-screen Approve
        button calls this directly because a visible button click
        is already explicit intent (#900)."""
        if not self._selected_task_id:
            return
        self._review_task(self._selected_task_id, decision="approve")

    def action_approve_task(self) -> None:
        # #840 fix — single-letter `a` is destructive in a screen that
        # also has a visible search input. `destructive_action_safe`
        # combines the Input-focus check with explicit-selection
        # detection and an "arm then confirm" two-press flow so a
        # bench cursor sitting on a real task cannot approve it on a
        # single keystroke. The on-screen Approve button bypasses
        # this gate via :meth:`_fire_approve_task` (#900).
        if self._has_search_filter():
            self.action_focus_search()
            return
        if not self._selected_task_id:
            return
        if not destructive_action_safe(
            self,
            "approve_task",
            target_id=self._selected_task_id,
            selection=self._selected_task_ids,
        ):
            self._notify_arm_hint("approve")
            return
        self._fire_approve_task()

    def action_toggle_task_selection(self) -> None:
        if isinstance(getattr(self, "focused", None), Input):
            return
        task_id = self._current_task_key()
        if task_id is None:
            return
        if task_id in self._selected_task_ids:
            self._selected_task_ids.remove(task_id)
        else:
            self._selected_task_ids.add(task_id)
        self._render_table(select_first=False)

    def _review_selected_tasks(self, *, decision: str, reason: str | None = None) -> None:
        selected_tasks = [
            task
            for task in self._filtered_tasks()
            if task.task_id in self._selected_task_ids and task.work_status.value == "review"
        ]
        if not selected_tasks:
            return
        self._start_pending_review_action(
            task_ids=tuple(task.task_id for task in selected_tasks),
            task_numbers=tuple(task.task_number for task in selected_tasks),
            decision=decision,
            reason=reason,
        )

    def action_bulk_approve_selected(self) -> None:
        if isinstance(getattr(self, "focused", None), Input):
            return
        self._review_selected_tasks(decision="approve")

    def action_bulk_reject_selected(self) -> None:
        if isinstance(getattr(self, "focused", None), Input):
            return
        self._review_selected_tasks(
            decision="reject",
            reason="Bulk rejected from task cockpit",
        )

    def _fire_reject_task(self) -> None:
        """Open the reject-reason modal for the currently-selected task.

        Post-arming, post-search-redirect. Keyboard ``x``
        (``action_reject_task``) runs the gate first; the on-screen
        Reject button calls this directly (#900)."""
        if not self._selected_task_id:
            return
        task_id = self._selected_task_id
        modal = _TaskRejectReasonModal()
        self._active_reject_modal = modal

        def _after(reason: str | None) -> None:
            self._active_reject_modal = None
            if reason is None:
                return
            self._review_task(task_id, decision="reject", reason=reason)

        self.push_screen(modal, _after)

    def action_reject_task(self) -> None:
        # #840 fix — see ``action_approve_task`` above. Button-click
        # path bypasses arming via :meth:`_fire_reject_task` (#900).
        if self._has_search_filter():
            self.action_focus_search()
            return
        if not self._selected_task_id:
            return
        if not destructive_action_safe(
            self,
            "reject_task",
            target_id=self._selected_task_id,
            selection=self._selected_task_ids,
        ):
            self._notify_arm_hint("reject")
            return
        self._fire_reject_task()

    def action_toggle_resubmission_diff(self) -> None:
        if not self._selected_task_id:
            return
        svc = self._get_svc()
        if svc is None:
            return
        try:
            task = svc.get(self._selected_task_id)
            task.executions = svc.get_execution(self._selected_task_id)
        except Exception:  # noqa: BLE001
            return
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass
        if not _task_has_resubmission_diff(task):
            return
        self._show_resubmission_diff = not self._show_resubmission_diff
        self._show_detail(self._selected_task_id)

    @on(Input.Changed, "#tasks-search")
    def _on_search_changed(self, event: Input.Changed) -> None:
        keep_focus = self.search_input.has_focus
        self._search_query = event.value or ""
        self._render_table(select_first=True)
        if keep_focus:
            self.search_input.focus()
            self.search_input.cursor_position = len(self.search_input.value)

    @on(Input.Submitted, "#tasks-search")
    def _on_search_submitted(self) -> None:
        self.task_table.focus()

    @on(Button.Pressed, "#tasks-filter-all")
    def _on_filter_all(self) -> None:
        self._status_filter = "all"
        self._sync_filter_buttons()
        self._render_table(select_first=True)

    @on(Button.Pressed, "#tasks-filter-active")
    def _on_filter_active(self) -> None:
        self._status_filter = "active"
        self._sync_filter_buttons()
        self._render_table(select_first=True)

    @on(Button.Pressed, "#tasks-filter-review")
    def _on_filter_review(self) -> None:
        self._status_filter = "review"
        self._sync_filter_buttons()
        self._render_table(select_first=True)

    @on(Button.Pressed, "#tasks-filter-blocked")
    def _on_filter_blocked(self) -> None:
        self._status_filter = "blocked"
        self._sync_filter_buttons()
        self._render_table(select_first=True)

    @on(Button.Pressed, "#tasks-filter-done")
    def _on_filter_done(self) -> None:
        self._status_filter = "done"
        self._sync_filter_buttons()
        self._render_table(select_first=True)

    @on(Button.Pressed, "#tasks-chip-status")
    def _on_press_status_chip(self) -> None:
        self._status_filter = "all"
        self._sync_filter_buttons()
        self._render_table(select_first=True)

    @on(Button.Pressed, "#tasks-chip-search")
    def _on_press_search_chip(self) -> None:
        self._clear_search_filter()

    @on(Button.Pressed, "#tasks-chip-clear-all")
    def _on_press_clear_all_chips(self) -> None:
        self.action_clear_filters()

    @on(Button.Pressed, "#task-approve")
    def _on_press_approve(self) -> None:
        # #767 — when the button has morphed into Undo (a pending
        # approval is in flight), the same click target now cancels
        # the pending action instead of starting a new one. Saves
        # the user a cognitive jump to a different control.
        if self._pending_review_action is not None and self.approve_button.has_class("-undo"):
            self.action_undo_pending_review()
            return
        # #900 — the visible Approve button is explicit intent and
        # already protected by the existing 5-second undo banner. It
        # must not inherit the keyboard arming flow (#881). Call the
        # fire method directly so a single click opens the undo banner.
        self._fire_approve_task()

    @on(Button.Pressed, "#task-reject")
    def _on_press_reject(self) -> None:
        if self._pending_review_action is not None and self.reject_button.has_class("-undo"):
            self.action_undo_pending_review()
            return
        # #900 — single-click reject opens the reason modal.
        self._fire_reject_task()

    @on(Button.Pressed, "#task-bulk-approve")
    def _on_press_bulk_approve(self) -> None:
        self.action_bulk_approve_selected()

    @on(Button.Pressed, "#task-review-diff-toggle")
    def _on_press_review_diff_toggle(self) -> None:
        self.action_toggle_resubmission_diff()

    @on(Button.Pressed, "#task-review-inline-diff-expand")
    def _on_press_review_inline_diff_expand(self) -> None:
        bundle = self._review_inline_diff
        if bundle is None:
            return
        diff_file = bundle.files[self._review_inline_diff_file_index]
        if review_diff_hidden_line_count(diff_file) == 0:
            return
        self._review_inline_diff_expanded = not self._review_inline_diff_expanded
        self._sync_inline_review_diff()
        self.call_after_refresh(self._scroll_review_panel_home)

    @on(Button.Pressed, "#task-refresh-live")
    def _on_press_refresh_live(self) -> None:
        self.action_refresh_live()

    def on_key(self, event: events.Key) -> None:
        if not isinstance(getattr(self, "focused", None), Input):
            if self._has_search_filter() and event.key in {"backspace", "ctrl+u"}:
                event.stop()
                event.prevent_default()
                self._clear_search_filter(focus_search=True)
                return
        self._review_nav_prefix = self._handle_review_diff_key(event)
        modal = self._active_reject_modal
        if modal is None:
            return
        if event.key == "1":
            event.stop()
            modal.action_pick_tests()
        elif event.key == "2":
            event.stop()
            modal.action_pick_scope()
        elif event.key == "3":
            event.stop()
            modal.action_pick_fix()
        elif event.key == "4":
            event.stop()
            modal.action_pick_other()

    def _handle_review_diff_key(self, event: events.Key) -> str | None:
        if isinstance(getattr(self, "focused", None), Input):
            return None
        if not self._review_tab_is_active():
            return None
        bundle = self._review_inline_diff
        if bundle is None or len(bundle.files) < 2:
            return None
        key_char = event.character or ""
        if key_char in {"[", "]"}:
            return key_char
        if key_char == "f" and self._review_nav_prefix in {"[", "]"}:
            event.stop()
            self._jump_review_diff_file(1 if self._review_nav_prefix == "]" else -1)
        return None

    @on(DataTable.RowHighlighted, "#tasks-table")
    def _on_task_highlighted(self) -> None:
        task_id = self._current_task_key()
        if task_id is None or task_id == self._selected_task_id:
            return
        self._show_detail(task_id)

    @on(DataTable.RowSelected, "#tasks-table")
    def _on_task_selected(self) -> None:
        task_id = self._current_task_key()
        if task_id is None:
            return
        # Cursor moved — clear any armed destructive action so a
        # stale arm cannot survive into a different row (#840).
        cancel_arming(self)
        self._show_detail(task_id)


# ---------------------------------------------------------------------------
# Cockpit interaction contract registration (#881)
# ---------------------------------------------------------------------------
#
# Tasks is the canonical screen the contract was designed around: it
# has a visible search Input, opens with DataTable focus, and binds
# destructive single-key actions (`a`, `x`). The arming requirement
# is therefore mandatory.

_TASKS_CONTRACT_BINDINGS: tuple[CockpitBinding, ...] = (
    CockpitBinding(
        keys=("slash",),
        action="focus_search",
        description="Search",
        kind=ActionKind.READ,
        show_in_help=False,
    ),
    CockpitBinding(
        keys=("r",),
        action="refresh",
        description="Refresh",
        kind=ActionKind.READ,
    ),
    CockpitBinding(
        keys=("a",),
        action="approve_task",
        description="Approve",
        kind=ActionKind.DESTRUCTIVE,
    ),
    CockpitBinding(
        keys=("x",),
        action="reject_task",
        description="Reject",
        kind=ActionKind.DESTRUCTIVE,
    ),
    CockpitBinding(
        keys=("c",),
        action="clear_filters",
        description="Clear Filters",
        kind=ActionKind.WRITE,
        show_in_help=False,
    ),
    CockpitBinding(
        keys=("space",),
        action="toggle_task_selection",
        description="Select",
        kind=ActionKind.WRITE,
        show_in_help=False,
    ),
    CockpitBinding(
        keys=("A",),
        action="bulk_approve_selected",
        description="Approve Selected",
        kind=ActionKind.DESTRUCTIVE,
        show_in_help=False,
    ),
    CockpitBinding(
        keys=("X",),
        action="bulk_reject_selected",
        description="Reject Selected",
        kind=ActionKind.DESTRUCTIVE,
        show_in_help=False,
    ),
    CockpitBinding(
        keys=("d",),
        action="toggle_resubmission_diff",
        description="Review Diff",
        kind=ActionKind.READ,
        show_in_help=False,
    ),
    CockpitBinding(
        keys=("z",),
        action="undo_pending_review",
        description="Undo",
        kind=ActionKind.WRITE,
        show_in_help=False,
    ),
    CockpitBinding(
        keys=("escape",),
        action="back",
        description="Back",
        kind=ActionKind.NAVIGATION,
    ),
)


_COCKPIT_INTERACTION_REGISTRY.register(
    ScreenContract(
        screen_name="PollyTasksApp",
        initial_focus=FocusKind.TABLE,
        has_visible_input=True,
        bindings=_TASKS_CONTRACT_BINDINGS,
        arming_required_for_destructive=True,
        notes=(
            "Tasks opens with DataTable focus and renders a search Input. "
            "Single-key `a`/`x` are destructive and route through "
            "`destructive_action_safe`. Bulk `A`/`X` are inherently armed "
            "by requiring a non-empty `_selected_task_ids` set."
        ),
    )
)
