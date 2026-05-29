"""Extended ``pm doctor`` coverage beyond PR #1931 (#1824).

PR #1931 (commit a8f8620e9) added
``tests/test_doctor_enhancements_recovery.py`` with the PASS/WARN/
FAIL monkeypatch coverage for the comprehensive checks. This module
extends that with the surfaces #1931 explicitly deferred:

* End-to-end CLI ``doctor`` invocations: exit codes (--all-pass /
  warnings-only / any-error), ``--fix`` invocation + verification,
  ``--fix-dry-run`` enumeration, ``--json`` AutoFix metadata.
* ``--fix`` anti-lie guards from #1063 + #1064
  (``test_fix_cli_does_not_lie_when_handler_noops``,
  ``test_verify_fix_results_demotes_missing_check``).
* ``AutoFixPlan`` plumbing on the per-tool checks (tmux / claude-cli
  / codex-cli) + the renderer / JSON output that consumes it.
* The ``check_scheduler_last_fired`` cadence sweep — needs a
  bootstrapped sqlite messages table for the event rows, so it sits
  in this file rather than the PR #1931 monkeypatch-only one.
* ``check_sessions_table_populated`` + ``check_sessions_table_vs_tmux``
  (session drift) — these need a bootstrapped sqlite state DB for
  the ``sessions`` row count.
* The ``check_inbox_aggregator_path`` checks against
  ``pollypm.work.cli._resolve_db_path``.
* Pluralisation guards on the ``render_fix_summary`` /
  ``render_fix_dry_run`` footers and on the maintenance-handler
  result lines.
* The ``--fix`` fixer plumbing for plan-gate, task-assignment
  sweeper-DBs, agent-worktree prune, and logs-dir rotate.

Pure-helper coverage (each check's PASS/WARN/FAIL via monkeypatch)
lives in ``tests/test_doctor_enhancements_recovery.py``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm import doctor


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _make_state_db(path: Path, *, sessions: int = 0) -> Path:
    """Synthetic state DB with the ``sessions`` shape the doctor reads.

    Installs the ``sessions`` table (still a domain table owned by
    :class:`StateStore`, #342-followup). Pre-sqlite-ripout this also
    bootstrapped the unified ``messages`` schema via
    ``SQLAlchemyStore`` so the cadence check could read events;
    post-#1971 the messages table lives in pg and the scheduler
    cadence test path was deleted from this module (the doctor now
    reads via ``get_store(load_config())`` rather than a sqlite path
    monkeypatched onto ``_primary_state_db``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                name TEXT PRIMARY KEY,
                role TEXT,
                project TEXT,
                provider TEXT,
                account TEXT,
                cwd TEXT,
                window_name TEXT
            );
            """
        )
        for i in range(sessions):
            conn.execute(
                "INSERT INTO sessions "
                "(name, role, project, provider, account, cwd, window_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"sess{i}", "worker", "demo", "claude", "acct", "/tmp",
                 f"worker-{i}"),
            )
        conn.commit()
    finally:
        conn.close()
    return path


# --------------------------------------------------------------------- #
# Pipeline — additional check coverage that needs real-fs assertions.
# --------------------------------------------------------------------- #


def test_task_assignment_sweeper_dbs_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """One project, state.db present → ok status with singular project."""
    project_path = tmp_path / "proj-a"
    db = project_path / ".pollypm" / "state.db"
    db.parent.mkdir(parents=True)
    db.write_text("")
    fake_project = type("P", (), {"path": project_path, "tracked": True})
    fake_config = type("C", (), {"projects": {"proj-a": fake_project}})
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config),
    )
    result = doctor.check_task_assignment_sweeper_dbs()
    assert result.passed
    assert "1 tracked project" in result.status


def test_project_local_guide_drift_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    project_path = tmp_path / "proj-a"
    project_path.mkdir()
    fake_project = type("P", (), {"path": project_path, "name": "Project A"})
    fake_config = type("C", (), {"projects": {"proj-a": fake_project}})
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config),
    )
    monkeypatch.setattr(doctor, "_list_drifted_project_guides", lambda _path: [])
    result = doctor.check_project_local_guide_drift()
    assert result.passed
    assert "no stale project-local guides" in result.status


# --------------------------------------------------------------------- #
# Scheduler cadence — DELETED post-sqlite-ripout (refs #1971).
#
# The pre-ripout tests seeded ``type='event'`` rows into a tmp-path
# sqlite ``messages`` table via ``SQLAlchemyStore`` and monkeypatched
# ``_primary_state_db`` so the doctor read from that path. Post-ripout
# ``check_scheduler_last_fired`` resolves its store via
# ``get_store(load_config())`` (i.e. the configured pg backend), so a
# sqlite tmp-path seed is no longer visible to the check. The
# equivalent coverage belongs in a pg-backed test that pre-seeds the
# per-test schema's ``messages`` table via ``PgStore.record_event``
# and lets the doctor read it back; that re-port is a follow-up to
# this ripout.
# --------------------------------------------------------------------- #


# --------------------------------------------------------------------- #
# Resources — state DB size error path (fixable) + tmux/claude/codex
# AutoFix plans.
# --------------------------------------------------------------------- #


def test_state_db_size_error_and_fixable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``stat``-based size lookup at 3 GB → error + fixable."""
    db = tmp_path / "huge.db"
    db.write_bytes(b"\0")
    real_stat = db.stat()

    class _BigStat:
        st_size = 3 * 1024 * 1024 * 1024  # 3 GB

        def __getattr__(self, name: str):
            return getattr(real_stat, name)

    monkeypatch.setattr(
        Path, "stat", lambda self, **kw: _BigStat() if self == db else real_stat,
    )
    monkeypatch.setattr(doctor, "_state_db_candidates", lambda: [db])
    result = doctor.check_state_db_size()
    assert not result.passed
    assert result.severity == "error"
    assert result.fixable
    assert callable(result.fix_fn)


# --------------------------------------------------------------------- #
# Sessions table population — sqlite state DB read.
# --------------------------------------------------------------------- #


def test_sessions_table_populated_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = _make_state_db(tmp_path / "state.db", sessions=3)
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    result = doctor.check_sessions_table_populated()
    assert result.passed
    assert "3 row" in result.status


def test_sessions_table_populated_warn_when_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = _make_state_db(tmp_path / "state.db", sessions=0)
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    result = doctor.check_sessions_table_populated()
    assert not result.passed
    assert result.severity == "warning"
    assert result.fixable
    assert callable(result.fix_fn)


def test_sessions_table_populated_skip_without_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: None)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: None)
    result = doctor.check_sessions_table_populated()
    assert result.passed and result.skipped


