"""Tests for the daily memory-curator handler (M06 / #235).

Cover:

* TTL sweep — entries with ``ttl_at`` in the past are deleted and
  logged; entries with future / null TTL are untouched.
* Dedup — near-duplicates within the same (scope, type) are merged;
  higher-importance row wins and carries a "merged from" marker.
* Importance decay — rows older than 90 days with stale ``updated_at``
  drop by one importance level (floor 1); fresher rows untouched.
* Episodic → pattern promotion — 3+ similar episodic entries in the
  same project yield a promotion-candidate action.
* Integration — a seeded 100-entry store shrinks under a curator
  pass and the audit log captures the actions.
* Inbox summary — quiet runs produce an empty summary; non-trivial
  runs produce the spec's sections.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.memory_backends import (
    EpisodicMemory,
    FeedbackMemory,
    FileMemoryBackend,
    MemoryType,
    PatternMemory,
    ProjectMemory,
    UserMemory,
)
from pollypm.memory_curator import (
    DECAY_AGE_DAYS,
    DECAY_UNREAD_DAYS,
    EPISODIC_PROMOTION_MIN,
    build_inbox_summary,
    curate_memory,
)


# ---------------------------------------------------------------------------
# Helpers — age injection so tests can simulate the 90-day / 30-day windows.
# ---------------------------------------------------------------------------


def _back_date_row(backend: FileMemoryBackend, entry_id: int, *, created: datetime, updated: datetime) -> None:
    """Rewrite a row's created_at / updated_at so decay tests can exercise
    the age predicates without waiting 90 days."""
    backend.store.execute(
        "UPDATE memory_entries SET created_at = ?, updated_at = ? WHERE id = ?",
        (created.isoformat(), updated.isoformat(), entry_id),
    )
    backend.store.commit()


def _set_ttl(backend: FileMemoryBackend, entry_id: int, *, ttl_at: datetime) -> None:
    backend.store.execute(
        "UPDATE memory_entries SET ttl_at = ? WHERE id = ?",
        (ttl_at.isoformat(), entry_id),
    )
    backend.store.commit()


# ---------------------------------------------------------------------------
# TTL sweep
# ---------------------------------------------------------------------------


def test_ttl_sweep_deletes_expired_entries(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    past = backend.write_entry(
        ProjectMemory(fact="will expire", why="short-lived", how_to_apply="ignore", scope="pollypm")
    )
    future = backend.write_entry(
        ProjectMemory(fact="still active", why="long-lived", how_to_apply="keep", scope="pollypm")
    )
    now = datetime.now(UTC)
    _set_ttl(backend, past.entry_id, ttl_at=now - timedelta(days=1))
    _set_ttl(backend, future.entry_id, ttl_at=now + timedelta(days=7))

    result = curate_memory(backend, now=now)

    assert result.ttl_deleted == 1
    ids = [e.entry_id for e in backend.list_entries()]
    assert future.entry_id in ids
    assert past.entry_id not in ids


def test_ttl_sweep_ignores_null_ttl(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    backend.write_entry(
        ProjectMemory(fact="forever", why="core", how_to_apply="always", scope="pollypm")
    )
    result = curate_memory(backend, now=datetime.now(UTC))
    assert result.ttl_deleted == 0
    assert len(backend.list_entries()) == 1


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_dedup_merges_near_duplicates(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    # Two highly overlapping project facts.
    keep = backend.write_entry(
        ProjectMemory(
            fact="test runner is pytest short traceback",
            why="team convention for pollypm",
            how_to_apply="run pytest tb short before commit",
            scope="pollypm",
            importance=5,
        )
    )
    loser = backend.write_entry(
        ProjectMemory(
            fact="test runner pytest short traceback project",
            why="team convention pollypm",
            how_to_apply="run pytest tb short before commit always",
            scope="pollypm",
            importance=3,
        )
    )
    result = curate_memory(backend, now=datetime.now(UTC))
    assert result.duplicates_merged == 1
    entries = backend.list_entries(scope="pollypm", type=MemoryType.PROJECT.value)
    assert len(entries) == 1
    survivor = entries[0]
    assert survivor.entry_id == keep.entry_id
    assert "Merged from curator" in survivor.body


def test_dedup_leaves_distinct_entries_alone(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    backend.write_entry(
        ProjectMemory(fact="test runner is pytest", why="w", how_to_apply="h", scope="pollypm")
    )
    backend.write_entry(
        ProjectMemory(fact="deploy target is railway", why="w", how_to_apply="h", scope="pollypm")
    )
    result = curate_memory(backend, now=datetime.now(UTC))
    assert result.duplicates_merged == 0
    assert len(backend.list_entries(scope="pollypm")) == 2


# ---------------------------------------------------------------------------
# Importance decay
# ---------------------------------------------------------------------------


def test_decay_drops_importance_on_stale_old_entry(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    entry = backend.write_entry(
        ProjectMemory(
            fact="stale fact",
            why="no longer touched",
            how_to_apply="ignore",
            scope="pollypm",
            importance=4,
        )
    )
    now = datetime.now(UTC)
    # Back-date: created 120 days ago, updated 60 days ago.
    _back_date_row(
        backend,
        entry.entry_id,
        created=now - timedelta(days=DECAY_AGE_DAYS + 30),
        updated=now - timedelta(days=DECAY_UNREAD_DAYS + 30),
    )
    result = curate_memory(backend, now=now)
    assert result.decayed == 1
    refreshed = backend.read_entry(entry.entry_id)
    assert refreshed is not None
    assert refreshed.importance == 3


def test_decay_respects_floor(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    entry = backend.write_entry(
        ProjectMemory(
            fact="minimum", why="w", how_to_apply="h", scope="pollypm", importance=1
        )
    )
    now = datetime.now(UTC)
    _back_date_row(
        backend,
        entry.entry_id,
        created=now - timedelta(days=DECAY_AGE_DAYS + 30),
        updated=now - timedelta(days=DECAY_UNREAD_DAYS + 30),
    )
    result = curate_memory(backend, now=now)
    # Floor 1 — no change, no action.
    assert result.decayed == 0
    refreshed = backend.read_entry(entry.entry_id)
    assert refreshed is not None
    assert refreshed.importance == 1


def test_decay_ignores_fresh_entries(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    backend.write_entry(
        ProjectMemory(
            fact="fresh", why="w", how_to_apply="h", scope="pollypm", importance=4
        )
    )
    result = curate_memory(backend, now=datetime.now(UTC))
    assert result.decayed == 0


# ---------------------------------------------------------------------------
# Episodic → pattern promotion
# ---------------------------------------------------------------------------


def test_promotion_candidate_when_cluster_meets_threshold(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    base = "Worker hit a flaky pytest run and retried with short traceback to diagnose"
    for i in range(EPISODIC_PROMOTION_MIN):
        backend.write_entry(
            EpisodicMemory(
                summary=f"{base} session {i}",
                session_id=f"s-{i}",
                started_at=f"2026-04-{10 + i:02d}T09:00:00Z",
                ended_at=f"2026-04-{10 + i:02d}T10:00:00Z",
                scope="pollypm",
            )
        )
    result = curate_memory(backend, now=datetime.now(UTC))
    assert result.promotion_candidates == 1
    action = next(a for a in result.actions if a.kind == "promotion_candidate")
    assert action.candidate_scope == "pollypm"
    assert "pattern" in (action.candidate_summary or "").lower()


def test_promotion_skipped_when_cluster_below_threshold(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    for i in range(EPISODIC_PROMOTION_MIN - 1):
        backend.write_entry(
            EpisodicMemory(
                summary=f"Worker hit flaky test {i}",
                session_id=f"s-{i}",
                started_at=f"2026-04-1{i}T09:00:00Z",
                ended_at=f"2026-04-1{i}T10:00:00Z",
                scope="pollypm",
            )
        )
    result = curate_memory(backend, now=datetime.now(UTC))
    assert result.promotion_candidates == 0


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_audit_log_appends_one_line_per_action(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    entry = backend.write_entry(
        ProjectMemory(fact="expired", why="w", how_to_apply="h", scope="pollypm")
    )
    now = datetime.now(UTC)
    _set_ttl(backend, entry.entry_id, ttl_at=now - timedelta(hours=1))

    log_path = tmp_path / ".pollypm" / "memory-curator.jsonl"
    result = curate_memory(backend, log_path=log_path, now=now)
    assert result.ttl_deleted == 1
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["kind"] == "ttl_sweep"
    assert record["entry_id"] == entry.entry_id
    assert record["timestamp"] == now.isoformat()


def test_audit_log_quiet_run_writes_nothing(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    backend.write_entry(
        ProjectMemory(fact="fine", why="w", how_to_apply="h", scope="pollypm")
    )
    log_path = tmp_path / ".pollypm" / "memory-curator.jsonl"
    result = curate_memory(backend, log_path=log_path, now=datetime.now(UTC))
    assert result.total_changes() == 0
    assert not log_path.exists()


# ---------------------------------------------------------------------------
# Integration — seeded 100-entry store
# ---------------------------------------------------------------------------


def test_integration_curator_shrinks_seeded_store(tmp_path: Path) -> None:
    """Exercise TTL sweep + dedup + decay on a populated store."""
    backend = FileMemoryBackend(tmp_path)
    now = datetime.now(UTC)

    # 20 expired TTL entries.
    expired_ids = []
    for i in range(20):
        e = backend.write_entry(
            ProjectMemory(
                fact=f"expired fact {i}",
                why="short-lived",
                how_to_apply="ignore",
                scope="pollypm",
            )
        )
        _set_ttl(backend, e.entry_id, ttl_at=now - timedelta(days=1))
        expired_ids.append(e.entry_id)

    # 10 near-duplicate pairs (20 entries total, should dedup to 10).
    for i in range(10):
        backend.write_entry(
            ProjectMemory(
                fact=f"shared fact {i} alpha beta gamma delta epsilon",
                why=f"same reason {i}",
                how_to_apply=f"same steps {i}",
                scope="pollypm",
                importance=4,
            )
        )
        backend.write_entry(
            ProjectMemory(
                fact=f"shared fact {i} alpha beta gamma delta epsilon zeta",
                why=f"same reason {i} minor rewording",
                how_to_apply=f"same steps {i}",
                scope="pollypm",
                importance=2,
            )
        )

    # 30 old/stale rows eligible for decay. Each carries truly distinct
    # vocabulary (bodies don't share boilerplate) so dedup leaves them
    # alone; dedup triggers only on the paired-dupes above.
    decay_lines = [
        ("postgres", "tune shared_buffers carefully", "database sizing"),
        ("redis", "maxmemory-policy allkeys-lru", "caching strategy"),
        ("docker", "multistage builds trim image size", "container hygiene"),
        ("kubernetes", "resource limits prevent noisy neighbors", "scheduling"),
        ("terraform", "remote state needs locking", "infrastructure"),
        ("ansible", "idempotent handlers matter", "orchestration"),
        ("jenkins", "pipeline as code in jenkinsfile", "ci strategy"),
        ("grafana", "templated dashboards beat duplicates", "observability"),
        ("prometheus", "recording rules reduce query cost", "metrics"),
        ("elasticsearch", "shard planning is not retroactive", "search"),
        ("kafka", "compacted topics dedupe by key", "messaging"),
        ("rabbitmq", "ack model guards delivery", "queues"),
        ("nginx", "worker_connections capped by ulimit", "reverse proxy"),
        ("haproxy", "maxconn bounds backpressure", "load balancer"),
        ("cassandra", "read path beats quorum writes", "wide column"),
        ("mongodb", "replica set elections stall writes", "document store"),
        ("mysql", "innodb buffer sized for working set", "relational"),
        ("nodejs", "event loop blocks on cpu bound work", "javascript runtime"),
        ("rust", "borrow checker shapes ownership", "systems language"),
        ("golang", "goroutine leaks via unbuffered channels", "concurrency"),
        ("python", "gil serializes cpu bound threads", "interpreter"),
        ("ruby", "gc copies aging objects", "memory"),
        ("elixir", "supervisor trees isolate failure", "otp"),
        ("scala", "implicit conversions surprise", "jvm language"),
        ("clojure", "persistent structures share tails", "lisp"),
        ("haskell", "laziness defers evaluation", "types"),
        ("ocaml", "module system is first class", "functional"),
        ("lua", "tables encode everything", "scripting"),
        ("kotlin", "null safety lifts nulls into types", "android"),
        ("swift", "protocol extensions add method dispatch", "ios"),
    ]
    decay_targets = []
    for i, (a, b, c) in enumerate(decay_lines):
        e = backend.write_entry(
            ProjectMemory(
                fact=f"{a}: {b}",
                why=f"learned while profiling {c}",
                how_to_apply=f"check {a} {c} before rollout",
                scope="pollypm",
                importance=4,
            )
        )
        _back_date_row(
            backend,
            e.entry_id,
            created=now - timedelta(days=DECAY_AGE_DAYS + 10),
            updated=now - timedelta(days=DECAY_UNREAD_DAYS + 10),
        )
        decay_targets.append(e.entry_id)

    # 30 fresh rows with distinct vocabulary to hit the scan bound
    # without triggering dedup actions.
    fresh_lines = [
        ("playwright", "chromium selector smoke tests"),
        ("cypress", "command chain retries implicit"),
        ("jest", "snapshot files committed under control"),
        ("vitest", "shared jest api with vite speed"),
        ("mocha", "bdd style describe it"),
        ("chai", "should versus expect readability"),
        ("sinon", "stub sandbox tears down hooks"),
        ("karma", "browser orchestration legacy"),
        ("jasmine", "builtin assertion library dsl"),
        ("enzyme", "shallow render component tree"),
        ("storybook", "component gallery renders stories"),
        ("webpack", "code splitting reduces bundle"),
        ("rollup", "tree shaking library output"),
        ("vite", "esbuild powered dev server"),
        ("parcel", "zero configuration bundler"),
        ("babel", "plugin driven transform pipeline"),
        ("eslint", "plugin architecture with shareable rules"),
        ("prettier", "opinionated formatter eliminates debate"),
        ("typescript", "structural typing catches shape bugs"),
        ("flow", "nominal types differ from structural"),
        ("redux", "single store with reducer composition"),
        ("mobx", "observables track autorun dependencies"),
        ("recoil", "atoms and selectors react state"),
        ("zustand", "tiny hook based state library"),
        ("jotai", "atomic units compose"),
        ("apollo", "normalized cache across graphql"),
        ("relay", "fragment colocation pays off"),
        ("urql", "exchanges modular request pipeline"),
        ("swr", "stale while revalidate caching"),
        ("tanstack", "query cache plus mutations"),
    ]
    for i, (a, b) in enumerate(fresh_lines):
        backend.write_entry(
            ProjectMemory(
                fact=f"{a}: {b}",
                why=f"captured during onboarding for {a}",
                how_to_apply=f"adopt {a} only when {b}",
                scope="pollypm",
            )
        )

    # Raw DB count — list_entries filters expired TTL via the recall
    # path, so it undercounts what's actually in the table.
    def _raw_count() -> int:
        row = backend.store.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE scope = ?", ("pollypm",)
        ).fetchone()
        return int(row[0])

    before = _raw_count()
    assert before == 100

    log_path = tmp_path / ".pollypm" / "memory-curator.jsonl"
    result = curate_memory(backend, log_path=log_path, now=now)

    after = _raw_count()
    assert result.ttl_deleted == 20
    assert result.duplicates_merged >= 10  # at least one per paired batch
    assert result.decayed == 30
    # Net shrinkage: expired + merged duplicates.
    assert after == before - result.ttl_deleted - result.duplicates_merged

    # Audit log captured an action per TTL + merge + decay event.
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == result.ttl_deleted + result.duplicates_merged + result.decayed


# ---------------------------------------------------------------------------
# Inbox summary
# ---------------------------------------------------------------------------


def test_build_inbox_summary_quiet_run_returns_empty(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    result = curate_memory(backend, now=datetime.now(UTC))
    assert build_inbox_summary(result) == ""


def test_build_inbox_summary_reports_actions(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    # Use TTL so we exercise the full summary pipeline.
    entry = backend.write_entry(
        ProjectMemory(fact="expired", why="w", how_to_apply="h", scope="pollypm")
    )
    now = datetime.now(UTC)
    _set_ttl(backend, entry.entry_id, ttl_at=now - timedelta(hours=1))
    result = curate_memory(backend, now=now)
    summary = build_inbox_summary(result)
    assert summary.startswith("# Memory curator")
    assert "TTL sweep: 1" in summary
