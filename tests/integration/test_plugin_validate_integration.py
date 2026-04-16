"""Integration tests for plugin validation harness."""

import shutil
from pathlib import Path

from pollypm.plugin_api.v1 import PollyPMPlugin
from pollypm.plugin_host import ExtensionHost
from pollypm.plugin_validate import (
    validate_all_plugins,
    validate_plugin_by_name,
)


def _write_plugin(
    plugin_dir: Path,
    *,
    name: str,
    body: str,
    api_version: str = "1",
    kind: str = "provider",
    capabilities: tuple[str, ...] = ("provider",),
) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "pollypm-plugin.toml").write_text(
        "\n".join([
            f'api_version = "{api_version}"',
            f'name = "{name}"',
            f'kind = "{kind}"',
            'version = "0.1.0"',
            'entrypoint = "plugin.py:plugin"',
            "capabilities = [" + ", ".join(f'"{c}"' for c in capabilities) + "]",
        ])
        + "\n"
    )
    (plugin_dir / "plugin.py").write_text(body)


def test_validate_all_builtin_plugins_pass(tmp_path: Path) -> None:
    """All builtin plugins should pass validation."""
    host = ExtensionHost(tmp_path)
    report = validate_all_plugins(host)

    assert report.all_passed, (
        f"Builtin plugin validation failures: "
        + "; ".join(
            f"{r.plugin_name}: {r.errors}"
            for r in report.results
            if not r.passed
        )
    )
    assert len(report.disabled_plugins) == 0
    assert report.total_checks > 0


def test_validate_broken_plugin_is_disabled(tmp_path: Path) -> None:
    """A plugin with a broken provider factory should be disabled."""
    plugin_dir = tmp_path / ".pollypm" / "plugins" / "broken_provider"
    _write_plugin(
        plugin_dir,
        name="broken_provider",
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "def bad_factory():\n"
            "    raise RuntimeError('broken factory')\n"
            "plugin = PollyPMPlugin(\n"
            "    name='broken_provider',\n"
            "    providers={'broken': bad_factory},\n"
            ")\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    # Plugin should load (the factory isn't called until validation)
    assert "broken_provider" in host.plugins()

    report = validate_all_plugins(host)

    assert "broken_provider" in report.disabled_plugins
    assert "broken_provider" not in host.plugins()

    # Errors should be recorded
    broken_results = [r for r in report.results if r.plugin_name == "broken_provider"]
    assert len(broken_results) == 1
    assert not broken_results[0].passed
    assert any("broken factory" in e for e in broken_results[0].errors)


def test_validate_plugin_missing_interface_methods(tmp_path: Path) -> None:
    """A plugin registering an incomplete provider should fail validation."""
    plugin_dir = tmp_path / ".pollypm" / "plugins" / "incomplete"
    _write_plugin(
        plugin_dir,
        name="incomplete",
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "class BadProvider:\n"
            "    name = 'bad'\n"
            "    # Missing transcript_sources, launch_command, usage_snapshot\n"
            "plugin = PollyPMPlugin(\n"
            "    name='incomplete',\n"
            "    providers={'bad': BadProvider},\n"
            ")\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    report = validate_all_plugins(host)

    assert "incomplete" in report.disabled_plugins

    incomplete_results = [r for r in report.results if r.plugin_name == "incomplete"]
    assert len(incomplete_results) == 1
    assert any("transcript_sources" in e for e in incomplete_results[0].errors)
    assert any("build_launch_command" in e for e in incomplete_results[0].errors)
    assert any("collect_usage_snapshot" in e for e in incomplete_results[0].errors)


def test_validate_plugin_by_name_found(tmp_path: Path) -> None:
    """Validate a specific plugin by name."""
    host = ExtensionHost(tmp_path)
    # 'claude' is a builtin plugin
    result = validate_plugin_by_name(host, "claude")
    assert result is not None
    assert result.passed
    assert result.plugin_name == "claude"


def test_validate_plugin_by_name_not_found(tmp_path: Path) -> None:
    """Returns None for unknown plugin name."""
    host = ExtensionHost(tmp_path)
    result = validate_plugin_by_name(host, "nonexistent")
    assert result is None


def test_valid_plugin_remains_active_after_validation(tmp_path: Path) -> None:
    """A valid custom plugin should remain active after validation."""
    plugin_dir = tmp_path / ".pollypm" / "plugins" / "good_plugin"
    _write_plugin(
        plugin_dir,
        name="good_plugin",
        kind="heartbeat",
        capabilities=("heartbeat",),
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "class GoodBackend:\n"
            "    def run(self, api, *, snapshot_lines=200):\n"
            "        return []\n"
            "plugin = PollyPMPlugin(\n"
            "    name='good_plugin',\n"
            "    heartbeat_backends={'custom': GoodBackend},\n"
            ")\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    assert "good_plugin" in host.plugins()

    report = validate_all_plugins(host)

    assert "good_plugin" not in report.disabled_plugins
    assert "good_plugin" in host.plugins()

    good_results = [r for r in report.results if r.plugin_name == "good_plugin"]
    assert len(good_results) == 1
    assert good_results[0].passed


def test_mixed_valid_and_invalid_plugins(tmp_path: Path) -> None:
    """Only invalid plugins are disabled; valid ones remain."""
    good_dir = tmp_path / ".pollypm" / "plugins" / "good_mix"
    bad_dir = tmp_path / ".pollypm" / "plugins" / "bad_mix"

    _write_plugin(
        good_dir,
        name="good_mix",
        kind="heartbeat",
        capabilities=("heartbeat",),
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "class GoodHB:\n"
            "    def run(self, api, *, snapshot_lines=200):\n"
            "        return []\n"
            "plugin = PollyPMPlugin(name='good_mix', heartbeat_backends={'good': GoodHB})\n"
        ),
    )

    _write_plugin(
        bad_dir,
        name="bad_mix",
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "plugin = PollyPMPlugin(name='', providers={'x': 'not_callable'})\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    report = validate_all_plugins(host)

    assert "bad_mix" in report.disabled_plugins
    assert "good_mix" not in report.disabled_plugins
    assert "good_mix" in host.plugins()
