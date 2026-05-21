"""Unit tests for the doubled-path cleanup helper (refs #1972).

Covers the helper directly (``cleanup_doubled_path``) and the doctor
``--fix`` wiring on :func:`pollypm.doctor.check_doubled_pollypm_path`.

The helper is intentionally pure (takes a target path, returns a plan)
so these tests run entirely against ``tmp_path`` — they never touch
the real ``~/.pollypm/.pollypm/`` on the developer's machine.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pollypm import doctor
from pollypm.doctor_cleanup_doubled_path import (
    ANOMALOUS_DIRECT_CHILDREN,
    CleanupPlan,
    _backup_path_for,
    cleanup_doubled_path,
    format_summary,
)


# ---------------------------------------------------------------------------
# Fixture: a fake ``~/.pollypm/.pollypm/`` populated with stray files.
# ---------------------------------------------------------------------------


def _make_doubled(tmp_path: Path) -> Path:
    """Build ``<tmp>/home/.pollypm/.pollypm/`` with a few stray files."""
    home = tmp_path / "home" / ".pollypm"
    doubled = home / ".pollypm"
    doubled.mkdir(parents=True)
    (doubled / "stray.toml").write_text("legacy = true\n" * 20)
    (doubled / "logs").mkdir()
    (doubled / "logs" / "old.log").write_text("noise\n" * 50)
    (doubled / "errors.log").write_text("err\n" * 10)
    return doubled


# ---------------------------------------------------------------------------
# Safety: refuse anomalous contents.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("anomaly", sorted(ANOMALOUS_DIRECT_CHILDREN))
def test_cleanup_refuses_when_real_config_subdirs_present(
    tmp_path: Path, anomaly: str,
) -> None:
    """Refuse if a real ``~/.pollypm/`` child name appears at the top.

    Parametrized over each name in the anomaly set so adding a new
    sentinel automatically gets coverage.
    """
    doubled = _make_doubled(tmp_path)
    # Plant the suspicious child. For ``state.db`` etc. (files) we
    # touch a file; for ``audit``/``agent_homes``/``plugins`` we mkdir.
    child = doubled / anomaly
    if anomaly.endswith(".db") or anomaly.endswith(".toml") or "-" in anomaly:
        child.write_text("real data")
    else:
        child.mkdir()

    plan = cleanup_doubled_path(doubled)

    assert plan.refused_reason is not None
    assert anomaly in plan.refused_reason
    assert plan.moved is False
    # Doubled tree is still on disk — refusal means no mutation.
    assert doubled.exists()
    # And no backup got created.
    assert not plan.backup_path.exists()


def test_cleanup_refuses_when_target_missing(tmp_path: Path) -> None:
    """Refuse rather than silently no-op when the path doesn't exist."""
    plan = cleanup_doubled_path(tmp_path / "does-not-exist")
    assert plan.refused_reason is not None
    assert "does not exist" in plan.refused_reason
    assert plan.moved is False


def test_cleanup_refuses_when_target_is_symlink(tmp_path: Path) -> None:
    """Symlinks must be refused so we never relocate the user's real dir."""
    real = tmp_path / "real_pollypm"
    real.mkdir()
    link = tmp_path / "home" / ".pollypm" / ".pollypm"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    plan = cleanup_doubled_path(link)
    assert plan.refused_reason is not None
    assert "symlink" in plan.refused_reason
    assert plan.moved is False
    # The symlink + its target both survive.
    assert link.is_symlink()
    assert real.exists()


def test_cleanup_refuses_when_target_is_file(tmp_path: Path) -> None:
    """Refuse a non-directory target — defensive guard."""
    home = tmp_path / "home" / ".pollypm"
    home.mkdir(parents=True)
    target = home / ".pollypm"
    target.write_text("not a directory")
    plan = cleanup_doubled_path(target)
    assert plan.refused_reason is not None
    assert plan.moved is False


def test_cleanup_refuses_when_backup_already_exists(tmp_path: Path) -> None:
    """Pre-existing backup → refuse rather than merge trees."""
    doubled = _make_doubled(tmp_path)
    now = datetime(2026, 5, 20, 12, 34, 56)
    pre_backup = _backup_path_for(doubled, now=now)
    pre_backup.mkdir()
    plan = cleanup_doubled_path(doubled, now=now)
    assert plan.refused_reason is not None
    assert "backup path already exists" in plan.refused_reason
    assert plan.moved is False
    assert doubled.exists()


# ---------------------------------------------------------------------------
# Happy path: move cleanly.
# ---------------------------------------------------------------------------


def test_cleanup_moves_doubled_tree_to_timestamped_backup(
    tmp_path: Path,
) -> None:
    doubled = _make_doubled(tmp_path)
    now = datetime(2026, 5, 20, 12, 34, 56)
    plan = cleanup_doubled_path(doubled, now=now)
    assert plan.refused_reason is None
    assert plan.moved is True
    # 3 files: stray.toml, errors.log, logs/old.log
    assert plan.file_count == 3
    assert plan.total_bytes > 0
    # Doubled tree gone from the original location.
    assert not doubled.exists()
    # Backup landed sibling-ward with the expected suffix.
    expected_backup = doubled.parent / ".pollypm.bak-20260520-123456"
    assert plan.backup_path == expected_backup
    assert expected_backup.exists()
    # Contents survived the move.
    assert (expected_backup / "stray.toml").exists()
    assert (expected_backup / "logs" / "old.log").exists()


