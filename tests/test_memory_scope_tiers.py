"""Tests for the M03 tiered scope model (#232).

Covers:

* Schema migration — pre-M03 databases gain ``scope_tier`` with
  ``'project'`` default and pre-existing rows keep working.
* Writing with ``scope_tier`` persists the tier end-to-end.
* Session-tier lifecycle — ``purge_session_scope`` removes session-tier
  entries, and the supervisor's ``prune_sessions`` path auto-purges
  session-tier memory for any session that is no longer live (the
  session-lifecycle observer hook).
* Task-tier lifecycle — ``expire_task_scope`` sets ``ttl_at`` to
  terminal + 30 days. Back-dating the terminal so the TTL is already in
  the past makes the entry drop out of recall.
* Project/user tiers — never auto-expire under either lifecycle path.
* Recall extensions — ``scope_tier`` filter (single + list) and
  ``scope=[(tier, scope_id)]`` tuple-list compose correctly.
* Cross-tier composition — a session entry and a project entry for the
  same query surface together when the caller asks for both tiers.
* Validation — invalid tier values raise ``ValueError``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.memory_backends import (
    FileMemoryBackend,
    PatternMemory,
    ProjectMemory,
    ReferenceMemory,
    ScopeTier,
    UserMemory,
    VALID_SCOPE_TIERS,
    validate_typed_memory,
)
from pollypm.storage.state import StateStore


# ---------------------------------------------------------------------------
# Migration — fresh schema + upgrade path
# ---------------------------------------------------------------------------


def test_fresh_schema_has_scope_tier_column(tmp_path: Path) -> None:
    db_path = tmp_path / ".pollypm" / "state.db"
    store = StateStore(db_path)
    try:
        cols = {
            row[1]
            for row in store.execute("PRAGMA table_info(memory_entries)").fetchall()
        }
        assert "scope_tier" in cols
    finally:
        store.close()


def test_migration_backfills_scope_tier_on_pre_m03_entries(tmp_path: Path) -> None:
    """Opening a pre-M03 database should add ``scope_tier`` with
    ``'project'`` DEFAULT; existing rows survive the upgrade with tier
    set to ``'project'`` — matching the acceptance contract that
    pre-M03 entries get the never-auto-expire lifecycle.
    """
    db_path = tmp_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Legacy schema — no ``scope_tier`` column.
    conn = sqlite3.connect(db_path)
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
            "legacy-project",
            "note",
            "Legacy row",
            "pre-M03 content",
            "[]",
            "manual",
            "/tmp/x.md",
            "/tmp/x.md",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    store = StateStore(db_path)
    try:
        cols = {
            row[1]
            for row in store.execute("PRAGMA table_info(memory_entries)").fetchall()
        }
        assert "scope_tier" in cols

        row = store.execute(
            "SELECT scope_tier FROM memory_entries WHERE title = 'Legacy row'"
        ).fetchone()
        assert row[0] == "project"

        records = store.list_memory_entries(scope="legacy-project")
        assert len(records) == 1
        assert records[0].scope_tier == "project"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Write path — scope_tier is persisted on typed and legacy writes
# ---------------------------------------------------------------------------


def _backend(tmp_path: Path) -> FileMemoryBackend:
    return FileMemoryBackend(tmp_path)


def test_write_typed_entry_persists_scope_tier(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    entry = backend.write_entry(
        ProjectMemory(
            fact="Session transcript summarised",
            why="resume context next turn",
            how_to_apply="read from session memory at launch",
            scope="sess-abc",
            scope_tier=ScopeTier.SESSION.value,
            importance=3,
        )
    )
    assert entry.scope_tier == "session"
    assert entry.scope == "sess-abc"
    # Re-read from the store to confirm it persisted, not just set on
    # the returned dataclass.
    reread = backend.read_entry(entry.entry_id)
    assert reread is not None
    assert reread.scope_tier == "session"


def test_write_typed_entry_defaults_to_project_tier(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    entry = backend.write_entry(
        ProjectMemory(
            fact="something",
            why="because",
            how_to_apply="do X",
            scope="p",
        )
    )
    assert entry.scope_tier == "project"


def test_write_entry_rejects_invalid_scope_tier(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    with pytest.raises(ValueError):
        backend.write_entry(
            ProjectMemory(
                fact="f", why="w", how_to_apply="h", scope="p", scope_tier="galaxy"
            )
        )


def test_write_entry_rejects_invalid_scope_tier_override(tmp_path: Path) -> None:
    """scope_tier override at call-site is validated too."""
    backend = _backend(tmp_path)
    with pytest.raises(ValueError):
        backend.write_entry(
            ProjectMemory(fact="f", why="w", how_to_apply="h", scope="p"),
            scope_tier="universe",
        )


def test_validate_typed_memory_rejects_invalid_tier() -> None:
    memory = ProjectMemory(
        fact="F", why="W", how_to_apply="H", scope_tier="not-a-tier"
    )
    with pytest.raises(ValueError) as exc:
        validate_typed_memory(memory)
    assert "scope_tier" in str(exc.value)


# ---------------------------------------------------------------------------
# Session-tier lifecycle
# ---------------------------------------------------------------------------


def test_purge_session_scope_removes_matching_entries(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    entry = backend.write_entry(
        ProjectMemory(
            fact="Current turn drafted email",
            why="keep working set",
            how_to_apply="read as context",
            scope="session-42",
            scope_tier=ScopeTier.SESSION.value,
        )
    )
    other_scope = backend.write_entry(
        ProjectMemory(
            fact="Other session working state",
            why="x",
            how_to_apply="y",
            scope="session-99",
            scope_tier=ScopeTier.SESSION.value,
        )
    )
    project_entry = backend.write_entry(
        ProjectMemory(
            fact="Project-tier fact",
            why="x",
            how_to_apply="y",
            scope="pollypm",
            scope_tier=ScopeTier.PROJECT.value,
        )
    )

    removed = backend.purge_session_scope("session-42")
    assert removed == 1
    # Target session entry gone.
    assert backend.read_entry(entry.entry_id) is None
    # Other session + project entries survive.
    assert backend.read_entry(other_scope.entry_id) is not None
    assert backend.read_entry(project_entry.entry_id) is not None


def test_purge_session_scope_is_idempotent(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    backend.write_entry(
        ProjectMemory(
            fact="x",
            why="x",
            how_to_apply="x",
            scope="session-1",
            scope_tier=ScopeTier.SESSION.value,
        )
    )
    assert backend.purge_session_scope("session-1") == 1
    assert backend.purge_session_scope("session-1") == 0


def test_purge_session_scope_only_touches_session_tier(tmp_path: Path) -> None:
    """A project-tier entry whose scope happens to match a session ID
    (unlikely in practice but not impossible) must not be deleted.
    """
    backend = _backend(tmp_path)
    shared = backend.write_entry(
        ProjectMemory(
            fact="project fact for 'session-1'",
            why="x",
            how_to_apply="y",
            scope="session-1",
            scope_tier=ScopeTier.PROJECT.value,
        )
    )
    sess = backend.write_entry(
        ProjectMemory(
            fact="session fact",
            why="x",
            how_to_apply="y",
            scope="session-1",
            scope_tier=ScopeTier.SESSION.value,
        )
    )
    removed = backend.purge_session_scope("session-1")
    assert removed == 1
    assert backend.read_entry(sess.entry_id) is None
    assert backend.read_entry(shared.entry_id) is not None


def test_prune_sessions_auto_purges_orphaned_session_memory(tmp_path: Path) -> None:
    """The supervisor's session-lifecycle hook: ``prune_sessions`` must
    sweep session-tier memory for any session id that's no longer live.

    This is the session-lifecycle observer wiring — the supervisor
    already calls ``prune_sessions`` during reconciliation, and the
    M03 extension piggy-backs the session-tier purge on that call.
    """
    backend = _backend(tmp_path)
    # Simulate three sessions with working memory.
    for sid in ("alive-1", "alive-2", "dead-3"):
        backend.write_entry(
            ProjectMemory(
                fact=f"wm for {sid}",
                why="x",
                how_to_apply="y",
                scope=sid,
                scope_tier=ScopeTier.SESSION.value,
            )
        )
    # And a project-tier entry that must survive.
    project_entry = backend.write_entry(
        ProjectMemory(
            fact="Project fact",
            why="x",
            how_to_apply="y",
            scope="pollypm",
            scope_tier=ScopeTier.PROJECT.value,
        )
    )
    # Only alive-1 and alive-2 are still live — dead-3 has ended.
    backend.store.prune_sessions({"alive-1", "alive-2"})
    remaining_session = backend.list_entries(
        scope_tier=ScopeTier.SESSION.value, limit=50
    )
    scopes = {e.scope for e in remaining_session}
    assert scopes == {"alive-1", "alive-2"}
    # Project-tier untouched.
    assert backend.read_entry(project_entry.entry_id) is not None


def test_prune_sessions_with_empty_set_purges_all_session_memory(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    backend.write_entry(
        ProjectMemory(
            fact="wm",
            why="x",
            how_to_apply="y",
            scope="s1",
            scope_tier=ScopeTier.SESSION.value,
        )
    )
    backend.write_entry(
        ProjectMemory(
            fact="pf",
            why="x",
            how_to_apply="y",
            scope="pollypm",
            scope_tier=ScopeTier.PROJECT.value,
        )
    )
    backend.store.prune_sessions(set())
    assert backend.list_entries(scope_tier=ScopeTier.SESSION.value) == []
    # Project-tier survives the "no live sessions" case.
    assert backend.list_entries(scope_tier=ScopeTier.PROJECT.value)


# ---------------------------------------------------------------------------
# Task-tier lifecycle
# ---------------------------------------------------------------------------


def test_expire_task_scope_sets_ttl_30_days_from_terminal(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    entry = backend.write_entry(
        ProjectMemory(
            fact="Notes captured during task",
            why="continuity for future tasks",
            how_to_apply="read as historical context",
            scope="task-123",
            scope_tier=ScopeTier.TASK.value,
        )
    )
    terminal = "2026-04-01T00:00:00+00:00"
    updated = backend.expire_task_scope("task-123", terminal_at=terminal)
    assert updated == 1
    reread = backend.read_entry(entry.entry_id)
    assert reread is not None
    # TTL should be terminal + 30 days ⇒ 2026-05-01.
    assert reread.ttl_at is not None
    ttl = datetime.fromisoformat(reread.ttl_at)
    expected = datetime.fromisoformat(terminal) + timedelta(days=30)
    assert abs((ttl - expected).total_seconds()) < 1.0


def test_expire_task_scope_drops_expired_entries_from_recall(
    tmp_path: Path,
) -> None:
    """Back-date the terminal so the TTL is already in the past; recall
    must stop surfacing the entry — closing the task-tier lifecycle loop.
    """
    backend = _backend(tmp_path)
    entry = backend.write_entry(
        ProjectMemory(
            fact="Playwright debugging notes",
            why="reused across sessions",
            how_to_apply="cross-reference",
            scope="task-77",
            scope_tier=ScopeTier.TASK.value,
        )
    )
    # Terminal transition 60 days ago ⇒ TTL 30 days ago ⇒ expired.
    terminal = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    backend.expire_task_scope("task-77", terminal_at=terminal)

    # Recall filters expired TTLs out.
    results = backend.recall("playwright", scope="task-77")
    assert not any(r.entry.entry_id == entry.entry_id for r in results)


def test_expire_task_scope_does_not_extend_earlier_ttl(tmp_path: Path) -> None:
    """A caller-set short TTL must win over the terminal+30d TTL — we
    MIN() them in SQL so we never extend a pinned expiry.
    """
    backend = _backend(tmp_path)
    earlier_ttl = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    entry = backend.write_entry(
        ProjectMemory(
            fact="Short-lived experiment note",
            why="time-boxed",
            how_to_apply="re-evaluate in 3 days",
            scope="task-short",
            scope_tier=ScopeTier.TASK.value,
            ttl_at=earlier_ttl,
        )
    )
    backend.expire_task_scope("task-short")  # would want terminal+30d
    reread = backend.read_entry(entry.entry_id)
    assert reread is not None
    # The earlier TTL should be preserved.
    stored_ttl = datetime.fromisoformat(reread.ttl_at)
    earlier_parsed = datetime.fromisoformat(earlier_ttl)
    assert abs((stored_ttl - earlier_parsed).total_seconds()) < 1.0


def test_expire_task_scope_ignores_non_task_tier(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    entry = backend.write_entry(
        ProjectMemory(
            fact="Project-tier fact",
            why="x",
            how_to_apply="y",
            scope="task-foo",
            scope_tier=ScopeTier.PROJECT.value,
        )
    )
    updated = backend.expire_task_scope("task-foo")
    assert updated == 0
    reread = backend.read_entry(entry.entry_id)
    assert reread is not None
    assert reread.ttl_at is None


# ---------------------------------------------------------------------------
# Project/user tiers never auto-expire
# ---------------------------------------------------------------------------


def test_project_tier_persists_across_session_pruning(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    entry = backend.write_entry(
        ProjectMemory(
            fact="Canonical project fact",
            why="forever",
            how_to_apply="reuse",
            scope="pollypm",
            scope_tier=ScopeTier.PROJECT.value,
        )
    )
    backend.store.prune_sessions(set())
    assert backend.read_entry(entry.entry_id) is not None


def test_user_tier_persists_across_project_pruning_and_session_end(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path)
    entry = backend.write_entry(
        UserMemory(
            name="Sam",
            description="Senior engineer who likes small modules.",
            body="Persists across all projects.",
            scope="user-sam",
            scope_tier=ScopeTier.USER.value,
            importance=4,
        )
    )
    # Simulate every session ending.
    backend.store.prune_sessions(set())
    reread = backend.read_entry(entry.entry_id)
    assert reread is not None
    assert reread.scope_tier == "user"


# ---------------------------------------------------------------------------
# Recall extensions — scope_tier filter + (tier, scope) tuple recall
# ---------------------------------------------------------------------------


def _seed_cross_tier(backend: FileMemoryBackend) -> None:
    backend.write_entry(
        ProjectMemory(
            fact="Session-tier authentication note",
            why="wm",
            how_to_apply="z",
            scope="session-1",
            scope_tier=ScopeTier.SESSION.value,
            importance=3,
        )
    )
    backend.write_entry(
        ProjectMemory(
            fact="Project-tier authentication canonical",
            why="forever",
            how_to_apply="z",
            scope="pollypm",
            scope_tier=ScopeTier.PROJECT.value,
            importance=4,
        )
    )
    backend.write_entry(
        UserMemory(
            name="Sam",
            description="authentication preferences",
            body="Sam prefers SSO for authentication across projects.",
            scope="user-sam",
            scope_tier=ScopeTier.USER.value,
            importance=4,
        )
    )
    backend.write_entry(
        PatternMemory(
            when="writing authentication tests",
            then="mock the SSO provider",
            scope="task-42",
            scope_tier=ScopeTier.TASK.value,
            importance=3,
        )
    )


def test_recall_filters_by_scope_tier_single(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    _seed_cross_tier(backend)
    results = backend.recall("authentication", scope_tier=ScopeTier.PROJECT.value)
    assert results
    assert all(r.entry.scope_tier == "project" for r in results)


def test_recall_filters_by_scope_tier_list(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    _seed_cross_tier(backend)
    results = backend.recall(
        "authentication",
        scope_tier=[ScopeTier.PROJECT.value, ScopeTier.USER.value],
    )
    tiers = {r.entry.scope_tier for r in results}
    assert tiers <= {"project", "user"}
    assert "project" in tiers
    assert "user" in tiers
    assert "session" not in tiers
    assert "task" not in tiers


def test_recall_accepts_tier_scope_tuple_list(tmp_path: Path) -> None:
    """The spec says `recall` can take a list of (tier, scope_id) — the
    cross-tier composition path ("this session AND this project")."""
    backend = _backend(tmp_path)
    _seed_cross_tier(backend)
    results = backend.recall(
        "authentication",
        scope=[("session", "session-1"), ("project", "pollypm")],
    )
    # Both entries match; user-tier + task-tier excluded.
    tiers = {r.entry.scope_tier for r in results}
    assert tiers <= {"session", "project"}
    assert "session" in tiers
    assert "project" in tiers


def test_recall_scope_tuple_rejects_invalid_tier(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    with pytest.raises(ValueError):
        backend.recall("x", scope=[("galaxy", "sid")])


def test_recall_cross_tier_composition_ranking(tmp_path: Path) -> None:
    """Cross-tier recall: session + project tiers combined, the ranker
    still applies importance + recency on top so a higher-importance
    project entry can outrank a lower-importance session entry.
    """
    backend = _backend(tmp_path)
    # Low-importance session entry.
    backend.write_entry(
        ProjectMemory(
            fact="deployment rumor from current session",
            why="draft",
            how_to_apply="verify",
            scope="session-current",
            scope_tier=ScopeTier.SESSION.value,
            importance=2,
        )
    )
    # High-importance project entry.
    backend.write_entry(
        ProjectMemory(
            fact="deployment policy: prod behind feature flags",
            why="canonical",
            how_to_apply="follow policy",
            scope="pollypm",
            scope_tier=ScopeTier.PROJECT.value,
            importance=5,
        )
    )
    results = backend.recall(
        "deployment",
        scope=[("session", "session-current"), ("project", "pollypm")],
    )
    assert len(results) == 2
    # Project (importance=5) should beat session (importance=2).
    assert results[0].entry.scope_tier == "project"


def test_recall_empty_scope_list_same_as_none(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    backend.write_entry(
        ProjectMemory(fact="fact", why="w", how_to_apply="h", scope="p")
    )
    results_none = backend.recall("fact", scope=None)
    results_empty = backend.recall("fact", scope=[])
    assert len(results_none) == len(results_empty) == 1


def test_recall_mixed_scope_list_raises(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    with pytest.raises(TypeError):
        backend.recall("x", scope=["p", ("project", "foo")])


def test_valid_scope_tiers_matches_enum() -> None:
    assert VALID_SCOPE_TIERS == frozenset(t.value for t in ScopeTier)
