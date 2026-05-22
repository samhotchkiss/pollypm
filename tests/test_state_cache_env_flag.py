"""Tests for the :envvar:`POLLYPM_STATE_CACHE` env flag.

Post-PR4: cache defaults ON. These tests pin the kill-switch
(``POLLYPM_STATE_CACHE=0``) fall-through path and the env-flag parser.
When the kill-switch is set, :func:`get_cache` returns a shim whose
``snapshot()`` is ``{}`` and ``get()`` returns ``None`` — so call sites
can hold a ``StateCacheLike`` reference unconditionally without checking
the flag.

Importing :mod:`pollypm.state_cache` MUST be side-effect-free in
either mode; the refresher only starts on the first :func:`get_cache`
call when the cache is active.
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


# ── PR #2029 — divergence sampler is deterministic-off in production ──


class TestDivergenceSamplerNoop:
    """PR #2029: the in-process sampler is deterministic-off.

    The legacy "sample when kill-switch is set" branch was
    unreachable (routed call sites guard on ``is_enabled()`` and
    return early before touching the sampler), so it was removed.
    The class is preserved so existing route-site sentinels keep
    working; ``always=True`` remains as the test escape hatch.
    """

    def test_sampler_is_silent_by_default(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No env var → cache authoritative → sampler returns False."""
        from pollypm.state_cache.divergence import DivergenceCounter

        monkeypatch.delenv(ENV_FLAG, raising=False)
        counter = DivergenceCounter(rate=1)
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

    def test_sampler_is_silent_when_killswitch_set(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Kill-switch set → routes short-circuit before the sampler.

        PR #2029 removed the kill-switch sampling branch entirely.
        Routed call sites return early via ``is_enabled()`` when the
        kill-switch is set, so the sampler is never reached in
        production. Verify it stays silent here too.
        """
        from pollypm.state_cache.divergence import DivergenceCounter

        monkeypatch.setenv(ENV_FLAG, "0")
        counter = DivergenceCounter(rate=3)
        for _ in range(10):
            assert counter.should_sample() is False

    def test_always_override_drives_legacy_cadence(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``always=True`` is the test-only escape hatch for parity tests."""
        from pollypm.state_cache.divergence import DivergenceCounter

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
