"""Move A PR 3 — config-identity guard (PR #2026 v7+v8 / Codex r7+r8 blocker).

The cache singleton can serve cross-config data when two
``PollyPMConfig`` objects share project keys but were loaded from
different on-disk roots (different ``config_path`` /
``workspace_root``). PR #2026 v7 stamps a ``config_identity`` on
every cache entry at refresh time and re-checks it at every cache
lookup. When the live config's identity disagrees with a stamped
entry, the lookup MUST decline (return ``None``) and fall through to
the direct DB path.

This file pins the cross-config invariant for all five routed call
sites:

* ``_maybe_cache_route_awaits_user`` (cockpit_inbox)
* ``_maybe_cache_count_awaits_user`` (cockpit_inbox)
* ``_maybe_cache_route_operator_view`` (dashboard/operator_view)
* ``CockpitRailRenderer._maybe_cache_route_rollups`` (cockpit_rail)
* ``_maybe_cache_route_state_map`` /
  ``project_state_map_from_config`` (dashboard/operator_view) —
  added in PR #2026 v8 (Codex r8 follow-up: the 5th routed site
  was missed in v7).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import pollypm.cockpit_inbox as cockpit_inbox
import pollypm.dashboard.operator_view as operator_view
from pollypm.dashboard.categorization import ProjectState
from pollypm.state_cache import (
    ProjectStateCache,
    ProjectStateCacheEntry,
    reset_for_test,
)
from pollypm.state_cache.entry import config_identity


@pytest.fixture(autouse=True)
def _isolate_state_cache_singletons():
    reset_for_test()
    yield
    reset_for_test()


@pytest.fixture(autouse=True)
def _reset_legacy_ttl_caches():
    cockpit_inbox._AWAITS_USER_CACHE.clear()
    operator_view._PREFETCH_PROJECT_STATE_CACHE.clear()
    yield
    cockpit_inbox._AWAITS_USER_CACHE.clear()
    operator_view._PREFETCH_PROJECT_STATE_CACHE.clear()


@pytest.fixture(autouse=True)
def _reset_call_site_divergence_counters():
    cockpit_inbox._AWAITS_USER_DIVERGENCE_COUNTER.reset()
    operator_view._STATE_MAP_DIVERGENCE_COUNTER.reset()
    yield


def _make_config(project_keys: list[str], workspace_root: Path) -> SimpleNamespace:
    """Build a minimal config with a distinct ``workspace_root``.

    The config identity helper resolves
    ``config.project.workspace_root`` when ``config.config_path`` is
    absent (the production ``PollyPMConfig`` shape today). Two
    configs with different roots but overlapping project keys are
    exactly the cross-config-leak case the guard exists to catch.
    """

    projects = {
        key: SimpleNamespace(
            key=key,
            path=workspace_root / key,
            tracked=True,
            name=key.title(),
        )
        for key in project_keys
    }
    return SimpleNamespace(
        projects=projects,
        project=SimpleNamespace(workspace_root=str(workspace_root)),
        storage=SimpleNamespace(backend="postgres"),
    )


def _stamped_entry(
    project_key: str,
    *,
    config: Any,
    items: list[Any] | None = None,
    state: ProjectState = ProjectState.WAITING,
) -> ProjectStateCacheEntry:
    """Build an entry stamped with ``config``'s identity (mirrors refresh)."""

    items = items or []
    return ProjectStateCacheEntry(
        project_key=project_key,
        project_path=Path(f"/tmp/{project_key}"),
        tracked=True,
        state=state,
        glyph="?",
        detail="x",
        rail_state=state,
        rail_badge=None,
        rail_sort_rank=0,
        rail_reason="",
        approvals_pending=0,
        awaits_user_count=len(items),
        awaits_user_items=tuple(items),
        config_identity=config_identity(config),
    )


def _seed_cache(
    monkeypatch: pytest.MonkeyPatch,
    entries: dict[str, ProjectStateCacheEntry],
) -> ProjectStateCache:
    cache = ProjectStateCache(refresh_fn=lambda k: None)
    for key, entry in entries.items():
        cache._install_for_test(key, entry)
    monkeypatch.setattr("pollypm.state_cache.is_enabled", lambda: True)
    monkeypatch.setattr("pollypm.state_cache.get_cache", lambda: cache)
    return cache


# ── primary regression: route_awaits_user declines on identity mismatch ──


