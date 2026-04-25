"""Unit tests for ``pollypm.doctor`` — each check has a pass + fail case."""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from pollypm import doctor


# --------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------- #


def test_parse_version_handles_common_forms() -> None:
    assert doctor._parse_version("tmux 3.5a") == (3, 5, 0)
    assert doctor._parse_version("git version 2.43.0") == (2, 43, 0)
    assert doctor._parse_version("Python 3.13.1") == (3, 13, 1)
    assert doctor._parse_version("") is None
    assert doctor._parse_version("garbage") is None


def test_run_cmd_missing_binary_returns_marker(tmp_path: Path) -> None:
    rc, out = doctor._run_cmd(["definitely-not-a-real-binary-zzz"])
    assert rc == -1
    assert "not found" in out


def test_run_cmd_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 0.1))

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    rc, out = doctor._run_cmd(["anything"])
    assert rc == -1
    assert "timed out" in out


# --------------------------------------------------------------------- #
# System prerequisites
# --------------------------------------------------------------------- #


def test_check_python_version_passes_on_current_interpreter() -> None:
    result = doctor.check_python_version()
    # This test runs on the PollyPM dev interpreter which, by
    # pyproject.toml contract, is >= 3.13.
    assert result.passed
    assert "Python" in result.status


def test_check_python_version_fails_when_below_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_read_pyproject_required_python", lambda: (3, 99, 0))
    result = doctor.check_python_version()
    assert not result.passed
    assert "below required" in result.status
    assert "brew install" in result.fix


def test_check_tmux_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: None)
    result = doctor.check_tmux()
    assert not result.passed
    assert "not found" in result.status
    assert "brew install tmux" in result.fix


def test_check_tmux_version_too_old(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (0, "tmux 3.1"))
    result = doctor.check_tmux()
    assert not result.passed
    assert "3.1" in result.status
    assert "pane-pipe-mode" in result.why


def test_check_tmux_version_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (0, "tmux 3.5a"))
    result = doctor.check_tmux()
    assert result.passed
    assert "3.5" in result.status


def test_check_git_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: None)
    result = doctor.check_git()
    assert not result.passed


def test_check_git_too_old(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/git")
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (0, "git version 2.30.0"))
    result = doctor.check_git()
    assert not result.passed
    assert "2.30" in result.status


def test_check_git_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/git")
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (0, "git version 2.43.0"))
    result = doctor.check_git()
    assert result.passed


def test_check_gh_installed_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: None)
    result = doctor.check_gh_installed()
    assert not result.passed


def test_check_gh_authenticated_skipped_when_gh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: None)
    result = doctor.check_gh_authenticated()
    assert result.skipped


def test_check_gh_authenticated_fails_when_logged_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (1, "not logged in"))
    result = doctor.check_gh_authenticated()
    assert not result.passed
    assert "gh auth login" in result.fix


def test_check_gh_authenticated_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (0, "Logged in to github.com"))
    result = doctor.check_gh_authenticated()
    assert result.passed


def test_check_uv_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: None)
    result = doctor.check_uv()
    assert not result.passed
    assert "astral.sh" in result.fix


def test_check_uv_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/local/bin/uv")
    result = doctor.check_uv()
    assert result.passed


def test_check_terminal_color_warns_on_dumb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLORTERM", "")
    result = doctor.check_terminal_color_support()
    assert not result.passed
    assert result.severity == "warning"


def test_check_terminal_color_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    result = doctor.check_terminal_color_support()
    assert result.passed


def test_extracted_system_module_is_importable_and_uses_public_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pollypm.doctor import system as doctor_system

    monkeypatch.setattr(doctor, "_tool_path", lambda name: None)
    result = doctor_system.check_uv()
    assert not result.passed
    assert "uv not found" in result.status


def test_extracted_doctor_modules_reexport_public_checks() -> None:
    from pollypm.doctor import filesystem, install_state, plugins, rendering

    assert install_state.check_pm_binary_resolves is doctor.check_pm_binary_resolves
    assert plugins.check_builtin_plugin_manifests is doctor.check_builtin_plugin_manifests
    assert filesystem.check_disk_space is doctor.check_disk_space
    assert rendering.render_json is doctor.render_json


# --------------------------------------------------------------------- #
# Install state
# --------------------------------------------------------------------- #


