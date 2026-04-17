"""Advisor inbox — one file per advisor_insight emission.

On ``emit=true`` the assess path writes an inbox entry under
``<base_dir>/advisor_insights/`` — one JSON + one markdown body file
per emission. Each entry carries ``kind="advisor_insight"``, the
severity, the topic, and the summary + details + suggestion from the
session's structured decision.

User affordances (ad06 CLI surfaces them):

* Acknowledge: closes the entry with ``outcome="acknowledged"``.
* Dismiss with ``--reason topic_cooldown``: closes the entry AND
  appends a soft-cooldown record to ``advisor-state.json``. The
  advisor persona sees that signal in its trajectory context on
  subsequent runs — it's behavioral, not system-enforced.
* Convert to task: turns the insight into a work-service task with
  ``flow=implement_module`` (the default target flow).

Auto-close: ``advisor.autoclose`` sweep (registered in ad01, wired to
the real handler here) closes entries that have had no user action
for 7 days. The 7-day threshold is explicit so a human operator can
reason about "why did this disappear."
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pollypm.atomic_io import atomic_write_text
from pollypm.plugins_builtin.advisor.handlers.assess import AdvisorDecision


logger = logging.getLogger(__name__)


INSIGHTS_DIRNAME = "advisor_insights"
INSIGHT_KIND = "advisor_insight"
DEFAULT_AUTO_CLOSE_DAYS = 7


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AdvisorInsight:
    """One insight entry as it sits in the advisor inbox."""

    insight_id: str            # timestamp-derived unique identifier
    project: str
    kind: str = INSIGHT_KIND
    topic: str = "other"
    severity: str = "suggestion"
    status: str = "open"       # open | closed
    outcome: str = ""          # acknowledged | dismissed | converted | autoclosed
    reason: str = ""           # e.g. "topic_cooldown" on dismissal
    created_at: str = ""       # UTC ISO
    closed_at: str = ""        # UTC ISO when closed
    task_id: str = ""          # optional: linked work-service task id
    converted_task_id: str = ""  # set when convert-to-task runs
    summary: str = ""
    details: str = ""
    suggestion: str = ""
    body_path: str = ""        # relative to base_dir
    commits_reviewed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "insight_id": self.insight_id,
            "project": self.project,
            "kind": self.kind,
            "topic": self.topic,
            "severity": self.severity,
            "status": self.status,
            "outcome": self.outcome,
            "reason": self.reason,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "task_id": self.task_id,
            "converted_task_id": self.converted_task_id,
            "summary": self.summary,
            "details": self.details,
            "suggestion": self.suggestion,
            "body_path": self.body_path,
            "commits_reviewed": list(self.commits_reviewed),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AdvisorInsight":
        commits_raw = raw.get("commits_reviewed") or []
        commits = [str(x) for x in commits_raw] if isinstance(commits_raw, list) else []
        return cls(
            insight_id=str(raw.get("insight_id") or ""),
            project=str(raw.get("project") or ""),
            kind=str(raw.get("kind") or INSIGHT_KIND),
            topic=str(raw.get("topic") or "other"),
            severity=str(raw.get("severity") or "suggestion"),
            status=str(raw.get("status") or "open"),
            outcome=str(raw.get("outcome") or ""),
            reason=str(raw.get("reason") or ""),
            created_at=str(raw.get("created_at") or ""),
            closed_at=str(raw.get("closed_at") or ""),
            task_id=str(raw.get("task_id") or ""),
            converted_task_id=str(raw.get("converted_task_id") or ""),
            summary=str(raw.get("summary") or ""),
            details=str(raw.get("details") or ""),
            suggestion=str(raw.get("suggestion") or ""),
            body_path=str(raw.get("body_path") or ""),
            commits_reviewed=commits,
        )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def insights_dir(base_dir: Path) -> Path:
    return Path(base_dir) / INSIGHTS_DIRNAME


def _sidecar_path(base_dir: Path, insight_id: str) -> Path:
    return insights_dir(base_dir) / f"{insight_id}.json"


def _body_path(base_dir: Path, insight_id: str) -> Path:
    return insights_dir(base_dir) / f"{insight_id}.md"


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _make_insight_id(project: str, timestamp: datetime) -> str:
    """Produce a sortable insight id: ``<project>-<YYYYMMDDTHHMMSSZ>``."""
    stamp = timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(ch if ch.isalnum() else "-" for ch in project).strip("-") or "project"
    return f"{safe}-{stamp}"


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------


def emit_insight(
    base_dir: Path,
    *,
    project: str,
    decision: AdvisorDecision,
    task_id: str = "",
    commits_reviewed: list[str] | None = None,
    now_utc: datetime | None = None,
) -> AdvisorInsight:
    """Write a new advisor_insight inbox entry.

    ``decision`` must be an emit decision. Silent decisions raise
    ``ValueError`` — callers are expected to check ``decision.emit``
    first and skip the inbox write on silent.
    """
    if not decision.emit:
        raise ValueError("emit_insight called with a silent decision")

    ts = now_utc or datetime.now(UTC)
    insight_id = _make_insight_id(project, ts)

    body_lines = [
        f"# Advisor insight — {decision.topic or 'other'} ({decision.severity or 'suggestion'})",
        "",
        f"**Project:** {project}",
        f"**Created:** {ts.isoformat()}",
        "",
        "## Summary",
        "",
        decision.summary or "(no summary)",
        "",
        "## Details",
        "",
        decision.details or "(no details)",
        "",
        "## Suggested next step",
        "",
        decision.suggestion or "(no suggestion)",
        "",
    ]
    body_md = "\n".join(body_lines)

    body_file = _body_path(base_dir, insight_id)
    body_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(body_file, body_md)

    insight = AdvisorInsight(
        insight_id=insight_id,
        project=project,
        topic=decision.topic or "other",
        severity=decision.severity or "suggestion",
        status="open",
        created_at=ts.isoformat(),
        task_id=task_id,
        summary=decision.summary,
        details=decision.details,
        suggestion=decision.suggestion,
        body_path=str(body_file.relative_to(base_dir)),
        commits_reviewed=list(commits_reviewed or []),
    )
    _save_sidecar(base_dir, insight)
    return insight


def _save_sidecar(base_dir: Path, insight: AdvisorInsight) -> None:
    sidecar = _sidecar_path(base_dir, insight.insight_id)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        sidecar,
        json.dumps(insight.to_dict(), indent=2, sort_keys=True) + "\n",
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def list_insights(
    base_dir: Path,
    *,
    status: str | None = "open",
    project: str | None = None,
) -> list[AdvisorInsight]:
    """Return all inbox entries, newest first, optionally filtered."""
    dir_path = insights_dir(base_dir)
    if not dir_path.exists():
        return []
    entries: list[AdvisorInsight] = []
    for sidecar in dir_path.glob("*.json"):
        try:
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        entry = AdvisorInsight.from_dict(raw)
        if status and status != "all" and entry.status != status:
            continue
        if project and entry.project != project:
            continue
        entries.append(entry)
    entries.sort(key=lambda e: e.created_at, reverse=True)
    return entries


def get_insight(base_dir: Path, insight_id: str) -> AdvisorInsight | None:
    sidecar = _sidecar_path(base_dir, insight_id)
    if not sidecar.exists():
        return None
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return AdvisorInsight.from_dict(raw)


# ---------------------------------------------------------------------------
# User actions
# ---------------------------------------------------------------------------


def acknowledge(base_dir: Path, insight_id: str, *, now_utc: datetime | None = None) -> AdvisorInsight | None:
    """Close the insight with outcome=acknowledged."""
    insight = get_insight(base_dir, insight_id)
    if insight is None or insight.status == "closed":
        return insight
    insight.status = "closed"
    insight.outcome = "acknowledged"
    insight.closed_at = (now_utc or datetime.now(UTC)).isoformat()
    _save_sidecar(base_dir, insight)
    return insight


def dismiss(
    base_dir: Path,
    insight_id: str,
    *,
    reason: str = "",
    now_utc: datetime | None = None,
) -> AdvisorInsight | None:
    """Close the insight with outcome=dismissed.

    When ``reason`` is ``"topic_cooldown"``, the caller (CLI / work
    service hook) is expected to also call
    :func:`pollypm.plugins_builtin.advisor.state.record_dismissal` so
    the advisor's next context pack sees the cooldown signal. The
    dismissal here is the inbox-side close; the state write is the
    behavioral-cooldown side. Keeping them separate so callers can
    dismiss without cooldown for other reasons.
    """
    insight = get_insight(base_dir, insight_id)
    if insight is None or insight.status == "closed":
        return insight
    insight.status = "closed"
    insight.outcome = "dismissed"
    insight.reason = reason or ""
    insight.closed_at = (now_utc or datetime.now(UTC)).isoformat()
    _save_sidecar(base_dir, insight)
    return insight


def mark_converted(
    base_dir: Path,
    insight_id: str,
    *,
    converted_task_id: str,
    now_utc: datetime | None = None,
) -> AdvisorInsight | None:
    """Close the insight with outcome=converted, linking the new task id."""
    insight = get_insight(base_dir, insight_id)
    if insight is None:
        return None
    if insight.status == "closed":
        return insight
    insight.status = "closed"
    insight.outcome = "converted"
    insight.converted_task_id = converted_task_id
    insight.closed_at = (now_utc or datetime.now(UTC)).isoformat()
    _save_sidecar(base_dir, insight)
    return insight


# ---------------------------------------------------------------------------
# Convert to work-service task
# ---------------------------------------------------------------------------


def convert_to_task(
    base_dir: Path,
    insight_id: str,
    *,
    work_service: Any,
    flow: str = "implement_module",
    actor: str = "user",
    now_utc: datetime | None = None,
) -> tuple[AdvisorInsight | None, Any]:
    """Create a work-service task from an insight, then close the insight.

    Returns ``(updated_insight, created_task)``. ``work_service`` must
    expose a ``create(...)`` matching the sqlite_service signature.
    The default flow is ``implement_module`` per spec §7.
    """
    insight = get_insight(base_dir, insight_id)
    if insight is None:
        return None, None
    if insight.status == "closed":
        return insight, None

    title = insight.summary or f"Advisor insight {insight.insight_id}"
    description_parts = [
        f"Converted from advisor_insight {insight.insight_id}.",
        f"Topic: {insight.topic}  Severity: {insight.severity}",
        "",
        "## Summary",
        insight.summary,
        "",
        "## Details",
        insight.details,
        "",
        "## Original suggestion",
        insight.suggestion,
    ]
    description = "\n".join(description_parts)

    task = work_service.create(
        title=title,
        description=description,
        type="task",
        project=insight.project,
        flow_template=flow,
        roles={"worker": actor},
        priority="normal",
        labels=["advisor", "converted_from_insight"],
        requires_human_review=False,
    )

    converted_id = getattr(task, "id", None) or getattr(task, "task_id", None) or ""
    if not converted_id:
        proj = getattr(task, "project", "") or insight.project
        num = getattr(task, "task_number", "")
        if proj and num:
            converted_id = f"{proj}/{num}"

    mark_converted(
        base_dir, insight_id,
        converted_task_id=str(converted_id),
        now_utc=now_utc,
    )
    # Reload so the caller sees the post-close state.
    return get_insight(base_dir, insight_id), task


# ---------------------------------------------------------------------------
# Auto-close sweep
# ---------------------------------------------------------------------------


def auto_close_expired(
    base_dir: Path,
    *,
    max_age_days: float = DEFAULT_AUTO_CLOSE_DAYS,
    now_utc: datetime | None = None,
) -> list[AdvisorInsight]:
    """Close open insights older than ``max_age_days``. Returns the closed list."""
    now = now_utc or datetime.now(UTC)
    cutoff = now - timedelta(days=max_age_days)
    closed: list[AdvisorInsight] = []
    for insight in list_insights(base_dir, status="open"):
        try:
            created = datetime.fromisoformat(insight.created_at)
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < cutoff:
            insight.status = "closed"
            insight.outcome = "autoclosed"
            insight.closed_at = now.isoformat()
            _save_sidecar(base_dir, insight)
            closed.append(insight)
    return closed
