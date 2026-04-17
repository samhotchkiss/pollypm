"""Candidate sourcing for the downtime tick.

Feeds ``downtime_tick_handler.pick_candidate``. Three sources, fed into
a scored selection loop with variety-preserving tie-breaks (spec §4):

1. **Planner output** — ``docs/downtime-backlog.md`` in the project
   root. Markdown-table format (see ``parse_backlog``). Planner writes
   it during Synthesis (dt04).
2. **User-queued** — ``.pollypm-state/downtime-queue.jsonl``. One
   ``Candidate`` per line as JSON; ``pm downtime add`` appends.
3. **Auto-discovered** — hooks for doc-drift / security rotation /
   dep-audit scanners. The scanner bodies live alongside the dt05
   exploration handlers; dt03 provides the hook surface so the scoring
   loop is a single pass.

Scoring rules:

* Base score is the candidate ``priority`` (1–5, 5 best).
* ``-2`` penalty if the candidate's ``kind`` equals the last-scheduled
  kind (variety).
* ``-1`` penalty if the candidate's ``source`` equals the last-scheduled
  source (variety).
* Deterministic tie-break: stable sort by (score, source, title). Tests
  rely on the order.

Dedupe:

* Candidates whose title matches ``state.recent_titles`` (last 20 titles
  of scheduled downtime tasks) are filtered out before scoring.
* User-queue entries are **removed on selection** by rewriting the
  queue file without the winning entry — so a queued idea fires at
  most once unless re-added.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from pollypm.atomic_io import atomic_write_text
from pollypm.plugins_builtin.downtime.settings import (
    KNOWN_CATEGORIES,
    DowntimeSettings,
)
from pollypm.plugins_builtin.downtime.state import DowntimeState


logger = logging.getLogger(__name__)


BACKLOG_RELATIVE_PATH = Path("docs") / "downtime-backlog.md"
USER_QUEUE_RELATIVE_PATH = Path(".pollypm-state") / "downtime-queue.jsonl"


# ---------------------------------------------------------------------------
# Candidate dataclass
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Candidate:
    """A pickable downtime-task candidate.

    ``title`` — one-line headline; also the dedupe key.
    ``kind`` — one of :data:`KNOWN_CATEGORIES`.
    ``description`` — body text the handler hands to the explorer.
    ``priority`` — 1-to-5, 5 best. Defaults to 3 (neutral).
    ``source`` — ``"planner"`` / ``"user"`` / ``"auto"``.
    ``metadata`` — arbitrary kind-specific extras (e.g. branch hints).
    """

    title: str
    kind: str
    description: str
    priority: int = 3
    source: str = "planner"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "kind": self.kind,
            "description": self.description,
            "priority": self.priority,
            "source": self.source,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate | None":
        """Parse a dict (e.g. queue JSON line) into a Candidate.

        Returns ``None`` on any validation failure — unknown kind,
        missing title, etc. Silent skip keeps the sourcer resilient to
        hand-edited queue files.
        """
        if not isinstance(data, dict):
            return None
        title = str(data.get("title") or "").strip()
        kind = str(data.get("kind") or "").strip()
        if not title or kind not in KNOWN_CATEGORIES:
            return None
        description = str(data.get("description") or "").strip()
        try:
            priority = int(data.get("priority", 3))
        except (TypeError, ValueError):
            priority = 3
        priority = max(1, min(5, priority))
        source = str(data.get("source") or "user").strip() or "user"
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            title=title,
            kind=kind,
            description=description,
            priority=priority,
            source=source,
            metadata=dict(metadata),
        )


# ---------------------------------------------------------------------------
# Planner backlog parser
# ---------------------------------------------------------------------------


_TABLE_HEADER_MARKERS: tuple[str, ...] = (
    "title",
    "kind",
)


def parse_backlog(text: str) -> list[Candidate]:
    """Parse ``docs/downtime-backlog.md`` into a list of planner candidates.

    Format (spec §4 / dt04):

    ```
    | title | kind | source | priority | description | why_deprioritized |
    |-------|------|--------|----------|-------------|-------------------|
    | foo   | spec_feature | planner | 4 | Spec out foo | critic deprioritized |
    ```

    Forgiving rules — skip malformed rows silently; never raise. The
    downtime plugin must not crash if the planner writes a nonsense row.
    """
    candidates: list[Candidate] = []
    in_table = False
    headers: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            # End of any active table.
            in_table = False
            headers = []
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not in_table:
            lowered = [c.lower() for c in cells]
            if all(marker in lowered for marker in _TABLE_HEADER_MARKERS):
                headers = lowered
                in_table = True
            continue
        # Skip the divider row (|---|---|...).
        if all(set(c) <= set("-: ") and c for c in cells):
            continue
        row = dict(zip(headers, cells))
        cand = _row_to_candidate(row)
        if cand is not None:
            candidates.append(cand)
    return candidates


def _row_to_candidate(row: dict[str, str]) -> Candidate | None:
    title = (row.get("title") or "").strip()
    kind = (row.get("kind") or "").strip()
    if not title or kind not in KNOWN_CATEGORIES:
        return None
    description = (row.get("description") or "").strip()
    why = (row.get("why_deprioritized") or "").strip()
    if why and why.lower() not in {"-", "n/a", "none"}:
        # Preserve the context so the explorer sees it.
        description = f"{description}\n\n_Why deprioritized:_ {why}".strip()
    try:
        priority_raw = row.get("priority") or "3"
        priority = int(priority_raw)
    except ValueError:
        priority = 3
    priority = max(1, min(5, priority))
    source = (row.get("source") or "planner").strip() or "planner"
    return Candidate(
        title=title,
        kind=kind,
        description=description,
        priority=priority,
        source=source,
    )


# ---------------------------------------------------------------------------
# User-queue reader / writer
# ---------------------------------------------------------------------------


def read_user_queue(path: Path) -> list[Candidate]:
    """Load every Candidate from a JSONL queue file.

    Malformed lines are logged and skipped — the queue file is
    hand-editable so robustness matters more than strict parsing.
    """
    if not path.exists():
        return []
    candidates: list[Candidate] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            logger.debug("downtime: skipping unparseable queue line %d", lineno)
            continue
        cand = Candidate.from_dict(data)
        if cand is None:
            logger.debug("downtime: skipping invalid queue candidate at line %d", lineno)
            continue
        candidates.append(cand)
    return candidates


def remove_from_user_queue(path: Path, candidate: Candidate) -> bool:
    """Rewrite the queue file without the first line matching ``candidate``.

    Match is by (title, kind) — enough to disambiguate in practice.
    Returns ``True`` when a line was removed, ``False`` otherwise (no
    matching line, or the file doesn't exist).
    """
    if not path.exists():
        return False
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False
    out_lines: list[str] = []
    removed = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        keep = True
        if not removed:
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                if (
                    str(data.get("title") or "").strip() == candidate.title
                    and str(data.get("kind") or "").strip() == candidate.kind
                ):
                    keep = False
                    removed = True
        if keep:
            out_lines.append(line)
    if not removed:
        return False
    new_text = "\n".join(out_lines)
    if raw.endswith("\n"):
        new_text = new_text + "\n"
    atomic_write_text(path, new_text)
    return True


def append_to_user_queue(path: Path, candidate: Candidate) -> None:
    """Append a single candidate to the JSONL user queue.

    Creates the parent directory if missing. Uses atomic rewrite so two
    concurrent appends can't tear the file (worst case: one append is
    retried).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    if existing and not existing.endswith("\n"):
        existing = existing + "\n"
    new_text = existing + json.dumps(candidate.to_dict(), sort_keys=True) + "\n"
    atomic_write_text(path, new_text)


