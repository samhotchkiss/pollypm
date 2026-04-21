"""Render memory recall into a session-prompt section (M05 / #234).

The load-bearing function here is :func:`build_memory_injection`.
Given a memory backend, a user scope, a project scope, and a short
``task_context_summary``, it calls ``backend.recall(...)`` and renders
the top-N entries into a ``## What you should know`` markdown section
that a session service can prepend to its persona prompt.

Deterministic: for a fixed store, fixed (user, project, task) triple,
the output is byte-identical. We rely on ``MemoryBackend.recall`` being
deterministic for a fixed query+store (it is — SQL ORDER BY + tie-break
by id DESC). The pure render function here doesn't introduce any
non-determinism.

Budget: 4K tokens, approximated as 16K UTF-8 chars (≈4 chars/token).
If the rendered section would exceed the budget, the lowest-score
entries drop out first.

wg02 / #239 extension: :func:`build_worker_protocol_injection` prepends
the canonical worker guide under a "Worker Protocol" heading when the
session role is ``worker``. This sits *above* the memory injection so
every worker session boots knowing the task lifecycle before any
recall-based context.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pollypm.memory_backends import MemoryBackend, MemoryEntry, MemoryType, RecallResult


# Spec: 4K token budget total. A conservative 4 chars/token approximation
# gives a 16K-char hard cap on the rendered injection. The heading line
# and internal separators cost on the order of 100 chars, so the effective
# entry budget is ~15.9K chars — plenty for 15 short bullets.
BUDGET_TOKENS = 4096
CHARS_PER_TOKEN = 4
BUDGET_CHARS = BUDGET_TOKENS * CHARS_PER_TOKEN

# The four types we surface in the "What you should know" section and
# the heading each renders under. Order here is the order in the output.
_SECTION_ORDER: tuple[tuple[str, str], ...] = (
    (MemoryType.USER.value, "About the user:"),
    (MemoryType.FEEDBACK.value, "Feedback from past sessions:"),
    (MemoryType.PROJECT.value, "Project facts:"),
    (MemoryType.PATTERN.value, "Patterns to apply:"),
)

_DEFAULT_TYPES = tuple(name for name, _heading in _SECTION_ORDER)

INJECTION_HEADING = "## What you should know"


@dataclass(slots=True)
class _Scored:
    """Internal pair carrying score-and-entry so we can drop tails."""

    entry: MemoryEntry
    score: float


def compute_task_context_summary(
    *,
    task_title: str | None = None,
    task_description: str | None = None,
    session_role: str | None = None,
    project: str | None = None,
) -> str:
    """Build a compact free-text query string for ``memory.recall``.

    Prefers ``task_title + task_description`` when available; falls back
    to ``session_role + project`` so persona sessions with no explicit
    task still get a meaningful recall query. Deterministic string
    concatenation — identical inputs yield identical output.
    """
    parts: list[str] = []
    if task_title:
        parts.append(task_title.strip())
    if task_description:
        parts.append(task_description.strip())
    if not parts:
        if session_role:
            parts.append(str(session_role))
        if project:
            parts.append(str(project))
    return " ".join(part for part in parts if part).strip()


def build_memory_injection(
    backend: MemoryBackend,
    *,
    user_id: str,
    project_name: str,
    task_context_summary: str,
    types: Iterable[str] | None = None,
    importance_min: int = 3,
    limit: int = 15,
    budget_chars: int = BUDGET_CHARS,
) -> str:
    """Render the ``## What you should know`` section for session start.

    Runs recall against the passed-in backend, scoped to
    ``[("user", user_id), ("project", project_name)]`` and filtered by
    the caller-specified memory types (defaults to the four surfacing
    types from the spec: user, feedback, project, pattern).

    Two-pass strategy:

    1. **Keyword-driven recall** — the caller's ``task_context_summary``
       becomes the FTS5 query. High-signal hits surface first.
    2. **Importance fallback** — we re-query with an empty string so
       entries that matter across any task (user preferences, load-bearing
       project facts) are considered even when no keyword in the summary
       hit them. Results are merged without duplicates.

    Returns an empty string when no relevant memories survive — so a
    brand-new project starts cleanly with no empty section.

    ``budget_chars`` caps the total rendered length. When the recall
    output would exceed the budget, the lowest-scored entries are
    dropped first (the coordinator sorts by score desc before rendering).
    """
    type_filter = list(types) if types is not None else list(_DEFAULT_TYPES)
    scope_pairs = [("user", user_id), ("project", project_name)]
    query = (task_context_summary or "").strip()

    def _safe_recall(q: str) -> list[RecallResult]:
        try:
            return backend.recall(
                query=q,
                scope=scope_pairs,
                types=type_filter,
                importance_min=int(importance_min),
                limit=int(limit),
            )
        except Exception:  # noqa: BLE001 — recall failure must not break session startup
            return []

    primary = _safe_recall(query) if query else []
    fallback = _safe_recall("") if (not query or len(primary) < limit) else []

    # Merge: primary first (preserving order), then fallback entries not
    # already present. Dedupe by entry_id.
    seen: set[int] = set()
    merged: list[RecallResult] = []
    for result in primary:
        if result.entry.entry_id in seen:
            continue
        seen.add(result.entry.entry_id)
        merged.append(result)
    for result in fallback:
        if len(merged) >= limit:
            break
        if result.entry.entry_id in seen:
            continue
        seen.add(result.entry.entry_id)
        merged.append(result)

    if not merged:
        return ""
    return _render_injection(merged, budget_chars=budget_chars)


def _render_injection(
    results: list[RecallResult],
    *,
    budget_chars: int,
) -> str:
    """Render a sorted recall set into the markdown section.

    Groups entries by type, preserves recall score order *within* each
    group, and drops the lowest-scored entries first if the budget
    would be exceeded.
    """
    # Group entries by type, preserving score order.
    by_type: dict[str, list[_Scored]] = {type_value: [] for type_value, _ in _SECTION_ORDER}
    for result in results:
        entry_type = (result.entry.type or "").lower()
        if entry_type in by_type:
            by_type[entry_type].append(_Scored(entry=result.entry, score=float(result.score)))

    if not any(by_type.values()):
        return ""

    # Render once to measure against the budget. If we overflow, drop
    # the lowest-scored entry (across all types) and re-render. This is
    # a simple loop — the worst case is O(limit^2) entry renders, but
    # limit is small (default 15) so it's negligible.
    ordered = _flatten_for_budget(by_type)

    while True:
        rendered = _render_by_type(by_type)
        if len(rendered) <= budget_chars:
            return rendered
        if not ordered:
            # No more entries we can drop — truncate defensively to the
            # budget so callers never see an over-budget payload.
            return rendered[:budget_chars]
        # Drop the lowest-scored surviving entry.
        victim = ordered.pop()
        by_type[victim[0]] = [
            s for s in by_type[victim[0]]
            if s.entry.entry_id != victim[1].entry.entry_id
        ]


def _flatten_for_budget(by_type: dict[str, list[_Scored]]) -> list[tuple[str, _Scored]]:
    """Return a score-descending flat list across all types.

    We pop from the end, so the list is sorted lowest-score-last for
    simple drop semantics. Stable sort — ties preserve SQL order, which
    itself is deterministic via id DESC tie-break in the recall path.
    """
    combined: list[tuple[str, _Scored]] = []
    for type_value, scored in by_type.items():
        for entry in scored:
            combined.append((type_value, entry))
    combined.sort(key=lambda pair: pair[1].score, reverse=True)
    return combined


def _render_by_type(by_type: dict[str, list[_Scored]]) -> str:
    """Render the current grouped state. Empty-group sections are skipped."""
    lines: list[str] = [INJECTION_HEADING, ""]
    any_written = False
    for type_value, heading in _SECTION_ORDER:
        entries = by_type.get(type_value, [])
        if not entries:
            continue
        if any_written:
            lines.append("")
        lines.append(heading)
        for scored in entries:
            lines.append(f"- {_render_entry_bullet(scored.entry)}")
        any_written = True
    if not any_written:
        return ""
    lines.append("")
    return "\n".join(lines)


def _render_entry_bullet(entry: MemoryEntry) -> str:
    """One-line bullet per entry.

    Prefer the entry title (which the file backend renders as the
    salient field of each type). Fall back to a trimmed body when the
    title is missing. Newlines in the source are collapsed so the
    bullet stays single-line.
    """
    raw = (entry.title or entry.body or "").strip()
    return " ".join(raw.split())


def prepend_memory_injection(prompt: str, injection: str) -> str:
    """Prepend the rendered injection to ``prompt``.

    No-op when ``injection`` is empty so a brand-new project's session
    prompt is unchanged. Separates the injection from the caller's
    prompt with a blank line so markdown parsing stays clean.

    The injection string renders with a trailing newline already, so
    we add a single additional newline to produce a blank-line gap
    between the injection and the caller's prompt.
    """
    if not injection:
        return prompt
    if not prompt:
        return injection
    separator = "\n" if injection.endswith("\n") else "\n\n"
    return f"{injection}{separator}{prompt}"


# ---------------------------------------------------------------------------
# Worker protocol injection (wg02 / #239)
# ---------------------------------------------------------------------------

WORKER_PROTOCOL_HEADING = "## Worker Protocol"

# Relative path from the pollypm package to the canonical worker guide
# inside the repo. At runtime we walk up from ``__file__`` to find it.
_WORKER_GUIDE_RELATIVE = "docs/worker-guide.md"


def _locate_worker_guide() -> Path | None:
    """Find docs/worker-guide.md in the enclosing repo.

    We walk up from this file's directory looking for a sibling
    ``docs/worker-guide.md``. In an editable install this resolves to
    the repo's real doc; in a packaged install where the doc isn't
    shipped, the search returns ``None`` and the caller falls through
    to an empty injection (so session startup isn't blocked).
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / _WORKER_GUIDE_RELATIVE
        if candidate.is_file():
            return candidate
    return None


def load_worker_guide_text() -> str:
    """Read the worker guide from disk.

    Returns ``""`` if the doc can't be located so callers never hard-
    fail a session launch on a missing guide. The caller must be
    resilient to an empty return.
    """
    path = _locate_worker_guide()
    if path is None:
        return ""
    try:
        return path.read_text()
    except OSError:  # noqa: BLE001
        return ""


def build_worker_protocol_injection(
    *,
    session_role: str | None,
    guide_text: str | None = None,
) -> str:
    """Render a short ``## Worker Protocol`` pointer for workers.

    Returns an empty string when ``session_role`` is not exactly
    ``"worker"``. Non-worker roles (PM, reviewer, operator, supervisor)
    get nothing — this is intentional, so adding the injection to the
    existing memory-injection path is a no-op for those sessions.

    ``guide_text`` is accepted for compatibility with the old guide-
    loading implementation, but the rendered prompt now points workers
    at ``docs/worker-guide.md`` instead of inlining the whole guide.
    """
    if session_role != "worker":
        return ""
    # Keep the kickoff compact: the full playbook lives in docs/worker-guide.md.
    return (
        f"{WORKER_PROTOCOL_HEADING}\n\n"
        "Read `docs/worker-guide.md` for the worker playbook, including how to "
        "claim work, ship it, and recover."
    )


def prepend_worker_protocol(prompt: str, injection: str) -> str:
    """Prepend a worker-protocol injection to ``prompt``.

    Mirror of :func:`prepend_memory_injection`; kept as a separate
    function so callers can compose the two injections in either order
    and tests can pin the concatenation semantics.
    """
    if not injection:
        return prompt
    if not prompt:
        return injection
    separator = "\n" if injection.endswith("\n") else "\n\n"
    return f"{injection}{separator}{prompt}"
