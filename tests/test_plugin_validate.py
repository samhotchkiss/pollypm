"""Unit tests for plugin validation harness."""

from pathlib import Path

from pollypm.plugin_api.v1 import PollyPMPlugin
from pollypm.plugin_validate import (
    ValidationReport,
    ValidationResult,
    validate_plugin,
    _validate_provider_factory,
    _validate_runtime_factory,
    _validate_heartbeat_factory,
    _validate_scheduler_factory,
    _validate_agent_profile_factory,
    _validate_observers,
    _validate_filters,
)


# ---------------------------------------------------------------------------
# Minimal valid stubs for testing
# ---------------------------------------------------------------------------


class StubProvider:
    name = "stub"

    def transcript_sources(self, account, session):
        return []

    def build_launch_command(self, account, session, cwd):
        return ["echo", "hi"]

    def collect_usage_snapshot(self, account):
        return None


class StubRuntime:
    def wrap_command(self, command, account, project):
        return command


class StubHeartbeatBackend:
    def run(self, api, *, snapshot_lines=200):
        return []


class StubScheduler:
    def schedule(self, job, supervisor):
        pass

    def list_jobs(self, supervisor):
        return []

    def run_due(self, supervisor, *, now=None):
        return []


class StubAgentProfile:
    name = "stub"

    def build_prompt(self, context):
        return "You are a stub."


# ---------------------------------------------------------------------------
# ValidationResult / ValidationReport
# ---------------------------------------------------------------------------


class TestValidationResult:
    def test_defaults_to_passed(self) -> None:
        result = ValidationResult(plugin_name="test", passed=True)
        assert result.passed
        assert result.checks == []
        assert result.errors == []


class TestValidationReport:
    def test_all_passed_empty(self) -> None:
        report = ValidationReport()
        assert report.all_passed

    def test_all_passed_with_results(self) -> None:
        report = ValidationReport(results=[
            ValidationResult(plugin_name="a", passed=True, checks=["ok"]),
            ValidationResult(plugin_name="b", passed=True, checks=["ok"]),
        ])
        assert report.all_passed
        assert report.total_checks == 2
        assert report.total_errors == 0

    def test_not_all_passed(self) -> None:
        report = ValidationReport(results=[
            ValidationResult(plugin_name="a", passed=True),
            ValidationResult(plugin_name="b", passed=False, errors=["bad"]),
        ])
        assert not report.all_passed
        assert report.total_errors == 1


# ---------------------------------------------------------------------------
# Provider validation
# ---------------------------------------------------------------------------


class TestProviderValidation:
    def test_valid_provider_passes(self) -> None:
        checks, errors = _validate_provider_factory("stub", StubProvider)
        assert len(errors) == 0
        assert len(checks) >= 4  # callable, instantiated, name, transcript_sources, launch_command, usage_snapshot

    def test_non_callable_factory_fails(self) -> None:
        checks, errors = _validate_provider_factory("bad", "not_callable")
        assert len(errors) == 1
        assert "not callable" in errors[0]

    def test_factory_that_raises_fails(self) -> None:
        def broken():
            raise RuntimeError("boom")

        checks, errors = _validate_provider_factory("broken", broken)
        assert len(errors) == 1
        assert "raised on instantiation" in errors[0]

    def test_missing_name_attribute(self) -> None:
        class NoName:
            def transcript_sources(self, a, b):
                return []
            def build_launch_command(self, a, b, c):
                return []
            def collect_usage_snapshot(self, a):
                return None

        checks, errors = _validate_provider_factory("noname", NoName)
        assert any("missing required attribute 'name'" in e for e in errors)

    def test_missing_method(self) -> None:
        class Incomplete:
            name = "incomplete"
            def transcript_sources(self, a, b):
                return []

        checks, errors = _validate_provider_factory("incomplete", Incomplete)
        assert any("build_launch_command" in e for e in errors)
        assert any("collect_usage_snapshot" in e for e in errors)


# ---------------------------------------------------------------------------
# Runtime validation
# ---------------------------------------------------------------------------


class TestRuntimeValidation:
    def test_valid_runtime_passes(self) -> None:
        checks, errors = _validate_runtime_factory("stub", StubRuntime)
        assert len(errors) == 0
        assert len(checks) >= 2

    def test_missing_method(self) -> None:
        class BadRuntime:
            pass

        checks, errors = _validate_runtime_factory("bad", BadRuntime)
        assert any("wrap_command" in e for e in errors)


# ---------------------------------------------------------------------------
# Heartbeat backend validation
# ---------------------------------------------------------------------------