# ---------------------------------------------------------------------------
# Auto-discovery scaffolding — stubs; dt05 wires the real scanners.
# ---------------------------------------------------------------------------


AutoDiscoverer = Callable[[Path], list[Candidate]]


def doc_drift_candidates(project_root: Path) -> list[Candidate]:
    """Scan for files changed without docs touched in ≥7 days.

    dt05 supplies the real implementation. For dt03 we return an empty
    list — the scoring loop tolerates empty auto-discovery silently.
    """
    return []


def security_audit_candidate(project_root: Path) -> list[Candidate]:
    """Rotating security-audit scope. dt05 wires the rotation policy."""
    return []


def dep_audit_candidates(project_root: Path) -> list[Candidate]:
    """Surface dep-audit findings as candidates. dt05 wires pip-audit."""
    return []


DEFAULT_AUTO_DISCOVERERS: tuple[AutoDiscoverer, ...] = (
    doc_drift_candidates,
    security_audit_candidate,
    dep_audit_candidates,
)


# ---------------------------------------------------------------------------
# Source aggregation
# ---------------------------------------------------------------------------


def gather_candidates(
    *,
    project_root: Path,
    backlog_path: Path | None = None,
    user_queue_path: Path | None = None,
    auto_discoverers: Sequence[AutoDiscoverer] = DEFAULT_AUTO_DISCOVERERS,
) -> list[Candidate]:
    """Walk all three sources and return the raw (unfiltered) candidate list.

    The scoring / dedupe / selection is a separate step so tests can
    exercise each layer in isolation.
    """
    out: list[Candidate] = []

    if backlog_path is None:
        backlog_path = project_root / BACKLOG_RELATIVE_PATH
    if backlog_path.is_file():
        try:
            text = backlog_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("downtime: backlog read failed: %s", exc)
            text = ""
        out.extend(parse_backlog(text))

    if user_queue_path is None:
        user_queue_path = project_root / USER_QUEUE_RELATIVE_PATH
    out.extend(read_user_queue(user_queue_path))

    for scan in auto_discoverers:
        try:
            out.extend(scan(project_root))
        except Exception as exc:  # noqa: BLE001 - scanners are best-effort
            logger.debug("downtime: auto-discoverer %s failed: %s", scan.__name__, exc)

    return out


