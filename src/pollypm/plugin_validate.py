"""Automated plugin validation harness.

Performs structural checks on loaded plugins: required metadata,
factory callability/instantiation where safe, known required methods,
and hook callability. Failing plugins are disabled with clear error
messages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pollypm.plugin_api.v1 import PollyPMPlugin

if TYPE_CHECKING:
    from pollypm.plugin_host import ExtensionHost

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ValidationResult:
    """Result of validating a single plugin."""

    plugin_name: str
    passed: bool
    checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ValidationReport:
    """Full report from validating all plugins."""

    results: list[ValidationResult] = field(default_factory=list)
    disabled_plugins: list[str] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def total_checks(self) -> int:
        return sum(len(r.checks) for r in self.results)

    @property
    def total_errors(self) -> int:
        return sum(len(r.errors) for r in self.results)


# ---------------------------------------------------------------------------
# Interface validators
# ---------------------------------------------------------------------------


def _validate_provider_factory(name: str, factory: object) -> tuple[list[str], list[str]]:
    """Validate a provider factory produces a valid provider."""
    checks: list[str] = []
    errors: list[str] = []

    if not callable(factory):
        errors.append(f"Provider factory '{name}' is not callable")
        return checks, errors

    checks.append(f"Provider factory '{name}' is callable")

    try:
        instance = factory()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Provider factory '{name}' raised on instantiation: {exc}")
        return checks, errors

    checks.append(f"Provider factory '{name}' instantiated successfully")

    # Check required attribute: name
    if not hasattr(instance, "name"):
        errors.append(f"Provider '{name}' missing required attribute 'name'")
    else:
        checks.append(f"Provider '{name}' has 'name' attribute")

    # Check required method: transcript_sources
    if not hasattr(instance, "transcript_sources"):
        errors.append(f"Provider '{name}' missing required method 'transcript_sources'")
    elif not callable(instance.transcript_sources):
        errors.append(f"Provider '{name}' 'transcript_sources' is not callable")
    else:
        checks.append(f"Provider '{name}' has 'transcript_sources' method")

    # Check required method: build_launch_command
    if not hasattr(instance, "build_launch_command"):
        errors.append(f"Provider '{name}' missing required method 'build_launch_command'")
    elif not callable(instance.build_launch_command):
        errors.append(f"Provider '{name}' 'build_launch_command' is not callable")
    else:
        checks.append(f"Provider '{name}' has 'build_launch_command' method")

    # Check required method: collect_usage_snapshot
    if not hasattr(instance, "collect_usage_snapshot"):
        errors.append(f"Provider '{name}' missing required method 'collect_usage_snapshot'")
    elif not callable(instance.collect_usage_snapshot):
        errors.append(f"Provider '{name}' 'collect_usage_snapshot' is not callable")
    else:
        checks.append(f"Provider '{name}' has 'collect_usage_snapshot' method")

    return checks, errors


def _validate_runtime_factory(name: str, factory: object) -> tuple[list[str], list[str]]:
    """Validate a runtime factory produces a valid runtime."""
    checks: list[str] = []
    errors: list[str] = []

    if not callable(factory):
        errors.append(f"Runtime factory '{name}' is not callable")
        return checks, errors

    checks.append(f"Runtime factory '{name}' is callable")

    try:
        instance = factory()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Runtime factory '{name}' raised on instantiation: {exc}")
        return checks, errors

    checks.append(f"Runtime factory '{name}' instantiated successfully")

    if not hasattr(instance, "wrap_command"):
        errors.append(f"Runtime '{name}' missing required method 'wrap_command'")
    elif not callable(instance.wrap_command):
        errors.append(f"Runtime '{name}' 'wrap_command' is not callable")
    else:
        checks.append(f"Runtime '{name}' has 'wrap_command' method")

    return checks, errors


def _validate_heartbeat_factory(name: str, factory: object) -> tuple[list[str], list[str]]:
    """Validate a heartbeat backend factory."""
    checks: list[str] = []
    errors: list[str] = []

    if not callable(factory):
        errors.append(f"Heartbeat factory '{name}' is not callable")
        return checks, errors

    checks.append(f"Heartbeat factory '{name}' is callable")

    try:
        instance = factory()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Heartbeat factory '{name}' raised on instantiation: {exc}")
        return checks, errors

    checks.append(f"Heartbeat factory '{name}' instantiated successfully")

    if not hasattr(instance, "run") or not callable(instance.run):
        errors.append(f"Heartbeat backend '{name}' missing required method 'run'")
    else:
        checks.append(f"Heartbeat backend '{name}' has 'run' method")

    return checks, errors


def _validate_scheduler_factory(name: str, factory: object) -> tuple[list[str], list[str]]:
    """Validate a scheduler backend factory."""
    checks: list[str] = []
    errors: list[str] = []

    if not callable(factory):
        errors.append(f"Scheduler factory '{name}' is not callable")
        return checks, errors

    checks.append(f"Scheduler factory '{name}' is callable")

    try:
        instance = factory()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Scheduler factory '{name}' raised on instantiation: {exc}")
        return checks, errors

    checks.append(f"Scheduler factory '{name}' instantiated successfully")

    for method_name in ("schedule", "list_jobs", "run_due"):
        if not hasattr(instance, method_name):
            errors.append(f"Scheduler backend '{name}' missing required method '{method_name}'")
        elif not callable(getattr(instance, method_name)):
            errors.append(f"Scheduler backend '{name}' '{method_name}' is not callable")
        else:
            checks.append(f"Scheduler backend '{name}' has '{method_name}' method")

    return checks, errors


def _validate_agent_profile_factory(name: str, factory: object) -> tuple[list[str], list[str]]:
    """Validate an agent profile factory."""
    checks: list[str] = []
    errors: list[str] = []

    if not callable(factory):
        errors.append(f"Agent profile factory '{name}' is not callable")
        return checks, errors

    checks.append(f"Agent profile factory '{name}' is callable")

    try:
        instance = factory()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Agent profile factory '{name}' raised on instantiation: {exc}")
        return checks, errors

    checks.append(f"Agent profile factory '{name}' instantiated successfully")

    if not hasattr(instance, "name"):
        errors.append(f"Agent profile '{name}' missing required attribute 'name'")
    else:
        checks.append(f"Agent profile '{name}' has 'name' attribute")

    if not hasattr(instance, "build_prompt"):
        errors.append(f"Agent profile '{name}' missing required method 'build_prompt'")
    elif not callable(instance.build_prompt):
        errors.append(f"Agent profile '{name}' 'build_prompt' is not callable")
    else:
        checks.append(f"Agent profile '{name}' has 'build_prompt' method")

    return checks, errors


def _validate_transcript_source_factory(name: str, factory: object) -> tuple[list[str], list[str]]:
    """Validate a transcript_source factory is callable. Factories may take
    kwargs (e.g. account, config) so we don't try to instantiate them here."""
    checks: list[str] = []
    errors: list[str] = []

    if not callable(factory):
        errors.append(f"Transcript source factory '{name}' is not callable")
        return checks, errors

    checks.append(f"Transcript source factory '{name}' is callable")
    return checks, errors


