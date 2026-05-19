"""Regression tests for config-threaded DSN resolution (#1819, #1754, #1796).

The pg pool's :func:`resolve_dsn` is the load-bearing function that
decides which DSN the process-wide pool opens against. Before the
#1819 fix:

* ``PgStore`` invoked ``get_rw_pool(None)`` / ``get_ro_pool(None)``,
  so a configured ``[storage] url`` was stored on ``self._url`` but
  silently ignored when opening pools.
* ``JobQueue`` and the heartbeat boot migration applier hit the
  pool with ``config=None``, falling back to the localhost default.
* ``[storage.pg] dsn`` (parsed into :class:`PgStorageSettings`) was
  documented in :class:`pollypm.models.PgStorageSettings` but never
  consulted by :func:`resolve_dsn` (the #1754 / #1796 follow-up).

These tests cover the pure-Python contract — they don't open a real
pg pool. The fixes:

1. :func:`resolve_dsn` reads ``config.storage.pg.dsn`` first, then
   falls back to ``config.storage.url``, then to ``DEFAULT_DSN``.
2. :class:`PgStore` threads its ``url`` through to the pool via a
   minimal config shim.
3. :func:`pollypm.jobs.cli.build_queue_for_config` passes the loaded
   ``PollyPMConfig`` into the ``JobQueue`` constructor.
"""

from __future__ import annotations

import importlib


def _reload_pool_module():
    import pollypm.storage.pg_pool as mod

    importlib.reload(mod)
    return mod


# --------------------------------------------------------------------- #
# resolve_dsn — reads ``[storage.pg].dsn`` (the #1754 / #1796 knob).
# --------------------------------------------------------------------- #


def test_resolve_dsn_reads_storage_pg_dsn(monkeypatch):
    """``[storage.pg] dsn`` is honoured when ``[storage] url`` is empty."""
    monkeypatch.delenv("POLLYPM_PG_DSN", raising=False)
    mod = _reload_pool_module()

    class _PgSection:
        dsn = "postgresql://configured-host:5433/configured_db"

    class _Storage:
        url = ""
        pg = _PgSection()

    class _Config:
        storage = _Storage()

    assert (
        mod.resolve_dsn(_Config())
        == "postgresql://configured-host:5433/configured_db"
    )


def test_resolve_dsn_pg_section_wins_over_storage_url(monkeypatch):
    """``[storage.pg] dsn`` takes priority over the legacy shared URL."""
    monkeypatch.delenv("POLLYPM_PG_DSN", raising=False)
    mod = _reload_pool_module()

    class _PgSection:
        dsn = "postgresql://pg-section-host:5432/db"

    class _Storage:
        url = "postgresql://legacy-url-host:5432/db"
        pg = _PgSection()

    class _Config:
        storage = _Storage()

    # The pg subsection is the dedicated knob — it wins so an operator
    # who set both can rely on the more specific one.
    assert mod.resolve_dsn(_Config()) == "postgresql://pg-section-host:5432/db"


def test_resolve_dsn_falls_back_to_storage_url_when_pg_dsn_empty(monkeypatch):
    """Empty ``[storage.pg] dsn`` does not block the legacy shared URL."""
    monkeypatch.delenv("POLLYPM_PG_DSN", raising=False)
    mod = _reload_pool_module()

    class _PgSection:
        dsn = ""

    class _Storage:
        url = "postgresql://legacy-host:5432/db"
        pg = _PgSection()

    class _Config:
        storage = _Storage()

    assert mod.resolve_dsn(_Config()) == "postgresql://legacy-host:5432/db"


def test_resolve_dsn_env_still_wins_over_pg_section(monkeypatch):
    """``POLLYPM_PG_DSN`` env override beats every config-level knob."""
    monkeypatch.setenv("POLLYPM_PG_DSN", "postgresql://env-host:5432/envdb")
    mod = _reload_pool_module()

    class _PgSection:
        dsn = "postgresql://pg-section-host:5432/db"

    class _Storage:
        url = "postgresql://legacy-url-host:5432/db"
        pg = _PgSection()

    class _Config:
        storage = _Storage()

    assert mod.resolve_dsn(_Config()) == "postgresql://env-host:5432/envdb"


def test_resolve_dsn_ignores_sqlite_pg_section(monkeypatch):
    """A non-pg DSN in ``[storage.pg] dsn`` is filtered out, not crashed."""
    monkeypatch.delenv("POLLYPM_PG_DSN", raising=False)
    mod = _reload_pool_module()

    class _PgSection:
        # Operator misconfiguration: sqlite path in the pg dsn slot.
        dsn = "sqlite:///tmp/state.db"

    class _Storage:
        url = "postgresql://valid-pg-host:5432/db"
        pg = _PgSection()

    class _Config:
        storage = _Storage()

    # The sqlite-shaped value is dropped; the next-priority pg URL wins.
    assert mod.resolve_dsn(_Config()) == "postgresql://valid-pg-host:5432/db"