class TestConfigIdentityGuard:
    """All 4 cache-routed call sites decline on cross-config lookup."""

    def _config_pair(
        self, tmp_path: Path,
    ) -> tuple[SimpleNamespace, SimpleNamespace]:
        """Build two configs with overlapping keys but distinct roots."""

        root_a = tmp_path / "workspace-a"
        root_b = tmp_path / "workspace-b"
        root_a.mkdir()
        root_b.mkdir()
        config_a = _make_config(["alpha", "beta"], root_a)
        config_b = _make_config(["alpha", "beta"], root_b)
        # Sanity: project keys overlap, identities don't.
        assert set(config_a.projects.keys()) == set(config_b.projects.keys())
        assert config_identity(config_a) != config_identity(config_b)
        return config_a, config_b

    def test_route_awaits_user_declines_on_identity_mismatch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Cache built for config_A must NOT serve config_B."""

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config_a, config_b = self._config_pair(tmp_path)

        # Seed cache with entries stamped against config_A.
        entries = {
            key: _stamped_entry(key, config=config_a, items=[])
            for key in config_a.projects.keys()
        }
        _seed_cache(monkeypatch, entries)

        # Looking up with config_B (overlapping keys, different
        # identity) MUST decline so the caller falls through.
        result = cockpit_inbox._maybe_cache_route_awaits_user(config_b)
        assert result is None

        # And looking up with config_A (matching identity) MUST hit.
        result_a = cockpit_inbox._maybe_cache_route_awaits_user(config_a)
        assert result_a is not None
        assert result_a == []  # no items seeded, but the path served

    def test_count_awaits_user_declines_on_identity_mismatch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config_a, config_b = self._config_pair(tmp_path)
        entries = {
            key: _stamped_entry(key, config=config_a, items=[])
            for key in config_a.projects.keys()
        }
        _seed_cache(monkeypatch, entries)

        assert cockpit_inbox._maybe_cache_count_awaits_user(config_b) is None
        # Matching identity → 0 items total.
        assert cockpit_inbox._maybe_cache_count_awaits_user(config_a) == 0

    def test_operator_view_declines_on_identity_mismatch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config_a, config_b = self._config_pair(tmp_path)
        entries = {
            key: _stamped_entry(key, config=config_a)
            for key in config_a.projects.keys()
        }
        _seed_cache(monkeypatch, entries)

        # The operator-view fast-path also calls ``_collect_project_scans``
        # which itself runs DB work; stub it out to a non-empty list so
        # we're testing the identity gate alone.
        monkeypatch.setattr(
            operator_view,
            "_collect_project_scans",
            lambda cfg: [
                SimpleNamespace(project_key=k)
                for k in cfg.projects.keys()
            ],
        )

        assert operator_view._maybe_cache_route_operator_view(config_b) is None

    def test_rail_rollups_declines_on_identity_mismatch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from pollypm.cockpit_rail import CockpitRouter

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config_a, config_b = self._config_pair(tmp_path)
        entries = {
            key: _stamped_entry(key, config=config_a)
            for key in config_a.projects.keys()
        }
        _seed_cache(monkeypatch, entries)

        # The router's helper takes (config, alerts). Empty alerts
        # list keeps the actionable-alert gate happy. Bypass __init__
        # — only the unbound method matters for this gate test.
        router = CockpitRouter.__new__(CockpitRouter)
        result = router._maybe_cache_route_rollups(config_b, [])
        assert result is None

    def test_state_map_route_declines_cross_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """``_maybe_cache_route_state_map`` declines on cross-config lookup.

        PR #2026 v8 (Codex r8 blocker): the v7 fix added the identity
        guard to 4 routed sites but missed
        ``_maybe_cache_route_state_map`` (the engine behind
        ``project_state_map_from_config``). With cache stamped for
        config_A and a lookup from config_B (overlapping keys, distinct
        workspace_root), the route MUST return ``None`` so the direct
        path runs.
        """

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config_a, config_b = self._config_pair(tmp_path)

        # Seed the cache with stamped entries against config_A. The
        # state-map fast path needs every tracked project's ``state``
        # populated, so stamp WAITING (any non-None state will do).
        entries = {
            key: _stamped_entry(key, config=config_a, state=ProjectState.WAITING)
            for key in config_a.projects.keys()
        }
        _seed_cache(monkeypatch, entries)

        # Cross-config lookup (config_B against config_A's snapshot)
        # MUST decline. Sanity-check the matching-identity path serves.
        assert operator_view._maybe_cache_route_state_map(config_b) is None
        served = operator_view._maybe_cache_route_state_map(config_a)
        assert served is not None
        assert set(served.keys()) == set(config_a.projects.keys())

    def test_unstamped_entries_skip_identity_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Legacy / test-fixture entries with ``config_identity=""``
        bypass the guard. Only the real refresher stamps; pinning
        backward compatibility for hand-rolled entries in existing
        parity tests.
        """

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config_a, _ = self._config_pair(tmp_path)

        # Hand-roll an unstamped entry (config_identity defaults to "").
        unstamped = ProjectStateCacheEntry(
            project_key="alpha",
            project_path=Path("/tmp/alpha"),
            tracked=True,
            state=ProjectState.IDLE,
            awaits_user_count=0,
            awaits_user_items=(),
        )
        assert unstamped.config_identity == ""
        _seed_cache(monkeypatch, {"alpha": unstamped})

        # Even with a mismatched-looking config, unstamped entries are
        # served (this is the test-compat escape hatch).
        config_solo = _make_config(["alpha"], tmp_path / "workspace-a")
        result = cockpit_inbox._maybe_cache_route_awaits_user(config_solo)
        # Either the served path returns the empty list, or the route
        # declines for an OTHER reason (workspace-root probe, etc.).
        # The point is: the identity guard didn't fire on the
        # unstamped entry.
        # Force the workspace-root probe to "no open" to isolate the
        # identity gate from other guards.
        monkeypatch.setattr(
            cockpit_inbox, "_workspace_root_inbox_has_open", lambda cfg: False,
        )
        result = cockpit_inbox._maybe_cache_route_awaits_user(config_solo)
        assert result == []


# ── refresh stamping ──────────────────────────────────────────────


def test_refresher_stamps_config_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``compute_entry_for_project`` must stamp the config's identity."""

    from pollypm.state_cache.refresh_impl import compute_entry_for_project

    config = _make_config(["alpha"], tmp_path / "workspace-a")

    # Stub out the awaits-user fetch + categorization import so the
    # refresher returns a real entry without hitting any DB.
    monkeypatch.setattr(
        cockpit_inbox, "_pm_inbox_awaits_user_list_uncached", lambda cfg: [],
    )

    entry = compute_entry_for_project("alpha", config)
    assert entry.config_identity == config_identity(config)
    assert entry.config_identity != ""
