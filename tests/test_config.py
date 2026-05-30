import ast
from pathlib import Path

from pollypm.agent_profiles.defaults import heartbeat_prompt, polly_prompt
from pollypm.config import (
    load_config,
    project_config_path,
    render_example_config,
    resolve_config_path,
    write_config,
    write_example_config,
)
from pollypm.models import (
    AccountConfig,
    KnownProject,
    ModelAssignment,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)


def test_config_does_not_import_core_agent_profiles_plugin() -> None:
    source_path = Path(__file__).resolve().parents[1] / "src" / "pollypm" / "config.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("pollypm.plugins_builtin.core_agent_profiles"):
                offenders.append(f"line {node.lineno}: from {module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("pollypm.plugins_builtin.core_agent_profiles"):
                    offenders.append(f"line {node.lineno}: import {alias.name}")

    assert offenders == []


def test_load_example_config(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(render_example_config())

    config = load_config(config_path)

    assert config.project.name == "PollyPM"
    assert config.project.tmux_session == "pollypm"
    assert config.project.workspace_root == Path.home() / "dev"
    assert config.pollypm.controller_account == "codex_primary"
    assert config.pollypm.open_permissions_by_default is True
    assert config.pollypm.failover_enabled is True
    assert config.pollypm.failover_accounts == ["claude_primary"]
    assert config.pollypm.failover_usage_threshold_pct == 85
    assert config.pollypm.lease_timeout_minutes == 30
    assert config.pollypm.role_assignments["operator_pm"].alias == "opus-4.8"
    assert config.pollypm.role_assignments["reviewer"].alias == "opus-4.8"
    assert config.memory.backend == "file"
    assert set(config.accounts) == {"codex_primary", "claude_primary"}
    assert set(config.sessions) == {"heartbeat", "operator"}
    assert config.projects == {}
    assert config.sessions["operator"].provider.value == "codex"
    assert config.sessions["heartbeat"].provider.value == "codex"
    assert "[projects." not in config_path.read_text()


def test_example_config_documents_storage_block(tmp_path: Path) -> None:
    """``pm example-config`` carries a commented ``[storage]`` block (#1751).

    The sqlite backend was removed in the #1737 cutover, so first-run
    operators need a visible hint that pg is required + the
    ``pm bootstrap-pg`` lead-in. Keeping the block commented out
    preserves round-trip parse (the parser still defaults the URL).
    """
    rendered = render_example_config()
    # The hint header must call out that pg is required.
    assert "[storage]" in rendered
    assert "Postgres" in rendered
    assert "pm bootstrap-pg" in rendered
    # The example DSN line is commented out so the parser falls
    # through to the documented default.
    assert '# url = "postgresql://localhost:5432/pollypm"' in rendered
    # And the rendered config still parses cleanly end-to-end.
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(rendered)
    config = load_config(config_path)
    assert config.storage.backend == "postgres"


def test_failover_usage_threshold_round_trips(tmp_path: Path) -> None:
    config = PollyPMConfig(
        project=ProjectSettings(root_dir=tmp_path),
        pollypm=PollyPMSettings(
            controller_account="claude_primary",
            failover_usage_threshold_pct=72,
        ),
        accounts={
            "claude_primary": AccountConfig(
                name="claude_primary",
                provider=ProviderKind.CLAUDE,
            )
        },
        sessions={},
        projects={},
    )
    config_path = tmp_path / "pollypm.toml"

    write_config(config, config_path)
    loaded = load_config(config_path)

    assert loaded.pollypm.failover_usage_threshold_pct == 72
    assert "failover_usage_threshold_pct = 72" in config_path.read_text()


def test_write_example_config_uses_fresh_install_session_name(tmp_path: Path) -> None:
    config_path = tmp_path / ".pollypm" / "pollypm.toml"

    write_example_config(config_path)
    config = load_config(config_path)

    assert config.project.tmux_session.startswith("pollypm-")
    assert config.project.tmux_session != "pollypm"


def test_resolve_config_path_returns_global_config(monkeypatch, tmp_path: Path) -> None:
    """resolve_config_path always returns the global config path.

    Project-specific overrides are loaded separately via
    _merge_project_local_config, not by walking up the directory tree.
    """
    project_root = tmp_path / "repo"
    nested = project_root / "src" / "pkg"
    nested.mkdir(parents=True)
    config_path = project_root / "pollypm.toml"
    config_path.write_text("[project]\nname = \"PollyPM\"\n")

    monkeypatch.chdir(nested)

    from pollypm.config import DEFAULT_CONFIG_PATH
    assert resolve_config_path() == DEFAULT_CONFIG_PATH.resolve()


def test_load_config_normalizes_control_prompts(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "pollypm"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."
prompt = "You are PollyPM session 0, remain as a true interactive CLI session."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."
prompt = "You are Polly, the PollyPM project manager, in session 1."

[sessions.worker_pollypm]
role = "worker"
provider = "claude"
account = "claude_primary"
cwd = "."
prompt = "Read the PollyPM issue queue, start with the highest-leverage open issue."

[projects.pollypm]
path = "."
name = "pollypm"
"""
    )

    config = load_config(config_path)

    assert config.project.name == "PollyPM"
    assert config.project.tmux_session == "pollypm"
    assert config.projects["pollypm"].name == "PollyPM"
    assert config.sessions["heartbeat"].prompt == heartbeat_prompt()
    assert config.sessions["operator"].prompt == polly_prompt()
    assert config.sessions["worker_pollypm"].prompt == "Read the PollyPM issue queue, start with the highest-leverage open issue."


def test_control_sessions_use_workspace_root_for_dot_cwd(tmp_path: Path) -> None:
    """Control sessions with cwd='.' should resolve to workspace_root, not base_dir."""
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "pollypm"
tmux_session = "pollypm"
workspace_root = "/Users/test/dev"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.worker]
role = "worker"
provider = "claude"
account = "claude_primary"
cwd = "."
"""
    )
    config = load_config(config_path)
    # Control sessions should use workspace_root
    assert config.sessions["heartbeat"].cwd == Path("/Users/test/dev")
    assert config.sessions["operator"].cwd == Path("/Users/test/dev")
    # Workers should use base_dir (config parent)
    assert config.sessions["worker"].cwd == tmp_path


def test_per_project_config_defaults_base_dir_into_project(tmp_path: Path) -> None:
    """#763: per-project configs at ``<root>/.pollypm/config/project.toml``
    must default ``base_dir`` into the project's own ``.pollypm/`` —
    never into the shared ``~/.pollypm/``. Previously this leaked, so
    control-prompts / state.db / logs from every project collided in
    the user's home directory.
    """
    project_root = tmp_path / "my_project"
    config_dir = project_root / ".pollypm" / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "project.toml"
    # Minimal body — this is exactly what a project scaffold writes.
    config_path.write_text('[project]\ndisplay_name = "My Project"\n')

    config = load_config(config_path)

    assert config.project.root_dir == project_root
    assert config.project.base_dir == project_root / ".pollypm"
    assert config.project.state_db == project_root / ".pollypm" / "state.db"
    assert config.project.logs_dir == project_root / ".pollypm" / "logs"
    # The control-prompts directory is computed from base_dir; the fix
    # makes this stay inside the project.
    assert (
        config.project.base_dir / "control-prompts"
        == project_root / ".pollypm" / "control-prompts"
    )


def test_load_config_flattens_legacy_global_dot_pollypm_paths(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".pollypm"
    config_dir.mkdir()
    config_path = config_dir / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "pollypm"
tmux_session = "pollypm"
base_dir = ".pollypm"
logs_dir = ".pollypm/logs"
snapshots_dir = ".pollypm/snapshots"
state_db = ".pollypm/state.db"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."
"""
    )

    config = load_config(config_path)

    assert config.project.root_dir == config_dir
    assert config.project.base_dir == config_dir
    assert config.project.logs_dir == config_dir / "logs"
    assert config.project.snapshots_dir == config_dir / "snapshots"
    assert config.project.state_db == config_dir / "state.db"
    assert config.accounts["claude_primary"].home == (
        config_dir / "homes" / "claude_primary"
    )


def test_write_config_flattens_global_dot_pollypm_paths(tmp_path: Path) -> None:
    config_dir = tmp_path / ".pollypm"
    config = PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=config_dir,
            base_dir=config_dir / ".pollypm",
            logs_dir=config_dir / ".pollypm" / "logs",
            snapshots_dir=config_dir / ".pollypm" / "snapshots",
            state_db=config_dir / ".pollypm" / "state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_primary"),
        accounts={
            "claude_primary": AccountConfig(
                name="claude_primary",
                provider=ProviderKind.CLAUDE,
                home=config_dir / ".pollypm" / "homes" / "claude_primary",
            )
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
                cwd=config_dir,
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
                cwd=config_dir,
            ),
        },
        projects={},
    )
    config_path = config_dir / "pollypm.toml"

    write_config(config, config_path, force=True)

    rendered = config_path.read_text()
    assert 'base_dir = "."' in rendered
    assert 'logs_dir = "logs"' in rendered
    assert 'snapshots_dir = "snapshots"' in rendered
    assert 'state_db = "state.db"' in rendered
    assert 'home = "homes/claude_primary"' in rendered


def test_load_config_parses_custom_lease_timeout_minutes(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "pollypm"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"
lease_timeout_minutes = 5

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[projects.pollypm]
path = "."
name = "pollypm"
"""
    )

    config = load_config(config_path)

    assert config.pollypm.lease_timeout_minutes == 5


def test_load_config_merges_project_local_worker_sessions(tmp_path: Path) -> None:
    project_root = tmp_path / "wire"
    project_root.mkdir()
    (project_root / ".pollypm" / "config").mkdir(parents=True)
    (project_root / ".pollypm" / "config" / "project.toml").write_text(
        """
[project]
display_name = "Wire"
persona_name = "Wren"

[sessions.worker_wire]
role = "worker"
provider = "claude"
account = "claude_primary"
cwd = "."
prompt = "Implement issue #1."
"""
    )
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "PollyPM"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[projects.wire]
path = "wire"
"""
    )

    config = load_config(config_path)

    assert config.projects["wire"].name == "Wire"
    assert config.projects["wire"].persona_name == "Wren"
    assert config.sessions["worker_wire"].project == "wire"
    assert config.sessions["worker_wire"].cwd == project_root


def test_load_config_reads_project_max_parallel_workers(tmp_path: Path) -> None:
    """#1737 — ``max_parallel_workers`` lands on KnownProject."""
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "PollyPM"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[projects.samblog]
path = "samblog"
max_parallel_workers = 3
"""
    )
    (tmp_path / "samblog").mkdir()

    config = load_config(config_path)

    assert config.projects["samblog"].max_parallel_workers == 3
    # Legacy field unaffected when only the new field is set.
    assert config.projects["samblog"].max_concurrent_workers is None


def test_render_config_round_trips_project_max_parallel_workers(
    tmp_path: Path,
) -> None:
    """#1885 — TOML emitter preserves per-project worker-cap overrides.

    Pre-fix, ``_render_global_config`` only carried name/persona/
    kind/tracked; ``auto_claim``, ``max_concurrent_workers``, and
    ``max_parallel_workers`` were silently dropped. Any code path
    that round-trips (``write_config``, the toml patcher) erased an
    operator-set cap, restoring the runtime default.
    """
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "PollyPM"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[projects.samblog]
path = "samblog"
auto_claim = true
max_parallel_workers = 7
max_concurrent_workers = 4
"""
    )
    (tmp_path / "samblog").mkdir()

    config = load_config(config_path)
    assert config.projects["samblog"].max_parallel_workers == 7
    assert config.projects["samblog"].max_concurrent_workers == 4
    assert config.projects["samblog"].auto_claim is True

    # Round-trip: write_config -> reload must preserve every override.
    output_path = tmp_path / "out.toml"
    write_config(config, output_path, force=True)
    rendered = output_path.read_text()
    assert "max_parallel_workers = 7" in rendered, rendered
    assert "max_concurrent_workers = 4" in rendered, rendered
    assert "auto_claim = true" in rendered, rendered

    reloaded = load_config(output_path)
    assert reloaded.projects["samblog"].max_parallel_workers == 7
    assert reloaded.projects["samblog"].max_concurrent_workers == 4
    assert reloaded.projects["samblog"].auto_claim is True


def test_render_config_omits_project_overrides_when_unset(
    tmp_path: Path,
) -> None:
    """A project with no overrides round-trips clean (no spurious keys).

    Guards against the over-eager emitter pattern where ``None``
    serialises as ``"None"`` or ``0`` and re-loads as a real value.
    """
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "PollyPM"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[projects.samblog]
path = "samblog"
"""
    )
    (tmp_path / "samblog").mkdir()
    config = load_config(config_path)
    output_path = tmp_path / "out.toml"
    write_config(config, output_path, force=True)
    rendered = output_path.read_text()
    assert "max_parallel_workers" not in rendered, rendered
    assert "max_concurrent_workers" not in rendered, rendered
    assert "auto_claim" not in rendered, rendered


def test_load_config_reads_project_local_planner_enforce_plan(tmp_path: Path) -> None:
    """Per-project ``[planner].enforce_plan`` lands on KnownProject."""
    project_root = tmp_path / "wire"
    project_root.mkdir()
    (project_root / ".pollypm" / "config").mkdir(parents=True)
    (project_root / ".pollypm" / "config" / "project.toml").write_text(
        """
[project]
display_name = "Wire"

[planner]
enforce_plan = false
"""
    )
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "PollyPM"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[projects.wire]
path = "wire"
"""
    )

    config = load_config(config_path)

    assert config.projects["wire"].enforce_plan is False
    # Global default unchanged — only the per-project override flips.
    assert config.planner.enforce_plan is True


