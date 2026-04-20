"""Shared primitives for the per-project dashboard sections (#403).

Every ``_section_*`` helper consumes a small set of constants and
formatting utilities. Centralising them here lets each section file
import only what it needs without circular dependencies between
sections, and gives us a single home for the rendering protocol that
the orchestration layer (``project_dashboard.py``) walks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


_STATUS_ICONS: dict[str, str] = {
    "draft": "\u25cc",
    "queued": "\u25cb",
    "in_progress": "\u27f3",
    "blocked": "\u2298",
    "on_hold": "\u23f8",
    "review": "\u25c9",
    "done": "\u2713",
    "cancelled": "\u2717",
}


_DASHBOARD_DIVIDER_WIDTH = 72
_DASHBOARD_BULLET = "  "  # two-space indent for every row


class SectionRenderer(Protocol):
    """Protocol every section helper conforms to.

    Sections are stateless functions. They take a context-shaped
    payload and return either a single line (``str``) or a list of
    lines (``list[str]``) that the orchestrator joins. Empty lines
    in the returned list are intentional vertical separators.
    """

    def __call__(self, *args: object, **kwargs: object) -> str | list[str]:
        ...


def _dashboard_divider(title: str = "") -> str:
    """Return a section divider line ``\u2500\u2500\u2500 title \u2500\u2500\u2500\u2500\u2500\u2500``."""
    if not title:
        return _DASHBOARD_BULLET + "\u2500" * (_DASHBOARD_DIVIDER_WIDTH - 2)
    prefix = f"\u2500\u2500\u2500 {title} "
    remaining = max(3, _DASHBOARD_DIVIDER_WIDTH - 2 - len(prefix))
    return _DASHBOARD_BULLET + prefix + "\u2500" * remaining


def _format_tokens(n: int) -> str:
    """Human-readable token count: ``1234`` \u2192 ``1.2k``, ``2_100_000`` \u2192 ``2.1M``."""
    if n < 1000:
        return str(int(n))
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _iso_to_dt(value: object):
    """Best-effort ISO-string \u2192 aware datetime. Returns ``None`` on failure."""
    from datetime import UTC, datetime

    if value is None:
        return None
    if hasattr(value, "tzinfo"):
        dt = value  # already a datetime
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _age_from_dt(dt, now=None) -> str:
    """Relative age: '5m ago', '2h ago'. Empty string on None."""
    from datetime import UTC, datetime

    if dt is None:
        return ""
    now = now or datetime.now(UTC)
    secs = (now - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _format_clock(dt) -> str:
    """Render ``HH:MM`` from a datetime for activity timeline rows."""
    from datetime import UTC

    if dt is None:
        return "     "
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.strftime("%H:%M")


def _spark_bar(values: list[int], width: int = 30) -> str:
    """Render a mini spark-line bar chart using Unicode block characters."""
    if not values:
        return ""
    max_val = max(values) or 1
    blocks = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    return "".join(blocks[min(8, int(v / max_val * 8))] for v in values)


def _find_commit_sha(task) -> str | None:
    """Pull a commit SHA from a task's most recent completed execution.

    Walks the task's executions newest-first and looks for an artifact
    with ``kind == ArtifactKind.COMMIT``. Returns the 7-char short SHA
    so the row stays narrow, or ``None`` if no commit was produced.
    """
    try:
        from pollypm.work.models import ArtifactKind
    except Exception:  # noqa: BLE001
        return None
    executions = getattr(task, "executions", None) or []
    for execution in reversed(executions):
        output = getattr(execution, "work_output", None)
        if output is None:
            continue
        for artifact in getattr(output, "artifacts", None) or []:
            if getattr(artifact, "kind", None) == ArtifactKind.COMMIT:
                ref = getattr(artifact, "ref", None)
                if ref:
                    return str(ref)[:7]
    return None


def _task_cycle_minutes(task) -> int | None:
    """Minutes between first in_progress transition and the terminal one.

    Falls back to ``None`` when transitions are missing or dates can't be
    parsed \u2014 keeps the rendering tolerant of partial state on old tasks.
    """
    transitions = getattr(task, "transitions", None) or []
    start = None
    end = None
    for tr in transitions:
        ts = getattr(tr, "timestamp", None)
        to_state = getattr(tr, "to_state", "")
        if to_state == "in_progress" and start is None:
            start = ts
        if to_state in ("done", "cancelled"):
            end = ts
    if start is None or end is None:
        return None
    try:
        return max(0, int((end - start).total_seconds() // 60))
    except (TypeError, ValueError):
        return None


def _aggregate_project_tokens(
    db_path: Path, project_key: str,
) -> tuple[int, int] | None:
    """SUM(total_input_tokens), SUM(total_output_tokens) for ``project_key``.

    Queries ``work_sessions`` directly \u2014 when #86 lands its aggregate
    helper we can swap this out for a single method call. Returns
    ``None`` when the table is missing (old DB) or the query fails, so
    the Tokens line degrades to "(n/a)" rather than breaking the render.
    """
    import sqlite3

    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_input_tokens), 0), "
                "       COALESCE(SUM(total_output_tokens), 0) "
                "FROM work_sessions WHERE task_project = ?",
                (project_key,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)
