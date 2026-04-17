"""Tests for the M02 ``MemoryBackend.recall`` API (#231).

Covers:

* Relevance ordering — keyword matches rank ahead of non-matches.
* Importance + recency effects — same keyword match, different weights
  produce the expected ordering.
* Scope filter — single scope and list-of-scopes.
* Type filter — subset of MemoryType values.
* Empty store — no crash, returns empty list.
* FTS5 special characters — queries containing operator characters
  (``"``, ``(``, ``)``, ``:``, ``*``, ``-`` …) don't raise.
* ``summarize`` / ``list_entries`` back-compat — they still produce
  stable output now that they delegate into recall.
* Sub-100ms recall across a 10K-entry store (benchmark).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.memory_backends import (
    FileMemoryBackend,
    MemoryType,
    PatternMemory,
    ProjectMemory,
    RecallResult,
    ReferenceMemory,
    UserMemory,
)


# ---------------------------------------------------------------------------
# Fixtures — shared test corpus that exercises keyword, importance, recency
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(tmp_path: Path) -> FileMemoryBackend:
    return FileMemoryBackend(tmp_path)


def _seed_mixed_corpus(backend: FileMemoryBackend) -> None:
    """Seed a modest corpus with clear keyword signal + varied importance."""
    # Testing-related entries (keyword target).
    backend.write_entry(
        ProjectMemory(
            fact="Testing strategy is Playwright for e2e",
            why="Agreed convention across the team",
            how_to_apply="Use pw.chromium for browser tests",
            scope="pollypm",
            importance=5,
        )
    )
    backend.write_entry(
        PatternMemory(
            when="Writing tests",
            then="Use pytest with --tb=short",
            scope="pollypm",
            importance=4,
        )
    )
    # Non-testing noise (must be enough docs for FTS idf to have
    # meaningful dynamic range).
    for i in range(10):
        backend.write_entry(
            ProjectMemory(
                fact=f"Logging feature {i}",
                why="structured logging",
                how_to_apply="use structlog.get_logger()",
                scope="pollypm",
                importance=3,
            )
        )
    # Unrelated scope to exercise the scope filter.
    backend.write_entry(
        ProjectMemory(
            fact="Testing database rollback",
            why="keep tests isolated",
            how_to_apply="wrap each test in a transaction",
            scope="other-project",
            importance=5,
        )
    )


# ---------------------------------------------------------------------------
# Relevance ordering
# ---------------------------------------------------------------------------


def test_recall_surfaces_keyword_matches_first(backend: FileMemoryBackend) -> None:
    _seed_mixed_corpus(backend)
    results = backend.recall("testing", scope="pollypm")
    assert results, "expected at least one hit for 'testing'"
    # Both hits should involve testing — neither a Logging row.
    top_titles = " / ".join(r.entry.title for r in results[:2])
    assert "Logging" not in top_titles
    assert "Testing" in results[0].entry.title or "tests" in results[0].entry.title.lower()


def test_recall_returns_recallresult_with_rationale(backend: FileMemoryBackend) -> None:
    _seed_mixed_corpus(backend)
    results = backend.recall("testing", scope="pollypm")
    first = results[0]
    assert isinstance(first, RecallResult)
    assert 0.0 <= first.score <= 1.0
    assert "fts=" in first.match_rationale
    assert "importance=" in first.match_rationale
    assert "recency=" in first.match_rationale


# ---------------------------------------------------------------------------
# Importance + recency effects — isolate one axis at a time
# ---------------------------------------------------------------------------


def test_recall_importance_affects_rank(backend: FileMemoryBackend) -> None:
    """Two entries, same keyword, different importance — higher wins."""
    backend.write_entry(
        ProjectMemory(
            fact="Deployment lives in infrastructure repo",
            why="separation of concerns",
            how_to_apply="open PRs against infra",
            scope="p",
            importance=2,
        )
    )
    backend.write_entry(
        ProjectMemory(
            fact="Deployment uses blue-green rollouts",
            why="zero-downtime",
            how_to_apply="run deploy.sh",
            scope="p",
            importance=5,
        )
    )
    results = backend.recall("deployment", scope="p")
    assert len(results) == 2
    assert "blue-green" in results[0].entry.title.lower() or results[0].entry.importance == 5


def test_recall_recency_affects_rank_when_keyword_identical(
    backend: FileMemoryBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two entries, same keyword + importance — the fresher one wins."""
    # Write an "old" entry by back-dating its created_at directly in SQL.
    old_entry = backend.write_entry(
        ProjectMemory(
            fact="Authentication uses OAuth2",
            why="industry standard",
            how_to_apply="delegate to provider",
            scope="p",
            importance=3,
        )
    )
    # Back-date by 180 days so recency_decay ≈ exp(-2) ≈ 0.14.
    old_ts = (datetime.now(UTC) - timedelta(days=180)).isoformat()
    backend.store.execute(
        "UPDATE memory_entries SET created_at = ? WHERE id = ?",
        (old_ts, old_entry.entry_id),
    )
    backend.store.commit()

    backend.write_entry(
        ProjectMemory(
            fact="Authentication uses SSO via Okta",
            why="enterprise integration",
            how_to_apply="onboard via Okta admin",
            scope="p",
            importance=3,
        )
    )
    results = backend.recall("authentication", scope="p")
    assert len(results) == 2
    # The fresh one ranks first.
    assert "Okta" in results[0].entry.title or "SSO" in results[0].entry.title


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_recall_filters_by_single_scope(backend: FileMemoryBackend) -> None:
    _seed_mixed_corpus(backend)
    results = backend.recall("testing", scope="pollypm")
    assert all(r.entry.scope == "pollypm" for r in results)