def test_check_pm_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: None)
    result = doctor.check_pm_binary_resolves()
    assert not result.passed
    assert "uv tool install" in result.fix


def test_check_pm_binary_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the first call ("pm") needs to return a path.
    calls = []

    def _which(name: str) -> str | None:
        calls.append(name)
        return "/usr/local/bin/pm" if name == "pm" else None

    monkeypatch.setattr(doctor, "_tool_path", _which)
    result = doctor.check_pm_binary_resolves()
    assert result.passed
    assert "/usr/local/bin/pm" in result.status


def test_check_installed_version_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_read_pyproject_version", lambda: "99.0.0")
    result = doctor.check_installed_version_matches_pyproject()
    assert not result.passed
    assert result.severity == "warning"
    assert "reinstall" in result.fix.lower()


def test_check_installed_version_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib.metadata import version as _mdver

    installed = _mdver("pollypm")
    monkeypatch.setattr(doctor, "_read_pyproject_version", lambda: installed)
    result = doctor.check_installed_version_matches_pyproject()
    assert result.passed


def test_check_config_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pollypm import config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "pollypm.toml")
    result = doctor.check_config_file()
    assert not result.passed
    assert "pm onboard" in result.fix


def test_check_config_file_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pollypm import config as config_mod

    path = tmp_path / "pollypm.toml"
    path.write_text("[project]\nname = \"pollypm\"\n")
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", path)
    result = doctor.check_config_file()
    assert result.passed


def test_check_provider_account_skipped_when_no_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from pollypm import config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "missing.toml")
    result = doctor.check_provider_account_configured()
    assert result.skipped


def test_check_provider_account_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from pollypm import config as config_mod

    path = tmp_path / "pollypm.toml"
    # Config with zero accounts.
    path.write_text(
        "[project]\nname = \"PollyPM\"\nworkspace_root = \".\"\n"
        "[pollypm]\ncontroller_account = \"none\"\n"
    )
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", path)
    # load_config validates controller_account; stub it so the check's
    # own error handling (not the validator) fires.
    monkeypatch.setattr(
        doctor, "_configured_providers", lambda: set(),
    )
    monkeypatch.setattr(
        "pollypm.config.load_config",
        lambda p=path: type("C", (), {"accounts": {}})(),
    )
    result = doctor.check_provider_account_configured()
    assert not result.passed
    assert "pm onboard" in result.fix


def test_check_provider_account_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pollypm import config as config_mod

    path = tmp_path / "pollypm.toml"
    path.touch()
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", path)
    monkeypatch.setattr(
        "pollypm.config.load_config",
        lambda p=path: type("C", (), {"accounts": {"alice": object()}})(),
    )
    result = doctor.check_provider_account_configured()
    assert result.passed
    # Cycle 55: pluralise the account count rather than ``account(s)``.
    assert "1 provider account configured" in result.status
    assert "account(s)" not in result.status


def test_check_provider_account_pluralisation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Plural account count must read ``N provider accounts configured``."""
    from pollypm import config as config_mod

    path = tmp_path / "pollypm.toml"
    path.touch()
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", path)
    monkeypatch.setattr(
        "pollypm.config.load_config",
        lambda p=path: type(
            "C", (), {"accounts": {"alice": object(), "bob": object(), "carol": object()}},
        )(),
    )
    result = doctor.check_provider_account_configured()
    assert result.passed
    assert "3 provider accounts configured" in result.status
    assert "account(s)" not in result.status


# --------------------------------------------------------------------- #
# Registered providers (#397 Phase E)
# --------------------------------------------------------------------- #


def test_check_registered_providers_passes_with_builtins() -> None:
    """Every installed PollyPM ships with 'claude' + 'codex' registered.

    The check walks the real entry-point registry; if this fails, the
    package is installed without its own entry points — reinstall.
    """
    result = doctor.check_registered_providers()
    assert result.passed, result.status
    providers = result.data.get("providers", [])
    assert "claude" in providers
    assert "codex" in providers
    assert "registered-providers:" in result.status


def test_check_registered_providers_fails_when_registry_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pollypm.acct import registry as registry_mod

    monkeypatch.setattr(registry_mod, "list_providers", lambda: [])
    # Force the doctor module to resolve the patched symbol.
    monkeypatch.setattr(
        "pollypm.acct.list_providers", lambda: [], raising=False
    )
    result = doctor.check_registered_providers()
    assert not result.passed
    assert "no providers registered" in result.status
    assert "uv tool install" in result.fix


def test_check_registered_providers_reports_per_provider_load_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single broken adapter is reported by name without masking others."""

    def _broken_get(name: str):
        if name == "bogus":
            raise ImportError("module 'bogus' has no attribute 'Adapter'")
        return object()

    monkeypatch.setattr(
        "pollypm.acct.list_providers",
        lambda: ["claude", "bogus"],
        raising=False,
    )
    monkeypatch.setattr(
        "pollypm.acct.get_provider", _broken_get, raising=False
    )
    result = doctor.check_registered_providers()
    assert not result.passed
    assert "bogus failed to load" in result.status
    assert "ImportError" in result.status
    assert "bogus" in result.data.get("failures", {})


