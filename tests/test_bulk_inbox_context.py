"""Cycle 130 — perf fix: collapse the inbox loader's per-task N+1.

The cockpit inbox loader used to call ``svc.get_context(...,
entry_type='read', limit=1)`` and ``svc.list_replies(task_id)`` once
per inbox task. At 9 projects × ~10 tasks each that was 180 SQLite
roundtrips on every 8s refresh tick. The fix replaces both with two
project-wide bulk queries.

These tests pin the bulk-method contract (correctness) and the
query-count win (perf — counted via SQLAlchemy's ``conn.execute``
trace).
"""

from __future__ import annotations

from pathlib import Path

from pollypm.work.sqlite_service import SQLiteWorkService


def _seed_inbox_tasks(svc: SQLiteWorkService, *, n: int = 3) -> list[int]:
    """Create N user-routed tasks and return their numbers."""
    numbers: list[int] = []
    for i in range(n):
        task = svc.create(
            title=f"task {i}",
            description="",
            type="task",
            flow_template="chat",
            roles={"requester": "user", "operator": "polly"},
            project="demo",
            priority="normal",
            created_by="test",
        )
        numbers.append(task.task_number)
    return numbers


def test_task_numbers_with_context_entry_returns_only_marked(tmp_path: Path) -> None:
    db = tmp_path / "work.db"
    project_path = tmp_path / "proj"
    project_path.mkdir()
    with SQLiteWorkService(db_path=db, project_path=project_path) as svc:
        nums = _seed_inbox_tasks(svc, n=3)
        # Mark only task 0 and task 2 as read.
        svc.mark_read(f"demo/{nums[0]}")
        svc.mark_read(f"demo/{nums[2]}")
        marked = svc.task_numbers_with_context_entry(
            project="demo", entry_type="read",
        )
        assert marked == {nums[0], nums[2]}


def test_bulk_list_replies_buckets_by_task_chronological(tmp_path: Path) -> None:
    db = tmp_path / "work.db"
    project_path = tmp_path / "proj"
    project_path.mkdir()
    with SQLiteWorkService(db_path=db, project_path=project_path) as svc:
        nums = _seed_inbox_tasks(svc, n=2)
        svc.add_reply(f"demo/{nums[0]}", "first reply")
        svc.add_reply(f"demo/{nums[0]}", "second reply")
        svc.add_reply(f"demo/{nums[1]}", "single reply on task 1")
        bulk = svc.bulk_list_replies(project="demo")
        assert set(bulk.keys()) == {nums[0], nums[1]}
        # Chronological — same as list_replies' contract.
        assert [e.text for e in bulk[nums[0]]] == ["first reply", "second reply"]
        assert [e.text for e in bulk[nums[1]]] == ["single reply on task 1"]


def test_bulk_list_replies_skips_non_reply_entry_types(tmp_path: Path) -> None:
    """Read markers and other context entries must not leak into the
    reply bucket — the inbox renders these as a thread, not a log."""
    db = tmp_path / "work.db"
    project_path = tmp_path / "proj"
    project_path.mkdir()
    with SQLiteWorkService(db_path=db, project_path=project_path) as svc:
        nums = _seed_inbox_tasks(svc, n=1)
        svc.mark_read(f"demo/{nums[0]}")
        svc.add_reply(f"demo/{nums[0]}", "the only reply")
        bulk = svc.bulk_list_replies(project="demo")
        assert nums[0] in bulk
        assert [e.text for e in bulk[nums[0]]] == ["the only reply"]


def test_inbox_loader_uses_bulk_methods(tmp_path: Path, monkeypatch) -> None:
    """The cockpit inbox loader's hot path must call the bulk methods
    once per project, not once per task. Counts roundtrips by spying
    on the methods."""
    from pollypm.cockpit_inbox_items import load_inbox_entries

    project_path = tmp_path / "proj"
    project_path.mkdir()
    db = project_path / ".pollypm" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteWorkService(db_path=db, project_path=project_path) as svc:
        nums = _seed_inbox_tasks(svc, n=5)
        for n in nums[:2]:
            svc.add_reply(f"demo/{n}", "hello")
            svc.mark_read(f"demo/{n}")

    # Build a minimal config.projects-like map. The loader iterates
    # ``_inbox_db_sources(config)`` which yields ``(project_key, db_path,
    # project_path)`` triples — patch that to a known-good one-row list.
    monkeypatch.setattr(
        "pollypm.cockpit_inbox_items._inbox_db_sources",
        lambda _config: [("demo", db, project_path)],
    )
    # Patch SQLAlchemyStore to a stub since we only care about the
    # SQLiteWorkService side here.
    class _NullStore:
        def __init__(self, *_a, **_kw):
            pass

        def query_messages(self, **_kw):
            return []

        def close(self):
            pass

    monkeypatch.setattr(
        "pollypm.cockpit_inbox_items.SQLAlchemyStore", _NullStore,
    )

    # Spy on the per-task and bulk methods to count roundtrips.
    orig_get_context = SQLiteWorkService.get_context
    orig_list_replies = SQLiteWorkService.list_replies
    orig_bulk_replies = SQLiteWorkService.bulk_list_replies
    orig_bulk_marks = SQLiteWorkService.task_numbers_with_context_entry

    counts = {"get_context": 0, "list_replies": 0, "bulk_replies": 0, "bulk_marks": 0}

    def spy_get_context(self, *a, **kw):
        counts["get_context"] += 1
        return orig_get_context(self, *a, **kw)

    def spy_list_replies(self, *a, **kw):
        counts["list_replies"] += 1
        return orig_list_replies(self, *a, **kw)

    def spy_bulk_replies(self, *a, **kw):
        counts["bulk_replies"] += 1
        return orig_bulk_replies(self, *a, **kw)

    def spy_bulk_marks(self, *a, **kw):
        counts["bulk_marks"] += 1
        return orig_bulk_marks(self, *a, **kw)

    monkeypatch.setattr(SQLiteWorkService, "get_context", spy_get_context)
    monkeypatch.setattr(SQLiteWorkService, "list_replies", spy_list_replies)
    monkeypatch.setattr(SQLiteWorkService, "bulk_list_replies", spy_bulk_replies)
    monkeypatch.setattr(
        SQLiteWorkService, "task_numbers_with_context_entry", spy_bulk_marks,
    )

    # Minimal config for the loader.
    from types import SimpleNamespace
    cfg = SimpleNamespace(projects={"demo": SimpleNamespace(path=project_path)})
    items, unread, replies_by_task = load_inbox_entries(cfg)

    # The bulk methods fire exactly once per project.
    assert counts["bulk_marks"] == 1
    assert counts["bulk_replies"] == 1
    # The per-task methods must NOT fire on the hot path.
    assert counts["get_context"] == 0, "per-task get_context call leaked back into the loader"
    assert counts["list_replies"] == 0, "per-task list_replies call leaked back into the loader"
    # Result correctness: the two read tasks aren't unread; the two
    # reply tasks have replies bucketed.
    assert len(items) == 5
    unread_numbers = {int(tid.split("/", 1)[1]) for tid in unread}
    assert unread_numbers == {nums[2], nums[3], nums[4]}
    reply_task_numbers = {int(tid.split("/", 1)[1]) for tid in replies_by_task}
    assert reply_task_numbers == {nums[0], nums[1]}