def _validate_recovery_policy_factory(name: str, factory: object) -> tuple[list[str], list[str]]:
    """Validate a recovery_policy factory produces a working policy."""
    checks: list[str] = []
    errors: list[str] = []

    if not callable(factory):
        errors.append(f"Recovery policy factory '{name}' is not callable")
        return checks, errors

    checks.append(f"Recovery policy factory '{name}' is callable")

    try:
        instance = factory()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Recovery policy factory '{name}' raised on instantiation: {exc}")
        return checks, errors

    checks.append(f"Recovery policy factory '{name}' instantiated successfully")

    for method_name in ("classify", "select_intervention"):
        if not hasattr(instance, method_name):
            errors.append(f"Recovery policy '{name}' missing required method '{method_name}'")
        elif not callable(getattr(instance, method_name)):
            errors.append(f"Recovery policy '{name}' '{method_name}' is not callable")
        else:
            checks.append(f"Recovery policy '{name}' has '{method_name}' method")

    return checks, errors


def _validate_launch_planner_factory(name: str, factory: object) -> tuple[list[str], list[str]]:
    """Validate a launch_planner factory is callable. Planners take a context
    kwarg at instantiation time, so we don't exercise the factory here —
    we just confirm it's callable. The Supervisor exercises it on construction."""
    checks: list[str] = []
    errors: list[str] = []

    if not callable(factory):
        errors.append(f"Launch planner factory '{name}' is not callable")
        return checks, errors

    checks.append(f"Launch planner factory '{name}' is callable")
    return checks, errors


