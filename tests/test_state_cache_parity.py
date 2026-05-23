"""Tests for the state-cache parity contract.

Uses ``always=True`` on the historical :class:`DivergenceCounter` to
exercise the test-only helper; production behavior is sampler-off per
PR #2029.

Pins ``docs/design/move-a-state-cache.md`` §6.2 / §7 / §8.4:

* The two PR-2-routed call sites — ``pm_inbox_awaits_user_list`` and
  ``project_state_map_from_config`` — must return semantically equal
  results on the cached-fast-path branch and the direct-DB branch
  for any config the cache has fully populated.
* ``tests/test_inbox_default_lens.py``'s three-surfaces-one-predicate
  invariant (``cockpit_inbox.py:268-295``) is NOT regressed — the
  routed helper still returns identical content when the cache is on
  vs off. The lens-test file itself doesn't exist in the tree today;
  what we pin here is its underlying invariant (rail badge,
  dashboard waiting section, inbox default lens all read the same
  predicate).
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import pollypm.cockpit_inbox as cockpit_inbox
import pollypm.dashboard.operator_view as operator_view
from pollypm.dashboard.categorization import ProjectState
from pollypm.state_cache import (
    DivergenceCounter,
    ProjectStateCache,
    ProjectStateCacheEntry,
    compare_awaits_user_lists,
    compare_state_maps,
    reset_for_test,
)


# ── shared fixtures ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_state_cache_singletons():
    """Drop the module-level cache singleton before + after each test."""

    reset_for_test()
    yield
    reset_for_test()


@pytest.fixture(autouse=True)
def _reset_legacy_ttl_caches():
    """The PR 2 fast-path coexists with the legacy 1s TTL cache."""

    cockpit_inbox._AWAITS_USER_CACHE.clear()
    operator_view._PREFETCH_PROJECT_STATE_CACHE.clear()
    yield
    cockpit_inbox._AWAITS_USER_CACHE.clear()
    operator_view._PREFETCH_PROJECT_STATE_CACHE.clear()


@pytest.fixture(autouse=True)
def _reset_call_site_divergence_counters():
    """Each call site has its own counter — reset between tests."""

    cockpit_inbox._AWAITS_USER_DIVERGENCE_COUNTER.reset()
    operator_view._STATE_MAP_DIVERGENCE_COUNTER.reset()
    yield


def _make_config(project_keys: list[str], tmp_path: Path) -> SimpleNamespace:
    """Build a minimal config exposing ``.projects`` for the routed helpers.

    Mirrors the shape ``test_operator_dashboard_pilot.py`` uses — the
    routed code paths only read ``config.projects`` and (transitively)
    a backend marker, so a ``SimpleNamespace`` is enough.
    """

    projects = {
        key: SimpleNamespace(
            key=key,
            path=tmp_path / key,
            tracked=True,
            name=key.title(),
        )
        for key in project_keys
    }
    return SimpleNamespace(
        projects=projects,
        project=SimpleNamespace(workspace_root=str(tmp_path)),
        storage=SimpleNamespace(backend="postgres"),
    )


def _inbox_item(
    *, project: str, source: str, ident: str, title: str = "",
) -> SimpleNamespace:
    """Construct an awaits-user item lookalike for parity comparisons."""

    if source == "task":
        return SimpleNamespace(
            project=project,
            scope=project,
            source="task",
            task_id=ident,
            message_id=None,
            title=title or f"{project}: task {ident}",
        )
    return SimpleNamespace(
        project=project,
        scope=project,
        source="message",
        task_id=None,
        message_id=ident,
        title=title or f"{project}: msg {ident}",
    )


def _entry(
    project_key: str,
    *,
    state: ProjectState,
    items: list[Any],
    rail_state: Any = None,
    rail_badge: str | None = None,
    rail_sort_rank: int = 0,
    rail_reason: str = "",
    approvals_pending: int = 0,
    glyph: str = "",
    detail: str = "",
    latest_heartbeat_by_session: dict[str, Any] | None = None,
    config_identity: str = "",
) -> ProjectStateCacheEntry:
    return ProjectStateCacheEntry(
        project_key=project_key,
        project_path=Path(f"/tmp/{project_key}"),
        tracked=True,
        state=state,
        glyph=glyph,
        detail=detail,
        rail_state=rail_state,
        rail_badge=rail_badge,
        rail_sort_rank=rail_sort_rank,
        rail_reason=rail_reason,
        approvals_pending=approvals_pending,
        awaits_user_count=len(items),
        awaits_user_items=tuple(items),
        latest_heartbeat_by_session=dict(latest_heartbeat_by_session or {}),
        config_identity=config_identity,
    )


def _seed_cache(
    monkeypatch: pytest.MonkeyPatch,
    entries: dict[str, ProjectStateCacheEntry],
) -> ProjectStateCache:
    """Install a hand-rolled cache + force the flag-on path on both sites.

    Avoids spinning up the real refresher (which would tail the audit
    log dir and run the full per-project compute). The cache itself
    is the real class; only its entries are pre-baked.
    """

    cache = ProjectStateCache(refresh_fn=lambda k: None)
    for key, entry in entries.items():
        cache._install_for_test(key, entry)
    monkeypatch.setattr("pollypm.state_cache.is_enabled", lambda: True)
    monkeypatch.setattr("pollypm.state_cache.get_cache", lambda: cache)
    return cache


# ── pm_inbox_awaits_user_list parity ───────────────────────────────


class TestAwaitsUserParity:
    """Cached + direct paths return the same items across 3 projects."""

    def _direct_items(self) -> list[Any]:
        # The "direct" path's contract: returns every awaits-user item
        # across every tracked project + workspace root. We simulate by
        # building the workspace-wide list the per-project entries
        # would have been derived from.
        return [
            _inbox_item(project="alpha", source="task", ident="alpha/1"),
            _inbox_item(project="alpha", source="message", ident="m-a1"),
            _inbox_item(project="beta", source="task", ident="beta/3"),
            _inbox_item(project="gamma", source="message", ident="m-g7"),
        ]

    def _entries_from_direct(self) -> dict[str, ProjectStateCacheEntry]:
        direct = self._direct_items()
        per_project: dict[str, list[Any]] = {"alpha": [], "beta": [], "gamma": []}
        for item in direct:
            per_project[item.project].append(item)
        return {
            key: _entry(key, state=ProjectState.WAITING, items=items)
            for key, items in per_project.items()
        }

    def test_flag_off_uses_direct_path_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """With the flag OFF the cache fast-path MUST be skipped.

        Pins the §6.2 contract: "Flag off → unchanged behavior."
        """
        monkeypatch.setenv("POLLYPM_STATE_CACHE", "0")
        direct = self._direct_items()
        config = _make_config(["alpha", "beta", "gamma"], tmp_path)

        monkeypatch.setattr(
            cockpit_inbox,
            "_pm_inbox_awaits_user_list_uncached",
            lambda cfg: list(direct),
        )

        result = cockpit_inbox.pm_inbox_awaits_user_list(config)
        # Same identity set as the direct call.
        matched, reason = compare_awaits_user_lists(result, direct)
        assert matched, reason

    def test_flag_on_with_populated_cache_uses_fast_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Cache fast-path returns identical content + skips the direct call."""
        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha", "beta", "gamma"], tmp_path)
        _seed_cache(monkeypatch, self._entries_from_direct())

        direct_called = {"n": 0}

        def _no_call(_cfg: Any) -> list[Any]:
            direct_called["n"] += 1
            return self._direct_items()

        monkeypatch.setattr(
            cockpit_inbox, "_pm_inbox_awaits_user_list_uncached", _no_call,
        )

        result = cockpit_inbox.pm_inbox_awaits_user_list(config)
        # Sampler is at 1-in-50; with the default counter fresh, the
        # first call lands on counter==1 → no sample. So the direct
        # path stays untouched.
        assert direct_called["n"] == 0
        matched, reason = compare_awaits_user_lists(result, self._direct_items())
        assert matched, reason

    def test_flag_on_with_empty_cache_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """An empty cache MUST fall back to the direct path (cold start)."""
        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        _seed_cache(monkeypatch, {})
        config = _make_config(["alpha"], tmp_path)
        direct = self._direct_items()
        called = {"n": 0}

        def _direct(_cfg: Any) -> list[Any]:
            called["n"] += 1
            return list(direct)

        monkeypatch.setattr(
            cockpit_inbox, "_pm_inbox_awaits_user_list_uncached", _direct,
        )
        result = cockpit_inbox.pm_inbox_awaits_user_list(config)
        assert called["n"] == 1
        assert len(result) == len(direct)

    def test_slow_uncached_fetch_does_not_write_born_expired_entry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """#1968 regression — when the uncached awaits-user fetch takes
        longer than ``_AWAITS_USER_TTL_SECONDS``, the cache MUST stamp
        the completion time (post-fetch) so the entry stays fresh for
        a full TTL window. Stamping the pre-fetch time would make the
        entry born-expired and force every subsequent caller within the
        TTL window to re-run the multi-second sweep.

        Asserts that back-to-back calls share a single uncached invocation
        even when the first call exceeds the TTL — matching the #1957
        contract already pinned for ``all_tasks_grouped`` /
        ``_gather_worker_roster`` / ``_prefetch_project_state``.
        """
        # Force the cache fast-path off so we exercise the legacy TTL
        # cache wrapper (the path that #1968 regresses).
        monkeypatch.setenv("POLLYPM_STATE_CACHE", "0")
        config = _make_config(["alpha"], tmp_path)
        direct = self._direct_items()

        call_count = {"n": 0}
        ttl = cockpit_inbox._AWAITS_USER_TTL_SECONDS
        # Drive the post-fetch monotonic stamp forward by more than the
        # TTL during the first call. Using a fake clock keeps the test
        # fast + deterministic (no real sleep). The first call advances
        # the clock by ``ttl + 0.2`` between the pre-fetch read inside
        # the wrapper and the post-fetch read; the second call should
        # then hit the cache because the post-fetch stamp was used.
        clock = {"t": 1000.0}

        def _slow_uncached(_cfg: Any) -> list[Any]:
            call_count["n"] += 1
            # Simulate a fetch that takes longer than the TTL.
            clock["t"] += ttl + 0.2
            return list(direct)

        def _fake_monotonic() -> float:
            return clock["t"]

        monkeypatch.setattr(
            cockpit_inbox,
            "_pm_inbox_awaits_user_list_uncached",
            _slow_uncached,
        )
        # Patch the ``time`` module imported lazily inside the wrapper.
        import time as _time

        monkeypatch.setattr(_time, "monotonic", _fake_monotonic)

        first = cockpit_inbox.pm_inbox_awaits_user_list(config)
        # Second call is immediately back-to-back; clock has not
        # advanced further (no sleep between calls). With the #1968 fix
        # the cache stamp is the post-fetch time, so the entry's age is
        # ~0s and the second call MUST be served from cache.
        second = cockpit_inbox.pm_inbox_awaits_user_list(config)

        assert call_count["n"] == 1, (
            "uncached fetch was invoked twice — cache entry was born-expired "
            "(stale pre-fetch stamp). #1968 regression."
        )
        assert len(first) == len(direct)
        assert len(second) == len(direct)


