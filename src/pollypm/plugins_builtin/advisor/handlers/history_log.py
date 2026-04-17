"""Advisor history log — one JSONL line per decision.

Every advisor run (emit OR silent) appends one line to
``<base_dir>/advisor-log.jsonl``. This is the sole observability
surface the user has for tuning the persona prompt; if the advisor
starts emitting too often, `pm advisor history --stats` (ad04) shows
the emit-rate climbing and we re-tune the prompt — we do NOT add a
system-enforced rate limit.

Schema per line (all strings unless noted; missing fields for silent
runs are stored as None/null):

    {
      "timestamp": "2026-04-16T12:30:00+00:00",  # UTC ISO
      "project": "pollypm",
      "decision": "emit" | "silent",
      "topic": "architecture_drift" | null,
      "severity": "recommendation" | null,
      "summary": "one-sentence crystallization"            # or rationale_if_silent
      "rationale_if_silent": "…"                           # when silent
      "task_id": "pollypm/412"                              # optional, for traceability
      "commits_reviewed": ["abc123", …]                     # optional
    }

Schema is intentionally tolerant — readers (ad03 trajectory pack, ad04
history CLI) handle missing fields gracefully.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


logger = logging.getLogger(__name__)


LOG_FILENAME = "advisor-log.jsonl"


@dataclass(slots=True)
class HistoryEntry:
    """One append-only log line.

    Both ``emit`` and ``silent`` decisions land here. Silent entries set
    ``decision="silent"`` and drop the topic/severity/details fields —
    but ``rationale_if_silent`` is always populated so the user can
    audit the advisor's judgment via `pm advisor history`.
    """

    timestamp: str
    project: str
    decision: str            # "emit" | "silent"
    topic: str | None = None
    severity: str | None = None
    summary: str = ""
    rationale_if_silent: str = ""
    details: str = ""
    suggestion: str = ""
    task_id: str = ""
    commits_reviewed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "timestamp": self.timestamp,
            "project": self.project,
            "decision": self.decision,
            "topic": self.topic,
            "severity": self.severity,
            "summary": self.summary,
            "rationale_if_silent": self.rationale_if_silent,
            "task_id": self.task_id,
            "commits_reviewed": list(self.commits_reviewed),
        }
        if self.decision == "emit":
            out["details"] = self.details
            out["suggestion"] = self.suggestion
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HistoryEntry":
        commits_raw = raw.get("commits_reviewed", []) or []
        commits = [str(x) for x in commits_raw] if isinstance(commits_raw, list) else []
        return cls(
            timestamp=str(raw.get("timestamp") or ""),
            project=str(raw.get("project") or ""),
            decision=str(raw.get("decision") or "silent"),
            topic=raw.get("topic") if isinstance(raw.get("topic"), str) else None,
            severity=raw.get("severity") if isinstance(raw.get("severity"), str) else None,
            summary=str(raw.get("summary") or ""),
            rationale_if_silent=str(raw.get("rationale_if_silent") or ""),
            details=str(raw.get("details") or ""),
            suggestion=str(raw.get("suggestion") or ""),
            task_id=str(raw.get("task_id") or ""),
            commits_reviewed=commits,
        )


def log_path(base_dir: Path) -> Path:
    return Path(base_dir) / LOG_FILENAME


def append_log_entry(base_dir: Path, entry: HistoryEntry) -> None:
    """Atomically append one entry to the advisor log.

    Atomicity is per-line — we open in append mode, serialize one JSON
    object, and write a single newline-terminated line. On partial
    writes (power-cut level) readers skip the malformed trailing line.
    """
    path = log_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry.to_dict(), sort_keys=True) + "\n"
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        logger.warning("advisor: history log append failed: %s", exc)


def append_decision(
    base_dir: Path,
    *,
    project: str,
    decision_json: dict[str, Any],
    task_id: str = "",
    commits_reviewed: Iterable[str] | None = None,
    timestamp: str | None = None,
) -> HistoryEntry:
    """Convenience wrapper: build a HistoryEntry from decision JSON + persist.

    ``decision_json`` is the advisor session's structured output (the
    schema from spec §5). Malformed payloads coerce to a silent entry
    with ``rationale_if_silent="invalid-output"`` so the tick doesn't
    crash and the audit log still reflects that the advisor ran.
    """
    if not isinstance(decision_json, dict):
        decision_json = {
            "emit": False,
            "rationale_if_silent": "invalid-output: non-dict payload",
        }

    emit = bool(decision_json.get("emit"))
    decision = "emit" if emit else "silent"

    entry = HistoryEntry(
        timestamp=timestamp or datetime.now(UTC).isoformat(),
        project=project,
        decision=decision,
        topic=decision_json.get("topic") if isinstance(decision_json.get("topic"), str) else None,
        severity=(
            decision_json.get("severity")
            if isinstance(decision_json.get("severity"), str)
            else None
        ),
        summary=str(decision_json.get("summary") or ""),
        rationale_if_silent=str(decision_json.get("rationale_if_silent") or ""),
        details=str(decision_json.get("details") or ""),
        suggestion=str(decision_json.get("suggestion") or ""),
        task_id=task_id,
        commits_reviewed=list(commits_reviewed or []),
    )
    append_log_entry(base_dir, entry)
    return entry


def read_log(base_dir: Path) -> list[HistoryEntry]:
    """Read every line of the log into ``HistoryEntry`` objects.

    Malformed lines (corrupt JSON) are skipped silently — the log is
    append-only, and a bad line should never block the user from
    reading the rest.
    """
    path = log_path(base_dir)
    if not path.exists():
        return []
    out: list[HistoryEntry] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(HistoryEntry.from_dict(data))
    return out


def recent_entries_for_project(
    base_dir: Path,
    project: str,
    *,
    limit: int = 3,
) -> list[HistoryEntry]:
    """Return the most recent ``limit`` entries for ``project``.

    Used by ad03's context packer to give the advisor its trajectory:
    the last few decisions (emit or silent) so it doesn't repeat itself
    and can escalate severity if a pattern compounds. Newest last so
    the trajectory reads in chronological order when rendered.
    """
    matching = [e for e in read_log(base_dir) if e.project == project]
    if limit <= 0:
        return matching
    return matching[-limit:]


def entries_in_window(
    base_dir: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    project: str | None = None,
    decision: str | None = None,
) -> list[HistoryEntry]:
    """Filter log entries by window / project / decision.

    Accepts either tz-aware or tz-naive bounds; naive bounds are
    assumed to be UTC. Malformed or missing ``timestamp`` values are
    treated as "unknown time" and only included when no bounds apply.
    """
    entries = read_log(base_dir)
    if project:
        entries = [e for e in entries if e.project == project]
    if decision:
        entries = [e for e in entries if e.decision == decision]

    if since is None and until is None:
        return entries

    def _parse(ts: str) -> datetime | None:
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt

    since_utc = since.replace(tzinfo=UTC) if since and since.tzinfo is None else since
    until_utc = until.replace(tzinfo=UTC) if until and until.tzinfo is None else until

    kept: list[HistoryEntry] = []
    for e in entries:
        dt = _parse(e.timestamp)
        if dt is None:
            continue
        if since_utc is not None and dt < since_utc:
            continue
        if until_utc is not None and dt >= until_utc:
            continue
        kept.append(e)
    return kept


def stats(
    base_dir: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Emit-rate per project + topic distribution over a window.

    Default window (when ``since`` is None) is the last 7 days — the
    same default the CLI uses when ``pm advisor history --stats`` is
    invoked without a ``--since`` flag. Returns a dict suitable for
    JSON serialization.
    """
    if since is None:
        since = datetime.now(UTC) - timedelta(days=7)
    if until is None:
        until = datetime.now(UTC)

    entries = entries_in_window(
        base_dir, since=since, until=until, project=project,
    )
    total = len(entries)
    emits = [e for e in entries if e.decision == "emit"]
    silents = [e for e in entries if e.decision == "silent"]

    per_project: dict[str, dict[str, int]] = {}
    topic_distribution: dict[str, int] = {}
    for e in entries:
        bucket = per_project.setdefault(
            e.project, {"emit": 0, "silent": 0, "total": 0},
        )
        bucket[e.decision] = bucket.get(e.decision, 0) + 1
        bucket["total"] += 1
        if e.decision == "emit" and e.topic:
            topic_distribution[e.topic] = topic_distribution.get(e.topic, 0) + 1

    # Emit-rate per project (emit / total), computed last so the entries
    # above stay clean integers.
    for bucket in per_project.values():
        total_p = bucket["total"]
        bucket["emit_rate"] = (
            round(bucket["emit"] / total_p, 4) if total_p else 0.0
        )

    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "total": total,
        "emit_count": len(emits),
        "silent_count": len(silents),
        "emit_rate": round(len(emits) / total, 4) if total else 0.0,
        "per_project": per_project,
        "topic_distribution": topic_distribution,
    }