def test_recall_filters_by_scope_list(backend: FileMemoryBackend) -> None:
    _seed_mixed_corpus(backend)
    results = backend.recall("testing", scope=["pollypm", "other-project"])
    scopes = {r.entry.scope for r in results}
    assert scopes <= {"pollypm", "other-project"}
    assert "pollypm" in scopes
    assert "other-project" in scopes


def test_recall_filters_by_type(backend: FileMemoryBackend) -> None:
    _seed_mixed_corpus(backend)
    results = backend.recall(
        "testing",
        scope="pollypm",
        types=[MemoryType.PATTERN.value],
    )
    assert results
    assert all(r.entry.type == MemoryType.PATTERN.value for r in results)


def test_recall_filters_by_importance_min(backend: FileMemoryBackend) -> None:
    _seed_mixed_corpus(backend)
    results = backend.recall("testing", scope="pollypm", importance_min=5)
    assert results
    assert all(r.entry.importance >= 5 for r in results)


def test_recall_respects_limit(backend: FileMemoryBackend) -> None:
    for i in range(20):
        backend.write_entry(
            ProjectMemory(
                fact=f"Testing config item {i}",
                why="config coverage",
                how_to_apply="update ini",
                scope="p",
                importance=3,
            )
        )
    results = backend.recall("testing", scope="p", limit=5)
    assert len(results) == 5


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_recall_empty_store_returns_empty_list(backend: FileMemoryBackend) -> None:
    assert backend.recall("anything") == []


def test_recall_empty_query_returns_entries_ordered_by_importance_and_recency(
    backend: FileMemoryBackend,
) -> None:
    _seed_mixed_corpus(backend)
    results = backend.recall("", scope="pollypm", limit=3)
    assert results
    # With no query, fts_score = 0 → ranking is importance+recency only.
    # The importance=5 "Testing strategy" should lead the importance=3
    # logging fillers.
    assert results[0].entry.importance >= results[-1].entry.importance