def _validate_observers(plugin: PollyPMPlugin) -> tuple[list[str], list[str]]:
    """Validate observer hooks are callable."""
    checks: list[str] = []
    errors: list[str] = []

    for hook_name, handlers in plugin.observers.items():
        if not isinstance(handlers, list):
            errors.append(f"Observer '{hook_name}' value is not a list")
            continue
        for i, handler in enumerate(handlers):
            if callable(handler):
                checks.append(f"Observer '{hook_name}[{i}]' is callable")
            else:
                errors.append(f"Observer '{hook_name}[{i}]' is not callable")

    return checks, errors


def _validate_filters(plugin: PollyPMPlugin) -> tuple[list[str], list[str]]:
    """Validate filter hooks are callable."""
    checks: list[str] = []
    errors: list[str] = []

    for hook_name, handlers in plugin.filters.items():
        if not isinstance(handlers, list):
            errors.append(f"Filter '{hook_name}' value is not a list")
            continue
        for i, handler in enumerate(handlers):
            if callable(handler):
                checks.append(f"Filter '{hook_name}[{i}]' is callable")
            else:
                errors.append(f"Filter '{hook_name}[{i}]' is not callable")

    return checks, errors


# ---------------------------------------------------------------------------
# Plugin validation
# ---------------------------------------------------------------------------


def validate_plugin(plugin: PollyPMPlugin) -> ValidationResult:
    """Validate a single plugin against its declared interfaces."""
    result = ValidationResult(plugin_name=plugin.name, passed=True)

    # Basic structure checks
    if not plugin.name:
        result.errors.append("Plugin has no name")
    else:
        result.checks.append(f"Plugin '{plugin.name}' has a name")

    if not plugin.api_version:
        result.errors.append("Plugin has no api_version")
    else:
        result.checks.append(f"Plugin '{plugin.name}' has api_version '{plugin.api_version}'")

    # Validate each registered factory by interface type
    for name, factory in plugin.providers.items():
        checks, errors = _validate_provider_factory(name, factory)
        result.checks.extend(checks)
        result.errors.extend(errors)

    for name, factory in plugin.runtimes.items():
        checks, errors = _validate_runtime_factory(name, factory)
        result.checks.extend(checks)
        result.errors.extend(errors)

    for name, factory in plugin.heartbeat_backends.items():
        checks, errors = _validate_heartbeat_factory(name, factory)
        result.checks.extend(checks)
        result.errors.extend(errors)

    for name, factory in plugin.scheduler_backends.items():
        checks, errors = _validate_scheduler_factory(name, factory)
        result.checks.extend(checks)
        result.errors.extend(errors)

    for name, factory in plugin.agent_profiles.items():
        checks, errors = _validate_agent_profile_factory(name, factory)
        result.checks.extend(checks)
        result.errors.extend(errors)

    for name, factory in plugin.transcript_sources.items():
        checks, errors = _validate_transcript_source_factory(name, factory)
        result.checks.extend(checks)
        result.errors.extend(errors)

    for name, factory in plugin.recovery_policies.items():
        checks, errors = _validate_recovery_policy_factory(name, factory)
        result.checks.extend(checks)
        result.errors.extend(errors)

    for name, factory in plugin.launch_planners.items():
        checks, errors = _validate_launch_planner_factory(name, factory)
        result.checks.extend(checks)
        result.errors.extend(errors)

    # Validate hooks
    obs_checks, obs_errors = _validate_observers(plugin)
    result.checks.extend(obs_checks)
    result.errors.extend(obs_errors)

    filt_checks, filt_errors = _validate_filters(plugin)
    result.checks.extend(filt_checks)
    result.errors.extend(filt_errors)

    result.passed = len(result.errors) == 0
    return result


def validate_all_plugins(host: ExtensionHost) -> ValidationReport:
    """Validate all loaded plugins and disable failing ones."""
    report = ValidationReport()

    for name, plugin in host.plugins().items():
        result = validate_plugin(plugin)
        report.results.append(result)

        if not result.passed:
            report.disabled_plugins.append(name)
            host.remove_plugin(name)
            for error in result.errors:
                logger.warning("Plugin '%s' validation failed: %s", name, error)
                host._record_error(
                    f"Plugin '{name}' disabled: {error}",
                    plugin=name,
                    stage="validate",
                )
        else:
            n_checks = len(result.checks)
            check_word = "check" if n_checks == 1 else "checks"
            logger.debug(
                "Plugin '%s' passed validation (%d %s)", name, n_checks, check_word,
            )

    return report


def validate_plugin_by_name(host: ExtensionHost, name: str) -> ValidationResult | None:
    """Validate a single plugin by name. Returns None if not found."""
    plugins = host.plugins()
    plugin = plugins.get(name)
    if plugin is None:
        return None
    return validate_plugin(plugin)