# --------------------------------------------------------------------- #
# Plugins
# --------------------------------------------------------------------- #


def test_check_builtin_plugin_manifests_parse() -> None:
    result = doctor.check_builtin_plugin_manifests()
    assert result.passed, result.status
    assert int(result.data.get("count", 0)) > 0


def test_check_no_critical_plugin_disabled_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from pollypm import config as config_mod

    path = tmp_path / "pollypm.toml"
    path.touch()
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", path)
    fake = type("C", (), {"plugins": type("P", (), {"disabled": ()})()})()
    monkeypatch.setattr("pollypm.config.load_config", lambda p=path: fake)
    result = doctor.check_no_critical_plugin_disabled()
    assert result.passed


def test_check_no_critical_plugin_disabled_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from pollypm import config as config_mod

    path = tmp_path / "pollypm.toml"
    path.touch()
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", path)
    fake = type(
        "C", (), {"plugins": type("P", (), {"disabled": ("tmux_session_service",)})()}
    )()
    monkeypatch.setattr("pollypm.config.load_config", lambda p=path: fake)
    result = doctor.check_no_critical_plugin_disabled()
    assert not result.passed
    assert "tmux_session_service" in result.status


def test_check_plugin_capability_shapes_clean() -> None:
    # The shipping builtin manifests use [[capabilities]] tables;
    # this must hold in CI.
    result = doctor.check_plugin_capabilities_no_deprecations()
    assert result.passed


# --------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------- #


def test_latest_state_migration_version_positive() -> None:
    latest = doctor._latest_state_migration_version()
    assert latest is not None
    assert latest > 0


def test_latest_work_migration_version_positive() -> None:
    latest = doctor._latest_work_migration_version()
    assert latest is not None
    assert latest > 0


def test_state_migrations_skipped_with_no_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from pollypm import config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "missing.toml")
    result = doctor.check_state_migrations()
    assert result.skipped


def test_state_migrations_detects_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Build a DB that has an old schema_version.
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER, description TEXT, applied_at TEXT)"
        )
        conn.execute("INSERT INTO schema_version VALUES (1, 'old', '2020-01-01')")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(doctor, "_state_db_candidates", lambda: [db])
    monkeypatch.setattr(doctor, "_latest_state_migration_version", lambda: 99)
    result = doctor.check_state_migrations()
    assert not result.passed
    assert "99" in result.status