def test_load_config_invalidates_cache_on_project_local_edit(tmp_path: Path) -> None:
    """Editing a project-local config invalidates the load_config cache.

    Without this, long-running processes (cockpit, sweeper) keep
    serving the stale merged config until something touches the global
    pollypm.toml. Sam's media-project ``[planner].enforce_plan = false``
    edit hit this exact path — the global mtime was unchanged, so the
    sweeper kept emitting plan_missing alerts until restart.
    """
    project_root = tmp_path / "wire"
    project_root.mkdir()
    local_dir = project_root / ".pollypm" / "config"
    local_dir.mkdir(parents=True)
    local_path = local_dir / "project.toml"
    local_path.write_text(
        """
[project]
display_name = "Wire"

[planner]
enforce_plan = true
"""
    )
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "PollyPM"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[projects.wire]
path = "wire"
"""
    )

    first = load_config(config_path)
    assert first.projects["wire"].enforce_plan is True

    # Bump the project-local file's mtime by writing the override flip.
    # Use a future mtime so the change is observable even on filesystems
    # with second-granularity stat() (the test would otherwise race).
    local_path.write_text(
        """
[project]
display_name = "Wire"

[planner]
enforce_plan = false
"""
    )
    import os
    future = local_path.stat().st_mtime + 5
    os.utime(local_path, (future, future))

    second = load_config(config_path)
    assert second.projects["wire"].enforce_plan is False, (
        "load_config returned a stale cached config — the per-project "
        "edit was not picked up"
    )


def test_write_config_splits_worker_sessions_into_project_local_files(tmp_path: Path) -> None:
    project_root = tmp_path / "wire"
    project_root.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_primary"),
        accounts={
            "claude_primary": AccountConfig(
                name="claude_primary",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm" / "homes" / "claude_primary",
            )
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
                cwd=tmp_path,
            ),
            "worker_wire": SessionConfig(
                name="worker_wire",
                role="worker",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
                cwd=project_root,
                project="wire",
                prompt="Implement issue #1.",
            ),
        },
        projects={
            "wire": KnownProject(
                key="wire",
                path=project_root,
                name="Wire",
                persona_name="Wren",
            )
        },
    )

    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

    global_text = config_path.read_text()
    local_text = project_config_path(project_root).read_text()

    assert "[sessions.worker_wire]" not in global_text
    assert '[projects.wire]' in global_text
    assert 'persona_name = "Wren"' in global_text
    assert '[project]' in local_text
    assert 'persona_name = "Wren"' in local_text
    assert '[sessions.worker_wire]' in local_text

    loaded = load_config(config_path)
    assert "worker_wire" in loaded.sessions
    assert loaded.sessions["worker_wire"].project == "wire"
    assert loaded.projects["wire"].persona_name == "Wren"


def _minimal_config_text(*, release_channel_line: str = "") -> str:
    """Minimal config TOML with a single account + operator session.

    Used by the release_channel tests so they focus on parsing the
    channel field without cross-referencing other config surfaces.
    """
    return (
        "[project]\n"
        'name = "pollypm"\n'
        'tmux_session = "pollypm"\n'
        "\n"
        "[pollypm]\n"
        'controller_account = "claude_primary"\n'
        + (f"{release_channel_line}\n" if release_channel_line else "")
        + "\n"
        "[accounts.claude_primary]\n"
        'provider = "claude"\n'
        'home = ".pollypm/homes/claude_primary"\n'
        "\n"
        "[sessions.operator]\n"
        'role = "operator-pm"\n'
        'provider = "claude"\n'
        'account = "claude_primary"\n'
        'cwd = "."\n'
        "\n"
        "[projects.pollypm]\n"
        'path = "."\n'
        'name = "pollypm"\n'
    )


def test_release_channel_default_is_stable(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(_minimal_config_text())
    config = load_config(config_path)
    assert config.pollypm.release_channel == "stable"


def test_release_channel_beta_round_trips(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        _minimal_config_text(release_channel_line='release_channel = "beta"')
    )

    config = load_config(config_path)
    assert config.pollypm.release_channel == "beta"

    out_path = tmp_path / "rewritten.toml"
    write_config(config, out_path, force=True)
    rendered = out_path.read_text()
    assert 'release_channel = "beta"' in rendered
    reloaded = load_config(out_path)
    assert reloaded.pollypm.release_channel == "beta"


def test_release_channel_invalid_falls_back_to_stable(
    tmp_path: Path, caplog
) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        _minimal_config_text(release_channel_line='release_channel = "bogus"')
    )
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="pollypm.config"):
        config = load_config(config_path)

    assert config.pollypm.release_channel == "stable"
    assert any(
        "release_channel" in record.getMessage() and "bogus" in record.getMessage()
        for record in caplog.records
    )


def test_role_assignments_round_trip_global_and_project_scope(tmp_path: Path) -> None:
    project_root = tmp_path / "wire"
    project_root.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm" / "logs",
            snapshots_dir=tmp_path / ".pollypm" / "snapshots",
            state_db=tmp_path / ".pollypm" / "state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="claude_primary",
            role_assignments={
                "operator_pm": ModelAssignment(alias="opus-4.7"),
                "architect": ModelAssignment(provider="claude", model="claude-opus-4-7"),
                "worker": ModelAssignment(alias="codex-gpt-5.4"),
                "reviewer": ModelAssignment(provider="codex", model="gpt-5.4"),
            },
        ),
        accounts={
            "claude_primary": AccountConfig(
                name="claude_primary",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm" / "homes" / "claude_primary",
            )
        },
        sessions={
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_primary",
                cwd=tmp_path,
            )
        },
        projects={
            "wire": KnownProject(
                key="wire",
                path=project_root,
                role_assignments={
                    "architect": ModelAssignment(alias="sonnet-4.6"),
                    "worker": ModelAssignment(provider="codex", model="gpt-5.4"),
                    "reviewer": ModelAssignment(alias="haiku-4.5"),
                },
            )
        },
    )

    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)

    loaded = load_config(config_path)

    assert loaded.pollypm.role_assignments == config.pollypm.role_assignments
    assert loaded.projects["wire"].role_assignments == config.projects["wire"].role_assignments


def test_invalid_role_assignments_warn_and_drop_entries(tmp_path: Path, caplog) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "pollypm"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[pollypm.roles.architect]
alias = "opus-4.7"
provider = "claude"
model = "claude-opus-4-7"

[pollypm.roles.worker]

[pollypm.roles.ghost]
alias = "haiku-4.5"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[projects.demo]
path = "demo"

[projects.demo.roles.operator_pm]
alias = "opus-4.7"
"""
    )
    (tmp_path / "demo").mkdir()

    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="pollypm.config"):
        config = load_config(config_path)

    assert config.pollypm.role_assignments == {}
    assert config.projects["demo"].role_assignments == {}
    messages = [record.getMessage() for record in caplog.records]
    assert any("pollypm.roles.architect" in message for message in messages)
    assert any("pollypm.roles.worker" in message for message in messages)
    assert any("pollypm.roles.ghost" in message for message in messages)
    assert any("projects.demo.roles.operator_pm" in message for message in messages)


