"""Move A PR 2 parity tests — cached vs direct paths agree.

Pins ``docs/design/move-a-state-cache.md`` §6.2 / §7 / §8.4:

* The two PR-2-routed call sites — ``pm_inbox_awaits_user_list`` and
  ``project_state_map_from_config`` — must return semantically equal
  results on the cached-fast-path branch and the direct-DB branch
  for any config the cache has fully populated.
* The 1-in-N divergence sampler logs a WARN when an injected
  mismatch lands; the WARN line is the surface PR 4's telemetry gate
  reads.
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
    project_key: str, *, state: ProjectState, items: list[Any],
) -> ProjectStateCacheEntry:
    return ProjectStateCacheEntry(
        project_key=project_key,
        project_path=Path(f"/tmp/{project_key}"),
        tracked=True,
        state=state,
        awaits_user_count=len(items),
        awaits_user_items=tuple(items),
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
        monkeypatch.delenv("POLLYPM_STATE_CACHE", raising=False)
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

        monkeypatch.delenv("POLLYPM_STATE_CACHE", raising=False)
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
    """The 1-in-N sampler logs a WARN line on mismatch."""

    def test_sampler_fires_on_nth_call(self) -> None:
        counter = DivergenceCounter(rate=3)
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
        cockpit_inbox._AWAITS_USER_DIVERGENCE_COUNTER = DivergenceCounter(rate=1)

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

        operator_view._STATE_MAP_DIVERGENCE_COUNTER = DivergenceCounter(rate=1)

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
        cockpit_inbox._AWAITS_USER_DIVERGENCE_COUNTER = DivergenceCounter(rate=1)
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
        monkeypatch.delenv("POLLYPM_STATE_CACHE", raising=False)
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