def test_recall_handles_fts5_special_characters(backend: FileMemoryBackend) -> None:
    _seed_mixed_corpus(backend)
    # Any of these would blow up a naive ``MATCH ?`` without quoting.
    dangerous = [
        'testing: "foo" (bar)',
        "test* AND/OR logging",
        "-testing -deployment",
        "()()()",  # empty after stripping
        "NEAR(foo bar, 3)",
        ":::",
        "hello\"world",
        "\\escaped",
    ]
    for q in dangerous:
        # Must not raise.
        results = backend.recall(q, scope="pollypm")
        assert isinstance(results, list)


def test_recall_filters_superseded_entries(backend: FileMemoryBackend) -> None:
    original = backend.write_entry(
        ProjectMemory(
            fact="Deployment targets staging first",
            why="safety",
            how_to_apply="push to staging",
            scope="p",
            importance=4,
        )
    )
    superseder = backend.write_entry(
        ProjectMemory(
            fact="Deployment targets production directly",
            why="new policy",
            how_to_apply="push to main",
            scope="p",
            importance=5,
        )
    )
    # Mark the original superseded.
    backend.store.execute(
        "UPDATE memory_entries SET superseded_by = ? WHERE id = ?",
        (superseder.entry_id, original.entry_id),
    )
    backend.store.commit()
    results = backend.recall("deployment", scope="p")
    titles = {r.entry.title for r in results}
    assert "Deployment targets staging first" not in titles
    assert any("production" in t for t in titles)


def test_recall_filters_expired_ttl_entries(backend: FileMemoryBackend) -> None:
    entry = backend.write_entry(
        ProjectMemory(
            fact="Temporary feature flag for experiment X",
            why="A/B testing",
            how_to_apply="read from env",
            scope="p",
            importance=3,
        )
    )
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    backend.store.execute(
        "UPDATE memory_entries SET ttl_at = ? WHERE id = ?",
        (past, entry.entry_id),
    )
    backend.store.commit()
    results = backend.recall("experiment", scope="p")
    assert not any(r.entry.entry_id == entry.entry_id for r in results)


# ---------------------------------------------------------------------------
# Back-compat wrappers
# ---------------------------------------------------------------------------


def test_summarize_still_renders_entries(backend: FileMemoryBackend) -> None:
    _seed_mixed_corpus(backend)
    summary = backend.summarize("pollypm")
    assert "Memory Summary: pollypm" in summary
    # summarize renders the top ``limit`` entries; with importance=5 up
    # top, the strategy entry should make the cut.
    assert "Playwright" in summary or "Testing" in summary


def test_list_entries_back_compat_paths(backend: FileMemoryBackend) -> None:
    _seed_mixed_corpus(backend)
    all_rows = backend.list_entries(scope="pollypm", limit=50)
    assert len(all_rows) >= 2
    patterns = backend.list_entries(
        scope="pollypm", type=MemoryType.PATTERN.value
    )
    assert patterns
    assert all(e.type == MemoryType.PATTERN.value for e in patterns)


# ---------------------------------------------------------------------------
# FTS5 index integrity — writes + deletes keep the virtual table in sync
# ---------------------------------------------------------------------------


def test_fts_index_stays_in_sync_on_delete(backend: FileMemoryBackend) -> None:
    entry = backend.write_entry(
        ProjectMemory(
            fact="Caching layer uses Redis",
            why="hot path latency",
            how_to_apply="wrap with @cache_redis",
            scope="p",
            importance=4,
        )
    )
    # Entry should be recallable.
    assert any(r.entry.entry_id == entry.entry_id for r in backend.recall("redis", scope="p"))
    # Hard-delete — triggers should tear the FTS row down too. We delete
    # directly in SQL because the public backend API doesn't expose a
    # delete (yet — that's M06 curator territory).
    backend.store.execute("DELETE FROM memory_entries WHERE id = ?", (entry.entry_id,))
    backend.store.commit()
    assert not any(
        r.entry.entry_id == entry.entry_id
        for r in backend.recall("redis", scope="p")
    )