def test_write_config_round_trips_non_ascii_project_name(tmp_path: Path) -> None:
    """Cycle 126 — pollypm.toml is UTF-8 by spec, but the writer used
    the locale-default encoding. A non-ASCII project ``name`` (or
    persona) round-tripped fine on a UTF-8 host but mojibaked on
    Windows CP-1252 / ``LC_ALL=C``. Pin both writer and loader to
    UTF-8 so the round-trip is portable.
    """
    config_path = tmp_path / "pollypm.toml"
    project_path = tmp_path / "café-project"
    project_path.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            name="プロジェクト",
            tmux_session="pollypm",
            workspace_root=tmp_path,
            base_dir=tmp_path,
            logs_dir=tmp_path / "logs",
            snapshots_dir=tmp_path / "snapshots",
            state_db=tmp_path / "state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_primary"),
        accounts={
            "claude_primary": AccountConfig(
                name="claude_primary",
                provider=ProviderKind.CLAUDE,
                email="user@example.com",
                home=tmp_path / "home",
            ),
        },
        sessions={},
        projects={
            "demo": KnownProject(
                key="demo",
                name="café",
                path=project_path,
                persona_name="Niño",
            ),
        },
    )
    write_config(config, config_path, force=True)
    loaded = load_config(config_path)
    assert loaded.project.name == "プロジェクト"
    assert loaded.projects["demo"].name == "café"
    assert loaded.projects["demo"].persona_name == "Niño"
    # Bytes must contain the UTF-8 encoding of "café" (c-a-f + 0xC3 0xA9).
    raw = config_path.read_bytes()
    assert b"caf\xc3\xa9" in raw