def test_work_migrations_detects_missing_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    # No work_schema_version table — treated as behind.
    conn.execute("CREATE TABLE other (id INTEGER)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(doctor, "_state_db_candidates", lambda: [db])
    monkeypatch.setattr(doctor, "_latest_work_migration_version", lambda: 5)
    result = doctor.check_work_migrations()
    assert not result.passed


def test_state_migrations_pluralises_db_word(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Cycle 68: state-migrations status uses ``DB`` / ``DBs`` per count.

    The drift line previously read ``1 state DB(s) behind latest …``
    and the ok path was unconditionally ``state DBs on v…``. Both
    flip to the singular ``state DB`` at count=1 — the boundary that
    fires when the user has exactly one tracked project.
    """
    one_behind = tmp_path / "behind.db"
    conn = sqlite3.connect(one_behind)
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER)")
        conn.execute("INSERT INTO schema_version VALUES (1)")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(doctor, "_state_db_candidates", lambda: [one_behind])
    monkeypatch.setattr(doctor, "_latest_state_migration_version", lambda: 5)
    fail = doctor.check_state_migrations()
    assert not fail.passed
    assert "1 state DB behind" in fail.status
    assert "DB(s)" not in fail.status

    one_current = tmp_path / "current.db"
    conn = sqlite3.connect(one_current)
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER)")
        conn.execute("INSERT INTO schema_version VALUES (5)")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(doctor, "_state_db_candidates", lambda: [one_current])
    monkeypatch.setattr(doctor, "_latest_state_migration_version", lambda: 5)
    ok = doctor.check_state_migrations()
    assert ok.passed
    assert "state DB on v5" in ok.status
    assert "state DBs on v5" not in ok.status


def test_work_migrations_pluralises_db_word(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Same shape as state-migrations: ``work DB`` / ``work DBs`` per count."""
    one_behind = tmp_path / "behind.db"
    conn = sqlite3.connect(one_behind)
    try:
        conn.execute("CREATE TABLE other (id INTEGER)")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(doctor, "_state_db_candidates", lambda: [one_behind])
    monkeypatch.setattr(doctor, "_latest_work_migration_version", lambda: 3)
    fail = doctor.check_work_migrations()
    assert not fail.passed
    assert "1 work DB behind" in fail.status
    assert "DB(s)" not in fail.status


# --------------------------------------------------------------------- #
# Filesystem
# --------------------------------------------------------------------- #


def test_pollypm_home_writable_creates_on_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(doctor, "_pollypm_home", lambda: fake_home / ".pollypm")
    result = doctor.check_pollypm_home_writable()
    assert not result.passed
    assert result.fixable and result.fix_fn is not None
    success, _ = result.fix_fn()
    assert success
    assert (fake_home / ".pollypm").is_dir()

    # Re-running now passes.
    monkeypatch.setattr(doctor, "_pollypm_home", lambda: fake_home / ".pollypm")
    ok = doctor.check_pollypm_home_writable()
    assert ok.passed


def test_pollypm_plugins_dir_fix_creates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(doctor, "_pollypm_home", lambda: fake_home)
    result = doctor.check_pollypm_plugins_dir()
    assert not result.passed
    assert result.fixable
    success, _ = result.fix_fn()
    assert success
    assert (fake_home / "plugins").is_dir()


def test_tracked_project_paths_all_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from pollypm import config as config_mod

    cfg_path = tmp_path / "pollypm.toml"
    cfg_path.touch()
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)
    fake = type(
        "C", (),
        {
            "projects": {
                "demo": type(
                    "P", (),
                    {"tracked": True, "path": tmp_path},
                )()
            }
        },
    )()
    monkeypatch.setattr("pollypm.config.load_config", lambda p=cfg_path: fake)
    result = doctor.check_tracked_project_state_parents()
    assert result.passed


def test_tracked_project_paths_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pollypm import config as config_mod

    cfg_path = tmp_path / "pollypm.toml"
    cfg_path.touch()
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", cfg_path)
    fake = type(
        "C", (),
        {
            "projects": {
                "demo": type(
                    "P", (),
                    {"tracked": True, "path": tmp_path / "does_not_exist"},
                )()
            }
        },
    )()
    monkeypatch.setattr("pollypm.config.load_config", lambda p=cfg_path: fake)
    result = doctor.check_tracked_project_state_parents()
    assert not result.passed


def test_disk_space_passes_when_plenty(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Usage:
        free = 50 * (1024 ** 3)
        total = 100 * (1024 ** 3)
        used = 50 * (1024 ** 3)

    monkeypatch.setattr("shutil.disk_usage", lambda path: _Usage())
    result = doctor.check_disk_space()
    assert result.passed


def test_disk_space_fails_when_low(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Usage:
        free = 100 * (1024 ** 2)  # 100 MiB
        total = 100 * (1024 ** 3)
        used = 100 * (1024 ** 3)

    monkeypatch.setattr("shutil.disk_usage", lambda path: _Usage())
    result = doctor.check_disk_space()
    assert not result.passed


# --------------------------------------------------------------------- #
# Tmux session state
# --------------------------------------------------------------------- #


def test_tmux_daemon_skipped_when_tmux_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: None)
    assert doctor.check_tmux_daemon().skipped


def test_tmux_daemon_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (0, "pollypm: 1 windows"))
    assert doctor.check_tmux_daemon().passed


def test_tmux_daemon_not_running_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (1, "no server running"))
    assert doctor.check_tmux_daemon().passed