# --------------------------------------------------------------------- #
# Sessions drift vs tmux — sqlite + monkeypatched tmux probe.
# --------------------------------------------------------------------- #


def test_session_drift_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = _make_state_db(tmp_path / "state.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO sessions "
        "(name, role, project, provider, account, cwd, window_name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("worker-x", "worker", "demo", "claude", "acct", "/tmp", "worker-x"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    monkeypatch.setattr(
        doctor, "_tool_path",
        lambda name: "/usr/bin/tmux" if name == "tmux" else None,
    )
    monkeypatch.setattr(
        doctor, "_run_cmd", lambda cmd, **kw: (0, "polly:worker-x"),
    )
    result = doctor.check_sessions_table_vs_tmux()
    assert result.passed


def test_session_drift_warn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = _make_state_db(tmp_path / "state.db")
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    monkeypatch.setattr(
        doctor, "_tool_path",
        lambda name: "/usr/bin/tmux" if name == "tmux" else None,
    )
    monkeypatch.setattr(
        doctor, "_run_cmd", lambda cmd, **kw: (0, "polly:worker-rogue"),
    )
    monkeypatch.setattr(doctor, "_planned_session_window_names", lambda: set())
    result = doctor.check_sessions_table_vs_tmux()
    assert not result.passed
    assert result.severity == "warning"
    assert "worker-rogue" in result.status
    assert result.fixable is False
    assert result.fix_fn is None
    assert result.data["orphan_drift"] == ["worker-rogue"]


def test_session_drift_plan_window_stays_fixable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = _make_state_db(tmp_path / "state.db")
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    monkeypatch.setattr(
        doctor,
        "_tool_path",
        lambda name: "/usr/bin/tmux" if name == "tmux" else None,
    )
    monkeypatch.setattr(
        doctor, "_run_cmd", lambda cmd, **kw: (0, "polly:worker-pollypm"),
    )
    monkeypatch.setattr(
        doctor, "_planned_session_window_names", lambda: {"worker-pollypm"},
    )

    result = doctor.check_sessions_table_vs_tmux()

    assert not result.passed
    assert result.fixable is True
    assert result.fix_fn is not None
    assert result.data["repairable_drift"] == ["worker-pollypm"]


