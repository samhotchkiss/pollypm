from pathlib import Path
import shutil

from pollypm.models import ProviderKind, RuntimeKind
from pollypm.plugin_api.v1 import HookFilterResult
from pollypm.plugin_host import ExtensionHost
from pollypm.providers import get_provider
from pollypm.runtimes import get_runtime


def _write_plugin(
    plugin_dir: Path,
    *,
    name: str,
    body: str,
    api_version: str = "1",
    kind: str = "provider",
    capabilities: tuple[str, ...] = ("provider", "hook"),
) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "pollypm-plugin.toml").write_text(
        "\n".join(
            [
                f'api_version = "{api_version}"',
                f'name = "{name}"',
                f'kind = "{kind}"',
                'version = "0.1.0"',
                'entrypoint = "plugin.py:plugin"',
                'capabilities = [' + ', '.join(f'"{item}"' for item in capabilities) + ']',
            ]
        )
        + "\n"
    )
    (plugin_dir / "plugin.py").write_text(body)


def test_extension_host_loads_builtin_provider_and_runtime(tmp_path: Path) -> None:
    host = ExtensionHost(tmp_path)

    assert host.get_provider("claude").name == "claude"
    assert host.get_provider("codex").name == "codex"
    assert type(host.get_runtime("local")).__name__ == "LocalRuntimeAdapter"
    assert type(host.get_runtime("docker")).__name__ == "DockerRuntimeAdapter"
    assert type(host.get_heartbeat_backend("local")).__name__ == "LocalHeartbeatBackend"
    assert type(host.get_scheduler_backend("inline")).__name__ == "InlineSchedulerBackend"
    assert host.get_agent_profile("polly").name == "polly"


def test_repo_local_plugin_overrides_user_plugin(monkeypatch, tmp_path: Path) -> None:
    builtin_root = Path(__file__).resolve().parents[1] / "src" / "pollypm" / "plugins_builtin"
    user_root = tmp_path / "user-plugins"
    repo_root = tmp_path / ".pollypm-state" / "plugins"
    user_plugin = user_root / "override_provider_test"
    repo_plugin = tmp_path / ".pollypm-state" / "plugins" / "override_provider_test"
    monkeypatch.setattr(
        ExtensionHost,
        "_plugin_search_paths",
        lambda self: [("builtin", builtin_root), ("user", user_root), ("repo", repo_root)],
    )
    try:
        _write_plugin(
            user_plugin,
            name="override_provider_test",
            body=(
                "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
                "from pollypm.providers.claude import ClaudeAdapter\n"
                "plugin = PollyPMPlugin(name='override_provider_test', providers={'claude': ClaudeAdapter})\n"
            ),
        )
        _write_plugin(
            repo_plugin,
            name="override_provider_test",
            body=(
                "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
                "from pollypm.providers.codex import CodexAdapter\n"
                "plugin = PollyPMPlugin(name='override_provider_test', providers={'claude': CodexAdapter})\n"
            ),
        )

        host = ExtensionHost(tmp_path)
        assert host.get_provider("claude").name == "codex"
    finally:
        if user_plugin.exists():
            shutil.rmtree(user_plugin)
        if repo_plugin.exists():
            shutil.rmtree(repo_plugin)


def test_extension_host_rejects_wrong_api_version(tmp_path: Path) -> None:
    bad_plugin = tmp_path / ".pollypm-state" / "plugins" / "bad"
    _write_plugin(
        bad_plugin,
        name="bad",
        api_version="99",
        body="from pollypm.plugin_api.v1 import PollyPMPlugin\nplugin = PollyPMPlugin(name='bad')\n",
    )

    host = ExtensionHost(tmp_path)

    assert "bad" not in host.plugins()
    assert any("API version 99" in item for item in host.errors)


def test_extension_host_runs_observers_and_filters_safely(tmp_path: Path) -> None:
    plugin_dir = tmp_path / ".pollypm-state" / "plugins" / "hooks"
    _write_plugin(
        plugin_dir,
        name="hooks",
        body=(
            "from pollypm.plugin_api.v1 import HookFilterResult, PollyPMPlugin\n"
            "events = []\n"
            "def observer(ctx):\n"
            "    events.append(('observe', ctx.hook_name, ctx.payload))\n"
            "def mutate(ctx):\n"
            "    return HookFilterResult(action='mutate', payload={'value': ctx.payload['value'] + 1})\n"
            "def broken(ctx):\n"
            "    raise RuntimeError('boom')\n"
            "plugin = PollyPMPlugin(\n"
            "    name='hooks',\n"
            "    observers={'session.after_launch': [observer, broken]},\n"
            "    filters={'session.before_launch': [mutate, broken]},\n"
            ")\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    failures = host.run_observers("session.after_launch", {"value": 1})
    result = host.run_filters("session.before_launch", {"value": 1})

    assert failures
    assert isinstance(result, HookFilterResult)
    assert result.action == "allow"
    assert result.payload == {"value": 2}
    assert any("failed: boom" in item for item in host.errors)


def test_get_provider_and_runtime_resolve_through_extension_host(tmp_path: Path) -> None:
    provider = get_provider(ProviderKind.CLAUDE, root_dir=tmp_path)
    runtime = get_runtime(RuntimeKind.LOCAL, root_dir=tmp_path)

    assert provider.name == "claude"
    assert type(runtime).__name__ == "LocalRuntimeAdapter"


def test_repo_heartbeat_plugin_overrides_builtin_backend(monkeypatch, tmp_path: Path) -> None:
    builtin_root = Path(__file__).resolve().parents[1] / "src" / "pollypm" / "plugins_builtin"
    user_root = tmp_path / "user-plugins"
    repo_root = tmp_path / ".pollypm-state" / "plugins"
    repo_plugin = repo_root / "override_heartbeat_test"
    monkeypatch.setattr(
        ExtensionHost,
        "_plugin_search_paths",
        lambda self: [("builtin", builtin_root), ("user", user_root), ("repo", repo_root)],
    )
    try:
        _write_plugin(
            repo_plugin,
            name="override_heartbeat_test",
            kind="heartbeat",
            capabilities=("heartbeat",),
            body=(
                "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
                "class RepoHeartbeatBackend:\n"
                '    name = "local"\n'
                "    def run(self, api, *, snapshot_lines=200):\n"
                "        return []\n"
                'plugin = PollyPMPlugin(name="override_heartbeat_test", heartbeat_backends={"local": RepoHeartbeatBackend})\n'
            ),
        )

        host = ExtensionHost(tmp_path)
        backend = host.get_heartbeat_backend("local")

        assert type(backend).__name__ == "RepoHeartbeatBackend"
    finally:
        if repo_plugin.exists():
            shutil.rmtree(repo_plugin)