def test_storage_closet_not_running_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (1, ""))
    result = doctor.check_storage_closet_reachable()
    assert result.passed


def test_stale_dead_panes_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/tmux")
    calls = {"i": 0}

    def _run_cmd(cmd, **kw):
        calls["i"] += 1
        if "has-session" in cmd:
            return (0, "")
        if "list-panes" in cmd:
            return (0, "0\n1\n1\n0\n")
        return (0, "")

    monkeypatch.setattr(doctor, "_run_cmd", _run_cmd)
    result = doctor.check_no_stale_dead_panes()
    assert not result.passed
    assert result.severity == "warning"
    assert int(result.data.get("dead", 0)) == 2


def test_stale_dead_panes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/tmux")

    def _run_cmd(cmd, **kw):
        if "has-session" in cmd:
            return (0, "")
        if "list-panes" in cmd:
            return (0, "0\n0\n")
        return (0, "")

    monkeypatch.setattr(doctor, "_run_cmd", _run_cmd)
    assert doctor.check_no_stale_dead_panes().passed


# --------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------- #


def test_network_github_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(socket, "create_connection", _raise)
    result = doctor.check_network_github()
    assert not result.passed
    assert result.severity == "warning"


def test_network_github_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass

    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: _FakeSock())
    result = doctor.check_network_github()
    assert result.passed


def test_network_anthropic_skipped_without_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_configured_providers", lambda: set())
    assert doctor.check_network_anthropic().skipped


def test_network_openai_skipped_without_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_configured_providers", lambda: {"claude"})
    assert doctor.check_network_openai().skipped


# --------------------------------------------------------------------- #
# Runner + rendering
# --------------------------------------------------------------------- #


def test_run_checks_isolates_crashing_check() -> None:
    def _boom() -> doctor.CheckResult:
        raise RuntimeError("oops")

    checks = [doctor.Check("crashy", _boom, "test")]
    report = doctor.run_checks(checks)
    assert len(report.results) == 1
    check, result = report.results[0]
    assert check.name == "crashy"
    assert not result.passed
    assert "crashy" in result.fix
    assert "RuntimeError" in result.status


def test_render_human_includes_summary_and_failure_detail() -> None:
    def _pass() -> doctor.CheckResult:
        return doctor._ok("all good")

    def _fail() -> doctor.CheckResult:
        return doctor._fail("broken", why="it does not work", fix="fix it: run x")

    report = doctor.run_checks([
        doctor.Check("passing", _pass, "test"),
        doctor.Check("failing", _fail, "test"),
    ])
    text = doctor.render_human(report)
    assert "Summary:" in text
    assert "passing: all good" in text
    assert "failing: broken" in text
    # Failure detail block rendered at the bottom.
    assert "Why: it does not work" in text
    assert "fix it: run x" in text


def test_render_human_summary_pluralisation() -> None:
    """The final ``Summary:`` line must not show ``warning(s)`` / ``error(s)``.

    Every ``pm doctor`` invocation prints this line as the closing
    summary — it's one of the most user-visible strings in the
    install. Mirrors the doctor (s) cleanup from cycles 45/47/48/49/50
    on individual check messages and locks the summary's plural
    handling for both the warn-only (``1 warning``) and error-only
    (``1 error``) cases.
    """
    def _warn() -> doctor.CheckResult:
        return doctor._fail("warned", why="why-w", fix="fix-w", severity="warning")

    def _err() -> doctor.CheckResult:
        return doctor._fail("erred", why="why-e", fix="fix-e", severity="error")

    one_each = doctor.run_checks([
        doctor.Check("warn", _warn, "test"),
        doctor.Check("err", _err, "test"),
    ])
    text = doctor.render_human(one_each)
    assert "1 warning, 1 error," in text
    assert "warning(s)" not in text
    assert "error(s)" not in text
    # The dotted breakdown line right after Summary picks up the same
    # singular form — at count=1 the bare ``1 warnings · 1 errors``
    # was just as wrong as the parenthetical-s line above it.
    assert "1 warning · 1 error" in text

    only_warn = doctor.run_checks([
        doctor.Check("warn-a", _warn, "test"),
        doctor.Check("warn-b", _warn, "test"),
    ])
    text = doctor.render_human(only_warn)
    assert "2 warnings, 0 errors," in text
    assert "warning(s)" not in text
    assert "error(s)" not in text
    assert "2 warnings · 0 errors" in text


