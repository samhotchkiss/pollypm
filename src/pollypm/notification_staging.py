"""Notification volume-tiering backend — staging + milestone-boundary digests.

The ``pm notify`` command classifies each notification into one of three
priority tiers:

* ``immediate`` — things requiring a decision or action. Lands in the
  inbox as a work-service task the same as the pre-tiering behaviour.
* ``digest``   — routine progress (task done, PR merged, status update).
  Does NOT surface an inbox task; instead we write a row to the
  ``notification_staging`` table and flush them as one rollup when the
  owning milestone completes (or the project goes idle with staged
  items older than 10 minutes).
* ``silent``   — pure audit trail. No inbox task, no staging row; just
  a ``record_event`` call so the activity feed can pick it up.

The tier can be inferred from subject/body keywords when not explicitly
passed. Classifier rules are intentionally conservative — when in doubt
we fall back to ``immediate`` so the user never silently loses a
notification.

This module is the lifecycle owner for the ``notification_staging``
table. It exposes:

* :func:`classify_priority`        — keyword heuristic
* :func:`stage_notification`       — insert a staging row
* :func:`list_pending`             — pending rows for a (project, milestone)
* :func:`flush_milestone_digest`   — emit one rollup inbox task + mark flushed
* :func:`detect_milestone_completion` — check docs/plan/milestones/ for 100% done
* :func:`check_and_flush_on_done`  — transition hook: milestone-or-idle flush
* :func:`check_regression_on_reopen` — transition hook: milestone regression ping
* :func:`prune_old_staging`        — 30-day hygiene for the plugin handler
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Priority classification
# ---------------------------------------------------------------------------

# Tier-keyword mapping. Match is case-insensitive, word-boundary where the
# token is plain-ASCII so "done" doesn't match "condone". Multi-word
# phrases are matched as a literal substring with whitespace collapsed.
_IMMEDIATE_KEYWORDS: tuple[str, ...] = (
    "blocker",
    "question",
    "rejected",
    "needs decision",
    "stuck",
    "failed",
    "persona swap",
)

_DIGEST_KEYWORDS: tuple[str, ...] = (
    "done",
    "shipped",
    "merged",
    "approved",
    "completed",
)

_SILENT_KEYWORDS: tuple[str, ...] = (
    "test pass",
    "audit",
    "recorded",
)

_VALID_PRIORITIES: frozenset[str] = frozenset({"immediate", "digest", "silent"})


def _has_keyword(text: str, keywords: Iterable[str]) -> bool:
    """Case-insensitive substring match with whitespace normalisation.

    Single-word tokens must match on a word boundary (so "done" hits
    "Done: deploy" but not "condone"). Multi-word phrases match as
    literal substrings after collapsing runs of whitespace.
    """
    haystack = re.sub(r"\s+", " ", text.lower())
    for raw in keywords:
        kw = raw.lower().strip()
        if not kw:
            continue
        if " " in kw:
            if kw in haystack:
                return True
        else:
            # \b on both sides — catches word-boundary single tokens.
            pattern = rf"\b{re.escape(kw)}\b"
            if re.search(pattern, haystack):
                return True
    return False


def classify_priority(subject: str, body: str) -> str:
    """Infer notify priority from subject + body keywords.

    Precedence: immediate > silent > digest > default(immediate). The
    "silent" tier beats "digest" because audit-trail markers ("test
    pass", "recorded") commonly coexist with outcome words ("done")
    and the operator's intent is the quieter tier in that case.

    Returns one of ``'immediate'`` / ``'digest'`` / ``'silent'``.
    """
    text = f"{subject}\n{body}"
    if _has_keyword(text, _IMMEDIATE_KEYWORDS):
        return "immediate"
    if _has_keyword(text, _SILENT_KEYWORDS):
        return "silent"
    if _has_keyword(text, _DIGEST_KEYWORDS):
        return "digest"
    # Ambiguous — over-notify is safer than under-notify.
    return "immediate"


def validate_priority(priority: str) -> str:
    """Normalise and validate a priority string. Raises ValueError on bad input."""
    p = (priority or "").strip().lower()
    if p not in _VALID_PRIORITIES:
        raise ValueError(
            f"Invalid priority '{priority}'. Expected one of: "
            f"{sorted(_VALID_PRIORITIES)}"
        )
    return p


# ---------------------------------------------------------------------------
# Staging table
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ensure_staging_table(conn: sqlite3.Connection) -> None:
    """Create the staging table (and indexes) if missing.

    The canonical migration lives in :mod:`pollypm.work.schema`; this
    helper is a fallback for direct-connection callers (e.g. the
    ``pm notify`` path which opens a bare ``StateStore`` before any
    ``SQLiteWorkService`` ran migrations).
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS notification_staging (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            actor TEXT NOT NULL,
            priority TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            milestone_key TEXT,
            created_at TEXT NOT NULL,
            flushed_at TEXT,
            rollup_task_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_notification_staging_pending
            ON notification_staging(project, milestone_key, flushed_at);
        CREATE INDEX IF NOT EXISTS idx_notification_staging_created
            ON notification_staging(created_at);
        """
    )


def stage_notification(
    conn: sqlite3.Connection,
    *,
    project: str,
    subject: str,
    body: str,
    actor: str,
    priority: str,
    milestone_key: str | None,
    payload: dict[str, Any] | None = None,
) -> int:
    """Insert a single staging row. Returns the new row id.

    ``priority`` must be ``'digest'`` or ``'silent'`` — ``'immediate'``
    never stages (it creates an inbox task instead). The caller is
    responsible for validation; we assert here as a safety net.
    """
    assert priority in {"digest", "silent"}, (
        f"stage_notification called with non-stageable priority {priority!r}"
    )
    _ensure_staging_table(conn)
    payload_dict = dict(payload or {})
    payload_dict.setdefault("subject", subject)
    payload_dict.setdefault("body", body)
    payload_dict.setdefault("actor", actor)
    payload_dict.setdefault("project", project)
    cur = conn.execute(
        "INSERT INTO notification_staging "
        "(project, subject, body, actor, priority, payload_json, "
        "milestone_key, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project,
            subject,
            body,
            actor,
            priority,
            json.dumps(payload_dict, separators=(",", ":"), default=str),
            milestone_key,
            _now_iso(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_pending(
    conn: sqlite3.Connection,
    *,
    project: str,
    milestone_key: str | None,
) -> list[sqlite3.Row]:
    """Return pending digest rows for (project, milestone_key), oldest first.

    "Pending" = ``flushed_at IS NULL`` AND ``priority = 'digest'``. Silent
    rows are skipped — they're audit-only and never show up in rollups.
    ``milestone_key IS NULL`` is matched when ``milestone_key`` is None.
    """
    _ensure_staging_table(conn)
    conn.row_factory = sqlite3.Row
    if milestone_key is None:
        rows = conn.execute(
            "SELECT * FROM notification_staging "
            "WHERE project = ? AND milestone_key IS NULL "
            "AND flushed_at IS NULL AND priority = 'digest' "
            "ORDER BY created_at, id",
            (project,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM notification_staging "
            "WHERE project = ? AND milestone_key = ? "
            "AND flushed_at IS NULL AND priority = 'digest' "
            "ORDER BY created_at, id",
            (project, milestone_key),
        ).fetchall()
    return list(rows)


# ---------------------------------------------------------------------------
# Digest rendering
# ---------------------------------------------------------------------------


def _render_digest_body(
    rows: list[sqlite3.Row],
    *,
    milestone_key: str | None,
    milestone_name: str | None,
) -> str:
    """Compose a markdown rollup body listing each staged notification."""
    if milestone_name:
        header = f"# {milestone_name} — {len(rows)} updates"
    elif milestone_key:
        header = f"# {milestone_key} — {len(rows)} updates"
    else:
        header = f"# Digest — {len(rows)} updates"

    lines: list[str] = [header, ""]
    for r in rows:
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(r["payload_json"]) or {}
        except (TypeError, ValueError):
            payload = {}
        created_at = r["created_at"] or ""
        subject = r["subject"] or "(no subject)"
        body = (r["body"] or "").strip()
        # Short body: first non-empty line, truncated to 200 chars.
        first_line = next(
            (ln for ln in body.splitlines() if ln.strip()), body,
        )
        short = first_line[:200] + ("…" if len(first_line) > 200 else "")

        ref_bits: list[str] = []
        for key in ("commit", "pr", "pull_request", "url"):
            val = payload.get(key)
            if val:
                ref_bits.append(f"{key}={val}")
        ref_suffix = f" — {', '.join(ref_bits)}" if ref_bits else ""

        lines.append(
            f"- **{subject}** ({r['actor']}, {created_at}){ref_suffix}"
        )
        if short:
            lines.append(f"  {short}")
    return "\n".join(lines)


def _milestone_title_components(
    project_path: Path | None,
    milestone_key: str | None,
) -> tuple[str | None, str | None]:
    """Derive (number, name) for a milestone_key of shape ``milestones/NN-name``.

    Returns (None, None) if the key doesn't fit the shape or the file
    can't be read. The number is the leading NN; the name is the
    human-readable title (from the first ``# ...`` heading if the file
    exists, else the slug).
    """
    if not milestone_key or not milestone_key.startswith("milestones/"):
        return None, None
    slug = milestone_key.split("/", 1)[1]
    m = re.match(r"^(\d+)[-_\s]+(.+)$", slug)
    if m:
        number = m.group(1)
        fallback_name = m.group(2).replace("-", " ").replace("_", " ").title()
    else:
        number = None
        fallback_name = slug.replace("-", " ").replace("_", " ").title()

    name = fallback_name
    if project_path is not None:
        md_path = project_path / "docs" / "plan" / "milestones" / f"{slug}.md"
        if md_path.exists():
            try:
                first_line = md_path.read_text(encoding="utf-8").splitlines()[0]
                if first_line.startswith("#"):
                    name = first_line.lstrip("#").strip() or fallback_name
            except (OSError, IndexError):
                pass
    return number, name


# ---------------------------------------------------------------------------
# Flush
# ---------------------------------------------------------------------------


def flush_milestone_digest(
    svc,
    *,
    project: str,
    milestone_key: str | None,
    actor: str = "polly",
    project_path: Path | None = None,
) -> str | None:
    """Collapse pending digest rows into one rollup inbox task.

    Returns the new rollup task_id, or None when there was nothing to
    flush (empty staging → no-op).

    The rollup task is created through the same ``SQLiteWorkService.create``
    path that ``pm notify`` uses so the inbox_view picks it up naturally.
    Staging rows are marked ``flushed_at=now, rollup_task_id=<id>`` atomically
    after the task exists.
    """
    conn: sqlite3.Connection = svc._conn  # type: ignore[attr-defined]
    _ensure_staging_table(conn)

    rows = list_pending(conn, project=project, milestone_key=milestone_key)
    if not rows:
        return None

    number, name = _milestone_title_components(project_path, milestone_key)
    if number is not None and name:
        title = f"Milestone {number} ({name}) ready for review — {len(rows)} updates"
    elif name:
        title = f"{name} — {len(rows)} updates ready for review"
    elif milestone_key:
        title = f"{milestone_key} — {len(rows)} updates ready for review"
    else:
        title = f"Digest — {len(rows)} updates ready for review"

    body_md = _render_digest_body(
        rows, milestone_key=milestone_key, milestone_name=name,
    )

    # Mirror pm notify's inbox-visible shape: chat flow + requester=user.
    task = svc.create(
        title=title,
        description=body_md,
        type="task",
        project=project,
        flow_template="chat",
        roles={"requester": "user", "operator": actor},
        priority="normal",
        created_by=actor,
    )

    now = _now_iso()
    ids = [int(r["id"]) for r in rows]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE notification_staging SET flushed_at = ?, rollup_task_id = ? "
        f"WHERE id IN ({placeholders})",
        [now, task.task_id, *ids],
    )
    conn.commit()
    return task.task_id


# ---------------------------------------------------------------------------
# Milestone detection
# ---------------------------------------------------------------------------


_MILESTONE_HEADING_RE = re.compile(r"^#+\s+(.+)$")
_TASK_LINE_RE = re.compile(
    r"(?:^|\s)(?:task\s+)?(?P<pid>[a-zA-Z0-9_\-]+)/(?P<num>\d+)\b"
)


def _parse_milestone_file(path: Path) -> dict[str, Any]:
    """Extract a lightweight spec from a milestone markdown file.

    Returns a dict with:

    * ``title`` — first ``#`` heading (fallback: filename slug)
    * ``task_ids`` — set of ``project/number`` references mentioned
    * ``labels`` — list of explicit labels (``labels: foo, bar``
      frontmatter-style or ``Labels:`` line in the body)
    * ``keywords`` — other substring hints (``keywords: foo`` line)
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"title": path.stem, "task_ids": set(), "labels": [], "keywords": []}

    title = path.stem
    task_ids: set[str] = set()
    labels: list[str] = []
    keywords: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if title == path.stem:
            m = _MILESTONE_HEADING_RE.match(stripped)
            if m:
                title = m.group(1).strip()
        low = stripped.lower()
        if low.startswith("labels:"):
            rest = stripped.split(":", 1)[1]
            labels.extend(
                t.strip() for t in rest.split(",") if t.strip()
            )
        elif low.startswith("keywords:"):
            rest = stripped.split(":", 1)[1]
            keywords.extend(
                t.strip() for t in rest.split(",") if t.strip()
            )
        for m in _TASK_LINE_RE.finditer(stripped):
            task_ids.add(f"{m.group('pid')}/{m.group('num')}")

    return {
        "title": title,
        "task_ids": task_ids,
        "labels": labels,
        "keywords": keywords,
    }


def _task_matches_milestone(task, spec: dict[str, Any]) -> bool:
    """True when a task should be counted toward a milestone."""
    tid = getattr(task, "task_id", None)
    if tid and tid in spec.get("task_ids", set()):
        return True
    task_labels = set(getattr(task, "labels", []) or [])
    for lbl in spec.get("labels", []):
        if lbl in task_labels:
            return True
    title = (getattr(task, "title", "") or "").lower()
    for kw in spec.get("keywords", []):
        if kw.lower() in title:
            return True
    return False


def _list_milestone_specs(project_path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(milestone_key, spec)`` pairs for each milestone file.

    ``milestone_key`` is the canonical form ``"milestones/<slug>"``
    where slug is the filename minus ``.md``. Returns an empty list if
    the milestones directory is missing.
    """
    md_dir = project_path / "docs" / "plan" / "milestones"
    if not md_dir.is_dir():
        return []
    results: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(md_dir.glob("*.md")):
        spec = _parse_milestone_file(path)
        results.append((f"milestones/{path.stem}", spec))
    return results


def detect_milestone_completion(
    svc,
    project: str,
    completed_task,
    project_path: Path | None,
) -> str | None:
    """Return milestone_key if a milestone just flipped to 100% done, else None.

    A milestone "just flipped" iff (a) ``completed_task`` matches the
    milestone spec AND (b) every other task associated with the milestone
    is already in ``done`` status AND (c) the milestone has ≥1 associated
    task (so an unmatched milestone doesn't trigger on every done).

    Uses :func:`_list_milestone_specs` to enumerate milestones; if the
    project has no milestones directory, returns None (the caller can
    fall back to the project-idle heuristic).
    """
    if project_path is None:
        return None
    specs = _list_milestone_specs(project_path)
    if not specs:
        return None

    try:
        all_tasks = svc.list_tasks(project=project)
    except Exception:  # noqa: BLE001
        logger.debug("detect_milestone_completion: list_tasks failed", exc_info=True)
        return None

    # Late import to avoid a circular import at module load; the status
    # enum lives alongside SQLiteWorkService.
    from pollypm.work.models import WorkStatus

    for milestone_key, spec in specs:
        if not _task_matches_milestone(completed_task, spec):
            continue
        matched = [t for t in all_tasks if _task_matches_milestone(t, spec)]
        if not matched:
            continue
        # Every matched task must be in DONE.
        if all(t.work_status == WorkStatus.DONE for t in matched):
            return milestone_key
    return None


# ---------------------------------------------------------------------------
# Project-idle fallback (no milestones defined)
# ---------------------------------------------------------------------------


_IDLE_KEY = "project-idle"
_IDLE_MIN_AGE_SECONDS = 600  # 10 minutes


def _project_is_idle(svc, project: str) -> bool:
    """True when no tasks in the project are queued/in_progress/review/on_hold.

    Draft tasks don't block idleness — they haven't been picked up yet.
    Terminal (done/cancelled) tasks are ignored. An empty project
    (no non-draft tasks at all) also qualifies.
    """
    from pollypm.work.models import WorkStatus

    try:
        tasks = svc.list_tasks(project=project)
    except Exception:  # noqa: BLE001
        return False
    for t in tasks:
        if t.work_status in (
            WorkStatus.QUEUED,
            WorkStatus.IN_PROGRESS,
            WorkStatus.REVIEW,
            WorkStatus.ON_HOLD,
            WorkStatus.BLOCKED,
        ):
            return False
    return True


def _has_old_pending_idle_rows(
    conn: sqlite3.Connection,
    *,
    project: str,
    min_age_seconds: int = _IDLE_MIN_AGE_SECONDS,
) -> bool:
    """True when ≥1 pending idle-bucket row is older than ``min_age_seconds``."""
    _ensure_staging_table(conn)
    cutoff = (datetime.now(UTC) - timedelta(seconds=min_age_seconds)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM notification_staging "
        "WHERE project = ? AND milestone_key = ? AND flushed_at IS NULL "
        "AND priority = 'digest' AND created_at <= ?",
        (project, _IDLE_KEY, cutoff),
    ).fetchone()
    return bool(row and row[0])


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------


def _task_had_flushed_rollup(
    conn: sqlite3.Connection,
    task_id: str,
) -> str | None:
    """Return the milestone_key of a prior flushed rollup that referenced this task.

    We can't direct-match a task_id to a milestone_key without parsing
    payload bodies; instead, we conservatively answer: "did any flushed
    staging row carry this task_id in its payload?". This covers the
    case where a digest rollup bundled the task's "done" notification
    before the task was re-opened.
    """
    _ensure_staging_table(conn)
    rows = conn.execute(
        "SELECT milestone_key, payload_json FROM notification_staging "
        "WHERE flushed_at IS NOT NULL "
        "ORDER BY flushed_at DESC LIMIT 500"
    ).fetchall()
    needle = f'"task_id": "{task_id}"'
    needle_compact = f'"task_id":"{task_id}"'
    for r in rows:
        payload = r[1] or ""
        if needle in payload or needle_compact in payload or task_id in payload:
            return r[0]
    return None


def check_regression_on_reopen(
    svc,
    *,
    project: str,
    task_id: str,
    from_state: str,
    to_state: str,
    actor: str = "system",
) -> str | None:
    """Emit an immediate inbox item when a previously-flushed task re-opens.

    Returns the new regression task_id, or None if no regression fires.
    A regression fires iff the transition moves away from ``done`` AND
    a prior flushed rollup referenced the task. We deliberately do NOT
    re-flush the original rollup — that would bundle old and new work.
    """
    if from_state != "done":
        return None
    if to_state == "done":
        return None

    conn: sqlite3.Connection = svc._conn  # type: ignore[attr-defined]
    milestone_key = _task_had_flushed_rollup(conn, task_id)
    if milestone_key is None:
        return None

    subject = f"Regression: {milestone_key} re-opened (task {task_id})"
    body = (
        f"Task `{task_id}` was part of the already-flushed rollup for "
        f"**{milestone_key}** but just transitioned "
        f"`{from_state}` → `{to_state}`.\n\n"
        f"The original rollup is not re-sent; review this task and "
        f"re-land it, or cancel if it is obsolete."
    )
    try:
        regression = svc.create(
            title=subject,
            description=body,
            type="task",
            project=project,
            flow_template="chat",
            roles={"requester": "user", "operator": actor},
            priority="high",
            created_by=actor,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("regression notify failed for %s: %s", task_id, exc)
        return None
    return regression.task_id


# ---------------------------------------------------------------------------
# Transition hook — called from sqlite_service after a task moves to done.
# ---------------------------------------------------------------------------


def check_and_flush_on_done(
    svc,
    *,
    project: str,
    completed_task,
    actor: str = "polly",
    project_path: Path | None,
) -> str | None:
    """Fire a milestone (or project-idle) flush if appropriate.

    Returns the rollup task_id if a flush fired, None otherwise.
    Safe to call on every task.mark_done / flow-terminal transition —
    internally gated on milestone completion or project-idle with old
    staged rows.
    """
    # Primary path: docs/plan/milestones/*.md completion.
    milestone_key = detect_milestone_completion(
        svc, project, completed_task, project_path,
    )
    if milestone_key is not None:
        return flush_milestone_digest(
            svc,
            project=project,
            milestone_key=milestone_key,
            actor=actor,
            project_path=project_path,
        )

    # Fallback: no milestones defined, project idle, staged rows ≥10min old.
    # Only fires if a milestones dir is absent — when the dir exists, the
    # user is opting in to milestone-boundary flushes explicitly.
    if project_path is not None:
        md_dir = project_path / "docs" / "plan" / "milestones"
        if md_dir.is_dir():
            return None
    if not _project_is_idle(svc, project):
        return None
    conn: sqlite3.Connection = svc._conn  # type: ignore[attr-defined]
    if not _has_old_pending_idle_rows(conn, project=project):
        return None
    return flush_milestone_digest(
        svc,
        project=project,
        milestone_key=_IDLE_KEY,
        actor=actor,
        project_path=project_path,
    )


# ---------------------------------------------------------------------------
# Pruning (scheduled handler payload)
# ---------------------------------------------------------------------------


def prune_old_staging(
    conn: sqlite3.Connection,
    *,
    retain_days: int = 30,
) -> dict[str, int]:
    """Delete flushed rows + silent rows older than ``retain_days``.

    Pending digest rows are never pruned — they belong to a milestone
    that simply hasn't closed yet. Returns a summary for logging.
    """
    _ensure_staging_table(conn)
    cutoff = (datetime.now(UTC) - timedelta(days=retain_days)).isoformat()

    cur = conn.execute(
        "DELETE FROM notification_staging "
        "WHERE flushed_at IS NOT NULL AND flushed_at <= ?",
        (cutoff,),
    )
    flushed_deleted = cur.rowcount or 0

    cur = conn.execute(
        "DELETE FROM notification_staging "
        "WHERE priority = 'silent' AND created_at <= ?",
        (cutoff,),
    )
    silent_deleted = cur.rowcount or 0

    conn.commit()
    return {
        "flushed_pruned": int(flushed_deleted),
        "silent_pruned": int(silent_deleted),
    }


__all__ = [
    "classify_priority",
    "validate_priority",
    "stage_notification",
    "list_pending",
    "flush_milestone_digest",
    "detect_milestone_completion",
    "check_and_flush_on_done",
    "check_regression_on_reopen",
    "prune_old_staging",
]
