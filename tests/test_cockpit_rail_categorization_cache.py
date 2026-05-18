"""Tests for the rail's project-state categorization TTL cache (#1642).

Pre-#1642, ``CockpitRouter._project_categorizations`` called through to
``project_state_map_from_config`` on every rail-refresh tick. That
function opens every project DB + walks every inbox source, so a
navigation burst could stack a multi-second sweep inside the
``rail_refresh`` worker and contend with route workers + pane loaders.

The fix wraps the call in a short-TTL cache keyed on the config
identity. These tests pin two invariants:

* Within the TTL, repeated calls collapse to a single underlying sweep
  (no DB re-scans on every tick).
* The cache is invalidated when ``_clear_rail_caches`` runs — which is
  the same chokepoint ``_load_config`` already routes config-mtime
  reloads through.
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


def test_project_categorizations_caches_within_ttl(monkeypatch, tmp_path: Path) -> None:
    """Repeated calls within the TTL must not re-scan project DBs.

    Counts the number of times ``project_state_map_from_config`` is
    invoked; on a 0.8s-tick cockpit, 10 calls landing inside the cache
    window must collapse to exactly one underlying sweep.
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

    assert len(calls) == 1, (
        "rail re-scanned project DBs within TTL — expected cached result"
    )
    assert first == {"alpha": "working", "beta": "idle"}


def test_project_categorizations_invalidates_on_cache_clear(
    monkeypatch, tmp_path: Path,
) -> None:
    """``_clear_rail_caches`` (called by ``_load_config`` on mtime change)
    must drop the categorization cache so a reloaded config doesn't keep
    painting stale glyphs."""
    router = _router(tmp_path)
    config = object()

    state = {"map": {"alpha": ProjectState.WORKING}}
    calls = 0

    def _fake_map(cfg):  # noqa: ANN001, ANN202
        nonlocal calls
        calls += 1
        return state["map"]

    monkeypatch.setattr(
        "pollypm.dashboard.operator_view.project_state_map_from_config",
        _fake_map,
    )

    router._project_categorizations(config)
    assert calls == 1
    router._clear_rail_caches()
    state["map"] = {"alpha": ProjectState.WAITING}
    result = router._project_categorizations(config)
    assert calls == 2
    assert result == {"alpha": "waiting"}


def test_project_categorizations_returns_independent_dict(
    monkeypatch, tmp_path: Path,
) -> None:
    """Callers must not be able to mutate the cached map.

    The rail glues the result onto ``CockpitItem`` rendering; if a
    caller pops keys (e.g. while walking project rows) the cached map
    must stay intact for the next tick.
    """
    router = _router(tmp_path)
    config = object()

    monkeypatch.setattr(
        "pollypm.dashboard.operator_view.project_state_map_from_config",
        lambda _cfg: {"alpha": ProjectState.WORKING},
    )

    first = router._project_categorizations(config)
    first["alpha"] = "MUTATED"
    second = router._project_categorizations(config)
    assert second["alpha"] == "working"


def test_project_categorizations_caches_empty_on_failure(
    monkeypatch, tmp_path: Path,
) -> None:
    """A raising sweep must still be cached as ``{}`` so the rail
    doesn't re-trigger the (expensive, failing) scan every tick."""
    router = _router(tmp_path)
    config = object()

    calls = 0

    def _boom(_cfg):  # noqa: ANN001, ANN202
        nonlocal calls
        calls += 1
        raise RuntimeError("DB unavailable")

    monkeypatch.setattr(
        "pollypm.dashboard.operator_view.project_state_map_from_config",
        _boom,
    )

    result_a = router._project_categorizations(config)
    result_b = router._project_categorizations(config)
    assert result_a == {} == result_b
    assert calls == 1