def test_render_json_is_parseable() -> None:
    def _pass() -> doctor.CheckResult:
        return doctor._ok("ok", data={"foo": 1})

    report = doctor.run_checks([doctor.Check("sample", _pass, "test")])
    payload = json.loads(doctor.render_json(report))
    assert payload["ok"] is True
    assert payload["summary"]["passed"] == 1
    assert payload["checks"][0]["name"] == "sample"
    assert payload["checks"][0]["data"] == {"foo": 1}


def test_apply_fixes_invokes_fix_fn(tmp_path: Path) -> None:
    called = {"ok": False}

    def _do_fix() -> tuple[bool, str]:
        called["ok"] = True
        return (True, "created widget")

    def _check() -> doctor.CheckResult:
        return doctor._fail(
            "widget missing", why="no widget", fix="create widget",
            fixable=True, fix_fn=_do_fix,
        )

    report = doctor.run_checks([doctor.Check("widget", _check, "test")])
    fix_results = doctor.apply_fixes(report)
    assert called["ok"]
    assert fix_results == [("widget", True, "created widget")]


def test_doctor_performance_budget() -> None:
    """Full doctor run should complete well under 5 seconds.

    We tolerate up to 10s in CI since some checks spawn subprocesses
    (tmux, git, gh) and DNS lookups can be slow on constrained runners.
    """
    report = doctor.run_checks()
    assert report.duration_seconds < 10.0, f"took {report.duration_seconds:.2f}s"


# --------------------------------------------------------------------- #
# Integration: doctor CLI on a synthetic clean-ish environment
# --------------------------------------------------------------------- #


def test_cli_doctor_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor", "--json"])
    # Exit code is 0 or 1 depending on the test host's own environment.
    # What we care about is: (a) we produced parseable JSON, (b) the
    # payload describes checks.
    assert result.exit_code in (0, 1)
    payload = json.loads(result.stdout)
    assert "checks" in payload
    assert isinstance(payload["checks"], list)
    assert payload["summary"]["total"] == len(payload["checks"])


def test_cli_doctor_human_output(monkeypatch: pytest.MonkeyPatch) -> None:
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor"])
    assert result.exit_code in (0, 1)
    assert "Summary:" in result.stdout


def test_cli_doctor_all_pass_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Integration check: when every check passes, doctor exits 0."""
    from pollypm import doctor as doctor_mod

    def _pass() -> doctor_mod.CheckResult:
        return doctor_mod._ok("ok")

    monkeypatch.setattr(
        doctor_mod, "_registered_checks",
        lambda: [doctor_mod.Check("demo", _pass, "test")],
    )
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor"])
    assert result.exit_code == 0
    assert "Summary: 1/1" in result.stdout


def test_release_channel_line_defaults_to_stable_without_config(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.toml"
    assert doctor.release_channel_line(missing) == "Release channel: stable"


def test_release_channel_line_reads_configured_channel(tmp_path: Path) -> None:
    from pollypm.config import load_config, write_config

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "[project]\n"
        'name = "pollypm"\n'
        'tmux_session = "pollypm"\n'
        "\n"
        "[pollypm]\n"
        'controller_account = "claude_primary"\n'
        'release_channel = "beta"\n'
        "\n"
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
    # Force a re-render through write_config so the rendered TOML matches
    # what ``pm doctor`` would see after any on-disk mutation.
    write_config(load_config(config_path), config_path, force=True)

    assert doctor.release_channel_line(config_path) == "Release channel: beta"


def test_cli_doctor_prints_release_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The human ``pm doctor`` output must contain a release-channel line."""
    from pollypm import doctor as doctor_mod

    def _pass() -> doctor_mod.CheckResult:
        return doctor_mod._ok("ok")

    monkeypatch.setattr(
        doctor_mod, "_registered_checks",
        lambda: [doctor_mod.Check("demo", _pass, "test")],
    )
    monkeypatch.setattr(
        doctor_mod, "release_channel_line", lambda: "Release channel: beta"
    )
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor"])
    assert result.exit_code == 0
    assert "Release channel: beta" in result.stdout