# ---------------------------------------------------------------------------
# Scoring + selection
# ---------------------------------------------------------------------------


def score_candidate(
    cand: Candidate, *, state: DowntimeState,
) -> int:
    score = cand.priority
    if state.last_kind and cand.kind == state.last_kind:
        score -= 2
    if state.last_source and cand.source == state.last_source:
        score -= 1
    return score


def pick_candidate(
    *,
    config: Any = None,
    settings: DowntimeSettings,
    state: DowntimeState,
    project_root: Path,
    backlog_path: Path | None = None,
    user_queue_path: Path | None = None,
    auto_discoverers: Sequence[AutoDiscoverer] = DEFAULT_AUTO_DISCOVERERS,
) -> Candidate | None:
    """Select one candidate to schedule this tick.

    Returns ``None`` when no valid candidate is available. Otherwise
    removes the winning candidate from the user queue (if it came from
    there) so it fires at most once per append.
    """
    raw = gather_candidates(
        project_root=project_root,
        backlog_path=backlog_path,
        user_queue_path=user_queue_path,
        auto_discoverers=auto_discoverers,
    )

    # Dedupe by recent titles + disabled categories.
    recent_titles = set(state.recent_titles)
    disabled = set(settings.disabled_categories)
    filtered = [
        c for c in raw
        if c.title not in recent_titles and c.kind not in disabled
    ]

    if not filtered:
        return None

    # Deterministic sort, highest score first.
    scored: list[tuple[int, Candidate]] = [
        (score_candidate(c, state=state), c) for c in filtered
    ]
    scored.sort(key=lambda sc: (-sc[0], sc[1].source, sc[1].title))
    chosen = scored[0][1]

    # User-queue entries are consumed on pick.
    if chosen.source == "user":
        queue_path = user_queue_path if user_queue_path is not None else (
            project_root / USER_QUEUE_RELATIVE_PATH
        )
        try:
            remove_from_user_queue(queue_path, chosen)
        except Exception as exc:  # noqa: BLE001
            logger.debug("downtime: failed to consume user queue entry: %s", exc)

    return chosen