def test_session_drift_skip_without_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: None)
    result = doctor.check_sessions_table_vs_tmux()
    assert result.passed and result.skipped


# --------------------------------------------------------------------- #
# Inbox aggregator path probe.
# --------------------------------------------------------------------- #


def test_inbox_aggregator_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    resolved = workspace / ".pollypm" / "state.db"
    resolved.parent.mkdir(parents=True)

    fake_project = type("P", (), {"workspace_root": workspace})
    fake_config = type("C", (), {"project": fake_project})
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config),
    )

    import pollypm.work.cli as work_cli
    monkeypatch.setattr(
        work_cli, "_resolve_db_path", lambda db, project=None: resolved,
    )
    result = doctor.check_inbox_aggregator_path()
    assert result.passed
    assert str(resolved) in result.status


def test_inbox_aggregator_warn_when_outside_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    resolved = tmp_path / "elsewhere" / ".pollypm" / "state.db"
    resolved.parent.mkdir(parents=True)

    fake_project = type("P", (), {"workspace_root": workspace})
    fake_config = type("C", (), {"project": fake_project})
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config),
    )

    import pollypm.work.cli as work_cli
    monkeypatch.setattr(
        work_cli, "_resolve_db_path", lambda db, project=None: resolved,
    )
    result = doctor.check_inbox_aggregator_path()
    assert not result.passed
    assert result.severity == "warning"
    assert "not under workspace root" in result.status


# --------------------------------------------------------------------- #
# CLI exit codes + --fix / --fix-dry-run semantics
# --------------------------------------------------------------------- #


def test_exit_code_zero_when_all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    def _pass() -> doctor.CheckResult:
        return doctor._ok("ok")

    monkeypatch.setattr(
        doctor, "_registered_checks",
        lambda: [doctor.Check("only", _pass, "pipeline")],
    )
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor"])
    assert result.exit_code == 0


def test_exit_code_zero_when_warnings_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: warnings do not flip the exit code."""
    def _warn() -> doctor.CheckResult:
        return doctor._fail("w", why="x", fix="y", severity="warning")

    monkeypatch.setattr(
        doctor, "_registered_checks",
        lambda: [doctor.Check("warns", _warn, "resources", severity="warning")],
    )
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor"])
    assert result.exit_code == 0


def test_exit_code_one_when_any_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _err() -> doctor.CheckResult:
        return doctor._fail("e", why="x", fix="y", severity="error")

    monkeypatch.setattr(
        doctor, "_registered_checks",
        lambda: [doctor.Check("breaks", _err, "pipeline")],
    )
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor"])
    assert result.exit_code == 1


def test_fix_runs_registered_fixers(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1063 — --fix verifies via re-running the check, so the fixture
    must actually flip fail -> pass when the fixer runs.
    """
    invoked = {"count": 0}
    state = {"healed": False}

    def _fixer() -> tuple[bool, str]:
        invoked["count"] += 1
        state["healed"] = True
        return (True, "fixed")

    def _check() -> doctor.CheckResult:
        if state["healed"]:
            return doctor._ok("now ok")
        return doctor._fail(
            "broken", why="w", fix="f",
            fixable=True, fix_fn=_fixer, severity="warning",
        )

    monkeypatch.setattr(
        doctor, "_registered_checks",
        lambda: [doctor.Check("fixme", _check, "resources", severity="warning")],
    )
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor", "--fix"])
    assert result.exit_code == 0
    assert invoked["count"] == 1
    assert "fixed" in result.stdout