# ── project_state_map_from_config parity ───────────────────────────


class TestProjectStateMapParity:
    """Cached + direct paths return identical ``{key: state}`` dicts."""

    def _direct_map(self) -> dict[str, ProjectState]:
        return {
            "alpha": ProjectState.WAITING,
            "beta": ProjectState.WORKING,
            "gamma": ProjectState.IDLE,
        }

    def _entries_from_direct(self) -> dict[str, ProjectStateCacheEntry]:
        return {
            key: _entry(key, state=state, items=[])
            for key, state in self._direct_map().items()
        }

    def test_flag_off_runs_direct_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Flag off — the public helper bypasses the cache fast-path entirely."""

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "0")
        config = _make_config(["alpha", "beta", "gamma"], tmp_path)
        # The fast-path returns None when the flag is off; we pin that
        # explicitly rather than re-running the (real-DB-touching)
        # body, which the §6.2 "Flag off → unchanged behavior"
        # contract guarantees is unmodified by PR 2.
        assert operator_view._maybe_cache_route_state_map(config) is None
        # Run the helper too with the svc stubbed so the body degrades
        # to the IDLE-for-each-tracked-project branch — confirms the
        # public helper still returns a per-project map.
        monkeypatch.setattr(
            operator_view, "_open_shared_work_service", lambda cfg: None,
        )
        result = operator_view.project_state_map_from_config(config)
        assert set(result.keys()) == {"alpha", "beta", "gamma"}

    def test_flag_on_with_populated_cache_uses_fast_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha", "beta", "gamma"], tmp_path)
        _seed_cache(monkeypatch, self._entries_from_direct())

        direct_called = {"n": 0}

        def _direct_called(_cfg: Any) -> dict[str, ProjectState]:
            direct_called["n"] += 1
            return self._direct_map()

        monkeypatch.setattr(
            operator_view,
            "_direct_project_state_map_from_config",
            _direct_called,
        )
        result = operator_view.project_state_map_from_config(config)
        assert direct_called["n"] == 0
        matched, reason = compare_state_maps(result, self._direct_map())
        assert matched, reason

    def test_flag_on_with_partial_cache_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """If the cache is missing a tracked project, defer to direct.

        Partial cache → the fast-path returns ``None`` and the public
        helper runs its original (direct) body. We assert the
        fast-path declined by checking ``_maybe_cache_route_state_map``
        returned ``None`` for this config.
        """
        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha", "beta", "gamma"], tmp_path)
        # Only seed alpha and beta — gamma is missing.
        partial = {
            key: _entry(key, state=ProjectState.IDLE, items=[])
            for key in ("alpha", "beta")
        }
        _seed_cache(monkeypatch, partial)

        # Direct assertion on the routing decision: a partial cache
        # must NOT serve a cached map.
        assert operator_view._maybe_cache_route_state_map(config) is None

        # The public helper then degrades through the original body
        # (which we stub via _open_shared_work_service=None so it
        # doesn't try to open a real pg connection from a fake config).
        monkeypatch.setattr(
            operator_view, "_open_shared_work_service", lambda cfg: None,
        )
        result = operator_view.project_state_map_from_config(config)
        # The direct body returns IDLE for every tracked project when
        # no svc opens — confirms we fell through.
        assert set(result.keys()) == {"alpha", "beta", "gamma"}


# ── divergence sampler ─────────────────────────────────────────────


class TestDivergenceSampler:
    """Test-only helper exercise: ``always=True`` for test setup, not
    runtime sampling. Production is sampler-off per PR #2029."""

    def test_sampler_fires_on_nth_call(self) -> None:
        # PR 4: pass ``always=True`` so the cache-authoritative no-op
        # doesn't short-circuit the sampler under test.
        counter = DivergenceCounter(rate=3, always=True)
        # Calls 1, 2 → no sample; call 3 → sample.
        assert counter.should_sample() is False
        assert counter.should_sample() is False
        assert counter.should_sample() is True
        # Then 4, 5 → no; 6 → yes.
        assert counter.should_sample() is False
        assert counter.should_sample() is False
        assert counter.should_sample() is True

    def test_compare_awaits_user_lists_identity(self) -> None:
        a = _inbox_item(project="alpha", source="task", ident="alpha/1")
        b = _inbox_item(project="beta", source="task", ident="beta/1")
        matched, _ = compare_awaits_user_lists([a, b], [b, a])
        assert matched, "Comparator must be order-insensitive"

    def test_compare_awaits_user_lists_detects_diff(self) -> None:
        a = _inbox_item(project="alpha", source="task", ident="alpha/1")
        b = _inbox_item(project="beta", source="task", ident="beta/1")
        matched, reason = compare_awaits_user_lists([a], [a, b])
        assert not matched
        assert "missing_from_cache" in reason
        assert "len(cached)=1" in reason
        assert "len(direct)=2" in reason

    def test_compare_state_maps_normalizes_enum_values(self) -> None:
        # ProjectState.WAITING.value == "waiting" — comparator must
        # accept either the enum or its string value.
        matched, _ = compare_state_maps(
            {"alpha": ProjectState.WAITING},
            {"alpha": "waiting"},
        )
        assert matched

    def test_awaits_user_sampler_logs_warn_on_mismatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Inject a mismatch, force a sample, assert the WARN landed."""

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha"], tmp_path)

        # Cache says ONE item for alpha; direct says TWO. Sampler MUST
        # log on the difference.
        cache_item = _inbox_item(project="alpha", source="task", ident="alpha/1")
        direct_items = [
            cache_item,
            _inbox_item(project="alpha", source="message", ident="m-extra"),
        ]
        entries = {"alpha": _entry("alpha", state=ProjectState.WAITING, items=[cache_item])}
        _seed_cache(monkeypatch, entries)

        # Force every call to sample by swapping in a rate-1 counter.
        cockpit_inbox._AWAITS_USER_DIVERGENCE_COUNTER = DivergenceCounter(rate=1, always=True)

        monkeypatch.setattr(
            cockpit_inbox,
            "_pm_inbox_awaits_user_list_uncached",
            lambda cfg: list(direct_items),
        )

        with caplog.at_level(logging.WARNING, logger="pollypm.state_cache.divergence"):
            cockpit_inbox.pm_inbox_awaits_user_list(config)

        warns = [
            rec for rec in caplog.records
            if rec.levelno == logging.WARNING
            and "state_cache: divergence at pm_inbox_awaits_user_list"
            in rec.getMessage()
        ]
        assert warns, (
            "expected a divergence WARN, got:\n"
            + "\n".join(r.getMessage() for r in caplog.records)
        )

    def test_state_map_sampler_logs_warn_on_mismatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """State-map sampler emits the canonical WARN line on mismatch."""

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha", "beta"], tmp_path)

        # Cache says WAITING+IDLE; direct says WORKING+IDLE.
        entries = {
            "alpha": _entry("alpha", state=ProjectState.WAITING, items=[]),
            "beta": _entry("beta", state=ProjectState.IDLE, items=[]),
        }
        _seed_cache(monkeypatch, entries)

        operator_view._STATE_MAP_DIVERGENCE_COUNTER = DivergenceCounter(rate=1, always=True)

        monkeypatch.setattr(
            operator_view,
            "_direct_project_state_map_from_config",
            lambda cfg: {
                "alpha": ProjectState.WORKING,
                "beta": ProjectState.IDLE,
            },
        )

        with caplog.at_level(logging.WARNING, logger="pollypm.state_cache.divergence"):
            operator_view.project_state_map_from_config(config)

        warns = [
            rec for rec in caplog.records
            if rec.levelno == logging.WARNING
            and "state_cache: divergence at project_state_map_from_config"
            in rec.getMessage()
        ]
        assert warns, (
            "expected a divergence WARN, got:\n"
            + "\n".join(r.getMessage() for r in caplog.records)
        )

    def test_no_warn_when_cache_and_direct_agree(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Sampler runs both paths but stays silent on agreement."""

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha"], tmp_path)

        item = _inbox_item(project="alpha", source="task", ident="alpha/1")
        entries = {
            "alpha": _entry("alpha", state=ProjectState.WAITING, items=[item])
        }
        _seed_cache(monkeypatch, entries)
        cockpit_inbox._AWAITS_USER_DIVERGENCE_COUNTER = DivergenceCounter(rate=1, always=True)
        monkeypatch.setattr(
            cockpit_inbox,
            "_pm_inbox_awaits_user_list_uncached",
            lambda cfg: [item],
        )

        with caplog.at_level(logging.WARNING, logger="pollypm.state_cache.divergence"):
            cockpit_inbox.pm_inbox_awaits_user_list(config)

        assert not any(
            "divergence at" in rec.getMessage()
            for rec in caplog.records
        )


