"""Tests for the rail's project-state categorization (#1642 → Move A PR 3).

Pre-#1642, ``CockpitRouter._project_categorizations`` called through to
``project_state_map_from_config`` on every rail-refresh tick. That
function opens every project DB + walks every inbox source, so a
navigation burst could stack a multi-second sweep inside the
``rail_refresh`` worker and contend with route workers + pane loaders.

The original #1642 fix wrapped the call in a short-TTL cache keyed on
the config identity. Move A PR 3 (docs/design/move-a-state-cache.md
§9.5) **removes** that TTL: the in-process state cache's
``global_version()`` short-circuit + the per-call TTL inside
:func:`project_state_map_from_config` (``_PREFETCH_PROJECT_STATE_TTL_SECONDS``)
already collapse navigation-burst refreshes into a single compute. Two
cache layers were worse than one — a freshly completed task hung
around as WORKING for up to 2s with no invalidation path.

These tests pin the post-removal invariants:

* The rail's :meth:`_project_categorizations` is now a thin wrapper
  around :func:`project_state_map_from_config`; every call delegates
  to the underlying helper, no router-side memoisation.
* Updates to the underlying state are visible on the next call (no
  waiting on a TTL to elapse).
* The router-side TTL attributes are gone — verified by introspection
  so a future "let's add caching back" PR breaks a test loud and clear.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.cockpit_rail import CockpitRouter
from pollypm.dashboard import ProjectState


def _router(tmp_path: Path) -> CockpitRouter:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "[project]\n"
        'name = "PollyPM"\n'
        f'root_dir = "{tmp_path}"\n'
        'tmux_session = "pollypm"\n'
        f'base_dir = "{tmp_path / ".pollypm"}"\n'
    )
    return CockpitRouter(config_path)


def test_project_categorizations_does_not_cache_within_ttl(
    monkeypatch, tmp_path: Path,
) -> None:
    """Move A PR 3 (§9.5): the rail-side TTL is GONE.

    Every call delegates to ``project_state_map_from_config`` so the
    rail picks up state changes immediately. ``project_state_map_from_config``
    itself has a 1s ``_PREFETCH_PROJECT_STATE_TTL_SECONDS`` cache plus
    (when the env flag is on) the state-cache fast path; both yield the
    same "skip wasted work when nothing changed" win without trapping
    stale data behind a rail-side timer.
    """
    router = _router(tmp_path)
    config = object()

    calls: list[object] = []

    def _fake_map(cfg):  # noqa: ANN001, ANN202
        calls.append(cfg)
        return {"alpha": ProjectState.WORKING, "beta": ProjectState.IDLE}

    monkeypatch.setattr(
        "pollypm.dashboard.operator_view.project_state_map_from_config",
        _fake_map,
    )

    first = router._project_categorizations(config)
    for _ in range(9):
        router._project_categorizations(config)

    # No router-side caching — every call delegates fresh.
    assert len(calls) == 10
    assert first == {"alpha": "working", "beta": "idle"}


def test_project_categorizations_updates_visible_immediately(
    monkeypatch, tmp_path: Path,
) -> None:
    """A change to the underlying state is visible on the next call.

    Regression test for the TTL pathology: freshly-completed work
    hanging around as WORKING for up to 2s. Now the rail picks up
    state transitions on the next tick.
    """
    router = _router(tmp_path)
    config = object()

    state = {"map": {"alpha": ProjectState.WORKING}}

    def _fake_map(cfg):  # noqa: ANN001, ANN202
        return state["map"]

    monkeypatch.setattr(
        "pollypm.dashboard.operator_view.project_state_map_from_config",
        _fake_map,
    )

    first = router._project_categorizations(config)
    assert first == {"alpha": "working"}

    # Simulate the task completing — the rail's next call MUST see the
    # new state, with no waiting on a TTL to elapse.
    state["map"] = {"alpha": ProjectState.IDLE}
    second = router._project_categorizations(config)
    assert second == {"alpha": "idle"}


def test_project_categorizations_handles_failure_without_raising(
    monkeypatch, tmp_path: Path,
) -> None:
    """A raising sweep is contained — caller gets ``{}`` instead of a crash.

    The router stays best-effort: a broken underlying helper degrades
    to an empty map rather than propagating into the rail render path.
    """
    router = _router(tmp_path)
    config = object()

    def _boom(_cfg):  # noqa: ANN001, ANN202
        raise RuntimeError("DB unavailable")

    monkeypatch.setattr(
        "pollypm.dashboard.operator_view.project_state_map_from_config",
        _boom,
    )

    result_a = router._project_categorizations(config)
    result_b = router._project_categorizations(config)
    assert result_a == {} == result_b


def test_router_no_longer_holds_categorization_cache_state(
    tmp_path: Path,
) -> None:
    """The router-side TTL attributes are GONE (Move A PR 3, §9.5).

    A future "let's add caching back" PR will break this test.
    """
    router = _router(tmp_path)
    assert not hasattr(router, "_project_categorizations_cache")
    assert not hasattr(router, "_project_categorizations_cache_key")
    assert not hasattr(router, "_project_categorizations_cached_at")
    assert not hasattr(CockpitRouter, "_PROJECT_CATEGORIZATIONS_TTL_SECONDS")
