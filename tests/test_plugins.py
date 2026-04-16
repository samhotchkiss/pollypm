from pathlib import Path
import shutil

from pollypm.models import ProviderKind, RuntimeKind
from pollypm.plugin_api.v1 import Capability, HookFilterResult
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
    # Use the new project-local path per docs/plugin-discovery-spec.md §2.
    bad_plugin = tmp_path / ".pollypm" / "plugins" / "bad"
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
    plugin_dir = tmp_path / ".pollypm" / "plugins" / "hooks"
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


def test_transcript_source_plugin_registers_and_resolves(monkeypatch, tmp_path: Path) -> None:
    builtin_root = Path(__file__).resolve().parents[1] / "src" / "pollypm" / "plugins_builtin"
    user_root = tmp_path / "user-plugins"
    repo_root = tmp_path / ".pollypm-state" / "plugins"
    repo_plugin = repo_root / "transcript_source_test"
    monkeypatch.setattr(
        ExtensionHost,
        "_plugin_search_paths",
        lambda self: [("builtin", builtin_root), ("user", user_root), ("repo", repo_root)],
    )
    try:
        _write_plugin(
            repo_plugin,
            name="transcript_source_test",
            kind="transcript_source",
            capabilities=("transcript_source",),
            body=(
                "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
                "from pollypm.provider_sdk import TranscriptSource\n"
                "from pathlib import Path\n"
                "def make_source(**kwargs):\n"
                "    return [TranscriptSource(root=Path('/tmp/does-not-exist'), pattern='*.jsonl')]\n"
                "plugin = PollyPMPlugin(name='transcript_source_test', transcript_sources={'fake': make_source})\n"
            ),
        )

        host = ExtensionHost(tmp_path)
        produced = host.get_transcript_source("fake")
        assert isinstance(produced, list) and len(produced) == 1

        pairs = host.iter_transcript_sources()
        assert any(name == "fake" for name, _ in pairs)
    finally:
        if repo_plugin.exists():
            shutil.rmtree(repo_plugin)


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


# ---------------------------------------------------------------------------
# Issue #168 — structured [[capabilities]] manifest parsing.
# ---------------------------------------------------------------------------