def test_fix_cli_does_not_lie_when_handler_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1063 — --fix MUST NOT report 'Applied' when the handler returned
    (True, ...) but the check still fails on re-run.
    """
    def _liar_fix() -> tuple[bool, str]:
        return (True, "did nothing")

    def _stuck_check() -> doctor.CheckResult:
        return doctor._fail(
            "still broken", why="w", fix="run a thing",
            fixable=True, fix_fn=_liar_fix, severity="warning",
        )

    monkeypatch.setattr(
        doctor, "_registered_checks",
        lambda: [doctor.Check("liar", _stuck_check, "resources", severity="warning")],
    )
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor", "--fix"])
    assert result.exit_code == 0
    assert "Applied 1 fix" not in result.stdout
    assert "Applied 0 fixes" in result.stdout
    assert "ran but check" in result.stdout
    assert "liar" in result.stdout


def test_verify_fix_results_demotes_missing_check() -> None:
    """#1064 — when the post-fix report lacks a check entry, the
    verification step demotes the handler's self-report rather than
    trusting it.
    """
    raw = [("ghost-check", True, "handler says it worked")]
    empty_report = doctor.DoctorReport(results=[])
    verified = doctor.verify_fix_results(raw, empty_report)

    assert len(verified) == 1
    name, ok, message = verified[0]
    assert name == "ghost-check"
    assert ok is False
    assert "unavailable to verify" in message


def test_fix_dry_run_lists_planned_fixes_without_mutating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--fix-dry-run enumerates fixable checks but never invokes fix_fn."""
    invoked = {"count": 0}

    def _fixer() -> tuple[bool, str]:
        invoked["count"] += 1
        return (True, "fixed")

    def _check() -> doctor.CheckResult:
        return doctor._fail(
            "broken", why="w",
            fix="Trigger a one-off prune\nOr run:  pm doctor --fix",
            fixable=True, fix_fn=_fixer, severity="warning",
        )

    monkeypatch.setattr(
        doctor, "_registered_checks",
        lambda: [doctor.Check("fixme", _check, "resources", severity="warning")],
    )
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor", "--fix-dry-run"])
    assert result.exit_code == 0
    assert "Would apply 1 fix" in result.stdout
    assert "fixme" in result.stdout
    assert invoked["count"] == 0


