"""Long-term planner memory — per-user JSONL at ~/.pollypm/memory/planner.jsonl.

Spec §8: the planner gets better over time per user. At the end of each
planning run, one structured JSONL entry is appended capturing:

- what was planned (module count, top risks)
- what shipped vs. what got cancelled (post-run reconciliation)
- opinionated narrative takeaways — "this user's test gates should be
  strict; permissive gates got abused in project X"

At the start of each new planning run, the architect reads a summary
of the memory and the prompt says: "Here are patterns from this user's
past projects. Weight your recommendations accordingly."

This module owns the memory file. Callers (the architect's synthesize
step and pp10's ``pm project replan`` wiring) use it without touching
the filesystem directly.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


MEMORY_DIR = Path.home() / ".pollypm" / "memory"
MEMORY_FILE = MEMORY_DIR / "planner.jsonl"


@dataclass(slots=True)
class PlannerMemoryEntry:
    """One entry in the planner's long-term memory.

    ``timestamp`` — ISO-8601 UTC; stable for ordering across hosts.
    ``project`` — project name the run belonged to.
    ``module_count`` — how many modules the final plan emitted.
    ``selected_candidate`` — tree-of-plans winner (A/B/C).
    ``top_risks`` — short list of the highest-priority risk-ledger
    entries, truncated to 5 for readability. Each entry is a short
    sentence from the critic that raised it.
    ``shipped_modules`` / ``cancelled_modules`` — reconciliation
    counts; populated by ``pm project replan`` when it runs.
    ``takeaways`` — opinionated narrative the architect wrote at
    synthesize time. Free-form markdown.
    """

    project: str
    timestamp: str
    module_count: int
    selected_candidate: str
    top_risks: list[str] = field(default_factory=list)
    shipped_modules: int = 0
    cancelled_modules: int = 0
    takeaways: str = ""

    @classmethod
    def new(
        cls,
        *,
        project: str,
        module_count: int,
        selected_candidate: str,
        top_risks: list[str] | None = None,
        takeaways: str = "",
    ) -> "PlannerMemoryEntry":
        return cls(
            project=project,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            module_count=module_count,
            selected_candidate=selected_candidate,
            top_risks=list(top_risks or [])[:5],
            takeaways=takeaways,
        )


def _memory_path(override: Path | None = None) -> Path:
    """Return the active memory file path (defaults to MEMORY_FILE).

    Tests inject a tmp-path via ``override`` to avoid clobbering the
    user's real memory.
    """
    return override if override is not None else MEMORY_FILE


def append_entry(entry: PlannerMemoryEntry, *, path: Path | None = None) -> Path:
    """Append one memory entry as a single JSON line.

    Creates the parent directory if absent. Fails loudly on I/O error;
    the caller decides whether to swallow.
    """
    target = _memory_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(entry), ensure_ascii=False))
        fh.write("\n")
    return target


def read_entries(
    *,
    project: str | None = None,
    limit: int | None = None,
    path: Path | None = None,
) -> list[PlannerMemoryEntry]:
    """Read memory entries, optionally filtered by project + limited.

    Malformed lines are skipped silently — memory is best-effort
    observability, not a hard dependency. Newest entries come last
    (append-order); callers that want reverse-chronological slice
    the list themselves.
    """
    target = _memory_path(path)
    if not target.is_file():
        return []

    entries: list[PlannerMemoryEntry] = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if project is not None and obj.get("project") != project:
            continue
        try:
            entries.append(PlannerMemoryEntry(**obj))
        except TypeError:
            # Schema drift: ignore entries that don't match current
            # dataclass shape rather than crash the reader.
            continue

    if limit is not None and limit > 0:
        return entries[-limit:]
    return entries


def summarise_for_prompt(entries: list[PlannerMemoryEntry]) -> str:
    """Produce a short markdown summary for the architect's system prompt.

    Deliberately opinionated rather than comprehensive — the architect
    doesn't need every prior plan's module list, they need the pattern.
    """
    if not entries:
        return (
            "<planner-memory>\n"
            "No prior planning runs recorded for this user.\n"
            "</planner-memory>"
        )

    lines = [
        "<planner-memory>",
        f"Planning runs recorded: {len(entries)}.",
        "",
        "Recent patterns:",
    ]
    for entry in entries[-5:]:
        shipped_vs_planned = (
            f"{entry.shipped_modules}/{entry.module_count} shipped"
            if entry.module_count
            else "unknown"
        )
        lines.append(
            f"- {entry.project} ({entry.timestamp}): "
            f"candidate {entry.selected_candidate}, {shipped_vs_planned}."
        )
        if entry.takeaways:
            # Indent takeaways one level so they read as a sub-point.
            for takeaway_line in entry.takeaways.splitlines():
                if takeaway_line.strip():
                    lines.append(f"  > {takeaway_line.strip()}")

    lines.append("")
    lines.append(
        "Weight your recommendations accordingly. Where past plans "
        "failed in similar ways, be more conservative; where they "
        "succeeded, repeat what worked."
    )
    lines.append("</planner-memory>")
    return "\n".join(lines)
