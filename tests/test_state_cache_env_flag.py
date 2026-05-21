"""Tests for the :envvar:`POLLYPM_STATE_CACHE` env flag.

PR 1 ships the cache OFF by default. When off, :func:`get_cache`
returns a shim whose ``snapshot()`` is ``{}`` and ``get()`` returns
``None`` — so call sites that opt in later (PR 2+) can hold a
``StateCacheLike`` reference unconditionally without checking the flag.

Importing :mod:`pollypm.state_cache` MUST be side-effect-free in
either mode; the refresher only starts on the first :func:`get_cache`
call when the flag is on.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

import pollypm.state_cache as state_cache_module
from pollypm.state_cache import (
    ENV_FLAG,
    ProjectStateCache,
    get_cache,
    get_refresher,
    is_enabled,
    reset_for_test,
)


@pytest.fixture(autouse=True)
def _isolate_singletons():
    """Drop the module singletons before and after every test."""

    reset_for_test()
    yield
    reset_for_test()


def test_flag_default_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Move A PR 4 (#1664, design §6.4): default is now ON.

    With no env var set, the cache is enabled. The env var stays as a
    kill-switch — :func:`is_enabled` returns False only when it's
    explicitly disabled (``0`` / ``false`` / ``no`` / ``off``).
    """
    monkeypatch.delenv(ENV_FLAG, raising=False)
    assert is_enabled() is True