# ── three-surfaces-one-predicate invariant (cockpit_inbox.py:268-295) ──


class TestInboxDefaultLensInvariant:
    """Rail badge, dashboard waiting section, inbox default lens agree.

    The invariant lives in :func:`pollypm.cockpit_inbox._count_inbox_tasks_for_label`
    — three surfaces, one predicate. PR 2's routing MUST NOT introduce
    a new "what is waiting on the user?" source. Confirms the routed
    ``pm_inbox_awaits_user_list`` and the count helper still return
    matching lengths in both flag modes.
    """

    def _items(self) -> list[Any]:
        return [
            _inbox_item(project="alpha", source="task", ident="alpha/1"),
            _inbox_item(project="beta", source="task", ident="beta/2"),
        ]

    def test_count_helper_matches_list_length_flag_off(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("POLLYPM_STATE_CACHE", "0")
        items = self._items()
        config = _make_config(["alpha", "beta"], tmp_path)
        monkeypatch.setattr(
            cockpit_inbox,
            "_pm_inbox_awaits_user_list_uncached",
            lambda cfg: list(items),
        )
        listed = cockpit_inbox.pm_inbox_awaits_user_list(config)
        counted = cockpit_inbox._count_inbox_tasks_for_label(config)
        assert len(listed) == counted == len(items)

    def test_count_helper_matches_list_length_flag_on(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        items = self._items()
        config = _make_config(["alpha", "beta"], tmp_path)
        entries = {
            "alpha": _entry("alpha", state=ProjectState.WAITING, items=[items[0]]),
            "beta": _entry("beta", state=ProjectState.WAITING, items=[items[1]]),
        }
        _seed_cache(monkeypatch, entries)
        # The legacy TTL would clobber the cache-routed result; clear
        # it between the listed-vs-counted call pair so the same path
        # serves both.
        listed = cockpit_inbox.pm_inbox_awaits_user_list(config)
        cockpit_inbox._AWAITS_USER_CACHE.clear()
        counted = cockpit_inbox._count_inbox_tasks_for_label(config)
        assert len(listed) == counted == len(items)


# ── Move A PR 3 parity — _count_inbox_tasks_for_label cache route ──


class TestCountInboxTasksForLabelParity:
    """PR 3 (§5 row 2): ``_count_inbox_tasks_for_label`` reads ``awaits_user_count``.

    Three projects, the cache fast-path sums ``entry.awaits_user_count``
    while the direct path runs the workspace-wide sweep + ``len()``.
    Both MUST agree.
    """

    def test_cached_count_skips_workspace_sweep(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Flag on + populated cache: no workspace sweep, sum from snapshot."""

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha", "beta", "gamma"], tmp_path)
        items_alpha = [_inbox_item(project="alpha", source="task", ident="alpha/1")]
        items_beta = [
            _inbox_item(project="beta", source="task", ident="beta/2"),
            _inbox_item(project="beta", source="message", ident="m-b1"),
        ]
        items_gamma: list[Any] = []
        entries = {
            "alpha": _entry("alpha", state=ProjectState.WAITING, items=items_alpha),
            "beta": _entry("beta", state=ProjectState.WAITING, items=items_beta),
            "gamma": _entry("gamma", state=ProjectState.IDLE, items=items_gamma),
        }
        _seed_cache(monkeypatch, entries)

        direct_called = {"n": 0}

        def _no_call(_cfg: Any) -> list[Any]:
            direct_called["n"] += 1
            return items_alpha + items_beta + items_gamma

        monkeypatch.setattr(
            cockpit_inbox, "_pm_inbox_awaits_user_list_uncached", _no_call,
        )

        counted = cockpit_inbox._count_inbox_tasks_for_label(config)
        # Three items total; the direct sweep wasn't touched.
        assert counted == 3
        assert direct_called["n"] == 0

    def test_flag_off_uses_direct_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Flag off — falls back to ``len(pm_inbox_awaits_user_list)``."""

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "0")
        config = _make_config(["alpha", "beta"], tmp_path)
        items = [
            _inbox_item(project="alpha", source="task", ident="alpha/1"),
            _inbox_item(project="beta", source="task", ident="beta/2"),
        ]
        monkeypatch.setattr(
            cockpit_inbox,
            "_pm_inbox_awaits_user_list_uncached",
            lambda cfg: list(items),
        )
        counted = cockpit_inbox._count_inbox_tasks_for_label(config)
        assert counted == 2

    def test_partial_cache_falls_through_to_direct(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Cache missing a tracked project → fall through to direct sweep.

        PR #2026 review: summing only the cached projects would
        silently under-count any tracked project that hasn't been
        refreshed yet (e.g. during the boot-time gap before the
        initial-full-refresh completes). The gate is
        ``known_projects.issubset(snapshot.keys())`` —
        when ``False`` we MUST fall through so the direct sweep
        covers the missing project, and the rail badge keeps showing
        every awaits-user item.
        """

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha", "beta"], tmp_path)
        # Only alpha in the cache — beta is unknown.
        entries = {
            "alpha": _entry(
                "alpha",
                state=ProjectState.WAITING,
                items=[_inbox_item(project="alpha", source="task", ident="alpha/1")],
            ),
        }
        _seed_cache(monkeypatch, entries)

        # Direct path returns BOTH projects' items (1 in alpha, 1 in
        # beta). If the fast-path under-counts (the pre-fix bug) the
        # helper would return 1 — alpha only — because beta isn't in
        # the snapshot. With the issubset() gate the helper MUST fall
        # through to the direct sweep and return 2.
        direct_items = [
            _inbox_item(project="alpha", source="task", ident="alpha/1"),
            _inbox_item(project="beta", source="task", ident="beta/2"),
        ]
        direct_called = {"n": 0}

        def _direct(_cfg: Any) -> list[Any]:
            direct_called["n"] += 1
            return list(direct_items)

        monkeypatch.setattr(
            cockpit_inbox, "_pm_inbox_awaits_user_list_uncached", _direct,
        )
        cockpit_inbox._AWAITS_USER_CACHE.clear()
        counted = cockpit_inbox._count_inbox_tasks_for_label(config)
        # Both projects counted because the fall-through ran the
        # direct sweep — beta was NOT silently dropped.
        assert counted == 2
        # And the direct sweep was actually invoked (proof of
        # fall-through, not the fast-path quietly returning 1).
        assert direct_called["n"] >= 1


# ── Move A PR 3 parity — load_operator_view_from_config cache route ─


class TestLoadOperatorViewParity:
    """PR 3 (§5 row 4): ``load_operator_view_from_config`` reads snapshot."""

    def test_cached_view_skips_shared_work_service_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Cache fast-path: no ``_open_shared_work_service`` call."""

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha", "beta", "gamma"], tmp_path)
        entries = {
            "alpha": _entry(
                "alpha", state=ProjectState.WAITING, items=[],
                glyph="!", detail="Waiting on you",
            ),
            "beta": _entry(
                "beta", state=ProjectState.WORKING, items=[],
                glyph="*", detail="claude active on /beta/1",
            ),
            "gamma": _entry(
                "gamma", state=ProjectState.IDLE, items=[],
                glyph=".", detail="Quiet",
            ),
        }
        _seed_cache(monkeypatch, entries)

        open_calls = {"n": 0}

        def _no_open(_cfg: Any) -> Any:
            open_calls["n"] += 1
            return None

        monkeypatch.setattr(
            operator_view, "_open_shared_work_service", _no_open,
        )

        view = operator_view.load_operator_view_from_config(config)
        assert open_calls["n"] == 0
        # Rows landed in the right sections per state.
        assert [r.project_key for r in view.waiting] == ["alpha"]
        assert [r.project_key for r in view.working] == ["beta"]
        assert [r.project_key for r in view.idle] == ["gamma"]
        # Detail strings come from the cache entry — confirms we
        # didn't silently re-derive them.
        waiting_row = view.waiting[0]
        assert waiting_row.detail == "Waiting on you"
        assert waiting_row.glyph == "!"

    def test_flag_off_runs_direct_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Flag off — the cache fast-path returns ``None``."""

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "0")
        config = _make_config(["alpha"], tmp_path)
        # Direct path will open svc=None → IDLE branch; tracked
        # projects with no awaits-user items land in idle.
        monkeypatch.setattr(
            operator_view, "_open_shared_work_service", lambda cfg: None,
        )
        monkeypatch.setattr(
            cockpit_inbox,
            "_pm_inbox_awaits_user_list_uncached",
            lambda cfg: [],
        )
        view = operator_view.load_operator_view_from_config(config)
        # alpha is tracked + has no awaits-user → IDLE (not PAUSED).
        assert {r.project_key for r in view.idle} == {"alpha"}

    def test_partial_cache_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Missing a tracked project in cache → defer to direct."""

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha", "beta"], tmp_path)
        entries = {
            "alpha": _entry("alpha", state=ProjectState.IDLE, items=[]),
        }
        _seed_cache(monkeypatch, entries)
        # Direct path stub so the fall-through completes without a real
        # work service.
        monkeypatch.setattr(
            operator_view, "_open_shared_work_service", lambda cfg: None,
        )
        monkeypatch.setattr(
            cockpit_inbox,
            "_pm_inbox_awaits_user_list_uncached",
            lambda cfg: [],
        )
        view = operator_view.load_operator_view_from_config(config)
        # Both projects show up — the fall-through covered the gap.
        seen = {
            *(r.project_key for r in view.waiting),
            *(r.project_key for r in view.working),
            *(r.project_key for r in view.idle),
            *(r.project_key for r in view.paused),
        }
        assert seen == {"alpha", "beta"}


# ── Move A PR 3 parity — _project_state_rollups cache route ─────────


class TestProjectStateRollupsParity:
    """PR 3 (§5 row 7): rail ``_project_state_rollups`` reads snapshot."""

    def _router(self, tmp_path: Path):
        from pollypm.cockpit_rail import CockpitRouter

        config_path = tmp_path / "pollypm.toml"
        config_path.write_text(
            "[project]\n"
            'name = "PollyPM"\n'
            f'root_dir = "{tmp_path}"\n'
            'tmux_session = "pollypm"\n'
            f'base_dir = "{tmp_path / ".pollypm"}"\n'
        )
        return CockpitRouter(config_path)

    def test_cached_rollups_skip_pg_fanout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Cache fast-path: no ``all_tasks_grouped`` call, no per-project rollup."""

        from pollypm.cockpit_project_state import ProjectRailState

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        router = self._router(tmp_path)
        config = _make_config(["alpha", "beta"], tmp_path)
        entries = {
            "alpha": _entry(
                "alpha", state=ProjectState.WAITING, items=[],
                rail_state=ProjectRailState.RED,
                rail_badge="(2⚠)",
                rail_sort_rank=10,
                rail_reason="2 approvals pending",
                approvals_pending=2,
            ),
            "beta": _entry(
                "beta", state=ProjectState.IDLE, items=[],
                rail_state=ProjectRailState.NONE,
            ),
        }
        _seed_cache(monkeypatch, entries)

        fanout_called = {"n": 0}

        def _no_fanout(_cfg: Any) -> Any:
            fanout_called["n"] += 1
            return {}

        monkeypatch.setattr(
            "pollypm.cockpit_pg_aggregates.all_tasks_grouped", _no_fanout,
        )

        rollups = router._project_state_rollups(config, alerts=[])
        assert fanout_called["n"] == 0
        assert set(rollups.keys()) == {"alpha", "beta"}
        assert rollups["alpha"].approvals_pending == 2
        assert rollups["alpha"].badge == "(2⚠)"
        assert rollups["alpha"].state is ProjectRailState.RED
        assert rollups["beta"].state is ProjectRailState.NONE

    def test_partial_cache_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """A project missing from cache → defer to direct rollup path."""

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        router = self._router(tmp_path)
        config = _make_config(["alpha", "beta"], tmp_path)
        entries = {
            "alpha": _entry("alpha", state=ProjectState.IDLE, items=[]),
        }
        _seed_cache(monkeypatch, entries)

        called = {"n": 0}

        def _fanout(_cfg: Any) -> dict:
            called["n"] += 1
            return {}

        monkeypatch.setattr(
            "pollypm.cockpit_pg_aggregates.all_tasks_grouped", _fanout,
        )

        rollups = router._project_state_rollups(config, alerts=[])
        # Fallthrough → direct path ran (called pg_all_grouped once).
        assert called["n"] == 1
        # Both keys present even though only one was cached.
        assert set(rollups.keys()) == {"alpha", "beta"}


# ── Move A PR 3 parity — latest_heartbeat cache route ───────────────


class TestLatestHeartbeatParity:
    """#2050: ``_latest_heartbeat_cached`` reads the state-cache snapshot
    first and only falls through to the direct pg facade on a miss.

    PR #2026 disabled the fast-path because the refresher had no
    ``heartbeat.*`` audit-event subscription and would serve a stale
    snapshot indefinitely. #2050 added ``heartbeat.tick`` to
    ``refresher._INVALIDATING_EVENTS`` so every workspace sweep
    invalidates the affected entries — the snapshot is bounded-stale
    by the tick cadence again, the rail's fast-path is restored, and
    the direct facade remains the cold-start / pg-failure fallback.
    """

    def _router(self, tmp_path: Path):
        from pollypm.cockpit_rail import CockpitRouter

        config_path = tmp_path / "pollypm.toml"
        config_path.write_text(
            "[project]\n"
            'name = "PollyPM"\n'
            f'root_dir = "{tmp_path}"\n'
            'tmux_session = "pollypm"\n'
            f'base_dir = "{tmp_path / ".pollypm"}"\n'
        )
        return CockpitRouter(config_path)

    def test_populated_cache_skips_direct_facade(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Populated cache entry → fast-path hit; pg facade NOT called.

        Pins the #2050 contract: when the refresher has already stamped
        a heartbeat into ``latest_heartbeat_by_session``, the rail
        serves it from the snapshot and skips the per-call pg query.
        """

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        router = self._router(tmp_path)

        cached_hb = SimpleNamespace(
            session_name="worker_alpha/1",
            created_at="2026-05-21T01:00:00Z",
            tag="FROM_CACHE",
        )
        entries = {
            "alpha": _entry(
                "alpha",
                state=ProjectState.WORKING,
                items=[],
                latest_heartbeat_by_session={"worker_alpha/1": cached_hb},
            ),
        }
        _seed_cache(monkeypatch, entries)

        pg_calls: list[str] = []

        def _direct(name, *, config=None):  # noqa: ANN001, ANN201
            pg_calls.append(name)
            return SimpleNamespace(tag="UNEXPECTED_DIRECT_HIT")

        monkeypatch.setattr(
            "pollypm.storage.pg_heartbeats.latest_heartbeat", _direct,
        )

        supervisor = SimpleNamespace(store=None, config=None)
        result = router._latest_heartbeat_cached(supervisor, "worker_alpha/1")
        # Cache hit; direct facade was NOT consulted.
        assert pg_calls == []
        assert result is cached_hb
        assert getattr(result, "tag", None) == "FROM_CACHE"

    def test_cache_miss_falls_through_to_direct_facade(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Session not in any cache entry → direct facade serves the read.

        Cold-start safety net: the rail must render heartbeat data even
        before the refresher has populated the entry for this session.
        """

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        router = self._router(tmp_path)

        # Cache entry exists but holds no heartbeat for the queried
        # session_name — that's the cache-miss shape.
        entries = {
            "alpha": _entry(
                "alpha",
                state=ProjectState.WORKING,
                items=[],
                latest_heartbeat_by_session={},
            ),
        }
        _seed_cache(monkeypatch, entries)

        direct_hb = SimpleNamespace(
            session_name="worker_alpha/1",
            created_at="2026-05-21T02:00:00Z",
            tag="FROM_DIRECT",
        )
        pg_calls: list[str] = []

        def _direct(name, *, config=None):  # noqa: ANN001, ANN201
            pg_calls.append(name)
            return direct_hb

        monkeypatch.setattr(
            "pollypm.storage.pg_heartbeats.latest_heartbeat", _direct,
        )

        supervisor = SimpleNamespace(store=None, config=None)
        result = router._latest_heartbeat_cached(supervisor, "worker_alpha/1")
        assert pg_calls == ["worker_alpha/1"]
        assert result is direct_hb

    def test_pg_failure_falls_back_to_supervisor_store(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """If pg_heartbeats raises, fall back to supervisor.store."""

        router = self._router(tmp_path)
        fallback = SimpleNamespace(created_at="2026-05-20T02:00:00Z")

        def _boom(name, *, config=None):  # noqa: ANN001, ANN201
            raise RuntimeError("pg pool unreachable")

        monkeypatch.setattr(
            "pollypm.storage.pg_heartbeats.latest_heartbeat", _boom,
        )

        store_calls: list[str] = []

        class _Store:
            def latest_heartbeat(self, name):  # noqa: ANN001, ANN201
                store_calls.append(name)
                return fallback

        supervisor = SimpleNamespace(store=_Store(), config=None)
        result = router._latest_heartbeat_cached(supervisor, "worker_beta/3")
        assert store_calls == ["worker_beta/3"]
        assert result is fallback

    def test_flag_off_still_uses_direct_facade(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Flag off → cache fast-path skipped; direct facade serves the read.

        Kill-switch behavior — operators can disable the cache route
        with ``POLLYPM_STATE_CACHE=0`` and the rail still renders
        heartbeats via the direct pg path.
        """

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "0")
        router = self._router(tmp_path)

        hb = SimpleNamespace(created_at="2026-05-20T03:00:00Z")
        called: list[str] = []

        def _direct(name, *, config=None):  # noqa: ANN001, ANN201
            called.append(name)
            return hb

        monkeypatch.setattr(
            "pollypm.storage.pg_heartbeats.latest_heartbeat", _direct,
        )

        supervisor = SimpleNamespace(store=None, config=None)
        result = router._latest_heartbeat_cached(supervisor, "worker_alpha/1")
        assert called == ["worker_alpha/1"]
        assert result is hb

    def test_cross_config_cache_entry_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Stamped cross-config entry → fast-path declines, direct serves.

        PR #2080 Codex r1 blocker: the cache singleton is process-wide
        and the heartbeat fast-path was returning the first
        ``by_session[session_name]`` hit without comparing
        ``entry.config_identity`` to the live config's identity. With
        overlapping session names across workspaces, that leaks
        cross-config data. Pin the guard: when the stamped identity
        disagrees, skip the entry and fall through to the direct pg
        facade (matches the guard in ``cockpit_inbox`` and the rail
        rollup map).
        """

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        router = self._router(tmp_path)

        cross_hb = SimpleNamespace(
            session_name="worker_alpha/1",
            created_at="2026-05-21T01:00:00Z",
            tag="STALE_CROSS_CONFIG",
        )
        # Entry stamped with config_identity="alpha" — a different
        # workspace from the live config below (identity "beta").
        entries = {
            "alpha": _entry(
                "alpha",
                state=ProjectState.WORKING,
                items=[],
                latest_heartbeat_by_session={"worker_alpha/1": cross_hb},
                config_identity="alpha",
            ),
        }
        _seed_cache(monkeypatch, entries)

        # Direct facade returns the authoritative live-config value.
        direct_hb = SimpleNamespace(
            session_name="worker_alpha/1",
            created_at="2026-05-21T02:00:00Z",
            tag="FROM_DIRECT_LIVE_CONFIG",
        )
        pg_calls: list[str] = []

        def _direct(name, *, config=None):  # noqa: ANN001, ANN201
            pg_calls.append(name)
            return direct_hb

        monkeypatch.setattr(
            "pollypm.storage.pg_heartbeats.latest_heartbeat", _direct,
        )

        # Live config has identity "beta" — different from the
        # stamped entry — via ``config_path`` (the preferred identity
        # source in ``state_cache.entry.config_identity``).
        live_config = SimpleNamespace(config_path="beta")
        supervisor = SimpleNamespace(store=None, config=live_config)

        result = router._latest_heartbeat_cached(supervisor, "worker_alpha/1")

        # The cross-config entry must NOT be served. The direct
        # facade was called and its result was returned.
        assert result is not cross_hb
        assert getattr(result, "tag", None) != "STALE_CROSS_CONFIG"
        assert pg_calls == ["worker_alpha/1"]
        assert result is direct_hb


# ── Move A PR 3 — TTL removal regression test ───────────────────────


class TestProjectCategorizationsNoTtl:
    """Design §9.5: state-cache routing replaces the 2s TTL.

    The `_project_categorizations` helper used to memoise its result
    behind ``_PROJECT_CATEGORIZATIONS_TTL_SECONDS = 2.0``. Now every
    call delegates straight to ``project_state_map_from_config`` so
    state changes are visible on the next call without waiting on a
    TTL to expire.
    """

    def _router(self, tmp_path: Path):
        from pollypm.cockpit_rail import CockpitRouter

        config_path = tmp_path / "pollypm.toml"
        config_path.write_text(
            "[project]\n"
            'name = "PollyPM"\n'
            f'root_dir = "{tmp_path}"\n'
            'tmux_session = "pollypm"\n'
            f'base_dir = "{tmp_path / ".pollypm"}"\n'
        )
        return CockpitRouter(config_path)

    def test_modifications_are_visible_on_next_call_no_ttl_wait(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """No TTL: a freshly-completed task is no longer stuck as WORKING."""

        router = self._router(tmp_path)
        config = object()
        state = {"map": {"alpha": ProjectState.WORKING}}

        def _fake_map(cfg):  # noqa: ANN001, ANN202
            return state["map"]

        monkeypatch.setattr(
            "pollypm.dashboard.operator_view.project_state_map_from_config",
            _fake_map,
        )

        assert router._project_categorizations(config) == {"alpha": "working"}
        # Without waiting for 2s, mutate the underlying state and the
        # next call MUST see it.
        state["map"] = {"alpha": ProjectState.IDLE}
        assert router._project_categorizations(config) == {"alpha": "idle"}

    def test_router_class_no_longer_has_ttl_constant(self) -> None:
        from pollypm.cockpit_rail import CockpitRouter

        assert not hasattr(
            CockpitRouter, "_PROJECT_CATEGORIZATIONS_TTL_SECONDS",
        )


# ── PR #2026 review blockers — fall-through regression tests ────────


class TestPr2026ReviewBlocker1ActionableAlertFallthrough:
    """Blocker 1: cache fast-path MUST defer when a live actionable
    task alert exists, because the direct ``rollup_project_state``
    folds alerts into ``rail_state`` / ``badge`` / ``reason`` /
    ``actionable_key`` and the cache entry's ``rail_*`` fields were
    computed without alert state.
    """

    def _router(self, tmp_path: Path):
        from pollypm.cockpit_rail import CockpitRouter

        config_path = tmp_path / "pollypm.toml"
        config_path.write_text(
            "[project]\n"
            'name = "PollyPM"\n'
            f'root_dir = "{tmp_path}"\n'
            'tmux_session = "pollypm"\n'
            f'base_dir = "{tmp_path / ".pollypm"}"\n'
        )
        return CockpitRouter(config_path)

    def test_actionable_alert_forces_cache_fallthrough(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Cached base state WORKING, live actionable alert exists →
        the cache MUST decline so the direct path can paint RED.
        """

        from pollypm.cockpit_project_state import ProjectRailState

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        router = self._router(tmp_path)
        config = _make_config(["alpha"], tmp_path)

        # Cache entry says WORKING / NONE — no idea about the alert.
        entries = {
            "alpha": _entry(
                "alpha", state=ProjectState.WORKING, items=[],
                rail_state=ProjectRailState.WORKING,
                rail_badge=None,
                rail_sort_rank=0,
                rail_reason="worker active",
                approvals_pending=0,
            ),
        }
        _seed_cache(monkeypatch, entries)

        # Live actionable alert for alpha/1 — should turn alpha RED.
        alert = SimpleNamespace(
            alert_type="stuck_on_task:alpha/1",
            severity="high",
            message="stuck",
        )

        # Cache fast-path declines (the gate fires).
        assert (
            router._maybe_cache_route_rollups(config, alerts=[alert]) is None
        )

    def test_no_alert_still_uses_cache_fast_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Empty / non-actionable alerts → cache fast-path stays available."""

        from pollypm.cockpit_project_state import ProjectRailState

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        router = self._router(tmp_path)
        config = _make_config(["alpha"], tmp_path)
        entries = {
            "alpha": _entry(
                "alpha", state=ProjectState.WORKING, items=[],
                rail_state=ProjectRailState.WORKING,
                rail_badge=None,
                rail_reason="worker active",
            ),
        }
        _seed_cache(monkeypatch, entries)

        rollups = router._maybe_cache_route_rollups(config, alerts=[])
        assert rollups is not None
        assert rollups["alpha"].state is ProjectRailState.WORKING
        # No alerts → actionable_key is None (matches direct path).
        assert rollups["alpha"].actionable_key is None


class TestPr2026ReviewBlocker2HeartbeatNoStale:
    """Blocker 2 (#2050 follow-up): heartbeat reads MUST NOT serve an
    indefinitely-stale snapshot. The refresher subscribes to
    ``heartbeat.tick`` audit events so every workspace sweep
    invalidates the affected project entries; the next refresh
    repopulates ``latest_heartbeat_by_session`` from the bulk pg query
    and the rail's fast-path serves the fresh row.

    These tests pin two invariants:
    1. ``heartbeat.tick`` is in ``refresher._INVALIDATING_EVENTS`` —
       the invalidation contract itself.
    2. After ``cache.invalidate(project)`` the rail no longer sees the
       previously-cached heartbeat (snapshot miss → direct facade
       picks up the fresh row).
    """

    def _router(self, tmp_path: Path):
        from pollypm.cockpit_rail import CockpitRouter

        config_path = tmp_path / "pollypm.toml"
        config_path.write_text(
            "[project]\n"
            'name = "PollyPM"\n'
            f'root_dir = "{tmp_path}"\n'
            'tmux_session = "pollypm"\n'
            f'base_dir = "{tmp_path / ".pollypm"}"\n'
        )
        return CockpitRouter(config_path)

    def test_heartbeat_tick_is_an_invalidating_event(self) -> None:
        """The invalidation contract that unblocks the cache fast-path."""

        from pollypm.state_cache.refresher import _INVALIDATING_EVENTS

        assert "heartbeat.tick" in _INVALIDATING_EVENTS

    def test_refresh_after_invalidate_replaces_stale_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Heartbeat tick → invalidate → refresh → fresh row served.

        Simulates the full production cascade end-to-end: the audit
        tail saw a ``heartbeat.tick`` event, the refresher called
        ``cache.invalidate(project)``, then drained the pending queue
        through ``cache.refresh`` (which now repopulates
        ``latest_heartbeat_by_session`` via ``latest_heartbeats_bulk``).
        The rail's next read sees the fresh row out of the snapshot —
        no indefinite stale window the way PR #2026 reverted around.
        """

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        router = self._router(tmp_path)

        stale = SimpleNamespace(created_at="2026-05-20T01:00:00Z", n="stale")
        entries = {
            "alpha": _entry(
                "alpha", state=ProjectState.WORKING, items=[],
                latest_heartbeat_by_session={"worker_alpha/1": stale},
            ),
        }
        cache = _seed_cache(monkeypatch, entries)

        # Refresher would call this on a ``heartbeat.tick`` event.
        cache.invalidate("alpha")

        fresh = SimpleNamespace(created_at="2026-05-20T03:00:00Z", n="fresh")
        # Re-point the refresh fn so the next refresh repopulates with
        # the fresh row (mirrors ``compute_entry_for_project`` calling
        # ``latest_heartbeats_bulk`` after the tick).
        cache._refresh_fn = lambda key: _entry(  # noqa: SLF001
            key, state=ProjectState.WORKING, items=[],
            latest_heartbeat_by_session={"worker_alpha/1": fresh},
        )
        # Drain the pending queue (the refresher worker would do this).
        for pending_key in cache.drain_pending():
            cache.refresh(pending_key)

        # Guard against any accidental direct-facade fall-through —
        # the snapshot MUST hold the fresh row now.
        def _direct(name, *, config=None):  # noqa: ANN001, ANN201
            raise AssertionError(
                "direct facade hit despite a populated snapshot",
            )

        monkeypatch.setattr(
            "pollypm.storage.pg_heartbeats.latest_heartbeat", _direct,
        )

        supervisor = SimpleNamespace(store=None, config=None)
        result = router._latest_heartbeat_cached(
            supervisor, "worker_alpha/1",
        )
        assert result is fresh

    def test_pg_failure_still_falls_back_to_supervisor_store(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Direct-facade safety net survives the fast-path restoration.

        PR #2026's blocker-2 fallback contract (pg unreachable →
        ``supervisor.store.latest_heartbeat``) MUST keep working when
        the cache is empty / missing the session.
        """

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        router = self._router(tmp_path)
        _seed_cache(monkeypatch, {})

        def _boom(name, *, config=None):  # noqa: ANN001, ANN201
            raise RuntimeError("pg pool unreachable")

        monkeypatch.setattr(
            "pollypm.storage.pg_heartbeats.latest_heartbeat", _boom,
        )

        fallback = SimpleNamespace(created_at="2026-05-20T02:00:00Z")

        class _Store:
            def latest_heartbeat(self, name):  # noqa: ANN001, ANN201
                return fallback

        supervisor = SimpleNamespace(store=_Store(), config=None)
        assert router._latest_heartbeat_cached(
            supervisor, "worker_beta/3",
        ) is fallback


class TestPr2026ReviewBlocker3WorkspaceRootFallthrough:
    """Blocker 3: workspace-root inbox messages (``scope IN ('', 'inbox')``)
    are not represented in any per-project cache entry, so the cache
    routes for awaits-user list + count MUST fall through to the
    direct path whenever the workspace-root inbox is non-empty.
    """

    def test_workspace_root_message_forces_list_fallthrough(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """A live workspace-root row → list cache declines, direct path runs.

        Pinned to the cached vs direct equality: both MUST return the
        same set of items (workspace-root included), proving the
        fall-through is what carried the workspace row.
        """

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha"], tmp_path)

        # Per-project items the cache CAN represent.
        per_project = [
            _inbox_item(project="alpha", source="task", ident="alpha/1"),
        ]
        # The workspace-root item with project="inbox" — the cache
        # CANNOT represent this (refresher filters by project_key only).
        workspace_root = _inbox_item(
            project="inbox", source="message", ident="ws-root-1",
        )
        # And the workspace-root probe MUST see it (simulate pg row).
        monkeypatch.setattr(
            cockpit_inbox,
            "_workspace_root_inbox_has_open",
            lambda cfg: True,
        )
        entries = {
            "alpha": _entry(
                "alpha", state=ProjectState.WAITING, items=per_project,
            ),
        }
        _seed_cache(monkeypatch, entries)

        # Direct sweep returns BOTH (per-project + workspace-root).
        direct_items = list(per_project) + [workspace_root]
        direct_called = {"n": 0}

        def _direct(_cfg: Any) -> list[Any]:
            direct_called["n"] += 1
            return list(direct_items)

        monkeypatch.setattr(
            cockpit_inbox, "_pm_inbox_awaits_user_list_uncached", _direct,
        )
        cockpit_inbox._AWAITS_USER_CACHE.clear()
        result = cockpit_inbox.pm_inbox_awaits_user_list(config)
        assert direct_called["n"] == 1
        # Workspace-root item is in the result (would have been
        # silently dropped if we had stayed on the fast-path).
        ids = {getattr(item, "message_id", None) or getattr(item, "task_id", None) for item in result}
        assert "ws-root-1" in ids
        assert "alpha/1" in ids

    def test_workspace_root_message_forces_count_fallthrough(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Same gate fires on the count helper — sums match direct len()."""

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha"], tmp_path)
        per_project = [
            _inbox_item(project="alpha", source="task", ident="alpha/1"),
        ]
        workspace_root = _inbox_item(
            project="inbox", source="message", ident="ws-root-2",
        )
        monkeypatch.setattr(
            cockpit_inbox,
            "_workspace_root_inbox_has_open",
            lambda cfg: True,
        )
        entries = {
            "alpha": _entry(
                "alpha", state=ProjectState.WAITING, items=per_project,
            ),
        }
        _seed_cache(monkeypatch, entries)

        direct_items = list(per_project) + [workspace_root]
        monkeypatch.setattr(
            cockpit_inbox,
            "_pm_inbox_awaits_user_list_uncached",
            lambda cfg: list(direct_items),
        )
        cockpit_inbox._AWAITS_USER_CACHE.clear()
        counted = cockpit_inbox._count_inbox_tasks_for_label(config)
        # Cache would have returned 1 (only alpha); fall-through gives 2.
        assert counted == 2

    def test_workspace_root_empty_keeps_cache_fast_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Empty workspace-root → cache fast-path stays available."""

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha"], tmp_path)
        per_project = [
            _inbox_item(project="alpha", source="task", ident="alpha/1"),
        ]
        monkeypatch.setattr(
            cockpit_inbox,
            "_workspace_root_inbox_has_open",
            lambda cfg: False,
        )
        entries = {
            "alpha": _entry(
                "alpha", state=ProjectState.WAITING, items=per_project,
            ),
        }
        _seed_cache(monkeypatch, entries)

        # Pin the sampler off so the divergence-comparator path
        # doesn't masquerade as a fall-through. Earlier tests in this
        # module may have swapped in a rate-1 counter that would
        # sample every call; restore a fresh default-rate counter.
        cockpit_inbox._AWAITS_USER_DIVERGENCE_COUNTER = DivergenceCounter()
        # Direct sweep MUST NOT be called.
        direct_called = {"n": 0}

        def _direct(_cfg: Any) -> list[Any]:
            direct_called["n"] += 1
            return list(per_project)

        monkeypatch.setattr(
            cockpit_inbox, "_pm_inbox_awaits_user_list_uncached", _direct,
        )
        cockpit_inbox._AWAITS_USER_CACHE.clear()
        result = cockpit_inbox.pm_inbox_awaits_user_list(config)
        assert direct_called["n"] == 0
        assert len(result) == 1


class TestPr2026V3WorkspaceRootProbeUnbounded:
    """PR #2026 v3 (Codex blocker 1): the workspace-root probe must NOT
    depend on row recency. The old implementation called
    ``open_messages(limit=50)`` and Python-scanned for ``scope IN ('',
    'inbox')``; with 50+ newer project-scoped rows + 1 older
    workspace-root row, the probe returned False and the cache silently
    dropped the workspace work.

    The fix adds a dedicated SQL existence query
    (``cockpit_pg_aggregates.has_workspace_root_open_messages``) with
    a workspace-root predicate + ``LIMIT 1`` — independent of how many
    newer non-workspace rows exist.

    This test calls the REAL ``_workspace_root_inbox_has_open`` (no
    monkeypatch on that function) against a fake pg_pool that simulates
    the bug scenario.
    """

    def test_workspace_root_row_under_50_newer_rows_is_still_seen(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Seed pg with 50 newer project-scoped rows + 1 older
        workspace-root row; the SQL existence query MUST return True.
        """

        config = _make_config(["alpha"], tmp_path)

        # Track which SQL was executed so the assertion proves the
        # workspace-root existence query (not the legacy newest-50
        # scan) is what gave the True answer.
        executed: list[tuple[str, tuple[Any, ...]]] = []

        class _FakeCursor:
            def __init__(self) -> None:
                self._result: list[tuple[Any, ...]] = []

            def __enter__(self) -> "_FakeCursor":
                return self

            def __exit__(self, *_a: Any) -> None:
                pass

            def execute(self, sql: str, params: Any) -> None:
                executed.append((sql, tuple(params)))
                # Workspace-root probe: predicate includes the
                # ``scope = '' OR scope IS NULL OR scope = 'inbox'``
                # branch + a ``LIMIT 1``. Return one row.
                if "scope = 'inbox'" in sql and "LIMIT 1" in sql:
                    self._result = [(1,)]
                    return
                # Anything else (e.g. the legacy newest-N scan) —
                # return 50 project-scoped rows so the OLD code
                # would have missed the workspace-root row entirely.
                self._result = [
                    (i, "alpha", "notify", "info", "user", "sender",
                     "open", "subj", "body", {}, [], "kind",
                     None, None, None)
                    for i in range(50)
                ]

            def fetchone(self) -> Any:
                return self._result[0] if self._result else None

            def fetchall(self) -> list[tuple[Any, ...]]:
                return list(self._result)

            @property
            def description(self) -> list[Any]:
                return [SimpleNamespace(name=n) for n in (
                    "id", "scope", "type", "tier", "recipient",
                    "sender", "state", "subject", "body",
                    "payload_json", "labels", "kind",
                    "created_at", "updated_at", "closed_at",
                )]

        class _FakeConn:
            def __enter__(self) -> "_FakeConn":
                return self

            def __exit__(self, *_a: Any) -> None:
                pass

            def cursor(self) -> _FakeCursor:
                return _FakeCursor()

        class _FakePool:
            def connection(self) -> _FakeConn:
                return _FakeConn()

        def _fake_get_ro_pool(_cfg: Any) -> _FakePool:
            return _FakePool()

        monkeypatch.setattr(
            "pollypm.storage.pg_pool.get_ro_pool", _fake_get_ro_pool,
        )

        # Call the REAL function (no monkeypatch on _workspace_root_inbox_has_open).
        assert cockpit_inbox._workspace_root_inbox_has_open(config) is True

        # Prove the SQL the function executed was the bounded-1
        # existence query, not the legacy newest-N scan.
        assert executed, "fake pg pool was never queried"
        executed_sqls = [sql for sql, _params in executed]
        assert any(
            "scope = 'inbox'" in sql and "LIMIT 1" in sql
            for sql in executed_sqls
        ), (
            f"workspace-root probe should issue a LIMIT-1 existence "
            f"query; saw {executed_sqls!r}"
        )

    def test_real_probe_drives_cache_fall_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """The probe's True answer MUST flip
        ``_maybe_cache_route_awaits_user`` to fall through to direct.
        """

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha"], tmp_path)

        class _FakeCursor:
            def __init__(self) -> None:
                self._result: list[tuple[Any, ...]] = []

            def __enter__(self) -> "_FakeCursor":
                return self

            def __exit__(self, *_a: Any) -> None:
                pass

            def execute(self, sql: str, params: Any) -> None:
                if "LIMIT 1" in sql:
                    self._result = [(1,)]
                else:
                    self._result = []

            def fetchone(self) -> Any:
                return self._result[0] if self._result else None

            def fetchall(self) -> list[tuple[Any, ...]]:
                return list(self._result)

            @property
            def description(self) -> list[Any]:
                return [SimpleNamespace(name="x")]

        class _FakeConn:
            def __enter__(self) -> "_FakeConn":
                return self

            def __exit__(self, *_a: Any) -> None:
                pass

            def cursor(self) -> _FakeCursor:
                return _FakeCursor()

        class _FakePool:
            def connection(self) -> _FakeConn:
                return _FakeConn()

        monkeypatch.setattr(
            "pollypm.storage.pg_pool.get_ro_pool", lambda _cfg: _FakePool(),
        )

        # Seed a populated cache for alpha. No monkeypatch on
        # _workspace_root_inbox_has_open — it executes the REAL pg
        # path against the fake pool above and returns True.
        entries = {
            "alpha": _entry(
                "alpha", state=ProjectState.IDLE, items=[],
            ),
        }
        _seed_cache(monkeypatch, entries)

        # The fast-path MUST decline so the direct sweep can carry
        # the workspace-root row.
        assert cockpit_inbox._maybe_cache_route_awaits_user(config) is None

    # ── PR #2026 v6 regression tests (Codex round-6 blocker) ──────────
    #
    # The v5 helper returns True on pool/query failure to keep the cache
    # authoritative-or-deferred. These tests pin that contract end-to-end:
    # (1) the helper itself returns True on each failure mode, and
    # (2) the failure flows through _maybe_cache_{route,count}_awaits_user
    # as a fall-through (cache declines).

    def test_pool_open_failure_returns_true_conservative(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """``has_workspace_root_open_messages`` must return True when the
        pg pool cannot be opened — conservative-on-failure contract.
        """

        from pollypm import cockpit_pg_aggregates

        config = _make_config(["alpha"], tmp_path)

        def _boom_ro(_cfg: Any) -> Any:
            raise RuntimeError("ro pool unavailable")

        def _boom_rw(_cfg: Any) -> Any:
            raise RuntimeError("rw pool unavailable")

        monkeypatch.setattr(
            "pollypm.storage.pg_pool.get_ro_pool", _boom_ro,
        )
        monkeypatch.setattr(
            "pollypm.storage.pg_pool.get_rw_pool", _boom_rw,
        )

        assert (
            cockpit_pg_aggregates.has_workspace_root_open_messages(config)
            is True
        )

    def test_query_failure_returns_true_conservative(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Pool open succeeds but ``cur.execute()`` raises — the helper
        must still return True (conservative).
        """

        from pollypm import cockpit_pg_aggregates

        config = _make_config(["alpha"], tmp_path)

        class _FakeCursor:
            def __enter__(self) -> "_FakeCursor":
                return self

            def __exit__(self, *_a: Any) -> None:
                pass

            def execute(self, _sql: str, _params: Any) -> None:
                raise RuntimeError("query exploded")

            def fetchone(self) -> Any:
                return None

        class _FakeConn:
            def __enter__(self) -> "_FakeConn":
                return self

            def __exit__(self, *_a: Any) -> None:
                pass

            def cursor(self) -> _FakeCursor:
                return _FakeCursor()

        class _FakePool:
            def connection(self) -> _FakeConn:
                return _FakeConn()

        monkeypatch.setattr(
            "pollypm.storage.pg_pool.get_ro_pool", lambda _cfg: _FakePool(),
        )

        assert (
            cockpit_pg_aggregates.has_workspace_root_open_messages(config)
            is True
        )

    def test_pool_failure_drives_cache_fall_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Pool failure → wrapper returns True → ``_maybe_cache_route_awaits_user``
        declines so the direct sweep carries any uncached awaits-user work.
        """

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha"], tmp_path)

        def _boom_ro(_cfg: Any) -> Any:
            raise RuntimeError("ro pool unavailable")

        def _boom_rw(_cfg: Any) -> Any:
            raise RuntimeError("rw pool unavailable")

        monkeypatch.setattr(
            "pollypm.storage.pg_pool.get_ro_pool", _boom_ro,
        )
        monkeypatch.setattr(
            "pollypm.storage.pg_pool.get_rw_pool", _boom_rw,
        )

        # Cache seeded with an uncached-style entry — the only way the
        # fast-path can decline is via the workspace-root guard, which
        # depends on the failing probe.
        entries = {
            "alpha": _entry(
                "alpha", state=ProjectState.IDLE, items=[],
            ),
        }
        _seed_cache(monkeypatch, entries)

        assert cockpit_inbox._maybe_cache_route_awaits_user(config) is None

    def test_query_failure_drives_count_fall_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """Query failure → wrapper returns True → ``_maybe_cache_count_awaits_user``
        declines so the rail badge falls back to the direct sweep.
        """

        monkeypatch.setenv("POLLYPM_STATE_CACHE", "1")
        config = _make_config(["alpha"], tmp_path)

        class _FakeCursor:
            def __enter__(self) -> "_FakeCursor":
                return self

            def __exit__(self, *_a: Any) -> None:
                pass

            def execute(self, _sql: str, _params: Any) -> None:
                raise RuntimeError("query exploded")

            def fetchone(self) -> Any:
                return None

        class _FakeConn:
            def __enter__(self) -> "_FakeConn":
                return self

            def __exit__(self, *_a: Any) -> None:
                pass

            def cursor(self) -> _FakeCursor:
                return _FakeCursor()

        class _FakePool:
            def connection(self) -> _FakeConn:
                return _FakeConn()

        monkeypatch.setattr(
            "pollypm.storage.pg_pool.get_ro_pool", lambda _cfg: _FakePool(),
        )

        entries = {
            "alpha": _entry(
                "alpha", state=ProjectState.IDLE, items=[],
            ),
        }
        _seed_cache(monkeypatch, entries)

        assert cockpit_inbox._maybe_cache_count_awaits_user(config) is None
