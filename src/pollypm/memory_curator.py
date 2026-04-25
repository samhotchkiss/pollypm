"""Daily memory-curator handler (M06 / #235).

The curator is a scheduled pass over the memory store that keeps recall
sharp as the store grows. It is intentionally simple — no embeddings,
no LLM calls, no per-type policy. Just four local rules:

1. **TTL sweep** — delete rows whose ``ttl_at`` is in the past.
2. **Dedup** — merge near-duplicate pairs (same ``(scope, type)``,
   high keyword overlap). Keep the higher-importance entry and append
   the loser's body as context.
3. **Episodic → pattern promotion** — when 3+ episodic entries in the
   same project describe the same recurring situation (keyword-overlap
   clustered), queue a pattern-candidate inbox entry for the user to
   approve or reject.
4. **Importance decay** — entries older than 90 days that haven't been
   recalled in 30 days drop importance by 1 (floor 1).

Every action is appended to ``.pollypm/memory-curator.jsonl`` as
one JSON record per line so the user can audit what the curator did.

Observability: a single daily inbox entry summarizes the run. If
nothing changed, no inbox entry is produced so quiet days stay quiet.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from pollypm.atomic_io import atomic_write_text
from pollypm.memory_backends import MemoryBackend, MemoryEntry, MemoryType


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Policy constants — tunable but stable across v1 curator runs
# ---------------------------------------------------------------------------


# Spec: "high keyword overlap" — Jaccard similarity threshold for dedup.
# Pitched high enough that routine boilerplate sharing (same framework
# words across distinct facts) doesn't trigger a merge. Entries with
# one or two load-bearing tokens in common should NOT count as duplicates;
# that's the curator's job.
DEDUP_SIMILARITY_THRESHOLD = 0.8

# Spec: "3+ similar episodic entries" promote to pattern candidate.
EPISODIC_PROMOTION_MIN = 3
EPISODIC_PROMOTION_SIMILARITY = 0.45  # cluster threshold (a bit looser)

# Spec: older than 90 days + unread for 30 days → importance -= 1.
DECAY_AGE_DAYS = 90
DECAY_UNREAD_DAYS = 30
DECAY_FLOOR = 1

# Cap the entries we scan per pass. Cheap safety net on pathological
# stores; real installations are far below this.
MAX_SCAN_ENTRIES = 5000


# ---------------------------------------------------------------------------
# Result dataclasses — what a run produced, for tests + the inbox summary.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CuratorAction:
    """One line of the curator audit log."""

    kind: str                                  # "ttl_sweep" | "dedup" | "decay" | "promotion_candidate"
    entry_id: int | None = None                # primary entry the action operated on
    merged_with_id: int | None = None          # dedup: the loser id
    delta_importance: int | None = None        # decay: the change applied
    candidate_scope: str | None = None         # promotion: scope the pattern applies to
    candidate_summary: str | None = None       # promotion: the suggested pattern body
    details: dict[str, Any] = field(default_factory=dict)

    def to_jsonl(self, timestamp: str) -> str:
        record = {"timestamp": timestamp, **asdict(self)}
        return json.dumps(record, separators=(",", ":"), sort_keys=True)


@dataclass(slots=True)
class CuratorResult:
    """Summary of a curator pass — feeds the daily inbox summary."""

    ttl_deleted: int = 0
    duplicates_merged: int = 0
    decayed: int = 0
    promotion_candidates: int = 0
    actions: list[CuratorAction] = field(default_factory=list)

    def total_changes(self) -> int:
        return (
            self.ttl_deleted
            + self.duplicates_merged
            + self.decayed
            + self.promotion_candidates
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def curate_memory(
    backend: MemoryBackend,
    *,
    log_path: Path | None = None,
    now: datetime | None = None,
) -> CuratorResult:
    """Run one curator pass against ``backend``.

    ``log_path`` — where to append the JSONL audit log (one line per
    action). Optional; when ``None`` actions still appear on the
    :class:`CuratorResult` but nothing is written to disk.

    ``now`` — override for deterministic tests. Defaults to UTC now.
    """
    current = now or datetime.now(UTC)
    result = CuratorResult()

    # 1. TTL sweep — uses the state store so FTS triggers stay in sync.
    result.ttl_deleted = _ttl_sweep(backend, current, result)

    # Re-fetch after deletion so dedup/decay see a consistent view.
    entries = _scan_active_entries(backend)

    # 2. Dedup — near-duplicate merging within (scope, type).
    _dedup_pass(backend, entries, result)

    # Decay and promotion operate on the (possibly reduced) active set.
    entries = _scan_active_entries(backend)

    # 3. Importance decay.
    _decay_pass(backend, entries, current, result)

    # 4. Episodic → pattern promotion candidates.
    _promotion_pass(entries, result)

    if log_path is not None:
        _append_log(log_path, result.actions, current)

    return result


# ---------------------------------------------------------------------------
# TTL sweep
# ---------------------------------------------------------------------------


def _ttl_sweep(
    backend: MemoryBackend,
    now: datetime,
    result: CuratorResult,
) -> int:
    """Delete rows whose ``ttl_at`` is in the past.

    Returns the count of rows removed. Emits one
    :class:`CuratorAction` per removed row so the audit log is at the
    entry level (not the batch level).
    """
    store = _store_from_backend(backend)
    if store is None:
        return 0
    now_iso = now.isoformat()
    # Fetch first so we can log the ids; small query because the set is
    # already filtered by the ttl predicate.
    rows = store.execute(
        """
        SELECT id, scope, kind, title, type
        FROM memory_entries
        WHERE ttl_at IS NOT NULL AND ttl_at <= ?
        """,
        (now_iso,),
    ).fetchall()
    removed = 0
    for row in rows:
        entry_id = int(row[0])
        store.execute("DELETE FROM memory_entries WHERE id = ?", (entry_id,))
        result.actions.append(CuratorAction(
            kind="ttl_sweep",
            entry_id=entry_id,
            details={"scope": row[1], "kind": row[2], "title": row[3], "type": row[4]},
        ))
        removed += 1
    if removed:
        store.commit()
    return removed


# ---------------------------------------------------------------------------
# Dedup pass
# ---------------------------------------------------------------------------


def _dedup_pass(
    backend: MemoryBackend,
    entries: list[MemoryEntry],
    result: CuratorResult,
) -> None:
    """Merge near-duplicates within each (scope, type) bucket.

    "Near-duplicate" = Jaccard similarity of normalized title+body
    token sets >= ``DEDUP_SIMILARITY_THRESHOLD``. When a duplicate is
    found, the entry with the higher importance wins; ties break by
    newer created_at. The loser's body is appended to the winner's
    body under a "Merged from curator" marker, and the loser row is
    deleted.
    """
    store = _store_from_backend(backend)
    if store is None:
        return

    # Group by (scope, type) so dedup stays local. Episodic memories
    # are excluded — each session is a distinct historical event, so
    # "merging" them would erase timeline data the operator may need.
    # The promotion pass handles episodic clustering separately with
    # its own similarity threshold.
    buckets: dict[tuple[str, str], list[MemoryEntry]] = {}
    for entry in entries:
        if entry.type == MemoryType.EPISODIC.value:
            continue
        buckets.setdefault((entry.scope, entry.type), []).append(entry)

    for (scope, type_value), group in buckets.items():
        # Two-pointer search inside the group — O(N^2) per bucket, but
        # buckets are small in practice and bounded by MAX_SCAN_ENTRIES.
        dead: set[int] = set()
        tokens = {entry.entry_id: _tokenize_entry(entry) for entry in group}
        # Sort by importance desc, then by newer created_at desc, so the
        # first entry in any matched pair is the keeper.
        ranked = sorted(
            group,
            key=lambda e: (-int(e.importance), e.created_at),
            reverse=True,
        )
        ranked.sort(
            key=lambda e: (int(e.importance), e.created_at),
            reverse=True,
        )

        for i, winner in enumerate(ranked):
            if winner.entry_id in dead:
                continue
            for loser in ranked[i + 1:]:
                if loser.entry_id in dead:
                    continue
                similarity = _jaccard(tokens[winner.entry_id], tokens[loser.entry_id])
                if similarity < DEDUP_SIMILARITY_THRESHOLD:
                    continue
                _merge_duplicate(store, winner, loser)
                dead.add(loser.entry_id)
                result.duplicates_merged += 1
                result.actions.append(CuratorAction(
                    kind="dedup",
                    entry_id=winner.entry_id,
                    merged_with_id=loser.entry_id,
                    details={
                        "scope": scope,
                        "type": type_value,
                        "similarity": round(similarity, 3),
                    },
                ))


def _merge_duplicate(store, winner: MemoryEntry, loser: MemoryEntry) -> None:
    """Append the loser's body to the winner's, then delete the loser row."""
    appended = winner.body.rstrip()
    marker = "\n\n---\n*Merged from curator — prior duplicate body below.*\n\n"
    new_body = appended + marker + loser.body.rstrip()
    now = datetime.now(UTC).isoformat()
    store.execute(
        "UPDATE memory_entries SET body = ?, updated_at = ? WHERE id = ?",
        (new_body, now, winner.entry_id),
    )
    store.execute("DELETE FROM memory_entries WHERE id = ?", (loser.entry_id,))
    store.commit()


# ---------------------------------------------------------------------------
# Importance decay
# ---------------------------------------------------------------------------


def _decay_pass(
    backend: MemoryBackend,
    entries: list[MemoryEntry],
    now: datetime,
    result: CuratorResult,
) -> None:
    """Drop importance by 1 on stale-and-unread entries (floor 1).

    Today the "unread" signal is absent from the memory schema, so we
    approximate "unread for 30 days" by ``updated_at`` staleness —
    a reader that calls ``read_entry`` or a writer that merges into
    the row both bump ``updated_at``. The approximation undercounts
    cold entries (a successful recall doesn't touch updated_at) but
    it's honest about what we can measure now; M09+ can refine.
    """
    store = _store_from_backend(backend)
    if store is None:
        return
    age_cutoff = (now - timedelta(days=DECAY_AGE_DAYS)).isoformat()
    unread_cutoff = (now - timedelta(days=DECAY_UNREAD_DAYS)).isoformat()

    for entry in entries:
        if entry.importance <= DECAY_FLOOR:
            continue
        if entry.created_at > age_cutoff:
            continue
        # updated_at tracks any write to the row; a "stale" entry
        # hasn't been touched for DECAY_UNREAD_DAYS.
        if entry.updated_at > unread_cutoff:
            continue
        new_importance = max(int(entry.importance) - 1, DECAY_FLOOR)
        if new_importance == entry.importance:
            continue
        store.execute(
            "UPDATE memory_entries SET importance = ?, updated_at = ? WHERE id = ?",
            (new_importance, now.isoformat(), entry.entry_id),
        )
        result.decayed += 1
        result.actions.append(CuratorAction(
            kind="decay",
            entry_id=entry.entry_id,
            delta_importance=new_importance - int(entry.importance),
            details={
                "from": int(entry.importance),
                "to": int(new_importance),
                "age_days": _days_between(entry.created_at, now),
            },
        ))
    if result.decayed:
        store.commit()


# ---------------------------------------------------------------------------
# Episodic → pattern promotion
# ---------------------------------------------------------------------------


def _promotion_pass(
    entries: list[MemoryEntry],
    result: CuratorResult,
) -> None:
    """Cluster episodic entries by keyword overlap; flag 3+ clusters.

    A cluster here is a simple greedy group: walk sorted entries, put
    each into the first cluster whose tokens overlap the entry by
    ``EPISODIC_PROMOTION_SIMILARITY`` or more, else start a new cluster.
    Clusters of size >= ``EPISODIC_PROMOTION_MIN`` produce one
    candidate-pattern action with the shared tokens as the pattern
    body — a starting point the user can approve, edit, or reject.
    """
    # Group by scope so we don't cross projects.
    by_scope: dict[str, list[MemoryEntry]] = {}
    for entry in entries:
        if entry.type != MemoryType.EPISODIC.value:
            continue
        by_scope.setdefault(entry.scope, []).append(entry)

    for scope, episodics in by_scope.items():
        if len(episodics) < EPISODIC_PROMOTION_MIN:
            continue
        clusters: list[list[MemoryEntry]] = []
        token_sets: list[set[str]] = []
        for entry in sorted(episodics, key=lambda e: e.created_at):
            tokens = _tokenize_entry(entry)
            matched = False
            for i, cluster_tokens in enumerate(token_sets):
                if _jaccard(tokens, cluster_tokens) >= EPISODIC_PROMOTION_SIMILARITY:
                    clusters[i].append(entry)
                    token_sets[i] = cluster_tokens | tokens
                    matched = True
                    break
            if not matched:
                clusters.append([entry])
                token_sets.append(tokens)
        for cluster, tokens in zip(clusters, token_sets):
            if len(cluster) < EPISODIC_PROMOTION_MIN:
                continue
            candidate = _build_promotion_candidate(cluster, tokens, scope)
            result.promotion_candidates += 1
            result.actions.append(CuratorAction(
                kind="promotion_candidate",
                candidate_scope=scope,
                candidate_summary=candidate,
                details={
                    "cluster_size": len(cluster),
                    "source_entry_ids": [e.entry_id for e in cluster],
                },
            ))


def _build_promotion_candidate(
    cluster: list[MemoryEntry],
    tokens: set[str],
    scope: str,
) -> str:
    """Produce a one-paragraph pattern-candidate body for the user to edit.

    The prose is intentionally plain so the user can read, approve, and
    edit in one step. Exact wording is not load-bearing — the human
    rewrites it before it graduates to a stored PatternMemory.
    """
    keywords = ", ".join(sorted(t for t in tokens if len(t) > 3)[:6])
    example_ids = ", ".join(f"#{e.entry_id}" for e in cluster[:3])
    return (
        f"Recurring pattern observed in {scope}: keywords={keywords}. "
        f"Seen in {len(cluster)} episodic sessions (examples {example_ids}). "
        f"Review and promote to a procedural pattern memory if it's load-bearing."
    )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _append_log(log_path: Path, actions: Sequence[CuratorAction], now: datetime) -> None:
    """Append each action to the JSONL audit log at ``log_path``.

    Uses one atomic write per run (not per action) so a crash can't
    leave a half-serialized record. The path's parent is created on
    first write.
    """
    if not actions:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = now.isoformat()
    lines = [action.to_jsonl(timestamp) for action in actions]
    existing = ""
    if log_path.exists():
        try:
            existing = log_path.read_text()
        except OSError:
            existing = ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    payload = existing + "\n".join(lines) + "\n"
    atomic_write_text(log_path, payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize_entry(entry: MemoryEntry) -> set[str]:
    """Lowercased word-token set over title + body, minus trivial stopwords.

    The curator only needs a rough overlap signal; we skip 1-2 char
    tokens and a small stop-word list to keep noise out of the Jaccard.
    """
    text = f"{entry.title}\n{entry.body}".lower()
    raw = _TOKEN_RE.findall(text)
    return {token for token in raw if len(token) > 2 and token not in _STOPWORDS}


_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into",
        "our", "your", "you", "yours", "are", "was", "were", "been",
        "being", "have", "has", "had", "but", "not", "any", "all",
        "when", "then", "what", "why", "how", "who", "can", "could",
        "should", "would", "may", "might", "just", "than", "about",
    }
)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _scan_active_entries(backend: MemoryBackend) -> list[MemoryEntry]:
    """Fetch all active entries (non-superseded, non-expired).

    Uses the backend's ``list_entries`` which already routes through
    the recall filter. Capped at ``MAX_SCAN_ENTRIES`` to bound curator
    memory; in practice real stores are far smaller.
    """
    try:
        return backend.list_entries(limit=MAX_SCAN_ENTRIES)
    except Exception:  # noqa: BLE001
        logger.warning("Curator could not enumerate entries; skipping pass")
        return []


def _store_from_backend(backend: MemoryBackend):
    """Resolve the underlying StateStore when ``backend`` exposes one.

    FileMemoryBackend publishes ``backend.store``. Non-file backends
    don't have one — in that case we skip mutating passes and only
    emit read-only promotion candidates (the only phase that doesn't
    require writes).
    """
    return getattr(backend, "store", None)


def _days_between(earlier_iso: str, now: datetime) -> int:
    try:
        earlier = datetime.fromisoformat(earlier_iso)
    except (TypeError, ValueError):
        return 0
    if earlier.tzinfo is None:
        earlier = earlier.replace(tzinfo=UTC)
    delta = now - earlier
    return int(delta.total_seconds() // 86400)


# ---------------------------------------------------------------------------
# Inbox summary builder — consumed by the plugin handler
# ---------------------------------------------------------------------------


def build_inbox_summary(result: CuratorResult) -> str:
    """Render a human-readable one-screen summary of what the curator did.

    Returns an empty string on a quiet run (no changes) so the plugin
    handler can suppress the inbox entry — we don't want the operator
    to wake up to a "curator did nothing" message every morning.
    """
    if result.total_changes() == 0:
        return ""
    lines = [
        "# Memory curator — daily summary",
        "",
        f"- TTL sweep: {result.ttl_deleted} entries deleted",
        f"- Dedup: {result.duplicates_merged} near-duplicates merged",
        f"- Decay: {result.decayed} entries dropped one importance level",
        f"- Promotion candidates: {result.promotion_candidates}",
        "",
    ]
    if result.promotion_candidates:
        lines.append("## Pattern candidates for review")
        lines.append("")
        for action in result.actions:
            if action.kind != "promotion_candidate":
                continue
            lines.append(f"- ({action.candidate_scope}) {action.candidate_summary}")
        lines.append("")
    return "\n".join(lines)