@pytest.mark.parametrize(
    "value", ["1", "true", "TRUE", "yes", "on", "", "  ", "garbage"]
)
def test_non_falsy_values_keep_cache_enabled(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    """PR 4: only the explicit kill-switch values disable.

    Empty string + whitespace + unknown values all leave the cache
    enabled because the default is now ON.
    """
    monkeypatch.setenv(ENV_FLAG, value)
    assert is_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
def test_kill_switch_values_disable_flag(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    """PR 4: ``POLLYPM_STATE_CACHE=0`` (and siblings) still disables.

    Kept for one release per design §6.4 so an emergency rollback is
    a process restart with the env var set.
    """
    monkeypatch.setenv(ENV_FLAG, value)
    assert is_enabled() is False


def test_get_cache_returns_shim_when_killswitch_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``POLLYPM_STATE_CACHE=0`` the shim cache is returned."""
    monkeypatch.setenv(ENV_FLAG, "0")
    cache = get_cache()
    assert cache.get("alpha") is None
    assert cache.snapshot() == {}
    assert cache.version("alpha") == 0
    assert cache.global_version() == 0
    # Invalidate must be a no-op (no raise).
    cache.invalidate("alpha")
    cache.invalidate(None)
    # No refresher gets created in shim mode.
    assert get_refresher() is None


def test_get_cache_returns_real_cache_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR 4 default: with no env var set, the real cache + refresher come up."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    cache = get_cache()
    try:
        assert isinstance(cache, ProjectStateCache)
        # Refresher came up alongside the cache.
        refresher = get_refresher()
        assert refresher is not None
        assert refresher.running
    finally:
        reset_for_test()


def test_get_cache_returns_real_cache_when_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    cache = get_cache()
    try:
        assert isinstance(cache, ProjectStateCache)
        # Refresher came up alongside the cache.
        refresher = get_refresher()
        assert refresher is not None
        assert refresher.running
    finally:
        reset_for_test()


def test_get_cache_is_idempotent_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    a = get_cache()
    b = get_cache()
    assert a is b


def test_shim_singleton_is_stable_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shim is shared when the kill-switch is set."""
    monkeypatch.setenv(ENV_FLAG, "0")
    a = get_cache()
    b = get_cache()
    assert a is b


def test_module_import_is_side_effect_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the package must NOT start the refresher.

    Real cache + refresher only spin up on the first :func:`get_cache`
    call. This protects test suites that import the module
    transitively but never opt in.
    """

    monkeypatch.setenv(ENV_FLAG, "1")
    reset_for_test()
    importlib.reload(state_cache_module)
    # No refresher created by import alone.
    assert state_cache_module.get_refresher() is None
    # Cleanup — re-acquire the original module's reset for the
    # autouse fixture.
    state_cache_module.reset_for_test()


def test_reset_for_test_stops_refresher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    get_cache()
    refresher = get_refresher()
    assert refresher is not None and refresher.running
    reset_for_test()
    assert not refresher.running
    assert get_refresher() is None


def test_singleton_wires_project_keys_provider_for_initial_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex blocker (#2016 review): with ``POLLYPM_STATE_CACHE=1`` the
    production singleton MUST construct the refresher with a
    config-backed ``project_keys`` provider so the startup
    :meth:`StateCacheRefresher._initial_full_refresh` actually enqueues
    refreshes for every configured project. Without the provider, the
    cache stays empty until per-project audit events arrive, which is a
    no-op for projects that never emit an invalidating event.
    """

    monkeypatch.setenv(ENV_FLAG, "1")

    # Stub ``load_config`` so we don't read the user's real config.
    # Mirrors the helper-only contract that the provider closure uses
    # (``getattr(config, "projects", {})``).
    fake_config = SimpleNamespace(
        projects={
            "alpha": SimpleNamespace(path="/tmp/alpha", tracked=True),
            "beta": SimpleNamespace(path="/tmp/beta", tracked=True),
            "gamma": SimpleNamespace(path="/tmp/gamma", tracked=True),
        }
    )

    def _fake_load_config(_path=None):
        return fake_config

    import pollypm.config as _config_module
    monkeypatch.setattr(_config_module, "load_config", _fake_load_config)

    # Stub the per-project compute so we don't pull cockpit_inbox /
    # dashboard imports into this leaf unit test — we just need to
    # observe that the refresher enqueues entries for every configured
    # project key.
    from pollypm.state_cache.entry import empty_entry
    monkeypatch.setattr(
        "pollypm.state_cache.compute_entry_for_project",
        lambda project_key, config: empty_entry(project_key),
    )
    # ``build_refresh_fn`` is imported into ``__init__`` at module
    # load — replace it on the module namespace so the singleton wiring
    # picks up the stub.
    monkeypatch.setattr(
        "pollypm.state_cache.build_refresh_fn",
        lambda provider: (lambda key: empty_entry(key)),
    )

    cache = get_cache()
    refresher = get_refresher()
    try:
        assert refresher is not None
        # The provider must be wired on the refresher instance — this is
        # the property whose absence was the Codex blocker.
        assert refresher._project_keys is not None  # noqa: SLF001
        assert sorted(refresher._project_keys()) == [  # noqa: SLF001
            "alpha", "beta", "gamma",
        ]
        # And the startup full-refresh must have populated the cache
        # with entries for every configured project (no audit events
        # required).
        snapshot = cache.snapshot()
        assert set(snapshot.keys()) == {"alpha", "beta", "gamma"}
    finally:
        reset_for_test()


# ── Move A PR 4 — divergence sampler is no-op when cache authoritative ──


class TestPr4DivergenceSamplerNoop:
    """PR 4 (#1664, design §6.4): sampler is silent when flag default applies.

    The parity-debugging window is over — the cache is authoritative
    when the kill-switch is not set. The sampler MUST NOT pay the
    cost of running the direct path alongside the cache.
    """

    def test_sampler_is_silent_by_default(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No env var → cache is authoritative → sampler returns False."""
        from pollypm.state_cache.divergence import DivergenceCounter

        monkeypatch.delenv(ENV_FLAG, raising=False)
        counter = DivergenceCounter(rate=1)
        # Even at rate=1, the sampler stays silent because the cache
        # is the default-on authoritative source.
        for _ in range(50):
            assert counter.should_sample() is False

    def test_sampler_is_silent_when_flag_explicitly_on(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``POLLYPM_STATE_CACHE=1`` → cache authoritative → no samples."""
        from pollypm.state_cache.divergence import DivergenceCounter

        monkeypatch.setenv(ENV_FLAG, "1")
        counter = DivergenceCounter(rate=1)
        for _ in range(50):
            assert counter.should_sample() is False

    def test_sampler_runs_when_killswitch_set(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Kill-switch set → operator debugging cache vs direct.

        The sampler still runs at its 1-in-N cadence so an operator
        who flipped the kill-switch can see parity warnings if their
        suspicion was right. (In practice this also means the
        kill-switch's "fall back to direct" branch keeps emitting the
        same divergence telemetry shape PR 2 introduced.)
        """
        from pollypm.state_cache.divergence import DivergenceCounter

        monkeypatch.setenv(ENV_FLAG, "0")
        counter = DivergenceCounter(rate=3)
        # 1, 2 → no; 3 → yes; 4, 5 → no; 6 → yes.
        results = [counter.should_sample() for _ in range(6)]
        assert results == [False, False, True, False, False, True]

    def test_always_override_ignores_killswitch(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``always=True`` is the test-only escape hatch for parity tests."""
        from pollypm.state_cache.divergence import DivergenceCounter

        # Even with the cache authoritative (default), always=True
        # makes the sampler fire at every Nth call.
        monkeypatch.delenv(ENV_FLAG, raising=False)
        counter = DivergenceCounter(rate=2, always=True)
        results = [counter.should_sample() for _ in range(4)]
        assert results == [False, True, False, True]


def test_singleton_provider_degrades_gracefully_on_config_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider must catch ``load_config`` failures and return ``[]``
    — the cache stays empty instead of taking down the cockpit.
    """

    monkeypatch.setenv(ENV_FLAG, "1")

    def _broken_load_config(_path=None):
        raise RuntimeError("config file missing")

    import pollypm.config as _config_module
    monkeypatch.setattr(_config_module, "load_config", _broken_load_config)

    from pollypm.state_cache.entry import empty_entry
    monkeypatch.setattr(
        "pollypm.state_cache.build_refresh_fn",
        lambda provider: (lambda key: empty_entry(key)),
    )

    cache = get_cache()
    refresher = get_refresher()
    try:
        assert refresher is not None
        # Provider is wired, but it absorbs the load_config exception
        # and yields the empty list.
        assert refresher._project_keys is not None  # noqa: SLF001
        assert refresher._project_keys() == []  # noqa: SLF001
        assert cache.snapshot() == {}
    finally:
        reset_for_test()
