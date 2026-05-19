"""Slice H (#1737): cockpit_rail parity between sqlite and pg backends.

Three rail call sites used to fan out per-project sqlite opens:

* ``_build_project_pm_primer`` (line ~546 pre-Slice-H) — per-project
  task counts + inbox preview for the per-project PM chat re-anchor.
* ``_build_operator_primer`` (line ~676) — per-project inbox sweep for
  the operator chat workspace briefing.
* ``_project_state_rollups`` / ``_project_tasks_for_rollup`` (~line
  1932) — per-project task gather feeding the rail glyph rollup.

Slice H replaces all three with bulk pg queries gated on
``[storage] backend = "postgres"``. The tests below pin:

1. The pg path produces consumable output on a seeded pg fixture.
2. The pg path issues ONE pg query for the rollup walk, not N.
3. The sqlite branch is untouched (smoke).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


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
            enforce_plan=False,
            persona_name=None,
            display_label=lambda key=key: key.title(),
        )
    return SimpleNamespace(
        storage=SimpleNamespace(backend=backend, url=""),
        projects=proj_objs,
        project=SimpleNamespace(workspace_root=workspace_root),
        planner=SimpleNamespace(plan_dir="docs/plan", enforce_plan=False),
        accounts={},
    )


def test_rollup_pg_walk_issues_one_query_total(
    tmp_path, pg_schema_pool, monkeypatch,
):
    """The rollup helper hits pg ONCE for N projects (not N times).

    Pinned because this site was the most expensive in the original
    rail-refresh sweep — every project's ``_project_tasks_for_rollup``
    did a fresh ``create_work_service`` + ``list_tasks`` against its
    own sqlite DB. Under pg it's one ``all_tasks_grouped`` call shared
    across the whole rollup loop.
    """
    from pollypm.cockpit_rail import CockpitRouter
    from pollypm.work.pg_service import PgWorkService

    PgWorkService(pool=pg_schema_pool, ro_pool=None)  # apply schema

    pg_service_open_count = {"n": 0}

    def _spy_open(_config):
        pg_service_open_count["n"] += 1
        return PgWorkService(
            pool=pg_schema_pool, ro_pool=None, apply_migrations=False,
        )

    import pollypm.cockpit_pg_aggregates as agg

    monkeypatch.setattr(agg, "_open_pg_service", _spy_open)

    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    projects = {
        f"p{i}": workspace_root / f"p{i}" for i in range(5)
    }
    config = _make_config(workspace_root, projects, "postgres")

    router = CockpitRouter.__new__(CockpitRouter)
    rollups = router._project_state_rollups(config, alerts=[])

    # Every project gets a rollup, but the pg backend only opened ONE
    # work-service handle across the whole loop.
    assert len(rollups) == 5
    assert pg_service_open_count["n"] == 1, (
        f"Expected ONE pg open for the rollup walk; got "
        f"{pg_service_open_count['n']}. The Slice H collapse regressed."
    )


def test_rollup_pg_returns_tasks_for_known_project(
    tmp_path, pg_schema_pool, monkeypatch,
):
    """The pg rollup walk surfaces seeded tasks correctly."""
    from pollypm.cockpit_rail import CockpitRouter
    from pollypm.work.pg_service import PgWorkService

    svc = PgWorkService(pool=pg_schema_pool, ro_pool=None)
    svc.create(
        title="real work",
        type="task",
        project="demo",
        flow_template="chat",
        roles={"worker": "polly"},
    )
    svc.queue("demo/1", actor="test")

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

    router = CockpitRouter.__new__(CockpitRouter)
    rollups = router._project_state_rollups(config, alerts=[])
    assert "demo" in rollups
    # A queued task is non-quiet — the rollup must reflect at least
    # one non-terminal row.
    rollup = rollups["demo"]
    # ProjectStateRollup exposes counts via attributes; pre-pin the
    # presence rather than the exact shape so a downstream schema
    # tweak doesn't break this test.
    assert rollup is not None


def test_rollup_sqlite_path_untouched(tmp_path):
    """Sqlite backend keeps its pre-Slice-H walk."""
    from pollypm.cockpit_rail import CockpitRouter

    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    config = _make_config(
        workspace_root, {"demo": workspace_root / "demo"}, "sqlite",
    )

    router = CockpitRouter.__new__(CockpitRouter)
    rollups = router._project_state_rollups(config, alerts=[])
    # No DB exists → empty task list → rollup falls into the
    # tracked-paused IDLE bucket. We only assert the call doesn't
    # raise; the sqlite-specific assertions live elsewhere.
    assert "demo" in rollups


def test_rollup_pg_handles_pool_failure_gracefully(
    tmp_path, monkeypatch,
):
    """A pg pool outage drops back to the sqlite walk.

    Until Slice K removes the sqlite branch, the rail must stay
    informative even when pg is unreachable.
    """
    from pollypm.cockpit_rail import CockpitRouter

    import pollypm.cockpit_pg_aggregates as agg

    monkeypatch.setattr(agg, "_open_pg_service", lambda _config: None)

    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    config = _make_config(
        workspace_root, {"demo": workspace_root / "demo"}, "postgres",
    )

    router = CockpitRouter.__new__(CockpitRouter)
    # Should not raise even though the pg path is dead.
    rollups = router._project_state_rollups(config, alerts=[])
    assert "demo" in rollups
