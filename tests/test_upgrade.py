"""Tests for `pm upgrade` (#716).

Exercises the installer-detection priority, per-installer plan shape,
channel routing, migration-check gating, and the CLI wiring.

Every test that would otherwise touch the real system uses the
``installer_overrides`` / ``emit`` / ``plan_only`` seams on
``upgrade()`` — no real ``uv``/``pip``/``brew``/``npm`` shells fire.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.cli import app as cli_app
from pollypm import upgrade as upgrade_mod


runner = CliRunner()


# --------------------------------------------------------------------------- #
# detect_installer priority order
# --------------------------------------------------------------------------- #

def test_detect_uv_wins_over_everything() -> None:
    assert upgrade_mod.detect_installer(
        overrides={"uv": True, "pip": True, "brew": True, "npm": True},
    ) == "uv"


def test_detect_pip_when_no_uv() -> None:
    assert upgrade_mod.detect_installer(
        overrides={"uv": False, "pip": True, "brew": True, "npm": True},
    ) == "pip"


def test_detect_brew_when_no_uv_no_pip() -> None:
    assert upgrade_mod.detect_installer(
        overrides={"uv": False, "pip": False, "brew": True, "npm": True},
    ) == "brew"


def test_detect_npm_when_only_npm() -> None:
    assert upgrade_mod.detect_installer(
        overrides={"uv": False, "pip": False, "brew": False, "npm": True},
    ) == "npm"


def test_detect_unknown_when_none() -> None:
    assert upgrade_mod.detect_installer(
        overrides={"uv": False, "pip": False, "brew": False, "npm": False},
    ) == "unknown"


# --------------------------------------------------------------------------- #
# plan_upgrade — per-installer + channel routing
# --------------------------------------------------------------------------- #

def test_plan_uv_stable() -> None:
    plan = upgrade_mod.plan_upgrade("uv", "stable")
    assert plan.command == ["uv", "tool", "upgrade", "pollypm"]
    assert plan.channel == "stable"


def test_plan_uv_beta_uses_prerelease_flag() -> None:
    plan = upgrade_mod.plan_upgrade("uv", "beta")
    assert "--prerelease" in plan.command
    assert plan.channel == "beta"


def test_plan_pip_stable() -> None:
    plan = upgrade_mod.plan_upgrade("pip", "stable")
    assert plan.command == ["pip", "install", "-U", "pollypm"]


def test_plan_pip_beta_uses_pre_flag() -> None:
    plan = upgrade_mod.plan_upgrade("pip", "beta")
    assert "--pre" in plan.command


def test_plan_brew_downgrades_beta_to_stable() -> None:
    # brew doesn't expose pre-release channels; explicit stable.
    plan = upgrade_mod.plan_upgrade("brew", "beta")
    assert plan.command == ["brew", "upgrade", "pollypm"]
    assert plan.channel == "stable"


def test_plan_npm_stable() -> None:
    plan = upgrade_mod.plan_upgrade("npm", "stable")
    assert plan.command == ["npm", "update", "-g", "pollypm"]


def test_plan_npm_beta_uses_beta_tag() -> None:
    plan = upgrade_mod.plan_upgrade("npm", "beta")
    assert plan.command == ["npm", "install", "-g", "pollypm@beta"]


def test_plan_unknown_returns_empty_command() -> None:
    plan = upgrade_mod.plan_upgrade("unknown", "stable")
    assert plan.command == []


def test_plan_unknown_channel_falls_back_to_stable() -> None:
    plan = upgrade_mod.plan_upgrade("uv", "nightly")
    assert plan.channel == "stable"


# --------------------------------------------------------------------------- #
# upgrade() flow
# --------------------------------------------------------------------------- #

def test_upgrade_aborts_on_unknown_installer() -> None:
    result = upgrade_mod.upgrade(
        channel="stable",
        installer_overrides={"uv": False, "pip": False, "brew": False, "npm": False},
    )
    assert result.ok is False
    assert result.installer == "unknown"
    assert "installer" in result.message.lower()
    assert "uv tool upgrade pollypm" in result.stderr  # help text


def test_upgrade_check_only_does_not_install() -> None:
    """check_only runs the migration gate and reports the plan without
    ever invoking the real subprocess."""
    result = upgrade_mod.upgrade(
        channel="stable",
        check_only=True,
        installer_overrides={"uv": True},
    )
    assert result.ok is True
    assert result.old_version == result.new_version
    assert "check-only" in result.message


def test_upgrade_aborts_when_migration_check_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        upgrade_mod, "run_migration_check",
        lambda: (False, "synthetic failure"),
    )
    result = upgrade_mod.upgrade(
        channel="stable",
        installer_overrides={"uv": True},
    )
    assert result.ok is False
    assert "migration" in result.message.lower()
    assert result.stderr == "synthetic failure"
    # Never called the installer; version unchanged.
    assert result.old_version == result.new_version


def test_upgrade_plan_only_stops_before_exec(monkeypatch) -> None:
    """plan_only is the test seam for composing without running."""
    result = upgrade_mod.upgrade(
        channel="beta",
        plan_only=True,
        installer_overrides={"uv": True},
    )
    assert result.ok is True
    assert "plan-only" in result.message


def test_upgrade_records_recycle_flags(monkeypatch) -> None:
    """recycle flags are accepted but defer behavior to #720."""
    captured: list[str] = []
    monkeypatch.setattr(
        upgrade_mod, "run_migration_check",
        lambda: (True, "ok"),
    )
    upgrade_mod.upgrade(
        channel="stable",
        plan_only=True,
        recycle_all=True,
        installer_overrides={"uv": True},
        emit=captured.append,
    )
    # plan_only returns before the recycle step runs, so this is just
    # a no-crash check — the flag plumbing is exercised.


