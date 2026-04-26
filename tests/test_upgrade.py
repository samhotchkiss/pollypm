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


def test_unsupported_installer_help_uses_structured_shape() -> None:
    """#760: every user-facing error should follow the four-field
    StructuredUserMessage shape. This one names a summary, explains
    why, points at a next step, and preserves the per-tool command
    list as pre-formatted details."""
    text = upgrade_mod.unsupported_installer_help()
    # Summary line with failure icon.
    assert text.startswith("✗ Could not detect how PollyPM was installed.")
    # Next-action line is prominent.
    assert "Next:" in text
    # Per-tool commands are in the details block and retained their
    # leading-space indentation (they're pre-formatted).
    assert "  uv tool upgrade pollypm" in text
    assert "  pip install -U pollypm" in text
    assert "  brew upgrade pollypm" in text
    assert "  npm update -g pollypm" in text


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


def test_upgrade_check_only_does_not_install(monkeypatch) -> None:
    """check_only runs the migration gate and reports the plan without
    ever invoking the real subprocess.

    Stub the migration check — the test environment's state.db can
    have legitimately pending migrations that would otherwise abort
    the upgrade before check-only can return its no-op summary.
    """
    monkeypatch.setattr(
        upgrade_mod, "run_migration_check", lambda: (True, "ok"),
    )
    result = upgrade_mod.upgrade(
        channel="stable",
        check_only=True,
        installer_overrides={"uv": True},
    )
    assert result.ok is True
    assert result.old_version == result.new_version
    assert "check-only" in result.message


def test_upgrade_check_only_skips_when_release_check_current(monkeypatch) -> None:
    monkeypatch.setattr(
        upgrade_mod, "_available_upgrade", lambda channel: ("1.0.0rc2", False),
    )
    monkeypatch.setattr(
        upgrade_mod, "run_migration_check", lambda: (True, "ok"),
    )
    result = upgrade_mod.upgrade(
        channel="stable",
        check_only=True,
        installer_overrides={"uv": True},
    )
    assert result.ok is True
    assert result.old_version == result.new_version
    assert "already up to date" in result.message


def test_upgrade_same_version_install_reports_noop(monkeypatch) -> None:
    monkeypatch.setattr(upgrade_mod, "_available_upgrade", lambda channel: None)
    monkeypatch.setattr(
        upgrade_mod,
        "run_migration_check",
        lambda: (True, "ok"),
    )
    monkeypatch.setattr(
        upgrade_mod.subprocess,
        "run",
        lambda *a, **kw: type(
            "Result",
            (),
            {"returncode": 0, "stdout": "", "stderr": ""},
        )(),
    )
    monkeypatch.setattr(upgrade_mod, "_read_new_version", lambda: upgrade_mod.pollypm.__version__)
    monkeypatch.setattr(
        upgrade_mod,
        "inject_notice",
        lambda old, new: (_ for _ in ()).throw(AssertionError("no notice for noop")),
    )
    result = upgrade_mod.upgrade(
        channel="stable",
        installer_overrides={"uv": True},
    )
    assert result.ok is True
    assert result.old_version == result.new_version
    assert result.notified is False
    assert "already up to date" in result.message


def test_upgrade_writes_post_upgrade_flag_after_real_version_change(
    tmp_path, monkeypatch,
) -> None:
    """A version-changing upgrade must drop the cockpit's restart-nudge
    sentinel; without it the rail's pill never says
    ``✓ Upgraded to v<new> · restart cockpit``."""
    import json

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".pollypm").mkdir()
    monkeypatch.setattr(
        upgrade_mod, "_POST_UPGRADE_FLAG",
        fake_home / ".pollypm" / "post-upgrade.flag",
    )
    monkeypatch.setattr(upgrade_mod, "_available_upgrade", lambda channel: None)
    monkeypatch.setattr(
        upgrade_mod, "run_migration_check", lambda: (True, "ok"),
    )
    monkeypatch.setattr(
        upgrade_mod.subprocess,
        "run",
        lambda *a, **kw: type(
            "Result",
            (),
            {"returncode": 0, "stdout": "", "stderr": ""},
        )(),
    )
    # Force the install to "produce" a different version than the
    # currently installed one, so the success path is taken.
    monkeypatch.setattr(upgrade_mod, "_read_new_version", lambda: "9.9.9")
    monkeypatch.setattr(
        upgrade_mod, "inject_notice", lambda old, new: (True, "ok"),
    )

    result = upgrade_mod.upgrade(
        channel="stable",
        installer_overrides={"uv": True},
    )
    assert result.ok is True
    assert result.new_version == "9.9.9"
    flag = fake_home / ".pollypm" / "post-upgrade.flag"
    assert flag.exists(), "successful upgrade must write the cockpit sentinel"
    payload = json.loads(flag.read_text())
    assert payload["to"] == "9.9.9"
    assert payload["from"] == result.old_version
    assert isinstance(payload["at"], (int, float))