def test_fix_dry_run_lists_manual_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cycle 56 — manual-intervention summary pluralises per count."""
    def _manual() -> doctor.CheckResult:
        return doctor._fail(
            "cant auto-fix", why="w",
            fix="Install Python manually",
            severity="error",
        )

    monkeypatch.setattr(
        doctor, "_registered_checks",
        lambda: [doctor.Check("needs-hands", _manual, "system")],
    )
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor", "--fix-dry-run"])
    assert result.exit_code == 1
    assert "1 issue requires manual intervention" in result.stdout
    assert "needs-hands" in result.stdout


def test_fix_dry_run_helpers_do_not_invoke_fixers() -> None:
    """``planned_fixes`` / ``manual_fixes`` never call ``fix_fn``."""
    invoked = {"count": 0}

    def _never() -> tuple[bool, str]:
        invoked["count"] += 1
        return (True, "nope")

    def _check() -> doctor.CheckResult:
        return doctor._fail(
            "x", why="w", fix="f1\nf2",
            fixable=True, fix_fn=_never, severity="warning",
        )

    report = doctor.run_checks([
        doctor.Check("c", _check, "pipeline", severity="warning"),
    ])
    planned = doctor.planned_fixes(report)
    manual = doctor.manual_fixes(report)
    assert planned == [("c", "f1")]
    assert manual == []
    assert invoked["count"] == 0


# --------------------------------------------------------------------- #
# Fix summary renderer pluralisation
# --------------------------------------------------------------------- #


def test_fix_summary_footer_counts_applied_and_remaining() -> None:
    """Cycle 56 — ``fix(es)`` / ``issue(s)`` parenthetical bans."""
    manual = [("needs-hands", "edit config manually"), ("another", "restart")]
    fix_results = [
        ("worktrees", True, "pruned 3"),
        ("logs", True, "rotated 1"),
        ("plan-gate", False, "write failed: permission denied"),
    ]
    summary = doctor.render_fix_summary(fix_results, manual)
    assert "Applied 2 fixes" in summary
    assert "worktrees" in summary and "logs" in summary
    assert "1 fix failed" in summary
    assert "plan-gate" in summary
    assert "2 issues remain" in summary
    assert "needs-hands" in summary
    assert "(es)" not in summary
    assert "(s)" not in summary


def test_fix_summary_footer_singular_pluralisation() -> None:
    one_applied = doctor.render_fix_summary(
        [("worktrees", True, "pruned 3")],
        [("needs-hands", "edit config")],
    )
    assert "Applied 1 fix:" in one_applied
    assert "1 issue remains" in one_applied
    assert "(es)" not in one_applied
    assert "(s)" not in one_applied


def test_fix_dry_run_pluralisation() -> None:
    one = doctor.render_fix_dry_run(
        [("worktrees", "would prune merged worktrees")],
        [("needs-hands", "edit config manually")],
    )
    assert "Would apply 1 fix:" in one
    assert "1 issue requires manual intervention" in one
    assert "(es)" not in one
    assert "(s)" not in one

    many = doctor.render_fix_dry_run(
        [("a", "intent-a"), ("b", "intent-b"), ("c", "intent-c")],
        [("m1", "do x"), ("m2", "do y")],
    )
    assert "Would apply 3 fixes:" in many
    assert "2 issues require manual intervention" in many
    assert "(es)" not in many
    assert "(s)" not in many


def test_manual_fixes_excludes_skipped_and_passing() -> None:
    """manual_fixes only lists failures that cannot auto-run."""
    def _pass() -> doctor.CheckResult:
        return doctor._ok("fine")

    def _skip_c() -> doctor.CheckResult:
        return doctor._skip("n/a")

    def _fixable() -> doctor.CheckResult:
        return doctor._fail(
            "x", why="w", fix="f",
            fixable=True, fix_fn=lambda: (True, "ok"),
        )

    def _manual_c() -> doctor.CheckResult:
        return doctor._fail("y", why="w", fix="do by hand")

    report = doctor.run_checks([
        doctor.Check("a", _pass, "pipeline"),
        doctor.Check("b", _skip_c, "pipeline"),
        doctor.Check("c", _fixable, "pipeline"),
        doctor.Check("d", _manual_c, "pipeline"),
    ])
    manual = doctor.manual_fixes(report)
    names = [n for n, _ in manual]
    assert names == ["d"]


# --------------------------------------------------------------------- #
# AutoFixPlan — system-tool missing exposes auto-fix command.
# --------------------------------------------------------------------- #


def test_tmux_missing_exposes_brew_auto_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor, "_tool_path",
        lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,
    )
    monkeypatch.setattr(doctor, "_current_platform", lambda: "macos")
    result = doctor.check_tmux()
    assert not result.passed
    assert result.auto_fix is not None
    assert result.auto_fix.command == ["brew", "install", "tmux"]
    assert result.auto_fix.platforms == ["macos"]


def test_tmux_missing_exposes_linux_package_manager_auto_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _tool_path(name: str) -> str | None:
        return "/usr/bin/apt-get" if name == "apt-get" else None

    monkeypatch.setattr(doctor, "_tool_path", _tool_path)
    monkeypatch.setattr(doctor, "_current_platform", lambda: "linux")
    result = doctor.check_tmux()
    assert not result.passed
    assert result.auto_fix is not None
    assert result.auto_fix.requires_sudo is True
    assert result.auto_fix.command == [
        "sudo", "apt-get", "install", "-y", "tmux",
    ]


def test_claude_cli_missing_has_auto_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _tool_path(name: str) -> str | None:
        return "/usr/bin/npm" if name == "npm" else None

    monkeypatch.setattr(doctor, "_tool_path", _tool_path)
    monkeypatch.setattr(doctor, "_current_platform", lambda: "linux")
    result = doctor.check_claude_cli()
    assert not result.passed
    assert result.auto_fix is not None
    assert result.auto_fix.command == [
        "npm", "i", "-g", "@anthropic-ai/claude-code",
    ]


def test_codex_cli_missing_has_auto_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _tool_path(name: str) -> str | None:
        return "/usr/bin/npm" if name == "npm" else None

    monkeypatch.setattr(doctor, "_tool_path", _tool_path)
    monkeypatch.setattr(doctor, "_current_platform", lambda: "linux")
    result = doctor.check_codex_cli()
    assert not result.passed
    assert result.auto_fix is not None
    assert result.auto_fix.command == ["npm", "i", "-g", "@openai/codex"]


def test_render_human_shows_fix_badge_for_supported_auto_fix() -> None:
    auto_fix = doctor.AutoFixPlan(
        description="Install tmux",
        command=["brew", "install", "tmux"],
        platforms=["macos", "linux"],
    )

    def _fail() -> doctor.CheckResult:
        return doctor._fail(
            "missing tmux", why="x", fix="y", auto_fix=auto_fix,
        )

    report = doctor.run_checks([doctor.Check("tmux", _fail, "system")])
    text = doctor.render_human(report)
    assert "[f] Fix" in text


def test_render_human_hides_fix_badge_for_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auto_fix = doctor.AutoFixPlan(
        description="Install tmux",
        command=["brew", "install", "tmux"],
        platforms=["macos"],
    )
    monkeypatch.setattr(doctor, "_current_platform", lambda: "windows")

    def _fail() -> doctor.CheckResult:
        return doctor._fail(
            "missing tmux", why="x", fix="y", auto_fix=auto_fix,
        )

    report = doctor.run_checks([doctor.Check("tmux", _fail, "system")])
    text = doctor.render_human(report)
    assert "[f] Fix" not in text


def test_json_output_includes_auto_fix_metadata() -> None:
    auto_fix = doctor.AutoFixPlan(
        description="Install Claude Code globally",
        command=["npm", "i", "-g", "@anthropic-ai/claude-code"],
        platforms=["macos", "linux"],
    )

    def _fail() -> doctor.CheckResult:
        return doctor._fail("missing", why="x", fix="y", auto_fix=auto_fix)

    report = doctor.run_checks([doctor.Check("claude", _fail, "system")])
    payload = json.loads(doctor.render_json(report))
    check = payload["checks"][0]
    assert check["auto_fix"]["description"] == "Install Claude Code globally"
    assert check["auto_fix"]["command"] == [
        "npm", "i", "-g", "@anthropic-ai/claude-code",
    ]
    assert check["auto_fix_available"] is True


def test_apply_fixes_runs_supported_auto_fix_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auto_fix = doctor.AutoFixPlan(
        description="Install Claude Code globally",
        command=["npm", "i", "-g", "@anthropic-ai/claude-code"],
        platforms=["macos", "linux"],
    )

    def _fail() -> doctor.CheckResult:
        return doctor._fail("missing", why="x", fix="y", auto_fix=auto_fix)

    monkeypatch.setattr(
        doctor, "run_auto_fix", lambda plan: (True, plan.description),
    )
    report = doctor.run_checks([doctor.Check("claude", _fail, "system")])
    assert doctor.apply_fixes(report) == [
        ("claude", True, "Install Claude Code globally"),
    ]


def test_planned_and_manual_fix_lists_treat_supported_auto_fix_as_runnable() -> None:
    auto_fix = doctor.AutoFixPlan(
        description="Install tmux",
        command=["brew", "install", "tmux"],
        platforms=["macos", "linux"],
    )

    def _fail() -> doctor.CheckResult:
        return doctor._fail(
            "missing tmux", why="x", fix="Install tmux", auto_fix=auto_fix,
        )

    report = doctor.run_checks([doctor.Check("tmux", _fail, "system")])
    assert doctor.planned_fixes(report) == [("tmux", "Install tmux")]
    assert doctor.manual_fixes(report) == []


# --------------------------------------------------------------------- #
# Plan-gate --fix rewriter + sweeper-DBs --fix initialiser.
# --------------------------------------------------------------------- #


def test_plan_gate_fix_rewrites_config_with_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """--fix on disabled plan gate writes a new config + .bak backup."""
    cfg_path = tmp_path / "pollypm.toml"
    cfg_path.write_text(
        "[project]\nname = 'x'\n\n[planner]\n"
        "enforce_plan = false\nplan_dir = 'docs/plan'\n"
    )

    fake_planner = type("P", (), {"enforce_plan": False, "plan_dir": "docs/plan"})
    fake_config = type("C", (), {"planner": fake_planner})
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (cfg_path, fake_config),
    )
    result = doctor.check_plan_presence_gate()
    assert not result.passed
    assert result.fixable
    assert callable(result.fix_fn)

    success, message = result.fix_fn()
    assert success, message
    bak = cfg_path.with_suffix(cfg_path.suffix + ".bak")
    assert bak.is_file()
    assert "enforce_plan = false" in bak.read_text()
    new_text = cfg_path.read_text()
    assert "enforce_plan = true" in new_text
    assert "enforce_plan = false" not in new_text


def test_plan_gate_fix_inserts_section_when_missing(tmp_path: Path) -> None:
    """The rewriter appends [planner] when no section exists yet."""
    cfg_path = tmp_path / "pollypm.toml"
    cfg_path.write_text("[project]\nname = 'x'\n")
    ok, _message = doctor._rewrite_planner_enforce_plan(cfg_path)
    assert ok
    text = cfg_path.read_text()
    assert "[planner]" in text
    assert "enforce_plan = true" in text


def test_task_assignment_sweeper_fix_initialises_state_dbs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """--fix creates state.db files for known-but-missing projects."""
    proj_a = tmp_path / "proj-a"
    proj_a.mkdir()
    proj_b = tmp_path / "proj-b"
    proj_b.mkdir()
    fake_projects = {
        "proj-a": type("P", (), {"path": proj_a, "tracked": True}),
        "proj-b": type("P", (), {"path": proj_b, "tracked": True}),
    }
    fake_config = type("C", (), {"projects": fake_projects})
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config),
    )
    result = doctor.check_task_assignment_sweeper_dbs()
    assert not result.passed
    assert result.fixable
    success, message = result.fix_fn()
    assert success, message
    assert (proj_a / ".pollypm" / "state.db").is_file()
    assert (proj_b / ".pollypm" / "state.db").is_file()


# --------------------------------------------------------------------- #
# Maintenance-handler invocations + pluralisation.
# --------------------------------------------------------------------- #


def test_agent_worktree_count_fix_invokes_prune(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """--fix on an over-threshold worktree dir calls the prune handler."""
    fake_dirs = [tmp_path / f"agent-{i}" for i in range(60)]
    for d in fake_dirs:
        d.mkdir()
    monkeypatch.setattr(doctor, "_agent_worktree_dirs", lambda: fake_dirs)
    result = doctor.check_agent_worktree_count()
    assert not result.passed
    assert result.fixable
    assert callable(result.fix_fn)

    called = {"n": 0}

    def _fake_handler(payload: dict) -> dict:
        called["n"] += 1
        return {
            "pruned": 3, "skipped_active": 0, "warned_stale": 0, "errors": 0,
        }

    from pollypm import maintenance_handlers_registry as reg
    monkeypatch.setattr(
        reg, "_handlers", {reg.AGENT_WORKTREE_PRUNE: _fake_handler},
    )
    success, message = result.fix_fn()
    assert success
    assert called["n"] == 1
    assert "pruned 3" in message


def test_logs_dir_size_fix_invokes_rotate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "big.log").write_bytes(b"\0" * (600 * 1024 * 1024))
    monkeypatch.setattr(doctor, "_logs_dir_candidates", lambda: [logs])
    result = doctor.check_logs_dir_size()
    assert not result.passed
    assert result.fixable
    assert callable(result.fix_fn)

    called = {"payload": None}

    def _fake_handler(payload: dict) -> dict:
        called["payload"] = payload
        return {"rotated": 1, "deleted": 0, "errors": 0}

    from pollypm import maintenance_handlers_registry as reg
    monkeypatch.setattr(reg, "_handlers", {reg.LOG_ROTATE: _fake_handler})
    success, message = result.fix_fn()
    assert success
    assert called["payload"] == {"logs_dir": str(logs)}
    assert "rotated 1" in message


def test_log_rotate_handler_message_pluralisation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``pm doctor --fix`` log-rotate message bans ``(s)`` parentheticals."""
    from pollypm import maintenance_handlers_registry as reg

    monkeypatch.setattr(
        reg, "_handlers",
        {reg.LOG_ROTATE: lambda payload: {
            "rotated": 1, "deleted": 1, "errors": 1,
        }},
    )
    success, message = doctor._invoke_log_rotate_handler(tmp_path)
    assert not success
    assert "rotated 1 log," in message
    assert "deleted 1 old archive," in message
    assert "1 error" in message
    assert "(s)" not in message

    monkeypatch.setattr(
        reg, "_handlers",
        {reg.LOG_ROTATE: lambda payload: {
            "rotated": 4, "deleted": 2, "errors": 0,
        }},
    )
    success, message = doctor._invoke_log_rotate_handler(tmp_path)
    assert success
    assert "rotated 4 logs," in message
    assert "deleted 2 old archives," in message
    assert "0 errors" in message
    assert "(s)" not in message


def test_prune_handler_message_pluralisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pollypm import maintenance_handlers_registry as reg

    monkeypatch.setattr(
        reg, "_handlers",
        {reg.AGENT_WORKTREE_PRUNE: lambda payload: {
            "pruned": 1, "warned_stale": 0, "errors": 1,
        }},
    )
    success, message = doctor._invoke_prune_handler()
    assert not success
    assert "pruned 1 merged worktree," in message
    assert "1 error" in message
    assert "(s)" not in message

    monkeypatch.setattr(
        reg, "_handlers",
        {reg.AGENT_WORKTREE_PRUNE: lambda payload: {
            "pruned": 5, "warned_stale": 0, "errors": 0,
        }},
    )
    success, message = doctor._invoke_prune_handler()
    assert success
    assert "pruned 5 merged worktrees," in message
    assert "0 errors" in message
    assert "(s)" not in message