# --------------------------------------------------------------------------- #
# Migration / notice injection stubs
# --------------------------------------------------------------------------- #

def test_run_migration_check_no_module_is_soft_pass() -> None:
    """Until #717 lands, the check returns OK with a 'not yet' note."""
    ok, detail = upgrade_mod.run_migration_check()
    assert ok is True  # soft pass — #717 not wired yet
    assert "not yet implemented" in detail or "ok" in detail


def test_inject_notice_no_module_is_soft_pass() -> None:
    ok, detail = upgrade_mod.inject_notice("0.1.0", "0.2.0")
    assert ok is True
    assert "not yet" in detail or "notif" in detail.lower()


# --------------------------------------------------------------------------- #
# read_changelog_diff
# --------------------------------------------------------------------------- #

def test_changelog_diff_returns_empty_when_missing(tmp_path) -> None:
    assert upgrade_mod.read_changelog_diff(
        "1.0.0", path=tmp_path / "CHANGELOG.md",
    ) == ""


def test_changelog_diff_returns_above_version(tmp_path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# CHANGELOG\n"
        "\n"
        "## 0.3.0\n"
        "\n"
        "- new thing\n"
        "\n"
        "## 0.2.0\n"
        "\n"
        "- older thing\n"
    )
    diff = upgrade_mod.read_changelog_diff("0.2.0", path=changelog)
    assert "0.3.0" in diff
    assert "new thing" in diff
    assert "older thing" not in diff


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #

def test_cli_upgrade_unknown_installer_exits_nonzero(monkeypatch) -> None:
    """CLI surfaces the 'no installer' error from the library with a
    non-zero exit code."""
    monkeypatch.setattr(
        upgrade_mod, "detect_installer", lambda overrides=None: "unknown",
    )
    result = runner.invoke(cli_app, ["upgrade", "--check-only"])
    assert result.exit_code == 1
    assert "installer" in result.stdout.lower()


def test_cli_upgrade_check_only_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(
        upgrade_mod, "detect_installer", lambda overrides=None: "uv",
    )
    monkeypatch.setattr(
        upgrade_mod, "run_migration_check", lambda: (True, "ok"),
    )
    result = runner.invoke(cli_app, ["upgrade", "--check-only", "--channel", "stable"])
    assert result.exit_code == 0
    assert "installer: uv" in result.stdout
    assert "check-only" in result.stdout