def test_upgrade_skips_post_upgrade_flag_when_version_unchanged(
    tmp_path, monkeypatch,
) -> None:
    """A no-op upgrade (same version after install) must NOT write the
    cockpit sentinel — there is nothing for the operator to restart
    into, so the pill must not flash 'Upgraded to vX'."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".pollypm").mkdir()
    flag = fake_home / ".pollypm" / "post-upgrade.flag"
    monkeypatch.setattr(upgrade_mod, "_POST_UPGRADE_FLAG", flag)
    monkeypatch.setattr(upgrade_mod, "_available_upgrade", lambda channel: None)
    monkeypatch.setattr(
        upgrade_mod, "run_migration_check", lambda: (True, "ok"),
    )
    monkeypatch.setattr(
        upgrade_mod.subprocess,
        "run",
        lambda *a, **kw: type(
            "Result",
            (),
            {"returncode": 0, "stdout": "", "stderr": ""},
        )(),
    )
    # Same version after install → no-op path
    monkeypatch.setattr(
        upgrade_mod, "_read_new_version", lambda: upgrade_mod.pollypm.__version__,
    )

    result = upgrade_mod.upgrade(
        channel="stable",
        installer_overrides={"uv": True},
    )
    assert result.ok is True
    assert result.old_version == result.new_version
    assert not flag.exists(), (
        "no-op upgrade must not write the cockpit restart-nudge sentinel"
    )


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


def test_upgrade_silent_about_migrations_when_none_pending(monkeypatch) -> None:
    """Happy path: no pending migration → no '[step] migration ...' noise.

    Regression for the user complaint that ``pm upgrade`` always prints
    migration-check chatter even when there is nothing for the operator
    to act on. The check still runs (so a real pending migration would
    abort the upgrade) — only the cosmetic step lines are suppressed.
    """
    captured: list[str] = []
    monkeypatch.setattr(
        upgrade_mod, "run_migration_check",
        lambda: (True, "ok: migrations up to date"),
    )
    result = upgrade_mod.upgrade(
        channel="stable",
        plan_only=True,
        installer_overrides={"uv": True},
        emit=captured.append,
    )
    assert result.ok is True
    migration_lines = [line for line in captured if "migration" in line.lower()]
    assert migration_lines == [], (
        f"expected no migration step noise, got: {migration_lines}"
    )


def test_upgrade_emits_migration_step_lines_only_on_failure(monkeypatch) -> None:
    """Failure path: a real pending migration must surface step lines.

    The silent-when-clean fix must not swallow the actionable case —
    when ``run_migration_check`` reports a problem, the operator needs
    the ``[step] migration check`` / ``[step] migration: <detail>``
    breadcrumbs so they can see what blocked the upgrade.
    """
    captured: list[str] = []
    monkeypatch.setattr(
        upgrade_mod, "run_migration_check",
        lambda: (False, "1 pending migration: [state] v42: synthetic"),
    )
    result = upgrade_mod.upgrade(
        channel="stable",
        installer_overrides={"uv": True},
        emit=captured.append,
    )
    assert result.ok is False
    assert any("[step] migration check" in line for line in captured)
    assert any(
        "[step] migration: 1 pending migration" in line for line in captured
    )


def test_upgrade_plan_only_stops_before_exec(monkeypatch) -> None:
    """plan_only is the test seam for composing without running.

    The migration gate (#717) is now wired and the workspace state.db
    used by the test runner can have legitimately pending migrations
    in flight (e.g. while a new schema bump is landing in another
    branch). Stub the check so this test covers the plan-only seam,
    not the gate's pending-migration detection — that's exercised by
    ``test_upgrade_aborts_on_pending_migrations``.
    """
    monkeypatch.setattr(
        upgrade_mod, "run_migration_check", lambda: (True, "ok"),
    )
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

def test_run_migration_check_returns_ok_or_pending_summary() -> None:
    """The migration gate (#717) is wired now: ``run_migration_check``
    delegates to :func:`pollypm.store.migrations.check_pending`, which
    walks the workspace state.db.

    The test environment's state.db can be in either state — fully
    migrated (``ok=True``) or pending (``ok=False`` with a list of
    unapplied migrations). Both are valid outcomes, so this test
    confirms the structural contract: the call returns a
    ``(bool, str)`` tuple with a non-empty detail string. The
    abort-on-pending behaviour is exercised by
    ``test_upgrade_aborts_on_pending_migrations``.
    """
    ok, detail = upgrade_mod.run_migration_check()
    assert isinstance(ok, bool)
    assert isinstance(detail, str) and detail


def test_inject_notice_is_default_disabled() -> None:
    """#755: the default pm upgrade flow no longer injects
    `<system-update>` notices; the live sessions reject them as prompt
    injection. Helper returns a soft-skip instead of dispatching."""
    ok, detail = upgrade_mod.inject_notice("0.1.0", "0.2.0")
    assert ok is True
    assert "skipped" in detail
    assert "#755" in detail or "disabled" in detail.lower()


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
    assert "version:   " in result.stdout
    assert "(current)" in result.stdout
    assert "→" not in result.stdout
    assert "check-only" in result.stdout