# --------------------------------------------------------------------- #
# PgStore threads its url into the pool via the shim config.
# --------------------------------------------------------------------- #


def test_pg_store_threads_url_into_get_rw_pool(monkeypatch):
    """``PgStore._rw_pool`` passes a config-shaped object carrying ``self._url``.

    The shim's ``storage.pg.dsn`` matches the constructor URL so the
    pool resolves to the configured DSN rather than localhost. The
    test stubs out ``apply_migrations`` and ``get_rw_pool`` so no
    real pg connection is made.
    """
    monkeypatch.delenv("POLLYPM_PG_DSN", raising=False)

    captured: dict = {"configs": []}

    def _fake_get_rw_pool(config):
        captured["configs"].append(config)
        return object()  # opaque pool; PgStore never uses it directly here

    def _fake_apply_migrations(pool):
        captured["migrations_applied"] = True

    monkeypatch.setattr(
        "pollypm.storage.pg_pool.get_rw_pool", _fake_get_rw_pool
    )
    monkeypatch.setattr(
        "pollypm.storage.pg_migrations.apply_migrations",
        _fake_apply_migrations,
    )

    from pollypm.store.backends.pg_store import PgStore

    PgStore(url="postgresql://configured-host:5432/db")

    assert captured["migrations_applied"] is True
    assert captured["configs"], (
        "PgStore did not call get_rw_pool — schema bootstrap path "
        "diverged from expectations"
    )
    # The schema bootstrap call must have threaded a config carrying
    # the constructor URL. Re-resolve through the real resolver to
    # prove the threaded shim is wired correctly.
    threaded_config = captured["configs"][0]
    assert threaded_config is not None, (
        "PgStore opened the pool with config=None — the #1819 regression "
        "where the configured DSN was silently dropped"
    )
    # Confirm both fields are populated so resolve_dsn honours either.
    assert (
        threaded_config.storage.pg.dsn
        == "postgresql://configured-host:5432/db"
    )
    assert (
        threaded_config.storage.url
        == "postgresql://configured-host:5432/db"
    )

    # And the resolver actually picks the threaded value:
    mod = _reload_pool_module()
    assert (
        mod.resolve_dsn(threaded_config)
        == "postgresql://configured-host:5432/db"
    )


def test_pg_store_skips_shim_for_sqlite_url(monkeypatch):
    """A sqlite URL in PgStore constructor must not produce a pg shim.

    PgStore against a sqlite URL is a misconfiguration; the registry
    routes sqlite to ``SQLAlchemyStore``. If a test or third-party
    caller does instantiate ``PgStore(url="sqlite:///...")``, the
    shim must fall through to ``config=None`` so the env / default
    path is honoured rather than handing the pool a sqlite DSN.
    """
    monkeypatch.delenv("POLLYPM_PG_DSN", raising=False)

    captured: dict = {"configs": []}

    def _fake_get_rw_pool(config):
        captured["configs"].append(config)
        return object()

    def _fake_apply_migrations(pool):
        return None

    monkeypatch.setattr(
        "pollypm.storage.pg_pool.get_rw_pool", _fake_get_rw_pool
    )
    monkeypatch.setattr(
        "pollypm.storage.pg_migrations.apply_migrations",
        _fake_apply_migrations,
    )

    from pollypm.store.backends.pg_store import PgStore

    PgStore(url="sqlite:///tmp/state.db")

    assert captured["configs"], "PgStore did not call get_rw_pool"
    assert captured["configs"][0] is None, (
        "PgStore should fall through to config=None for non-pg URLs"
    )


# --------------------------------------------------------------------- #
# build_queue_for_config — threads config through to JobQueue.
# --------------------------------------------------------------------- #


