"""Workspace-scope DB resolution — collapsed layout contract (#339).

Issue #339 collapsed PollyPM's on-disk layout to exactly two SQLite files:

* ``~/.pollypm/state.db`` — user scope
* ``<workspace_root>/.pollypm/state.db`` — workspace scope

``pollypm.work.cli._resolve_db_path`` must now return exactly one of:

1. The caller's explicit ``--db`` override unchanged;
2. ``<workspace_root>/.pollypm/state.db`` resolved from the loaded config;
3. A cwd-relative fallback when the config is not loadable.

The old per-project branch (``<project>/.pollypm/work/work.db``) is gone —
project isolation is row-level via the ``scope`` column, not per-file.

Run with an isolated HOME so Supervisor-adjacent imports don't touch the
real ``~/.pollypm/``:

    HOME=/tmp/pytest-agent-workspace-scope uv run pytest \
        tests/test_workspace_scope_resolution.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pollypm.work.cli import _resolve_db_path


_DEFAULT_DB = ".pollypm/state.db"


def _write_config(
    workspace_root: Path,
    project_path: Path,
    config_path: Path,
) -> None:
    config_path.write_text(
        "[project]\n"
        'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{workspace_root}"\n'
        "\n"
        "[projects.demo]\n"
        'key = "demo"\n'
        'name = "Demo"\n'
        f'path = "{project_path}"\n'
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project_path = workspace_root / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_config(workspace_root, project_path, config_path)

    # Pin the no-arg load_config() call inside _resolve_db_path to read
    # our test config instead of ~/.pollypm/pollypm.toml.
    from pollypm import config as config_module
    real_load = config_module.load_config
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda path=None: real_load(config_path if path is None else path),
    )

    return {
        "workspace_root": workspace_root,
        "project_path": project_path,
        "workspace_db": workspace_root / ".pollypm" / "state.db",
    }


class TestResolveDbPathDefaults:
    """Default ``--db .pollypm/state.db`` always routes to workspace scope."""

    def test_default_db_without_project_returns_workspace_root(self, env):
        resolved = _resolve_db_path(_DEFAULT_DB)
        assert resolved == env["workspace_db"]

    def test_default_db_with_registered_project_still_workspace_root(self, env):
        # Post-#339 the project filter no longer routes to a per-project
        # file — it's a row-level filter now. The resolver returns the
        # same workspace-scope DB regardless of the project argument.
        resolved = _resolve_db_path(_DEFAULT_DB, project="demo")
        assert resolved == env["workspace_db"]

    def test_default_db_with_unregistered_project_still_workspace_root(self, env):
        resolved = _resolve_db_path(_DEFAULT_DB, project="does-not-exist")
        assert resolved == env["workspace_db"]

    def test_default_db_with_hyphenated_project_still_workspace_root(self, env):
        # Hyphen/underscore normalization was part of the old per-project
        # branch. It should no longer affect which file is opened.
        resolved = _resolve_db_path(_DEFAULT_DB, project="some-project")
        assert resolved == env["workspace_db"]


class TestResolveDbPathExplicitOverride:
    """Explicit ``--db`` overrides always win — test/CI escape hatch."""

    def test_explicit_override_returns_given_path(self, env, tmp_path):
        custom = tmp_path / "custom" / "state.db"
        resolved = _resolve_db_path(str(custom))
        assert resolved == custom

    def test_explicit_override_with_project_ignores_project(self, env, tmp_path):
        custom = tmp_path / "custom" / "state.db"
        resolved = _resolve_db_path(str(custom), project="demo")
        assert resolved == custom

    def test_explicit_override_creates_parent_directory(self, env, tmp_path):
        custom = tmp_path / "fresh" / "subdir" / "state.db"
        assert not custom.parent.exists()
        _resolve_db_path(str(custom))
        assert custom.parent.is_dir()


class TestResolveDbPathNoConfigFallback:
    """When no config can be loaded, fall back to the literal default."""

    def test_fallback_returns_relative_default_when_load_fails(
        self, monkeypatch, tmp_path
    ):
        # Force load_config() to raise so the resolver can't find a
        # workspace_root. It should drop to the cwd-relative default.
        from pollypm import config as config_module

        def _boom(path=None):
            raise RuntimeError("no config available in this test")

        monkeypatch.setattr(config_module, "load_config", _boom)
        monkeypatch.chdir(tmp_path)

        resolved = _resolve_db_path(_DEFAULT_DB)
        # The fallback path is cwd-relative and did not crash.
        assert resolved.name == "state.db"
        assert resolved.parent.name == ".pollypm"


class TestLegacyPathNotUsed:
    """The old per-project ``.pollypm/work/work.db`` path is never returned."""

    def test_workspace_db_is_not_nested_under_work(self, env):
        resolved = _resolve_db_path(_DEFAULT_DB, project="demo")
        assert "work.db" not in resolved.name
        assert "work" not in [p.name for p in resolved.parents]

    def test_resolved_path_ends_at_workspace_scope(self, env):
        resolved = _resolve_db_path(_DEFAULT_DB, project="demo")
        # .../dev/.pollypm/state.db — parent is .pollypm, grandparent is
        # the workspace root.
        assert resolved.parent.name == ".pollypm"
        assert resolved.parent.parent == env["workspace_root"]
