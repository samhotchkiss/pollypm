"""Tests for the JobHandlerRegistry + plugin JobHandlerAPI surface."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from pollypm.jobs import (
    HandlerSpec,
    JobHandlerRegistry,
    JobQueue,
    JobStatus,
    JobWorkerPool,
    exponential_backoff,
)
from pollypm.plugin_api.v1 import JobHandlerAPI, PollyPMPlugin


# ---------------------------------------------------------------------------
# Registry basics
# ---------------------------------------------------------------------------


class TestJobHandlerRegistry:
    def test_register_and_lookup(self) -> None:
        registry = JobHandlerRegistry()

        def handler(payload: dict) -> None:
            pass

        is_new = registry.register(name="sweep", handler=handler, plugin_name="inbox")
        assert is_new is True
        spec = registry.get("sweep")
        assert spec is not None
        assert spec.name == "sweep"
        assert spec.handler is handler
        assert spec.max_attempts == 3
        assert spec.timeout_seconds == 30.0

    def test_register_respects_custom_attempts_and_timeout(self) -> None:
        registry = JobHandlerRegistry()

        def handler(payload: dict) -> None:
            pass

        registry.register(
            name="slow",
            handler=handler,
            plugin_name="p",
            max_attempts=10,
            timeout_seconds=60.0,
        )
        spec = registry.get("slow")
        assert spec is not None
        assert spec.max_attempts == 10
        assert spec.timeout_seconds == 60.0

    def test_missing_handler_returns_none(self) -> None:
        registry = JobHandlerRegistry()
        assert registry.get("nope") is None

    def test_collision_logs_warning_and_most_recent_wins(self, caplog) -> None:
        registry = JobHandlerRegistry()

        def handler_a(payload: dict) -> str:
            return "a"

        def handler_b(payload: dict) -> str:
            return "b"

        registry.register(name="sweep", handler=handler_a, plugin_name="plugin_a")
        with caplog.at_level(logging.WARNING, logger="pollypm.jobs.registry"):
            is_new = registry.register(
                name="sweep", handler=handler_b, plugin_name="plugin_b"
            )

        # Still there, but not new.
        assert is_new is False
        spec = registry.get("sweep")
        assert spec is not None
        assert spec.handler is handler_b  # most recent wins
        assert registry.source_of("sweep") == "plugin_b"

        # Warning should mention both plugins.
        assert any(
            "plugin_a" in rec.message and "plugin_b" in rec.message
            for rec in caplog.records
        )

    def test_same_plugin_reregister_is_silent(self, caplog) -> None:
        registry = JobHandlerRegistry()

        def h(payload: dict) -> None:
            pass

        registry.register(name="sweep", handler=h, plugin_name="p")
        with caplog.at_level(logging.WARNING, logger="pollypm.jobs.registry"):
            registry.register(name="sweep", handler=h, plugin_name="p")
        # No warning should have been emitted for same-plugin re-registration.
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []

    def test_register_rejects_invalid_inputs(self) -> None:
        registry = JobHandlerRegistry()
        with pytest.raises(ValueError):
            registry.register(name="", handler=lambda p: None, plugin_name="p")
        with pytest.raises(TypeError):
            registry.register(name="h", handler="not callable", plugin_name="p")  # type: ignore
        with pytest.raises(ValueError):
            registry.register(name="h", handler=lambda p: None, max_attempts=0, plugin_name="p")
        with pytest.raises(ValueError):
            registry.register(name="h", handler=lambda p: None, timeout_seconds=0, plugin_name="p")

    def test_unregister(self) -> None:
        registry = JobHandlerRegistry()
        registry.register(name="h", handler=lambda p: None, plugin_name="p")
        assert "h" in registry
        registry.unregister("h")
        assert "h" not in registry
        assert registry.get("h") is None

    def test_names_and_snapshot(self) -> None:
        registry = JobHandlerRegistry()
        registry.register(name="a", handler=lambda p: None, plugin_name="p")
        registry.register(name="b", handler=lambda p: None, plugin_name="p")
        assert sorted(registry.names()) == ["a", "b"]
        snap = registry.snapshot()
        assert set(snap.keys()) == {"a", "b"}
        # Snapshot is independent.
        snap.clear()
        assert len(registry) == 2


# ---------------------------------------------------------------------------
# JobHandlerAPI (plugin-facing)
# ---------------------------------------------------------------------------


class TestJobHandlerAPI:
    def test_register_forwards_to_registry(self) -> None:
        registry = JobHandlerRegistry()
        api = JobHandlerAPI(registry, plugin_name="inbox")

        def handler(payload: dict) -> None:
            pass

        assert (
            api.register_handler("sweep", handler, max_attempts=5, timeout_seconds=10.0)
            is True
        )
        spec = registry.get("sweep")
        assert spec is not None
        assert spec.max_attempts == 5
        assert spec.timeout_seconds == 10.0

    def test_second_registration_returns_false(self) -> None:
        registry = JobHandlerRegistry()
        api = JobHandlerAPI(registry, plugin_name="inbox")
        api.register_handler("sweep", lambda p: None)
        # Same plugin re-registering: returns False (already present).
        assert api.register_handler("sweep", lambda p: None) is False


# ---------------------------------------------------------------------------
# Plugin host integration
# ---------------------------------------------------------------------------


def test_plugin_host_invokes_register_handlers(tmp_path: Path) -> None:
    from pollypm.plugin_host import ExtensionHost

    def register_handlers(api: JobHandlerAPI) -> None:
        api.register_handler(
            "inbox.sweep", lambda payload: None, max_attempts=2, timeout_seconds=5.0
        )
        api.register_handler("boot", lambda payload: None)

    plugin = PollyPMPlugin(name="p", register_handlers=register_handlers)
    host = ExtensionHost(tmp_path)
    host._plugins = {"p": plugin}

    registry = host.job_handler_registry()
    assert set(registry.names()) == {"inbox.sweep", "boot"}
    spec = registry.get("inbox.sweep")
    assert spec is not None
    assert spec.max_attempts == 2


def test_plugin_host_job_registry_is_singleton(tmp_path: Path) -> None:
    from pollypm.plugin_host import ExtensionHost

    host = ExtensionHost(tmp_path)
    host._plugins = {}
    reg1 = host.job_handler_registry()
    reg2 = host.job_handler_registry()
    assert reg1 is reg2


def test_plugin_host_logs_cross_plugin_collision(tmp_path: Path, caplog) -> None:
    from pollypm.plugin_host import ExtensionHost

    def a(api: JobHandlerAPI) -> None:
        api.register_handler("sweep", lambda p: "a")

    def b(api: JobHandlerAPI) -> None:
        api.register_handler("sweep", lambda p: "b")

    host = ExtensionHost(tmp_path)
    host._plugins = {
        "plugin_a": PollyPMPlugin(name="plugin_a", register_handlers=a),
        "plugin_b": PollyPMPlugin(name="plugin_b", register_handlers=b),
    }

    with caplog.at_level(logging.WARNING, logger="pollypm.jobs.registry"):
        registry = host.job_handler_registry()

    spec = registry.get("sweep")
    assert spec is not None
    # Most recent wins — plugin_b registered last.
    assert registry.source_of("sweep") == "plugin_b"
    assert any(
        "plugin_a" in rec.message and "plugin_b" in rec.message
        for rec in caplog.records
    )


def test_plugin_host_captures_hook_failures(tmp_path: Path) -> None:
    from pollypm.plugin_host import ExtensionHost

    def broken(api: JobHandlerAPI) -> None:
        raise RuntimeError("boom")

    host = ExtensionHost(tmp_path)
    host._plugins = {"bad": PollyPMPlugin(name="bad", register_handlers=broken)}
    # Must not raise.
    registry = host.job_handler_registry()
    assert len(registry) == 0
    assert any("bad" in err and "boom" in err for err in host.errors)


# ---------------------------------------------------------------------------
# End-to-end: registry + queue + worker pool
# ---------------------------------------------------------------------------


def _wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_registered_handler_runs_when_enqueued(tmp_path: Path) -> None:
    registry = JobHandlerRegistry()
    seen: list[dict] = []

    registry.register(
        name="run",
        handler=lambda p: seen.append(p),
        plugin_name="p",
        timeout_seconds=5.0,
    )

    q = JobQueue(
        db_path=tmp_path / "q.db",
        retry_policy=exponential_backoff(
            base_seconds=0.01, factor=1.0, max_seconds=0.01, jitter=0
        ),
    )
    pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)
    pool.start(concurrency=1)
    try:
        q.enqueue("run", {"hi": "there"})
        assert _wait_until(lambda: q.stats().done == 1)
    finally:
        pool.stop(timeout=2)

    assert seen == [{"hi": "there"}]


def test_unknown_handler_fails_with_clear_error(tmp_path: Path) -> None:
    registry = JobHandlerRegistry()  # empty
    q = JobQueue(
        db_path=tmp_path / "q.db",
        retry_policy=exponential_backoff(
            base_seconds=0.01, factor=1.0, max_seconds=0.01, jitter=0
        ),
    )
    pool = JobWorkerPool(q, registry=registry, poll_interval=0.01)
    pool.start(concurrency=1)
    try:
        jid = q.enqueue("does.not.exist")
        assert _wait_until(lambda: q.stats().failed == 1)
    finally:
        pool.stop(timeout=2)

    stored = q.get(jid)
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    last_error = q.get_last_error(jid) or ""
    assert "does.not.exist" in last_error
    assert "No handler" in last_error
