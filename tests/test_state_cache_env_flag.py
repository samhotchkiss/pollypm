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


def test_flag_default_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    assert is_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_values_enable_flag(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv(ENV_FLAG, value)
    assert is_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_falsy_values_disable_flag(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv(ENV_FLAG, value)
    assert is_enabled() is False


def test_get_cache_returns_shim_when_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
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
    monkeypatch.delenv(ENV_FLAG, raising=False)
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
