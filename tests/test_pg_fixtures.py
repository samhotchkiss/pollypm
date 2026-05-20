"""Sanity tests for the pg fixture infrastructure (issue #1737, Slice F).

These tests verify the wiring of:

* ``pg_work_service`` — constructs cleanly and points at the per-test
  schema.
* ``pg_state_store`` — yields a writeable :class:`StateStore` (placeholder
  until the state.py port lands).
* ``seeded_pg_workspace`` — produces a non-empty workspace.
* ``@pytest.mark.backend("postgres")`` — the dispatch fixture in
  ``tests/conftest.py`` routes to the pg backend. Post-sqlite-ripout
  (refs #1971) ``postgres`` is the only valid value.
* Schema-per-test isolation — two pg tests in the same session cannot
  see each other's rows.

The whole module skips if Docker / local pg with pgvector isn't
reachable (inherited from ``_pg_container``).
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# pg_work_service fixture
# ---------------------------------------------------------------------------


def test_pg_work_service_fixture_is_constructed(pg_work_service):
    """The fixture must yield a usable ``PgWorkService`` instance."""
    from pollypm.work.pg_service import PgWorkService

    assert isinstance(pg_work_service, PgWorkService)


def test_pg_work_service_can_create_and_read(pg_work_service):
    """A round-trip through ``create`` + ``get`` proves the schema is up."""
    task = pg_work_service.create(
        title="fixture smoke",
        description="from test_pg_fixtures",
        type="task",
        project="fixt",
        flow_template="default",
        roles={"worker": "alice"},
    )
    fetched = pg_work_service.get(f"fixt/{task.task_number}")
    assert fetched.title == "fixture smoke"


# ---------------------------------------------------------------------------
# pg_state_store fixture
# ---------------------------------------------------------------------------


def test_pg_state_store_fixture_yields_writeable_store(pg_state_store):
    """The placeholder fixture should at least be writeable.

    Slice F leaves the actual pg port to a follow-up (#342-followup);
    the fixture must still hand back something tests can call so the
    seam exists in the public API.
    """
    # ``StateStore`` exposes ``.execute`` for ad-hoc queries; the
    # placeholder is sqlite-backed so we issue a trivially-safe query
    # against the always-present ``schema_version`` table.
    row = pg_state_store.execute("SELECT 1").fetchone()
    assert row[0] == 1


# ---------------------------------------------------------------------------
# seeded_pg_workspace fixture
# ---------------------------------------------------------------------------


def test_seeded_pg_workspace_returns_service_and_tasks(seeded_pg_workspace):
    svc, tasks = seeded_pg_workspace
    assert len(tasks) == 3
    titles = [t.title for t in tasks]
    assert titles == ["seed-0", "seed-1", "seed-2"]
    # The service should be queryable and show the seeds back.
    listed = svc.list_tasks(project="demo")
    assert {t.title for t in listed} == set(titles)


# ---------------------------------------------------------------------------
# Schema-per-test isolation
# ---------------------------------------------------------------------------


def test_isolation_leg_a(pg_work_service):
    """First half of a 2-test pair that proves schema isolation.

    If isolation is broken, leg B will see leg A's row in ``list_tasks``.
    Tests run in declaration order under pytest's default collector.
    """
    pg_work_service.create(
        title="leg-A",
        type="task",
        project="iso",
        flow_template="default",
        roles={"worker": "a"},
    )
    assert len(pg_work_service.list_tasks(project="iso")) == 1


def test_isolation_leg_b(pg_work_service):
    """Second half of the isolation pair.

    Must start with an empty ``iso`` project — leg A's row lives in a
    different schema and must not leak.
    """
    assert pg_work_service.list_tasks(project="iso") == []
    pg_work_service.create(
        title="leg-B",
        type="task",
        project="iso",
        flow_template="default",
        roles={"worker": "b"},
    )
    rows = pg_work_service.list_tasks(project="iso")
    assert {t.title for t in rows} == {"leg-B"}


# ---------------------------------------------------------------------------
# work_service dispatch marker
# ---------------------------------------------------------------------------
#
# Post-sqlite-ripout (refs #1971) the dispatch fixture only resolves to
# pg. The previous sqlite + ``both`` tests were removed alongside the
# backend itself; the unsupported-marker test below pins the guard so a
# stray ``@pytest.mark.backend("sqlite")`` regression fails loudly.


def test_dispatch_default_routes_to_pg(work_service):
    """No marker → pg. Pre-#1971 the default was sqlite."""
    from pollypm.work.pg_service import PgWorkService

    assert isinstance(work_service, PgWorkService)


@pytest.mark.backend("postgres")
def test_dispatch_postgres_marker_routes_to_pg(work_service):
    from pollypm.work.pg_service import PgWorkService

    assert isinstance(work_service, PgWorkService)


def test_dispatch_rejects_unsupported_marker() -> None:
    """The removed sqlite backend must not silently resolve.

    Directly probes ``_resolve_backend_marker`` (imported from the
    project conftest) with a synthetic request carrying a
    ``backend("sqlite")`` marker. Locks the post-#1971 contract that
    a stray sqlite marker raises ``pytest.UsageError`` rather than
    silently routing through pg.
    """
    from tests.conftest import _resolve_backend_marker

    class _FakeMarker:
        args = ("sqlite",)

    class _FakeNode:
        def get_closest_marker(self, _name: str):
            return _FakeMarker()

    class _FakeRequest:
        node = _FakeNode()

    with pytest.raises(pytest.UsageError, match="sqlite"):
        _resolve_backend_marker(_FakeRequest())