class TestHeartbeatValidation:
    def test_valid_heartbeat_passes(self) -> None:
        checks, errors = _validate_heartbeat_factory("stub", StubHeartbeatBackend)
        assert len(errors) == 0

    def test_missing_run_method(self) -> None:
        class BadHB:
            pass

        checks, errors = _validate_heartbeat_factory("bad", BadHB)
        assert any("run" in e for e in errors)


# ---------------------------------------------------------------------------
# Scheduler backend validation
# ---------------------------------------------------------------------------


class TestSchedulerValidation:
    def test_valid_scheduler_passes(self) -> None:
        checks, errors = _validate_scheduler_factory("stub", StubScheduler)
        assert len(errors) == 0

    def test_missing_methods(self) -> None:
        class BadSched:
            def schedule(self, j, s):
                pass

        checks, errors = _validate_scheduler_factory("bad", BadSched)
        assert any("list_jobs" in e for e in errors)
        assert any("run_due" in e for e in errors)


# ---------------------------------------------------------------------------
# Agent profile validation
# ---------------------------------------------------------------------------


class TestAgentProfileValidation:
    def test_valid_profile_passes(self) -> None:
        checks, errors = _validate_agent_profile_factory("stub", StubAgentProfile)
        assert len(errors) == 0

    def test_missing_name(self) -> None:
        class BadProfile:
            system_prompt = "test"

        checks, errors = _validate_agent_profile_factory("bad", BadProfile)
        assert any("name" in e for e in errors)

    def test_missing_build_prompt(self) -> None:
        class BadProfile:
            name = "bad"

        checks, errors = _validate_agent_profile_factory("bad", BadProfile)
        assert any("build_prompt" in e for e in errors)


# ---------------------------------------------------------------------------
# Observer and filter validation
# ---------------------------------------------------------------------------


class TestObserverValidation:
    def test_valid_observer(self) -> None:
        plugin = PollyPMPlugin(
            name="test",
            observers={"hook.name": [lambda ctx: None]},
        )
        checks, errors = _validate_observers(plugin)
        assert len(errors) == 0
        assert len(checks) == 1

    def test_non_callable_observer(self) -> None:
        plugin = PollyPMPlugin(
            name="test",
            observers={"hook.name": ["not_callable"]},
        )
        checks, errors = _validate_observers(plugin)
        assert len(errors) == 1


class TestFilterValidation:
    def test_valid_filter(self) -> None:
        plugin = PollyPMPlugin(
            name="test",
            filters={"hook.name": [lambda ctx: None]},
        )
        checks, errors = _validate_filters(plugin)
        assert len(errors) == 0

    def test_non_callable_filter(self) -> None:
        plugin = PollyPMPlugin(
            name="test",
            filters={"hook.name": [42]},
        )
        checks, errors = _validate_filters(plugin)
        assert len(errors) == 1


# ---------------------------------------------------------------------------
# Full plugin validation
# ---------------------------------------------------------------------------


class TestValidatePlugin:
    def test_valid_plugin_passes(self) -> None:
        plugin = PollyPMPlugin(
            name="good",
            providers={"stub": StubProvider},
            runtimes={"stub": StubRuntime},
        )
        result = validate_plugin(plugin)
        assert result.passed
        assert result.plugin_name == "good"
        assert len(result.errors) == 0

    def test_plugin_with_broken_factory_fails(self) -> None:
        def broken():
            raise RuntimeError("factory exploded")

        plugin = PollyPMPlugin(
            name="broken",
            providers={"bad": broken},
        )
        result = validate_plugin(plugin)
        assert not result.passed
        assert any("factory exploded" in e for e in result.errors)

    def test_empty_plugin_passes(self) -> None:
        plugin = PollyPMPlugin(name="empty")
        result = validate_plugin(plugin)
        assert result.passed

    def test_plugin_without_name_fails(self) -> None:
        plugin = PollyPMPlugin(name="")
        result = validate_plugin(plugin)
        assert not result.passed
        assert any("no name" in e for e in result.errors)

    def test_plugin_with_multiple_interfaces(self) -> None:
        plugin = PollyPMPlugin(
            name="multi",
            providers={"p": StubProvider},
            runtimes={"r": StubRuntime},
            heartbeat_backends={"h": StubHeartbeatBackend},
            scheduler_backends={"s": StubScheduler},
            agent_profiles={"a": StubAgentProfile},
        )
        result = validate_plugin(plugin)
        assert result.passed
        assert len(result.checks) >= 10  # Multiple checks across all interfaces