def test_build_queue_for_config_threads_config_into_queue(tmp_path, monkeypatch):
    """``build_queue_for_config`` must hand the loaded config to JobQueue.

    The #1819 regression was that the CLI loaded the config, then
    threw it away and built ``JobQueue(db_path=db_path)`` — silently
    using the localhost default DSN regardless of the operator's
    ``[storage.pg] dsn``.
    """
    monkeypatch.delenv("POLLYPM_PG_DSN", raising=False)

    # Construct a real config file on disk so build_queue_for_config's
    # ``load_config`` path runs untouched.
    config_text = (
        '[project]\n'
        'name = "test-1819"\n'
        'state_db = "state.db"\n'
        '[storage]\n'
        'backend = "postgres"\n'
        '[storage.pg]\n'
        'dsn = "postgresql://configured-host:5432/configured_db"\n'
    )
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(config_text, encoding="utf-8")

    captured: dict = {"queue_kwargs": []}

    # Patch ``JobQueue`` at the cli import site to capture the kwargs
    # without opening a real pg pool.
    class _StubQueue:
        def __init__(self, **kwargs):
            captured["queue_kwargs"].append(kwargs)

    import pollypm.jobs.cli as cli_mod

    monkeypatch.setattr(cli_mod, "JobQueue", _StubQueue)

    cli_mod.build_queue_for_config(config_path)

    assert captured["queue_kwargs"], "JobQueue was never constructed"
    kwargs = captured["queue_kwargs"][0]
    assert "config" in kwargs and kwargs["config"] is not None, (
        "build_queue_for_config did not pass config — the #1819 "
        "regression where the loaded config was discarded"
    )
    cfg = kwargs["config"]
    assert (
        cfg.storage.pg.dsn
        == "postgresql://configured-host:5432/configured_db"
    )

    # And the pool resolver picks up the threaded value end-to-end.
    mod = _reload_pool_module()
    assert (
        mod.resolve_dsn(cfg)
        == "postgresql://configured-host:5432/configured_db"
    )


# --------------------------------------------------------------------- #
# HeartbeatRail.from_config threads config into the boot migration applier.
# --------------------------------------------------------------------- #


def test_heartbeat_rail_from_config_threads_config_to_get_rw_pool(
    tmp_path, monkeypatch
):
    """The boot migration applier must use the loaded config, not None.

    Before #1819, ``HeartbeatRail.from_plugin_host`` called
    ``apply_migrations(get_rw_pool())`` with no config, so a
    configured ``[storage.pg] dsn`` was silently ignored and the
    migration ran against the localhost default. The fix threads
    ``config`` through to both the pool resolution and the JobQueue
    constructor.
    """
    monkeypatch.delenv("POLLYPM_PG_DSN", raising=False)

    config_text = (
        '[project]\n'
        'name = "test-1819-boot"\n'
        'state_db = "state.db"\n'
        '[storage]\n'
        'backend = "postgres"\n'
        '[storage.pg]\n'
        'dsn = "postgresql://boot-configured-host:5432/db"\n'
    )
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(config_text, encoding="utf-8")

    captured: dict = {
        "pool_configs": [],
        "queue_kwargs": [],
    }

    def _fake_get_rw_pool(config=None):
        captured["pool_configs"].append(config)
        return object()

    def _fake_apply_migrations(pool):
        return None

    monkeypatch.setattr(
        "pollypm.storage.pg_pool.get_rw_pool", _fake_get_rw_pool
    )
    monkeypatch.setattr(
        "pollypm.storage.pg_migrations.apply_migrations",
        _fake_apply_migrations,
    )

    import pollypm.heartbeat.boot as boot_mod

    class _StubQueue:
        def __init__(self, **kwargs):
            captured["queue_kwargs"].append(kwargs)

    class _StubPool:
        def __init__(self, *args, **kwargs):
            self.is_running = False

    class _StubHeartbeat:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(boot_mod, "JobQueue", _StubQueue)
    monkeypatch.setattr(boot_mod, "JobWorkerPool", _StubPool)

    class _StubPluginHost:
        def build_roster(self):
            return object()

        def job_handler_registry(self):
            return object()

        def initialize_plugins(self, **kwargs):
            return None

    # Avoid touching the real Heartbeat class — patch the import name
    # used inside ``from_plugin_host``.
    import pollypm.heartbeat as hb_pkg
    monkeypatch.setattr(hb_pkg, "Heartbeat", _StubHeartbeat)

    boot_mod.HeartbeatRail.from_config(config_path, _StubPluginHost())

    assert captured["pool_configs"], "get_rw_pool was never called"
    threaded_config = captured["pool_configs"][0]
    assert threaded_config is not None, (
        "HeartbeatRail.from_plugin_host opened the migration pool with "
        "config=None — the #1819 regression"
    )
    assert (
        threaded_config.storage.pg.dsn
        == "postgresql://boot-configured-host:5432/db"
    )

    assert captured["queue_kwargs"], "JobQueue was never constructed"
    queue_kwargs = captured["queue_kwargs"][0]
    assert queue_kwargs.get("config") is not None, (
        "JobQueue did not receive the threaded config — the #1819 "
        "regression where the queue silently used localhost"
    )
