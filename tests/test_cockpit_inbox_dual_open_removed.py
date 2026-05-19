"""Slice H (#1737): the dual-open is gone under the pg backend.

Pre-Slice-H, :func:`pollypm.cockpit_inbox.pm_inbox_awaits_user_list`
opened a ``create_work_service(...)`` AND a
``SQLAlchemyStore("sqlite:///<path>")`` for every project on every
rail refresh — the perf review on #1634 traced the rail latency to
this fanout. Slice H replaces it with two bulk pg queries.

This test pins the structural invariant: under
``[storage] backend = "postgres"``, the inbox-list path does NOT open
any ``SQLAlchemyStore`` and does NOT open any per-project sqlite work
service. It uses module-level monkeypatching to spy on construction
without needing a live pg backend — the bulk-query helpers themselves
are stubbed to return empty results so the function returns ``[]``.

Slice K will remove the sqlite branch entirely; until then this test
guards against accidental regressions that wire the sqlite fanout
back into the pg code path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def cfg_with_pg(tmp_path: Path, monkeypatch) -> Any:
    """A minimal config-like object whose storage.backend is 'postgres'.

    Avoids loading from disk so the test stays hermetic and fast — the
    inbox helper only reads ``storage.backend`` and ``projects`` /
    ``project.workspace_root`` off the config.
    """
    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project_a = workspace_root / "alpha"
    project_a.mkdir()
    project_b = workspace_root / "bravo"
    project_b.mkdir()

    def _make_project(key: str, path: Path):
        return SimpleNamespace(
            key=key,
            name=key.title(),
            path=path,
            tracked=True,
        )

    config = SimpleNamespace(
        storage=SimpleNamespace(backend="postgres", url=""),
        projects={
            "alpha": _make_project("alpha", project_a),
            "bravo": _make_project("bravo", project_b),
        },
        project=SimpleNamespace(workspace_root=workspace_root),
    )
    return config


def test_pg_backend_does_not_open_sqlalchemy_store(cfg_with_pg, monkeypatch):
    """Under pg, NO SQLAlchemyStore is constructed in the inbox load path."""
    open_count = {"n": 0}

    class _ShouldNotBeOpened:
        def __init__(self, *args, **kwargs):
            open_count["n"] += 1
            raise AssertionError(
                "SQLAlchemyStore must not be opened under "
                "[storage] backend = 'postgres'. The pg path uses one "
                "bulk messages query in cockpit_pg_aggregates.open_messages "
                "instead."
            )

    # Patch the import the inbox module pulls in lazily.
    import pollypm.store as store_mod

    monkeypatch.setattr(store_mod, "SQLAlchemyStore", _ShouldNotBeOpened)

    # Stub the pg-side bulk queries to return empty so the function
    # completes without needing a live pool.
    import pollypm.cockpit_pg_aggregates as agg

    monkeypatch.setattr(agg, "inbox_tasks_grouped", lambda _config: {})
    monkeypatch.setattr(
        agg, "open_messages", lambda _config, *, known_projects: [],
    )

    from pollypm.cockpit_inbox import pm_inbox_awaits_user_list

    result = pm_inbox_awaits_user_list(cfg_with_pg)

    assert result == []
    assert open_count["n"] == 0, (
        "SQLAlchemyStore was opened despite the pg backend being active. "
        "This re-introduces the dual-open the perf review flagged."
    )


def test_pg_backend_does_not_open_sqlite_work_service(cfg_with_pg, monkeypatch):
    """Under pg, NO per-project SQLiteWorkService is opened either.

    The sqlite path was the FIRST of the two opens in the dual-open.
    Slice H replaces the per-project ``create_work_service(db_path=...)``
    with one bulk ``inbox_tasks_grouped()`` call.
    """
    sqlite_opens = {"n": 0}

    # Patch ``create_work_service`` in the work-service factory; the
    # inbox module imports it lazily inside the function body, so we
    # patch where the symbol lives.
    import pollypm.work as work_mod

    real_factory = work_mod.create_work_service

    def _spy(*args, **kwargs):
        sqlite_opens["n"] += 1
        return real_factory(*args, **kwargs)

    monkeypatch.setattr(work_mod, "create_work_service", _spy)

    import pollypm.cockpit_pg_aggregates as agg

    monkeypatch.setattr(agg, "inbox_tasks_grouped", lambda _config: {})
    monkeypatch.setattr(
        agg, "open_messages", lambda _config, *, known_projects: [],
    )

    from pollypm.cockpit_inbox import pm_inbox_awaits_user_list

    pm_inbox_awaits_user_list(cfg_with_pg)

    assert sqlite_opens["n"] == 0, (
        "create_work_service was called under the pg backend. The pg "
        "path is supposed to consume one bulk pg query from "
        "cockpit_pg_aggregates.inbox_tasks_grouped instead."
    )


def test_sqlite_backend_keeps_legacy_path(tmp_path, monkeypatch):
    """Sanity check: the sqlite fallback is unchanged.

    The Slice H rewire is gated on ``storage.backend == "postgres"``.
    Under sqlite, ``pm_inbox_awaits_user_list`` must keep the exact
    pre-Slice-H per-project walk. We don't assert open counts here —
    just that the function still produces a (possibly empty) list
    without crashing on a real-config-shaped fixture.
    """
    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    config = SimpleNamespace(
        storage=SimpleNamespace(backend="sqlite", url=""),
        projects={},
        project=SimpleNamespace(workspace_root=workspace_root),
    )

    from pollypm.cockpit_inbox import pm_inbox_awaits_user_list

    result = pm_inbox_awaits_user_list(config)
    assert result == []


def test_pg_backend_falls_back_on_bulk_query_failure(cfg_with_pg, monkeypatch):
    """A pg pool outage drops back to the per-source path.

    Pinned because the rail badge must stay informative even when pg is
    unreachable — Slice K removes the sqlite branch, but for now a
    transient pg failure is recoverable.
    """
    import pollypm.cockpit_pg_aggregates as agg

    # Both bulk queries fail → pg_active flips off → for-loop walks
    # sources via the sqlite fallback. The fixture's projects have no
    # actual sqlite DBs, so the walk completes with an empty list, but
    # the function does not raise.
    monkeypatch.setattr(agg, "inbox_tasks_grouped", lambda _config: None)
    monkeypatch.setattr(
        agg, "open_messages", lambda _config, *, known_projects: None,
    )

    from pollypm.cockpit_inbox import pm_inbox_awaits_user_list

    result = pm_inbox_awaits_user_list(cfg_with_pg)
    assert result == []