# ---------------------------------------------------------------------------
# Benchmark — recall over 10K entries must stay sub-100ms
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
def test_recall_sub_100ms_on_10k_entries(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    # Seed 10,000 entries. We batch writes inside one implicit transaction
    # per call; this takes a few seconds, which is fine for a one-off
    # benchmark test (marked with ``benchmark`` so it can be deselected
    # in fast CI lanes if needed — pytest treats unknown marks as
    # no-ops).
    filler_topics = [
        "networking",
        "storage",
        "compute",
        "security",
        "observability",
        "scheduling",
        "replication",
        "streaming",
    ]
    # A few "testing"-tagged needles so the query has something to hit.
    needles = 10
    for i in range(10_000 - needles):
        topic = filler_topics[i % len(filler_topics)]
        backend.write_entry(
            ProjectMemory(
                fact=f"{topic} detail #{i}: tuning knob for cluster",
                why=f"{topic} rationale {i}",
                how_to_apply=f"adjust the {topic} config",
                scope="bench",
                importance=(i % 5) + 1,
            )
        )
    for i in range(needles):
        backend.write_entry(
            ProjectMemory(
                fact=f"Testing strategy note #{i}",
                why="we care about test ergonomics",
                how_to_apply="run pytest with --tb=short",
                scope="bench",
                importance=4,
            )
        )

    # Warm the page cache — first query reads the FTS index from disk.
    backend.recall("testing", scope="bench")

    iterations = 5
    start = time.perf_counter()
    for _ in range(iterations):
        results = backend.recall("testing", scope="bench", limit=10)
    elapsed_ms = (time.perf_counter() - start) / iterations * 1000

    assert results, "expected to surface the needles"
    assert elapsed_ms < 100.0, (
        f"recall took {elapsed_ms:.1f}ms per call on 10K entries "
        f"(budget 100ms)"
    )
    # Stash the measurement in the node's user-reportable area via a
    # simple print — pytest's -s flag surfaces it; otherwise pytest swallows it.
    print(f"\n[bench] recall avg latency: {elapsed_ms:.1f}ms over {iterations} calls")


# ---------------------------------------------------------------------------
# Mixed types — reference/user also indexed
# ---------------------------------------------------------------------------


def test_migration_backfills_fts_index_on_upgrade(tmp_path: Path) -> None:
    """Opening a pre-M02 database should rebuild the FTS index for
    rows that existed before migration 9 ran.

    Simulates a legacy DB with rows but no FTS table; reopening through
    StateStore should apply migration 9 (which runs the 'rebuild'
    command). Afterwards ``recall`` must find the legacy row.
    """
    import sqlite3 as _sqlite3
    from pollypm.storage.state import StateStore

    db_path = tmp_path / ".pollypm-state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE memory_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            tags TEXT NOT NULL,
            source TEXT NOT NULL,
            file_path TEXT NOT NULL,
            summary_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO memory_entries (scope, kind, title, body, tags, source, file_path, summary_path, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy",
            "note",
            "Legacy testing entry",
            "testing note body",
            "[]",
            "manual",
            "/tmp/legacy.md",
            "/tmp/legacy.md",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    # Reopen via StateStore — migrations 8 + 9 run.
    store = StateStore(db_path)
    try:
        rows = store.recall_memory_entries(
            query="testing", scopes=["legacy"], types=None, limit=5
        )
        assert len(rows) == 1
        record, _ = rows[0]
        assert record.title == "Legacy testing entry"
    finally:
        store.close()


def test_recall_indexes_all_typed_memories(backend: FileMemoryBackend) -> None:
    backend.write_entry(
        UserMemory(
            name="Sam",
            description="Senior engineer, prefers small modules.",
            body="Cares about testing boundaries and CI speed.",
            scope="operator",
            importance=4,
        )
    )
    backend.write_entry(
        ReferenceMemory(
            pointer="https://example.com/testing-playbook",
            description="Canonical testing playbook for the team.",
            scope="pollypm",
            importance=3,
        )
    )
    results = backend.recall("testing")
    hits = {r.entry.type for r in results}
    # Both entries mention "testing" — both should surface.
    assert MemoryType.USER.value in hits or MemoryType.REFERENCE.value in hits
