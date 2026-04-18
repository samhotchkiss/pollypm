from __future__ import annotations

import sqlite3
import warnings
from pathlib import Path

import pytest

from pollypm.memory_backends import (
    EpisodicMemory,
    FeedbackMemory,
    FileMemoryBackend,
    MemoryType,
    PatternMemory,
    ProjectMemory,
    ReferenceMemory,
    UserMemory,
    get_memory_backend,
    validate_typed_memory,
)
from pollypm.storage.state import StateStore


def test_file_memory_backend_writes_reads_and_compacts(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        entry = backend.write_entry(
            scope="demo",
            title="North Star",
            body="Keep the project moving in small testable chunks.",
            tags=["vision", "north-star"],
            source="manual",
        )

    assert entry.file_path.exists()
    assert entry.summary_path.exists()
    # Legacy path still maps to type='project' for back-compat.
    assert entry.type == "project"
    assert entry.importance == 3
    assert entry.superseded_by is None
    assert entry.ttl_at is None

    listed = backend.list_entries(scope="demo", kind="note")
    assert len(listed) == 1
    assert listed[0].title == "North Star"

    read_back = backend.read_entry(entry.entry_id)
    assert read_back is not None
    assert read_back.body.startswith("Keep the project moving")

    summary = backend.summarize("demo")
    assert "Memory Summary: demo" in summary

    compacted = backend.compact("demo")
    assert compacted.summary_path.exists()
    assert compacted.entry_count == 1
    assert backend.store.latest_memory_summary("demo") is not None


def test_get_memory_backend_returns_file_backend(tmp_path: Path) -> None:
    backend = get_memory_backend(tmp_path, "file")
    assert isinstance(backend, FileMemoryBackend)


# ---------------------------------------------------------------------------
# Typed-memory happy paths — one per MemoryType variant
# ---------------------------------------------------------------------------


def test_write_user_memory_typed(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    entry = backend.write_entry(
        UserMemory(
            name="Sam",
            description="Senior engineer, prefers small modules.",
            body="Sam reviews PRs weekly and cares about dependency boundaries.",
            scope="operator",
            importance=4,
        )
    )
    assert entry.type == MemoryType.USER.value
    assert entry.importance == 4
    assert entry.scope == "operator"
    assert entry.file_path.exists()
    assert "Sam" in entry.title


def test_write_feedback_memory_typed(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    entry = backend.write_entry(
        FeedbackMemory(
            rule="Never use --no-verify.",
            why="Hooks catch test regressions before push.",
            how_to_apply="If a pre-commit hook fails, fix the issue then re-commit.",
            scope="pollypm",
        )
    )
    assert entry.type == MemoryType.FEEDBACK.value


def test_write_project_memory_typed(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    entry = backend.write_entry(
        ProjectMemory(
            fact="Test runner is pytest with --tb=short.",
            why="Agreed convention across the team.",
            how_to_apply="Run `uv run python -m pytest --tb=short -q` before commit.",
            scope="pollypm",
            importance=5,
        )
    )
    assert entry.type == MemoryType.PROJECT.value
    assert entry.importance == 5


def test_write_reference_memory_typed(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    entry = backend.write_entry(
        ReferenceMemory(
            pointer="https://github.com/samhotchkiss/pollypm/issues",
            description="GitHub issues for PollyPM.",
            scope="pollypm",
        )
    )
    assert entry.type == MemoryType.REFERENCE.value


def test_write_pattern_memory_typed(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    entry = backend.write_entry(
        PatternMemory(
            when="When the cockpit crashes unexpectedly.",
            then="Run `pm down && pm up` to reset the supervisor.",
            scope="pollypm",
        )
    )
    assert entry.type == MemoryType.PATTERN.value


def test_write_episodic_memory_typed(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    entry = backend.write_entry(
        EpisodicMemory(
            summary="Worker built the auth module and shipped on approve.",
            session_id="session-5312",
            started_at="2026-04-16T09:00:00Z",
            ended_at="2026-04-16T10:30:00Z",
            scope="pollypm",
        )
    )
    assert entry.type == MemoryType.EPISODIC.value


# ---------------------------------------------------------------------------
# Validator errors — missing required fields raise ValueError
# ---------------------------------------------------------------------------


def test_feedback_memory_missing_why_raises() -> None:
    memory = FeedbackMemory(
        rule="Never use --no-verify.",
        why="",  # missing
        how_to_apply="Fix the hook instead.",
    )
    with pytest.raises(ValueError) as exc:
        validate_typed_memory(memory)
    assert "feedback" in str(exc.value)
    assert "why" in str(exc.value)


def test_project_memory_missing_fields_raises() -> None:
    memory = ProjectMemory(fact="Something", why="", how_to_apply="")
    with pytest.raises(ValueError) as exc:
        validate_typed_memory(memory)
    assert "why" in str(exc.value)
    assert "how_to_apply" in str(exc.value)


def test_user_memory_whitespace_only_field_raises() -> None:
    memory = UserMemory(name="   ", description="desc", body="body")
    with pytest.raises(ValueError):
        validate_typed_memory(memory)


def test_importance_out_of_range_raises() -> None:
    memory = ProjectMemory(
        fact="F", why="W", how_to_apply="H", importance=7
    )
    with pytest.raises(ValueError) as exc:
        validate_typed_memory(memory)
    assert "importance" in str(exc.value)


def test_write_entry_validates_before_persist(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    with pytest.raises(ValueError):
        backend.write_entry(FeedbackMemory(rule="R", why="", how_to_apply="H"))


def test_write_entry_rejects_non_typed_positional(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    with pytest.raises(TypeError):
        backend.write_entry("not a typed memory")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Legacy path emits DeprecationWarning
# ---------------------------------------------------------------------------


def test_legacy_write_entry_emits_deprecation_warning(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    with pytest.warns(DeprecationWarning):
        entry = backend.write_entry(
            scope="demo",
            title="Legacy",
            body="body",
            kind="note",
        )
    # Legacy path still lands as a project-type row (spec: back-compat mapping).
    assert entry.type == MemoryType.PROJECT.value


# ---------------------------------------------------------------------------
# Filtering by type on list_entries
# ---------------------------------------------------------------------------


def test_list_entries_filters_by_type(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)
    backend.write_entry(
        ProjectMemory(fact="A", why="B", how_to_apply="C", scope="p")
    )
    backend.write_entry(
        PatternMemory(when="X", then="Y", scope="p")
    )
    projects = backend.list_entries(scope="p", type=MemoryType.PROJECT.value)
    patterns = backend.list_entries(scope="p", type=MemoryType.PATTERN.value)
    assert len(projects) == 1
    assert len(patterns) == 1
    assert projects[0].type == "project"
    assert patterns[0].type == "pattern"


# ---------------------------------------------------------------------------
# Migration test — pre-M01 entries survive the upgrade with sane defaults.
# ---------------------------------------------------------------------------


def test_migration_backfills_type_and_importance_on_existing_entries(
    tmp_path: Path,
) -> None:
    """Simulate a pre-M01 SQLite + on-disk memory file, then open with the
    current StateStore and verify:

    * the schema has been migrated (new columns exist)
    * existing rows default to type='project' and importance=3
    * the on-disk markdown file is still readable via the backend
    """
    db_path = tmp_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Build a legacy (pre-M01) memory_entries schema by hand — no ``type``,
    # ``importance``, ``superseded_by``, or ``ttl_at`` columns.
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
    memory_file = tmp_path / ".pollypm" / "memory" / "legacy" / "20260101T000000Z-old.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("# Old entry\n\nLegacy content.\n")
    conn.execute(
        """
        INSERT INTO memory_entries (
            scope, kind, title, body, tags, source, file_path, summary_path, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy",
            "note",
            "Old entry",
            "Legacy content.",
            "[]",
            "manual",
            str(memory_file),
            str(memory_file),
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    # Re-open via StateStore — migration should run and back-fill columns.
    store = StateStore(db_path)

    cols = {row[1] for row in store.execute("PRAGMA table_info(memory_entries)").fetchall()}
    assert {"type", "importance", "superseded_by", "ttl_at"} <= cols

    # Raw row reads the defaults.
    row = store.execute(
        "SELECT type, importance, superseded_by, ttl_at FROM memory_entries WHERE title = 'Old entry'"
    ).fetchone()
    assert row[0] == "project"
    assert int(row[1]) == 3
    assert row[2] is None
    assert row[3] is None

    # get_memory_entry returns a populated record with those defaults.
    records = store.list_memory_entries(scope="legacy")
    assert len(records) == 1
    record = records[0]
    assert record.type == "project"
    assert record.importance == 3
    assert record.superseded_by is None
    assert record.ttl_at is None

    # And the FileMemoryBackend surfaces them on MemoryEntry.
    backend = FileMemoryBackend(tmp_path, state_store=store)
    entries = backend.list_entries(scope="legacy")
    assert len(entries) == 1
    assert entries[0].type == "project"
    assert entries[0].importance == 3
    # The on-disk file is still reachable.
    assert Path(entries[0].file_path) == memory_file
    assert memory_file.exists()
    store.close()