def test_load_config_auto_persist_returns_post_lock_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Concurrent disk edit during auth-token auto-persist must be visible.

    #2063 round 10 (Codex blocker): round 9 wrapped the legacy
    ``ensure_session_auth_tokens()`` write in ``config_rmw_lock`` and
    re-parsed under the lock as ``fresh``, but then only copied
    ``sessions[*].auth_token`` back into the pre-lock ``config`` and
    cached that pre-lock snapshot under the post-write mtime. If any
    config edit landed between the initial parse and the locked
    re-parse, disk kept the edit but the process cache served the
    stale pre-lock object as if it matched the latest mtime —
    subsequent ``load_config(path)`` calls returned stale config
    indefinitely.

    Reproduction (Codex's): seed a legacy session missing
    ``auth_token`` plus a project ``persona_name='Old'``. Monkeypatch
    ``ensure_session_auth_tokens`` so its FIRST invocation rewrites
    ``persona_name='New'`` to disk before returning (simulates a
    concurrent CLI/API edit racing the auto-persist). After the fix
    both the first and the second ``load_config(path)`` must observe
    the post-lock state: minted token AND the concurrent edit.
    """
    import pollypm.config as config_module
    import pollypm.session_auth as session_auth

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        """
[project]
name = "PollyPM"
tmux_session = "pollypm"

[pollypm]
controller_account = "claude_primary"

[accounts.claude_primary]
provider = "claude"
home = ".pollypm/homes/claude_primary"

[sessions.heartbeat]
role = "heartbeat-supervisor"
provider = "claude"
account = "claude_primary"
cwd = "."

[sessions.operator]
role = "operator-pm"
provider = "claude"
account = "claude_primary"
cwd = "."

[projects.myproj]
path = "myproj"
persona_name = "Old"
"""
    )

    # Clear any cache entry for this path from earlier tests / module
    # state so the load below exercises the auto-persist branch.
    config_module._config_cache.pop(config_path.resolve(), None)

    original = session_auth.ensure_session_auth_tokens
    call_count = {"n": 0}

    def patched(cfg):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Simulate a concurrent CLI edit landing between the
            # pre-lock parse and the locked re-parse: rewrite the
            # persona on disk BEFORE we mint tokens on the pre-lock
            # snapshot. The locked re-parse inside load_config will
            # then see persona='New'.
            disk = config_path.read_text()
            assert 'persona_name = "Old"' in disk
            config_path.write_text(
                disk.replace('persona_name = "Old"', 'persona_name = "New"')
            )
        return original(cfg)

    monkeypatch.setattr(session_auth, "ensure_session_auth_tokens", patched)

    loaded = config_module.load_config(config_path)

    # 1. Auto-persist worked: operator session now carries a minted token.
    assert loaded.sessions["operator"].auth_token, (
        "ensure_session_auth_tokens must have minted + persisted a token"
    )
    # 2. The concurrent persona_name edit (visible only to the locked
    #    re-parse) is reflected in the returned snapshot. Pre-fix this
    #    returned 'Old' because load_config returned the pre-lock object.
    assert loaded.projects["myproj"].persona_name == "New", (
        "load_config must return the post-lock snapshot that observed "
        "the concurrent disk edit, not the stale pre-lock parse"
    )
    # 3. A subsequent load_config call must also see the post-lock
    #    state. Pre-fix the cache held the pre-lock object under the
    #    post-write mtime, so this call kept returning 'Old' forever.
    again = config_module.load_config(config_path)
    assert again.projects["myproj"].persona_name == "New", (
        "load_config cache stored the stale pre-lock snapshot; "
        "subsequent calls return stale config indefinitely"
    )
    assert again.sessions["operator"].auth_token == loaded.sessions[
        "operator"
    ].auth_token