def test_cleanup_emits_audit_event_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The doctor ``--fix`` wrapper emits ``cockpit.doubled_path_artifacts_cleaned``.

    We assert via the audit log helper directly rather than reading the
    JSONL tail — the central log path is deliberately picked up from
    ``POLLYPM_AUDIT_HOME`` so the test stays isolated.
    """
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(tmp_path / "audit"))
    doubled = _make_doubled(tmp_path)

    from pollypm.doctor_cleanup_doubled_path import (
        cleanup_doubled_path as _cdp,
        emit_cleanup_audit_event,
    )

    plan = _cdp(doubled)
    assert plan.moved is True
    emit_cleanup_audit_event(plan)

    central = tmp_path / "audit" / "_workspace.jsonl"
    assert central.exists(), "audit tail not written"
    body = central.read_text()
    assert "cockpit.doubled_path_artifacts_cleaned" in body
    assert '"moved_file_count":3' in body or '"moved_file_count": 3' in body


# ---------------------------------------------------------------------------
# Dry-run: non-mutating.
# ---------------------------------------------------------------------------


def test_cleanup_dry_run_does_not_mutate(tmp_path: Path) -> None:
    doubled = _make_doubled(tmp_path)
    snapshot = sorted(p.relative_to(doubled) for p in doubled.rglob("*"))
    plan = cleanup_doubled_path(doubled, dry_run=True)
    assert plan.refused_reason is None
    assert plan.moved is False
    assert plan.dry_run is True
    # Counts are still populated so the user sees what WOULD happen.
    assert plan.file_count == 3
    assert plan.total_bytes > 0
    # Filesystem is byte-identical to before.
    assert doubled.exists()
    after = sorted(p.relative_to(doubled) for p in doubled.rglob("*"))
    assert snapshot == after
    # No backup got created.
    assert not plan.backup_path.exists()


def test_format_summary_handles_each_mode(tmp_path: Path) -> None:
    """Sanity-check the human renderer for refusal / dry-run / moved."""
    doubled = _make_doubled(tmp_path)

    refused = CleanupPlan(
        target=doubled,
        backup_path=doubled.parent / ".pollypm.bak-x",
        refused_reason="nope",
    )
    assert format_summary(refused).startswith("refused: nope")

    dry = cleanup_doubled_path(doubled, dry_run=True)
    assert "dry-run" in format_summary(dry)

    moved = cleanup_doubled_path(doubled)
    assert "moved" in format_summary(moved)


# ---------------------------------------------------------------------------
# Wiring: ``check_doubled_pollypm_path`` exposes ``fix_fn``.
# ---------------------------------------------------------------------------


def test_check_doubled_pollypm_path_exposes_fixable_fix_fn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the warning fires, the check carries a runnable ``fix_fn``."""
    fake_home = tmp_path / "home" / ".pollypm"
    doubled = fake_home / ".pollypm"
    doubled.mkdir(parents=True)
    (doubled / "stray.toml").write_text("legacy = true\n")
    monkeypatch.setattr("pollypm.config.GLOBAL_CONFIG_DIR", fake_home)

    result = doctor.check_doubled_pollypm_path()
    assert result.passed is False
    assert result.fixable is True
    assert result.fix_fn is not None
    # Fix help text advertises ``pm doctor --fix``.
    assert "pm doctor --fix" in result.fix


def test_check_doubled_pollypm_path_fix_fn_moves_and_returns_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invoking ``fix_fn`` end-to-end relocates the tree and reports success."""
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(tmp_path / "audit"))
    fake_home = tmp_path / "home" / ".pollypm"
    doubled = fake_home / ".pollypm"
    doubled.mkdir(parents=True)
    (doubled / "stray.toml").write_text("legacy = true\n")
    monkeypatch.setattr("pollypm.config.GLOBAL_CONFIG_DIR", fake_home)

    result = doctor.check_doubled_pollypm_path()
    assert result.fix_fn is not None
    ok, message = result.fix_fn()
    assert ok is True
    assert "moved" in message
    # Doubled tree relocated; backup exists.
    assert not doubled.exists()
    siblings = list(fake_home.iterdir())
    backups = [p for p in siblings if p.name.startswith(".pollypm.bak-")]
    assert len(backups) == 1


def test_check_doubled_pollypm_path_fix_fn_refuses_anomalous_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wired ``fix_fn`` propagates the helper's refusal verbatim."""
    fake_home = tmp_path / "home" / ".pollypm"
    doubled = fake_home / ".pollypm"
    doubled.mkdir(parents=True)
    (doubled / "stray.toml").write_text("legacy = true\n")
    # Plant a real-config-looking child.
    (doubled / "audit").mkdir()
    monkeypatch.setattr("pollypm.config.GLOBAL_CONFIG_DIR", fake_home)

    result = doctor.check_doubled_pollypm_path()
    assert result.fix_fn is not None
    ok, message = result.fix_fn()
    assert ok is False
    assert "audit" in message
    # Refusal means doubled tree untouched.
    assert doubled.exists()
    assert (doubled / "audit").exists()
