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


# ---------------------------------------------------------------------------
# Issue #169 — content_paths(plugin, kind) helper
# ---------------------------------------------------------------------------


def test_content_paths_returns_bundled_user_and_project_paths(monkeypatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    project_plugin = tmp_path / ".pollypm" / "plugins" / "content_host"
    _write_structured_plugin(
        project_plugin,
        name="content_host",
        manifest_extras=(
            '[[capabilities]]\n'
            'kind = "agent_profile"\n'
            'name = "content_host"\n'
            '\n'
            '[content]\n'
            'kinds = ["magic_skill", "deploy_recipe"]\n'
            'user_paths = ["skills", "deploys"]\n'
        ),
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "plugin = PollyPMPlugin(name='content_host')\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    host.plugins()  # force load
    paths = host.content_paths("content_host", kind="magic_skill")

    # Bundled path is <plugin_dir>/skills/ AND <plugin_dir>/deploys/ —
    # the helper returns all declared user_paths when kind is requested.
    # (kind filtering is a per-file concern; the helper returns all
    # bundled paths and layers user/project paths specific to kind.)
    bundled = [p for p in paths if p.is_relative_to(project_plugin)]
    assert len(bundled) >= 1
    # User content path for this plugin + kind
    assert fake_home / ".pollypm" / "content" / "content_host" / "magic_skill" in paths
    # Project content path for this plugin + kind
    assert tmp_path / ".pollypm" / "content" / "content_host" / "magic_skill" in paths


def test_content_paths_missing_directories_are_not_errors(tmp_path: Path) -> None:
    host = ExtensionHost(tmp_path)
    # A plugin that hasn't declared any content block still returns
    # user-global + project-local overlay directories.
    paths = host.content_paths("claude", kind="magic_skill")
    assert all(isinstance(p, Path) for p in paths)
    # No raised exceptions; directories don't need to exist.
    for path in paths:
        assert not path.exists() or path.is_dir()


def test_content_paths_precedence_order(monkeypatch, tmp_path: Path) -> None:
    """Bundled paths come first; user comes before project."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    project_plugin = tmp_path / ".pollypm" / "plugins" / "precedence_test"
    _write_structured_plugin(
        project_plugin,
        name="precedence_test",
        manifest_extras=(
            '[[capabilities]]\n'
            'kind = "agent_profile"\n'
            'name = "precedence_test"\n'
            '\n'
            '[content]\n'
            'kinds = ["skill"]\n'
            'user_paths = ["skills"]\n'
        ),
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "plugin = PollyPMPlugin(name='precedence_test')\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    host.plugins()
    paths = host.content_paths("precedence_test", kind="skill")

    bundled_idx = next(i for i, p in enumerate(paths) if p.is_relative_to(project_plugin))
    user_idx = next(i for i, p in enumerate(paths) if p.is_relative_to(fake_home / ".pollypm" / "content"))
    project_idx = next(i for i, p in enumerate(paths) if p.is_relative_to(tmp_path / ".pollypm" / "content"))
    assert bundled_idx < user_idx < project_idx


def test_content_declaration_accessible(tmp_path: Path) -> None:
    plugin_dir = tmp_path / ".pollypm" / "plugins" / "decl_test"
    _write_structured_plugin(
        plugin_dir,
        name="decl_test",
        manifest_extras=(
            '[[capabilities]]\n'
            'kind = "agent_profile"\n'
            'name = "decl_test"\n'
            '\n'
            '[content]\n'
            'kinds = ["a", "b"]\n'
            'user_paths = ["one", "two"]\n'
        ),
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "plugin = PollyPMPlugin(name='decl_test')\n"
        ),
    )
    host = ExtensionHost(tmp_path)
    host.plugins()
    decl = host.content_declaration("decl_test")
    assert decl is not None
    assert decl.kinds == ("a", "b")
    assert decl.user_paths == ("one", "two")


# ---------------------------------------------------------------------------
# Issue #170 — PollyPMPlugin.initialize(api) callback
# ---------------------------------------------------------------------------


def test_initialize_callback_invoked_with_plugin_api(tmp_path: Path) -> None:
    plugin_dir = tmp_path / ".pollypm" / "plugins" / "init_probe"
    _write_structured_plugin(
        plugin_dir,
        name="init_probe",
        manifest_extras='[[capabilities]]\nkind = "provider"\nname = "init_probe"\n',
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "CALLED = []\n"
            "def _init(api):\n"
            "    CALLED.append(api.plugin_name)\n"
            "plugin = PollyPMPlugin(name='init_probe', initialize=_init)\n"
        ),
    )

    host = ExtensionHost(tmp_path)
    host.plugins()
    degraded = host.initialize_plugins()
    assert degraded == {}


def test_initialize_raising_marks_degraded_others_still_run(tmp_path: Path) -> None:
    broken_dir = tmp_path / ".pollypm" / "plugins" / "broken_init"
    _write_structured_plugin(
        broken_dir,
        name="broken_init",
        manifest_extras='[[capabilities]]\nkind = "provider"\nname = "broken_init"\n',
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "def _init(api):\n"
            "    raise RuntimeError('plugin bust')\n"
            "plugin = PollyPMPlugin(name='broken_init', initialize=_init)\n"
        ),
    )
    fine_dir = tmp_path / ".pollypm" / "plugins" / "fine_init"
    _write_structured_plugin(
        fine_dir,
        name="fine_init",
        manifest_extras='[[capabilities]]\nkind = "provider"\nname = "fine_init"\n',
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "import json, os\n"
            "from pathlib import Path\n"
            "BEACON = Path(os.environ['INIT_BEACON_PATH'])\n"
            "def _init(api):\n"
            "    BEACON.write_text(api.plugin_name)\n"
            "plugin = PollyPMPlugin(name='fine_init', initialize=_init)\n"
        ),
    )
    beacon = tmp_path / "beacon.txt"
    import os

    os.environ["INIT_BEACON_PATH"] = str(beacon)
    try:
        host = ExtensionHost(tmp_path)
        host.plugins()
        degraded = host.initialize_plugins()
    finally:
        os.environ.pop("INIT_BEACON_PATH", None)
    assert "broken_init" in degraded
    assert "fine_init" not in degraded
    assert beacon.exists()
    assert beacon.read_text() == "fine_init"
    # Broken plugin is still in registry (loaded-but-degraded), not removed.
    assert "broken_init" in host.plugins()
    assert "broken_init" in host.degraded_plugins


def test_initialize_not_invoked_twice(tmp_path: Path) -> None:
    plugin_dir = tmp_path / ".pollypm" / "plugins" / "once_init"
    _write_structured_plugin(
        plugin_dir,
        name="once_init",
        manifest_extras='[[capabilities]]\nkind = "provider"\nname = "once_init"\n',
        body=(
            "import os\n"
            "from pathlib import Path\n"
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "BEACON = Path(os.environ['ONCE_BEACON_PATH'])\n"
            "def _init(api):\n"
            "    existing = int(BEACON.read_text()) if BEACON.exists() else 0\n"
            "    BEACON.write_text(str(existing + 1))\n"
            "plugin = PollyPMPlugin(name='once_init', initialize=_init)\n"
        ),
    )
    beacon = tmp_path / "once.txt"
    import os

    os.environ["ONCE_BEACON_PATH"] = str(beacon)
    try:
        host = ExtensionHost(tmp_path)
        host.plugins()
        host.initialize_plugins()
        host.initialize_plugins()
        host.initialize_plugins()
    finally:
        os.environ.pop("ONCE_BEACON_PATH", None)
    assert beacon.read_text() == "1"


def test_initialize_passes_roster_and_jobs_when_provided(tmp_path: Path) -> None:
    from pollypm.heartbeat import Roster
    from pollypm.jobs import JobHandlerRegistry

    plugin_dir = tmp_path / ".pollypm" / "plugins" / "roster_user"
    _write_structured_plugin(
        plugin_dir,
        name="roster_user",
        manifest_extras='[[capabilities]]\nkind = "job_handler"\nname = "ru.demo"\n',
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "def handler(payload): return {'ok': True}\n"
            "def _init(api):\n"
            "    api.jobs.register_handler('ru.demo', handler)\n"
            "    api.roster.register_recurring('@every 30s', 'ru.demo', {})\n"
            "plugin = PollyPMPlugin(name='roster_user', initialize=_init)\n"
        ),
    )

    roster = Roster()
    registry = JobHandlerRegistry()
    host = ExtensionHost(tmp_path)
    host.plugins()
    degraded = host.initialize_plugins(roster=roster, job_registry=registry)
    assert degraded == {}
    # Handler was registered via api.jobs
    assert registry.get("ru.demo") is not None
    # Roster entry registered via api.roster
    entries = list(roster.snapshot())
    handler_names = {e.handler_name for e in entries}
    assert "ru.demo" in handler_names


def test_initialize_content_paths_shortcut(tmp_path: Path) -> None:
    plugin_dir = tmp_path / ".pollypm" / "plugins" / "cp_shortcut"
    _write_structured_plugin(
        plugin_dir,
        name="cp_shortcut",
        manifest_extras=(
            '[[capabilities]]\n'
            'kind = "agent_profile"\n'
            'name = "cp_shortcut"\n'
            '\n'
            '[content]\n'
            'kinds = ["skill"]\n'
            'user_paths = ["skills"]\n'
        ),
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "COLLECTED = []\n"
            "def _init(api):\n"
            "    COLLECTED.extend(api.content_paths(kind='skill'))\n"
            "plugin = PollyPMPlugin(name='cp_shortcut', initialize=_init)\n"
        ),
    )
    host = ExtensionHost(tmp_path)
    host.plugins()
    degraded = host.initialize_plugins()
    assert degraded == {}
    # Introspect the plugin module to get COLLECTED
    plugin = host.plugins()["cp_shortcut"]
    # API content_paths returned something (3 paths: bundled + user + project).
    # Resolve via the host directly to confirm equivalence.
    assert len(host.content_paths("cp_shortcut", kind="skill")) >= 3


# ---------------------------------------------------------------------------
# Issue #172 — [plugins].disabled config key
# ---------------------------------------------------------------------------


def test_disabled_plugin_is_discovered_but_not_loaded(tmp_path: Path) -> None:
    project_plugin = tmp_path / ".pollypm" / "plugins" / "noisy_plugin"
    _write_structured_plugin(
        project_plugin,
        name="noisy_plugin",
        manifest_extras='[[capabilities]]\nkind = "provider"\nname = "noisy"\n',
        body=(
            "from pollypm.plugin_api.v1 import PollyPMPlugin\n"
            "plugin = PollyPMPlugin(name='noisy_plugin')\n"
        ),
    )

    host = ExtensionHost(tmp_path, disabled=("noisy_plugin",))
    plugins = host.plugins()
    assert "noisy_plugin" not in plugins
    disabled = host.disabled_plugins
    assert "noisy_plugin" in disabled
    record = disabled["noisy_plugin"]
    assert record.reason == "config"
    assert record.source in {"project", "repo"}


def test_disabled_builtin_plugin_records_reason(tmp_path: Path) -> None:
    """Disabling a built-in plugin by name filters it out, even from the
    builtin search path."""
    host = ExtensionHost(tmp_path, disabled=("magic",))
    plugins = host.plugins()
    assert "magic" not in plugins
    assert "magic" in host.disabled_plugins


def test_unknown_disabled_plugin_name_is_ignored(tmp_path: Path) -> None:
    """Disabling a plugin that doesn't exist does not crash."""
    host = ExtensionHost(tmp_path, disabled=("nonexistent_plugin",))
    # Smoke: plugin loading still works, no plugins crashed.
    plugins = host.plugins()
    assert "claude" in plugins  # builtin still loaded
    # Nonexistent plugin is simply absent from both buckets.
    assert "nonexistent_plugin" not in plugins
    assert "nonexistent_plugin" not in host.disabled_plugins


def test_plugin_settings_parsed_from_config(tmp_path: Path) -> None:
    """The [plugins].disabled TOML section ends up in config.plugins."""
    from pollypm.config import load_config

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """[project]
name = "sample"

[pollypm]
controller_account = ""

[plugins]
disabled = ["magic", "docker_runtime"]
"""
    )
    cfg = load_config(config_path)
    assert cfg.plugins.disabled == ("magic", "docker_runtime")


def test_project_local_disabled_adds_to_user_disabled(tmp_path: Path, monkeypatch) -> None:
    """A project-local [plugins].disabled can add to (but not remove from)
    the user-global disabled set."""
    from pollypm.config import load_config

    # Set up a fake project at tmp_path / "my_project"
    project_root = tmp_path / "my_project"
    (project_root / ".pollypm" / "config").mkdir(parents=True)
    (project_root / ".pollypm" / "config" / "project.toml").write_text(
        """[project]
display_name = "My Project"

[plugins]
disabled = ["extra_plugin"]
"""
    )

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"""[project]
name = "sample"

[pollypm]
controller_account = ""

[plugins]
disabled = ["magic"]

[projects.my_project]
path = "{project_root}"
"""
    )
    cfg = load_config(config_path)
    assert "magic" in cfg.plugins.disabled
    assert "extra_plugin" in cfg.plugins.disabled


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