def _write_structured_plugin(
    plugin_dir: Path,
    *,
    name: str,
    body: str,
    manifest_extras: str = "",
    requires_api: str | None = None,
) -> None:
    """Helper for writing a plugin with structured [[capabilities]]."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    top_requires = f'\nrequires_api = "{requires_api}"' if requires_api else ""
    (plugin_dir / "pollypm-plugin.toml").write_text(
        f'''api_version = "1"
name = "{name}"
version = "0.1.0"
entrypoint = "plugin.py:plugin"{top_requires}
{manifest_extras}
'''
    )
    (plugin_dir / "plugin.py").write_text(body)


def test_structured_capabilities_parse(monkeypatch, tmp_path: Path) -> None:
    builtin_root = Path(__file__).resolve().parents[1] / "src" / "pollypm" / "plugins_builtin"
    user_root = tmp_path / "user-plugins"
    repo_root = tmp_path / ".pollypm-state" / "plugins"
    repo_plugin = repo_root / "structured_caps_test"
    monkeypatch.setattr(
        ExtensionHost,
        "_plugin_search_paths",
        lambda self: [("builtin", builtin_root), ("user", user_root), ("repo", repo_root)],
    )
    _write_structured_plugin(
        repo_plugin,
        name="structured_caps_test",
        manifest_extras=(
            '[[capabilities]]\n'
            'kind = "provider"\n'
            'name = "my_provider"\n'
            'requires_api = ">=1,<2"\n'
            'replaces = ["old_provider"]\n'
            '\n'
            '[[capabilities]]\n'
            'kind = "runtime"\n'
            'name = "my_runtime"\n'
        ),
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "plugin = PollyPMPlugin(name='structured_caps_test')\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    plugins = host.plugins()
    assert "structured_caps_test" in plugins
    caps = plugins["structured_caps_test"].capabilities
    assert len(caps) == 2
    provider_cap = next(c for c in caps if c.kind == "provider")
    runtime_cap = next(c for c in caps if c.kind == "runtime")
    assert provider_cap.name == "my_provider"
    assert provider_cap.replaces == ("old_provider",)
    assert provider_cap.requires_api == ">=1,<2"
    assert runtime_cap.name == "my_runtime"


def test_legacy_bare_string_capabilities_still_parse(monkeypatch, tmp_path: Path, caplog) -> None:
    import logging

    builtin_root = Path(__file__).resolve().parents[1] / "src" / "pollypm" / "plugins_builtin"
    user_root = tmp_path / "user-plugins"
    repo_root = tmp_path / ".pollypm-state" / "plugins"
    repo_plugin = repo_root / "legacy_caps_test"
    monkeypatch.setattr(
        ExtensionHost,
        "_plugin_search_paths",
        lambda self: [("builtin", builtin_root), ("user", user_root), ("repo", repo_root)],
    )
    _write_structured_plugin(
        repo_plugin,
        name="legacy_caps_test",
        manifest_extras='capabilities = ["provider", "hook"]',
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "plugin = PollyPMPlugin(name='legacy_caps_test')\n"
        ),
    )
    with caplog.at_level(logging.WARNING, logger="pollypm.plugin_host"):
        host = ExtensionHost(tmp_path)
        plugin = host.plugins()["legacy_caps_test"]
    kinds = {c.kind for c in plugin.capabilities}
    assert kinds == {"provider", "hook"}
    assert any("legacy" in rec.message.lower() for rec in caplog.records)


def test_requires_api_mismatch_skips_plugin(monkeypatch, tmp_path: Path) -> None:
    builtin_root = Path(__file__).resolve().parents[1] / "src" / "pollypm" / "plugins_builtin"
    user_root = tmp_path / "user-plugins"
    repo_root = tmp_path / ".pollypm-state" / "plugins"
    repo_plugin = repo_root / "bad_requires_api"
    monkeypatch.setattr(
        ExtensionHost,
        "_plugin_search_paths",
        lambda self: [("builtin", builtin_root), ("user", user_root), ("repo", repo_root)],
    )
    _write_structured_plugin(
        repo_plugin,
        name="bad_requires_api",
        requires_api=">=2,<3",
        manifest_extras='[[capabilities]]\nkind = "provider"\nname = "mine"\n',
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "plugin = PollyPMPlugin(name='bad_requires_api')\n"
        ),
    )
    host = ExtensionHost(tmp_path)
    assert "bad_requires_api" not in host.plugins()
    assert any("requires_api" in err for err in host.errors)


def test_per_capability_requires_api_drops_single_capability(monkeypatch, tmp_path: Path) -> None:
    builtin_root = Path(__file__).resolve().parents[1] / "src" / "pollypm" / "plugins_builtin"
    user_root = tmp_path / "user-plugins"
    repo_root = tmp_path / ".pollypm-state" / "plugins"
    repo_plugin = repo_root / "cap_requires_api"
    monkeypatch.setattr(
        ExtensionHost,
        "_plugin_search_paths",
        lambda self: [("builtin", builtin_root), ("user", user_root), ("repo", repo_root)],
    )
    _write_structured_plugin(
        repo_plugin,
        name="cap_requires_api",
        manifest_extras=(
            '[[capabilities]]\n'
            'kind = "provider"\n'
            'name = "fine"\n'
            '\n'
            '[[capabilities]]\n'
            'kind = "runtime"\n'
            'name = "future"\n'
            'requires_api = ">=2"\n'
        ),
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "plugin = PollyPMPlugin(name='cap_requires_api')\n"
        ),
    )
    host = ExtensionHost(tmp_path)
    plugin = host.plugins()["cap_requires_api"]
    kept_kinds = {c.kind for c in plugin.capabilities}
    assert "provider" in kept_kinds
    assert "runtime" not in kept_kinds


def test_explicit_replaces_preserves_earlier_provider(monkeypatch, tmp_path: Path) -> None:
    """An explicit `replaces` capability wins over implicit last-write."""
    builtin_root = Path(__file__).resolve().parents[1] / "src" / "pollypm" / "plugins_builtin"
    user_root = tmp_path / "user-plugins"
    repo_root = tmp_path / ".pollypm-state" / "plugins"
    user_plugin = user_root / "explicit_replacer"
    repo_plugin = repo_root / "late_override"
    monkeypatch.setattr(
        ExtensionHost,
        "_plugin_search_paths",
        lambda self: [("builtin", builtin_root), ("user", user_root), ("repo", repo_root)],
    )
    # user plugin: explicitly replaces "claude"
    _write_structured_plugin(
        user_plugin,
        name="explicit_replacer",
        manifest_extras=(
            '[[capabilities]]\n'
            'kind = "provider"\n'
            'name = "claude"\n'
            'replaces = ["claude"]\n'
        ),
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "from pollypm.providers.codex import CodexAdapter\n"
            "plugin = PollyPMPlugin(name='explicit_replacer', providers={'claude': CodexAdapter})\n"
        ),
    )
    # repo plugin: tries implicit override (no `replaces`)
    _write_structured_plugin(
        repo_plugin,
        name="late_override",
        manifest_extras=(
            '[[capabilities]]\n'
            'kind = "provider"\n'
            'name = "claude"\n'
        ),
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "from pollypm.providers.claude import ClaudeAdapter\n"
            "plugin = PollyPMPlugin(name='late_override', providers={'claude': ClaudeAdapter})\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    provider = host.get_provider("claude")
    # The explicit replacer (user plugin, loaded earlier) wins — its
    # factory is CodexAdapter despite the later repo plugin trying to
    # override.
    assert provider.name == "codex"


def test_plugin_post_init_normalizes_bare_strings() -> None:
    from pollypm.plugin_api.v1 import PollyPMPlugin

    plugin = PollyPMPlugin(name="sample", capabilities=("provider", "hook"))
    assert all(isinstance(c, Capability) for c in plugin.capabilities)
    kinds = {c.kind for c in plugin.capabilities}
    assert kinds == {"provider", "hook"}
    # name falls through to plugin name
    assert all(c.name == "sample" for c in plugin.capabilities)


# ---------------------------------------------------------------------------
# Issue #167 — multi-path discovery (entry_points, user-global, project-local)
# ---------------------------------------------------------------------------


def test_user_global_plugin_loads_from_home_pollypm(monkeypatch, tmp_path: Path) -> None:
    """A plugin in ~/.pollypm/plugins/ is discovered automatically."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    user_plugin = fake_home / ".pollypm" / "plugins" / "user_global_test"
    _write_structured_plugin(
        user_plugin,
        name="user_global_test",
        manifest_extras='[[capabilities]]\nkind = "provider"\nname = "mine"\n',
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "plugin = PollyPMPlugin(name='user_global_test')\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    plugins = host.plugins()
    assert "user_global_test" in plugins
    assert host.plugin_source("user_global_test") == "user"


def test_project_local_plugin_loads_from_dot_pollypm(tmp_path: Path) -> None:
    """A plugin in <project>/.pollypm/plugins/ is discovered automatically."""
    project_plugin = tmp_path / ".pollypm" / "plugins" / "project_local_test"
    _write_structured_plugin(
        project_plugin,
        name="project_local_test",
        manifest_extras='[[capabilities]]\nkind = "provider"\nname = "mine"\n',
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "plugin = PollyPMPlugin(name='project_local_test')\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    plugins = host.plugins()
    assert "project_local_test" in plugins
    assert host.plugin_source("project_local_test") == "project"


def test_project_plugin_shadows_user_plugin(monkeypatch, tmp_path: Path) -> None:
    """Project-local plugin wins over user-global for name collision."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    user_plugin = fake_home / ".pollypm" / "plugins" / "shadow_test"
    project_plugin = tmp_path / ".pollypm" / "plugins" / "shadow_test"

    _write_structured_plugin(
        user_plugin,
        name="shadow_test",
        manifest_extras='[[capabilities]]\nkind = "provider"\nname = "mine"\n',
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "class UserMarker: pass\n"
            "plugin = PollyPMPlugin(name='shadow_test', providers={'x': UserMarker})\n"
        ),
    )
    _write_structured_plugin(
        project_plugin,
        name="shadow_test",
        manifest_extras='[[capabilities]]\nkind = "provider"\nname = "mine"\n',
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "class ProjectMarker: pass\n"
            "plugin = PollyPMPlugin(name='shadow_test', providers={'x': ProjectMarker})\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    plugin = host.plugins()["shadow_test"]
    assert list(plugin.providers.values())[0].__name__ == "ProjectMarker"
    assert host.plugin_source("shadow_test") == "project"


def test_entry_point_plugin_loads_without_manifest(monkeypatch, tmp_path: Path) -> None:
    """Entry-point plugins load from the pollypm.plugins group with no
    on-disk manifest — the PollyPMPlugin carries all metadata.
    """
    from pollypm.plugin_api.v1 import Capability as Cap, PollyPMPlugin

    sentinel_plugin = PollyPMPlugin(
        name="ep_test",
        capabilities=(Cap(kind="provider", name="ep_test"),),
    )

    class FakeEntryPoint:
        name = "ep_test"

        def load(self):
            return sentinel_plugin

    def fake_entry_points(*args, **kwargs):
        if kwargs.get("group") == "pollypm.plugins":
            return [FakeEntryPoint()]
        return []

    import importlib.metadata as im
    monkeypatch.setattr(im, "entry_points", fake_entry_points)

    host = ExtensionHost(tmp_path)
    plugins = host.plugins()
    assert "ep_test" in plugins
    assert host.plugin_source("ep_test") == "entry_point"
    assert plugins["ep_test"] is sentinel_plugin


def test_entry_point_plugin_with_wrong_api_version_skipped(monkeypatch, tmp_path: Path) -> None:
    from pollypm.plugin_api.v1 import PollyPMPlugin

    bad = PollyPMPlugin(name="ep_bad", api_version="99")

    class FakeEP:
        name = "ep_bad"
        def load(self): return bad

    def fake_entry_points(*args, **kwargs):
        if kwargs.get("group") == "pollypm.plugins":
            return [FakeEP()]
        return []

    import importlib.metadata as im
    monkeypatch.setattr(im, "entry_points", fake_entry_points)

    host = ExtensionHost(tmp_path)
    assert "ep_bad" not in host.plugins()
    assert any("API version 99" in e for e in host.errors)


def test_user_plugin_can_override_builtin(monkeypatch, tmp_path: Path) -> None:
    """A plugin in the user-global directory with the same name as a
    built-in supersedes the built-in (later source wins)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    user_plugin = fake_home / ".pollypm" / "plugins" / "claude"
    _write_structured_plugin(
        user_plugin,
        name="claude",
        manifest_extras='[[capabilities]]\nkind = "provider"\nname = "claude"\n',
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "from pollypm.providers.codex import CodexAdapter\n"
            "plugin = PollyPMPlugin(name='claude', providers={'claude': CodexAdapter})\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    # The user-global "claude" plugin registers CodexAdapter under name
    # 'claude', so resolving 'claude' gives a codex-named provider.
    provider = host.get_provider("claude")
    assert provider.name == "codex"
    assert host.plugin_source("claude") == "user"
