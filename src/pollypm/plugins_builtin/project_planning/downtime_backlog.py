"""Planner → downtime-backlog integration (dt04).

During Synthesis (stage 6), the planner emits a machine-readable
downtime backlog to ``docs/downtime-backlog.md``. The downtime plugin
reads this file in ``pick_candidate`` to source exploration tasks (spec
§4 / §8). The cross-plugin contract is pure file output — no shared
imports, no direct API call.

Three streams of entries land in the backlog:

1. **Non-winning tree-of-plans candidates** — every candidate the
   architect emitted at stage 2 that didn't win synthesis becomes a
   ``kind=try_alt_approach`` entry. The synthesis rationale supplies
   the "why deprioritized" text.
2. **Critic-deprioritized magic items** — items the architect flagged
   during the Magic stage (stage 4) that critics pushed back on but
   weren't discarded outright. Stored as ``kind=spec_feature`` or
   ``kind=build_speculative`` depending on the architect's hint.
3. **"Explore later" notes** — anything the architect explicitly
   flagged during any stage as worth revisiting after v1 ships. Pass
   as ``explore_later_notes`` to :func:`synthesize_backlog_entries`.

The file format is the markdown table the downtime plugin's
``parse_backlog`` already reads. Merges are title-based: if a row with
the same title already exists in the file, the existing row wins and
the new entry is skipped (planner reruns don't duplicate history).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


BACKLOG_RELATIVE_PATH = Path("docs") / "downtime-backlog.md"


# Columns shipped by the downtime plugin's parser — must stay in lockstep.
BACKLOG_COLUMNS: tuple[str, ...] = (
    "title",
    "kind",
    "source",
    "priority",
    "description",
    "why_deprioritized",
)


# Kinds the downtime plugin knows about. We intentionally avoid importing
# from the downtime plugin (cross-plugin API calls are off-spec); this
# constant mirrors downtime.settings.KNOWN_CATEGORIES and is guarded by a
# sanity test.
KNOWN_DOWNTIME_KINDS: frozenset[str] = frozenset(
    {
        "spec_feature",
        "build_speculative",
        "audit_docs",
        "security_scan",
        "try_alt_approach",
    }
)


@dataclass(slots=True, frozen=True)
class BacklogEntry:
    """One row in ``docs/downtime-backlog.md``."""

    title: str
    kind: str
    priority: int = 3
    description: str = ""
    why_deprioritized: str = ""
    source: str = "planner"

    def as_row(self) -> str:
        """Render as a markdown-table row — pipes escaped to keep the grid clean."""
        cells = [
            _escape_cell(self.title),
            _escape_cell(self.kind),
            _escape_cell(self.source),
            str(max(1, min(5, int(self.priority)))),
            _escape_cell(self.description),
            _escape_cell(self.why_deprioritized),
        ]
        return "| " + " | ".join(cells) + " |"


@dataclass(slots=True, frozen=True)
class MagicItem:
    """A magic-stage idea flagged for the downtime backlog.

    ``kind`` defaults to ``spec_feature`` — the architect may override
    to ``build_speculative`` when the item is concrete enough to
    prototype rather than just spec.
    """

    title: str
    description: str
    why_deprioritized: str = ""
    priority: int = 3
    kind: str = "spec_feature"


def _escape_cell(text: str) -> str:
    """Escape pipe + newline so a cell always stays on one table row."""
    if text is None:
        return ""
    out = str(text).strip()
    out = out.replace("\\", "\\\\")
    out = out.replace("|", "\\|")
    out = out.replace("\n", " ")
    out = out.replace("\r", " ")
    return out


# ---------------------------------------------------------------------------
# Entry synthesis
# ---------------------------------------------------------------------------


def synthesize_backlog_entries(
    *,
    winning_candidate_id: str,
    all_candidate_ids: Sequence[str],
    candidate_titles: dict[str, str] | None = None,
    candidate_descriptions: dict[str, str] | None = None,
    synthesis_rationale: str = "",
    magic_items: Iterable[MagicItem] = (),
    explore_later_notes: Iterable[MagicItem] = (),
) -> list[BacklogEntry]:
    """Build the list of backlog entries the planner should emit.

    Pure function — no filesystem side effects. Tests verify the
    entry shape; :func:`write_backlog` does the merge + disk write.

    * Non-winning candidates → ``try_alt_approach`` entries with the
      synthesis rationale as the "why deprioritized" note.
    * Magic items → ``spec_feature`` / ``build_speculative`` entries,
      carrying whatever critic pushback the architect logged.
    * Explore-later notes → same shape as magic items (the architect
      decides the kind; default is ``spec_feature``).
    """
    titles = dict(candidate_titles or {})
    descriptions = dict(candidate_descriptions or {})
    entries: list[BacklogEntry] = []

    for cid in all_candidate_ids:
        if cid == winning_candidate_id:
            continue
        title = titles.get(cid) or f"Alternative plan: candidate {cid}"
        desc = descriptions.get(cid) or (
            f"Tree-of-plans candidate {cid} — the non-winning decomposition "
            "from the initial planning session."
        )
        entries.append(
            BacklogEntry(
                title=title,
                kind="try_alt_approach",
                description=desc,
                why_deprioritized=(
                    synthesis_rationale
                    or "Not selected by synthesis; see docs/planning-session-log.md."
                ),
                priority=2,
                source="planner",
            )
        )

    for item in magic_items:
        if item.kind not in KNOWN_DOWNTIME_KINDS:
            continue
        entries.append(
            BacklogEntry(
                title=item.title,
                kind=item.kind,
                description=item.description,
                why_deprioritized=item.why_deprioritized,
                priority=item.priority,
                source="planner",
            )
        )

    for note in explore_later_notes:
        if note.kind not in KNOWN_DOWNTIME_KINDS:
            continue
        entries.append(
            BacklogEntry(
                title=note.title,
                kind=note.kind,
                description=note.description,
                why_deprioritized=(
                    note.why_deprioritized
                    or "Architect flagged as 'explore later' during planning."
                ),
                priority=note.priority,
                source="planner",
            )
        )

    return entries


# ---------------------------------------------------------------------------
# File parse + merge + write
# ---------------------------------------------------------------------------


def _parse_existing(path: Path) -> list[dict[str, str]]:
    """Parse an existing backlog file into a list of row dicts.

    Mirrors the downtime plugin's parser — forgiving, skips malformed
    rows, ignores prose. Returns each row as a dict keyed by column name.
    """
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict[str, str]] = []
    in_table = False
    headers: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            in_table = False
            headers = []
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not in_table:
            lowered = [c.lower() for c in cells]
            if "title" in lowered and "kind" in lowered:
                headers = lowered
                in_table = True
            continue
        if all(set(c) <= set("-: ") and c for c in cells):
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def _existing_titles(path: Path) -> set[str]:
    """Collected lowercased titles from an existing backlog for dedupe."""
    return {
        (row.get("title") or "").strip().lower()
        for row in _parse_existing(path)
        if (row.get("title") or "").strip()
    }


def _render_file(entries: Sequence[BacklogEntry]) -> str:
    """Render the full backlog file content from an entry list."""
    header_cells = [col for col in BACKLOG_COLUMNS]
    divider_cells = ["---" for _ in BACKLOG_COLUMNS]

    lines: list[str] = [
        "# Downtime Backlog",
        "",
        (
            "Autogenerated by the planner during Synthesis (stage 6). The "
            "downtime plugin reads this file to source exploration candidates "
            "during idle LLM budget windows. See docs/downtime-plugin-spec.md §4 / §8."
        ),
        "",
        "| " + " | ".join(header_cells) + " |",
        "| " + " | ".join(divider_cells) + " |",
    ]
    for entry in entries:
        lines.append(entry.as_row())
    lines.append("")
    return "\n".join(lines)


def write_backlog(
    project_root: str | Path,
    entries: Sequence[BacklogEntry],
    *,
    relative_path: Path = BACKLOG_RELATIVE_PATH,
) -> Path:
    """Merge ``entries`` into ``docs/downtime-backlog.md`` and return the path.

    Merge semantics:

    * Existing rows are preserved verbatim — planner reruns don't clobber
      history (spec §8 "merge, don't overwrite").
    * Incoming entries with a title already present (case-insensitive)
      are silently dropped.
    * Rows are written in "existing first, then new" order so a human
      scanning the file sees the stable top of the list first.

    Creates ``docs/`` if it doesn't exist. Uses atomic write so partial
    planner crashes can't leave a truncated backlog.
    """
    from pollypm.atomic_io import atomic_write_text

    root = Path(project_root)
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_rows = _parse_existing(path)
    existing_titles = {
        (row.get("title") or "").strip().lower()
        for row in existing_rows
        if (row.get("title") or "").strip()
    }

    # Coerce existing rows back into BacklogEntry so we can re-render
    # through a single serialiser.
    preserved: list[BacklogEntry] = []
    for row in existing_rows:
        title = (row.get("title") or "").strip()
        if not title:
            continue
        kind = (row.get("kind") or "").strip()
        if kind not in KNOWN_DOWNTIME_KINDS:
            continue
        try:
            priority = int(row.get("priority") or 3)
        except ValueError:
            priority = 3
        preserved.append(
            BacklogEntry(
                title=title,
                kind=kind,
                source=(row.get("source") or "planner").strip() or "planner",
                priority=max(1, min(5, priority)),
                description=(row.get("description") or "").strip(),
                why_deprioritized=(row.get("why_deprioritized") or "").strip(),
            )
        )

    merged: list[BacklogEntry] = list(preserved)
    seen = set(existing_titles)
    for entry in entries:
        key = entry.title.strip().lower()
        if not key or key in seen:
            continue
        if entry.kind not in KNOWN_DOWNTIME_KINDS:
            continue
        merged.append(entry)
        seen.add(key)

    atomic_write_text(path, _render_file(merged))
    return path


# ---------------------------------------------------------------------------
# Convenience wrapper — called from the synthesis post-hook.
# ---------------------------------------------------------------------------


def emit_backlog_from_synthesis(
    *,
    project_root: str | Path,
    winning_candidate_id: str,
    all_candidate_ids: Sequence[str],
    synthesis_rationale: str = "",
    candidate_titles: dict[str, str] | None = None,
    candidate_descriptions: dict[str, str] | None = None,
    magic_items: Iterable[MagicItem] = (),
    explore_later_notes: Iterable[MagicItem] = (),
) -> Path:
    """Build + write the downtime backlog in one call.

    The architect (or a post-synthesis hook invoked by the flow engine
    wrapper) runs this after tree-of-plans synthesize(). Pure functional
    decomposition makes the path fully testable without a live
    session.
    """
    entries = synthesize_backlog_entries(
        winning_candidate_id=winning_candidate_id,
        all_candidate_ids=all_candidate_ids,
        candidate_titles=candidate_titles,
        candidate_descriptions=candidate_descriptions,
        synthesis_rationale=synthesis_rationale,
        magic_items=magic_items,
        explore_later_notes=explore_later_notes,
    )
    return write_backlog(project_root, entries)
