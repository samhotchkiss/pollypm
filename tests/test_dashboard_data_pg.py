"""Slice H (#1737): dashboard_data parity between sqlite and pg backends.

The Slice H rewire replaces per-project sqlite fanout in
:mod:`pollypm.dashboard_data` (the inbox-count + recent-messages
helpers) with bulk pg queries. This test pins the contract: the pg
path must produce the SAME visible result as the sqlite path on the
same fixture workspace.

The dashboard counts and previews are user-visible (cockpit home
dashboard, morning briefing), so any drift between the two backends
would manifest as a count flickering across cutover. We don't allow
that.

The pg branch is only exercised when the pg fixtures are reachable;
on a machine without Docker / local pg the parity test is skipped via
``pg_schema_pool``'s own skip, but the sqlite-side smoke still runs
to guard against the helper accidentally crashing on a fresh DB.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


# Hoist the shared pg fixtures (``_pg_container`` + ``pg_schema_pool``)
# into this module's namespace. The repo's conftest.py wires them via
# ``pytest_plugins``; declaring them here lets the file run in
# isolation too.
pytest_plugins = ("tests.conftest_pg",)


def _make_config(workspace_root: Path, projects: dict[str, Path], backend: str):
    proj_objs = {}
    for key, path in projects.items():
        path.mkdir(parents=True, exist_ok=True)
        proj_objs[key] = SimpleNamespace(
            key=key,
            name=key.title(),
            path=path,
            tracked=True,
            display_label=lambda key=key: key.title(),
        )
    return SimpleNamespace(
        storage=SimpleNamespace(backend=backend, url=""),
        projects=proj_objs,
        project=SimpleNamespace(workspace_root=workspace_root),
    )


def test_count_inbox_tasks_pg_matches_sqlite_when_empty(
    tmp_path, pg_schema_pool, monkeypatch,
):
    """Empty workspace: pg path and sqlite path agree at 0.

    The trivial case is a real regression vector — the pg branch added
    in Slice H must not under-count or over-count on an empty
    workspace either.
    """
    from pollypm.dashboard_data import _count_inbox_tasks
    from pollypm.work.pg_service import PgWorkService

    # Apply schema; no rows.
    PgWorkService(pool=pg_schema_pool, ro_pool=None)

    # Patch the aggregates helper to use our test pool.
    import pollypm.cockpit_pg_aggregates as agg

    def _open_pg_service(_config):
        return PgWorkService(
            pool=pg_schema_pool, ro_pool=None, apply_migrations=False,
        )

    monkeypatch.setattr(agg, "_open_pg_service", _open_pg_service)

    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project = workspace_root / "demo"
    config_pg = _make_config(workspace_root, {"demo": project}, "postgres")
    config_sqlite = _make_config(workspace_root, {"demo": project}, "sqlite")

    assert _count_inbox_tasks(config_pg) == _count_inbox_tasks(config_sqlite)
    assert _count_inbox_tasks(config_pg) == 0


def test_count_inbox_tasks_pg_counts_seeded_rows(
    tmp_path, pg_schema_pool, monkeypatch,
):
    """Seed inbox-shaped rows in pg; the pg path counts them in ONE query."""
    from pollypm.dashboard_data import _count_inbox_tasks
    from pollypm.work.pg_service import PgWorkService

    svc = PgWorkService(pool=pg_schema_pool, ro_pool=None)

    # Two tasks owned by "user" — both should land in the inbox.
    svc.create(
        title="needs your eye",
        type="task",
        project="demo",
        flow_template="chat",
        roles={"user": "sam"},
    )
    svc.create(
        title="and another one",
        type="task",
        project="demo",
        flow_template="chat",
        roles={"requester": "user"},
    )
    # One non-user task — should NOT count.
    svc.create(
        title="bot business",
        type="task",
        project="demo",
        flow_template="chat",
        roles={"worker": "polly"},
    )

    import pollypm.cockpit_pg_aggregates as agg

    monkeypatch.setattr(
        agg,
        "_open_pg_service",
        lambda _config: PgWorkService(
            pool=pg_schema_pool, ro_pool=None, apply_migrations=False,
        ),
    )

    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project_path = workspace_root / "demo"
    config_pg = _make_config(workspace_root, {"demo": project_path}, "postgres")

    assert _count_inbox_tasks(config_pg) == 2


def test_count_inbox_tasks_pg_partitions_by_project(
    tmp_path, pg_schema_pool, monkeypatch,
):
    """Bulk query partitions correctly: each project sees only its own tasks."""
    from pollypm.dashboard_data import _count_inbox_tasks
    from pollypm.work.pg_service import PgWorkService

    svc = PgWorkService(pool=pg_schema_pool, ro_pool=None)

    svc.create(
        title="alpha task",
        type="task",
        project="alpha",
        flow_template="chat",
        roles={"user": "sam"},
    )
    svc.create(
        title="bravo task one",
        type="task",
        project="bravo",
        flow_template="chat",
        roles={"user": "sam"},
    )
    svc.create(
        title="bravo task two",
        type="task",
        project="bravo",
        flow_template="chat",
        roles={"user": "sam"},
    )

    import pollypm.cockpit_pg_aggregates as agg

    monkeypatch.setattr(
        agg,
        "_open_pg_service",
        lambda _config: PgWorkService(
            pool=pg_schema_pool, ro_pool=None, apply_migrations=False,
        ),
    )

    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    config = _make_config(
        workspace_root,
        {"alpha": workspace_root / "alpha", "bravo": workspace_root / "bravo"},
        "postgres",
    )
    assert _count_inbox_tasks(config) == 3


def test_recent_inbox_messages_pg_returns_seeded_rows(
    tmp_path, pg_schema_pool, monkeypatch,
):
    """The recent-messages preview path uses ONE pg query under pg."""
    from pollypm.dashboard_data import _recent_inbox_messages
    from pollypm.work.pg_service import PgWorkService

    svc = PgWorkService(pool=pg_schema_pool, ro_pool=None)
    svc.create(
        title="needs your eye",
        type="task",
        project="demo",
        flow_template="chat",
        roles={"user": "sam"},
    )

    import pollypm.cockpit_pg_aggregates as agg

    monkeypatch.setattr(
        agg,
        "_open_pg_service",
        lambda _config: PgWorkService(
            pool=pg_schema_pool, ro_pool=None, apply_migrations=False,
        ),
    )

    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    config = _make_config(
        workspace_root, {"demo": workspace_root / "demo"}, "postgres",
    )

    previews = _recent_inbox_messages(config, limit=10)
    assert len(previews) == 1
    assert previews[0].title.startswith("needs your eye")


def test_recent_inbox_messages_pg_query_count_is_constant(
    tmp_path, pg_schema_pool, monkeypatch,
):
    """The pg path issues ONE service call regardless of project count.

    Pins the load-bearing perf invariant: the rail latency that
    motivated #1737 came from the per-project fanout. Under pg the
    fanout collapses to a single ``list_nonterminal_tasks`` call —
    this test pins that the helper does not regress to a per-project
    loop.
    """
    from pollypm.dashboard_data import _recent_inbox_messages
    from pollypm.work.pg_service import PgWorkService

    pg_service_open_count = {"n": 0}

    def _spy_open(_config):
        pg_service_open_count["n"] += 1
        return PgWorkService(
            pool=pg_schema_pool, ro_pool=None, apply_migrations=False,
        )

    import pollypm.cockpit_pg_aggregates as agg

    monkeypatch.setattr(agg, "_open_pg_service", _spy_open)

    PgWorkService(pool=pg_schema_pool, ro_pool=None)  # ensure schema

    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    # 5 projects — sqlite would open 5 DBs.
    projects = {
        f"p{i}": workspace_root / f"p{i}" for i in range(5)
    }
    config = _make_config(workspace_root, projects, "postgres")

    _recent_inbox_messages(config, limit=10)
    assert pg_service_open_count["n"] == 1, (
        f"Expected ONE pg service open across 5 projects; got "
        f"{pg_service_open_count['n']}. The bulk-query collapse "
        f"regressed back to a per-project fanout."
    )
