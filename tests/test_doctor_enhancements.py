"""Targeted coverage for the comprehensive ``pm doctor`` health checks.

Each new check (pipeline / schedulers / resources / inbox / sessions)
gets a PASS, a WARN, and (where applicable) a FAIL case. Tests use
``monkeypatch`` to swap helper internals so we never touch the real
filesystem, real tmux, or the real state DB outside of ``tmp_path``.

Run targeted (full pytest is forbidden by the task spec):

    HOME=/tmp/pytest-agent-doctor uv run pytest \
        tests/test_doctor_enhancements.py -q
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm import doctor


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _make_state_db(path: Path, *, sessions: int = 0) -> Path:
    """Create a synthetic state DB with the schema bits the checks read."""
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
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT,
                event_type TEXT,
                message TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        for i in range(sessions):
            conn.execute(
                "INSERT INTO sessions (name, role, project, provider, account, cwd, window_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"sess{i}", "worker", "demo", "claude", "acct", "/tmp", f"worker-{i}"),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _record_event(db_path: Path, event_type: str, *, age_seconds: int = 0) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO events (session_name, event_type, message, created_at) "
            "VALUES (?, ?, ?, datetime('now', ?))",
            ("system", event_type, "ok", f"-{age_seconds} seconds"),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# Pipeline checks
# --------------------------------------------------------------------- #


def test_plan_gate_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_planner = type("P", (), {"enforce_plan": True, "plan_dir": "docs/plan"})
    fake_config = type("C", (), {"planner": fake_planner})
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    result = doctor.check_plan_presence_gate()
    assert result.passed
    assert "enabled" in result.status


def test_plan_gate_warn_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_planner = type("P", (), {"enforce_plan": False, "plan_dir": "docs/plan"})
    fake_config = type("C", (), {"planner": fake_planner})
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    result = doctor.check_plan_presence_gate()
    assert not result.passed
    assert result.severity == "warning"
    assert "disabled" in result.status


def test_plan_gate_skip_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (None, None))
    result = doctor.check_plan_presence_gate()
    assert result.passed and result.skipped


def test_architect_profile_present() -> None:
    # The profile ships with the package — this is a real-fs assertion.
    result = doctor.check_architect_profile()
    assert result.passed
    assert "architect profile present" in result.status


def test_architect_profile_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_path = tmp_path / "src" / "pollypm" / "doctor.py"
    fake_path.parent.mkdir(parents=True)
    fake_path.write_text("# fake")
    monkeypatch.setattr(doctor, "__file__", str(fake_path))
    result = doctor.check_architect_profile()
    assert not result.passed
    assert "architect profile missing" in result.status


def test_visual_explainer_skill_present() -> None:
    result = doctor.check_visual_explainer_skill()
    assert result.passed


def test_visual_explainer_skill_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_path = tmp_path / "src" / "pollypm" / "doctor.py"
    fake_path.parent.mkdir(parents=True)
    fake_path.write_text("# fake")
    monkeypatch.setattr(doctor, "__file__", str(fake_path))
    result = doctor.check_visual_explainer_skill()
    assert not result.passed
    assert "visual-explainer" in result.status


def test_task_assignment_sweeper_dbs_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_path = tmp_path / "proj-a"
    db = project_path / ".pollypm" / "state.db"
    db.parent.mkdir(parents=True)
    db.write_text("")
    fake_project = type("P", (), {"path": project_path, "tracked": True})
    fake_config = type("C", (), {"projects": {"proj-a": fake_project}})
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    result = doctor.check_task_assignment_sweeper_dbs()
    assert result.passed
    assert "1 tracked project" in result.status


def test_task_assignment_sweeper_dbs_warn_all_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_path = tmp_path / "proj-b"
    project_path.mkdir()
    fake_project = type("P", (), {"path": project_path, "tracked": True})
    fake_config = type("C", (), {"projects": {"proj-b": fake_project}})
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    result = doctor.check_task_assignment_sweeper_dbs()
    assert not result.passed
    assert result.severity == "warning"
    assert "no tracked project" in result.status


def test_sessions_table_populated_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db", sessions=3)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    result = doctor.check_sessions_table_populated()
    assert result.passed
    assert "3 row" in result.status


def test_sessions_table_populated_warn_when_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db", sessions=0)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    result = doctor.check_sessions_table_populated()
    assert not result.passed
    assert result.severity == "warning"
    assert result.fixable
    assert callable(result.fix_fn)


def test_sessions_table_populated_skip_without_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: None)
    result = doctor.check_sessions_table_populated()
    assert result.passed and result.skipped


# --------------------------------------------------------------------- #
# Scheduler checks
# --------------------------------------------------------------------- #


def test_scheduler_handlers_pass() -> None:
    # Real plugin import path — the builtin plugins ship with the package.
    result = doctor.check_scheduler_roster_handlers()
    assert result.passed
    assert "registered" in result.status


def test_scheduler_handlers_warn_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_expected_handlers_from_plugins", lambda: {"db.vacuum"})
    result = doctor.check_scheduler_roster_handlers()
    assert not result.passed
    assert "missing scheduled handler" in result.status


def test_scheduler_cadence_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    # Fresh event for every expected handler — all healthy.
    for handler in doctor._HANDLER_MAX_GAP_SECONDS:
        _record_event(db, handler, age_seconds=10)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    result = doctor.check_scheduler_cadence() if False else doctor.check_scheduler_last_fired()
    assert result.passed
    assert "within cadence" in result.status


def test_scheduler_cadence_warn_when_overdue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    # db.vacuum max gap is 2 days; record an event 5 days old.
    _record_event(db, "db.vacuum", age_seconds=5 * 86400)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    result = doctor.check_scheduler_last_fired()
    assert not result.passed
    assert result.severity == "warning"
    assert "overdue" in result.status


def test_scheduler_cadence_pass_when_no_events(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    result = doctor.check_scheduler_last_fired()
    # Fresh-install case: no events yet, no overdue → passes informationally.
    assert result.passed


# --------------------------------------------------------------------- #
# Resource checks
# --------------------------------------------------------------------- #


def test_state_db_size_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    monkeypatch.setattr(doctor, "_state_db_candidates", lambda: [db])
    result = doctor.check_state_db_size()
    assert result.passed
    assert "MB" in result.status


def test_state_db_size_warn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "big.db"
    db.write_bytes(b"\0" * (600 * 1024 * 1024))  # 600 MB → warn
    monkeypatch.setattr(doctor, "_state_db_candidates", lambda: [db])
    result = doctor.check_state_db_size()
    assert not result.passed
    assert result.severity == "warning"
    assert "warn at" in result.status


def test_state_db_size_error_and_fixable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # We can't easily create a 2 GB file in tests — instead patch
    # ``stat`` via a wrapper class that lies about size.
    db = tmp_path / "huge.db"
    db.write_bytes(b"\0")
    real_stat = db.stat()

    class _BigStat:
        st_size = 3 * 1024 * 1024 * 1024  # 3 GB → error

        def __getattr__(self, name: str):
            return getattr(real_stat, name)

    monkeypatch.setattr(Path, "stat", lambda self, **kw: _BigStat() if self == db else real_stat)  # type: ignore[arg-type]
    monkeypatch.setattr(doctor, "_state_db_candidates", lambda: [db])
    result = doctor.check_state_db_size()
    assert not result.passed
    assert result.severity == "error"
    assert result.fixable
    assert callable(result.fix_fn)


def test_agent_worktree_count_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dirs = [tmp_path / f"agent-{i}" for i in range(5)]
    for d in fake_dirs:
        d.mkdir()
    monkeypatch.setattr(doctor, "_agent_worktree_dirs", lambda: fake_dirs)
    result = doctor.check_agent_worktree_count()
    assert result.passed
    assert "5 agent worktree" in result.status


def test_agent_worktree_count_warn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_dirs = [tmp_path / f"agent-{i}" for i in range(60)]
    for d in fake_dirs:
        d.mkdir()
    monkeypatch.setattr(doctor, "_agent_worktree_dirs", lambda: fake_dirs)
    result = doctor.check_agent_worktree_count()
    assert not result.passed
    assert result.severity == "warning"


def test_logs_dir_size_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "a.log").write_bytes(b"hi")
    monkeypatch.setattr(doctor, "_logs_dir_candidates", lambda: [logs])
    result = doctor.check_logs_dir_size()
    assert result.passed


def test_logs_dir_size_warn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "big.log").write_bytes(b"\0" * (600 * 1024 * 1024))
    monkeypatch.setattr(doctor, "_logs_dir_candidates", lambda: [logs])
    result = doctor.check_logs_dir_size()
    assert not result.passed
    assert result.severity == "warning"


def test_logs_dir_size_skip_when_no_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_logs_dir_candidates", lambda: [])
    result = doctor.check_logs_dir_size()
    assert result.passed and result.skipped


def test_session_memory_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor, "_ps_claude_rss_kb",
        lambda: [(1, 50_000, "claude --headless"), (2, 80_000, "codex")],
    )
    result = doctor.check_session_memory_usage()
    assert result.passed
    assert "2 session" in result.status


def test_session_memory_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor, "_ps_claude_rss_kb",
        lambda: [(99, 1_500_000, "claude --headless")],  # 1.5 GB
    )
    result = doctor.check_session_memory_usage()
    assert not result.passed
    assert result.severity == "warning"
    assert "over 1 GB" in result.status


def test_session_memory_skip_when_no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_ps_claude_rss_kb", lambda: [])
    result = doctor.check_session_memory_usage()
    assert result.passed and result.skipped


# --------------------------------------------------------------------- #
# Inbox checks
# --------------------------------------------------------------------- #


def test_inbox_aggregator_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    resolved = workspace / ".pollypm" / "state.db"
    resolved.parent.mkdir(parents=True)

    fake_project = type("P", (), {"workspace_root": workspace})
    fake_config = type("C", (), {"project": fake_project})
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))

    import pollypm.work.cli as work_cli
    monkeypatch.setattr(work_cli, "_resolve_db_path", lambda db, project=None: resolved)
    result = doctor.check_inbox_aggregator_path()
    assert result.passed
    assert str(resolved) in result.status


def test_inbox_aggregator_warn_when_outside_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    resolved = tmp_path / "elsewhere" / ".pollypm" / "state.db"
    resolved.parent.mkdir(parents=True)

    fake_project = type("P", (), {"workspace_root": workspace})
    fake_config = type("C", (), {"project": fake_project})
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))

    import pollypm.work.cli as work_cli
    monkeypatch.setattr(work_cli, "_resolve_db_path", lambda db, project=None: resolved)
    result = doctor.check_inbox_aggregator_path()
    assert not result.passed
    assert result.severity == "warning"
    assert "not under workspace root" in result.status


def test_inbox_open_count_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_config = object()
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    import pollypm.dashboard_data as dd
    monkeypatch.setattr(dd, "_count_inbox_tasks", lambda cfg: 5)
    result = doctor.check_inbox_open_count()
    assert result.passed
    assert "5 open inbox" in result.status


def test_inbox_open_count_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_config = object()
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    import pollypm.dashboard_data as dd
    monkeypatch.setattr(dd, "_count_inbox_tasks", lambda cfg: 99)
    result = doctor.check_inbox_open_count()
    assert not result.passed
    assert result.severity == "warning"


# --------------------------------------------------------------------- #
# Sessions checks
# --------------------------------------------------------------------- #


def test_session_drift_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO sessions (name, role, project, provider, account, cwd, window_name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("worker-x", "worker", "demo", "claude", "acct", "/tmp", "worker-x"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (0, "polly:worker-x"))
    result = doctor.check_sessions_table_vs_tmux()
    assert result.passed


def test_session_drift_warn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")  # no sessions rows
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    # tmux reports a worker window the DB doesn't know about.
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (0, "polly:worker-rogue"))
    result = doctor.check_sessions_table_vs_tmux()
    assert not result.passed
    assert result.severity == "warning"
    assert "worker-rogue" in result.status


def test_session_drift_skip_without_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: None)
    result = doctor.check_sessions_table_vs_tmux()
    assert result.passed and result.skipped


def test_persona_swap_defense_pass() -> None:
    # Real-fs assertion — supervisor.py ships with the assertion wired.
    result = doctor.check_persona_swap_defense_wired()
    assert result.passed


def test_persona_swap_defense_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_supervisor = tmp_path / "supervisor.py"
    fake_supervisor.write_text("def boot():\n    return None\n")
    fake_doctor = tmp_path / "doctor.py"
    fake_doctor.write_text("# fake")
    monkeypatch.setattr(doctor, "__file__", str(fake_doctor))
    result = doctor.check_persona_swap_defense_wired()
    assert not result.passed
    assert "_assert_session_launch_matches" in result.status


# --------------------------------------------------------------------- #
# Renderer + summary footer
# --------------------------------------------------------------------- #


def test_render_human_includes_section_headers_and_footer() -> None:
    def _ok_check() -> doctor.CheckResult:
        return doctor._ok("good")

    def _warn_check() -> doctor.CheckResult:
        return doctor._fail("meh", why="w", fix="f", severity="warning")

    report = doctor.run_checks([
        doctor.Check("a", _ok_check, "pipeline"),
        doctor.Check("b", _warn_check, "resources", severity="warning"),
        doctor.Check("c", _ok_check, "sessions"),
    ])
    text = doctor.render_human(report)
    assert "-- Pipeline --" in text
    assert "-- Resources --" in text
    assert "-- Sessions --" in text
    # Compact footer: "<total> checks · <passed> passed · <warnings> warnings · <errors> errors"
    assert "3 checks" in text
    assert "2 passed" in text
    assert "1 warnings" in text
    assert "0 errors" in text


def test_summary_counts_are_accurate() -> None:
    """N checks · P passed · W warnings · E errors stays consistent."""
    def _pass() -> doctor.CheckResult:
        return doctor._ok("ok")

    def _warn() -> doctor.CheckResult:
        return doctor._fail("w", why="x", fix="y", severity="warning")

    def _err() -> doctor.CheckResult:
        return doctor._fail("e", why="x", fix="y", severity="error")

    checks = [
        doctor.Check("p1", _pass, "pipeline"),
        doctor.Check("p2", _pass, "pipeline"),
        doctor.Check("w1", _warn, "resources", severity="warning"),
        doctor.Check("e1", _err, "sessions"),
    ]
    report = doctor.run_checks(checks)
    text = doctor.render_human(report)
    assert "4 checks · 2 passed · 1 warnings · 1 errors" in text


# --------------------------------------------------------------------- #
# JSON output validity
# --------------------------------------------------------------------- #


def test_json_output_is_valid_for_new_categories() -> None:
    def _pass() -> doctor.CheckResult:
        return doctor._ok("ok", data={"foo": 1})

    report = doctor.run_checks([
        doctor.Check("pipeline-x", _pass, "pipeline"),
        doctor.Check("resource-x", _pass, "resources"),
        doctor.Check("inbox-x", _pass, "inbox"),
    ])
    payload = json.loads(doctor.render_json(report))
    assert payload["ok"] is True
    categories = {c["category"] for c in payload["checks"]}
    assert categories == {"pipeline", "resources", "inbox"}
    assert payload["summary"]["total"] == 3
    assert payload["summary"]["passed"] == 3


def test_cli_doctor_json_with_new_checks() -> None:
    """End-to-end: --json output remains parseable with new checks registered."""
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor", "--json"])
    assert result.exit_code in (0, 1)
    payload = json.loads(result.stdout)
    categories = {c["category"] for c in payload["checks"]}
    # Confirm every new category appears.
    for expected in ("pipeline", "schedulers", "resources", "inbox", "sessions"):
        assert expected in categories, f"{expected} missing from {categories}"


# --------------------------------------------------------------------- #
# Exit codes
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


def test_exit_code_zero_when_warnings_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def _warn() -> doctor.CheckResult:
        return doctor._fail("w", why="x", fix="y", severity="warning")

    monkeypatch.setattr(
        doctor, "_registered_checks",
        lambda: [doctor.Check("warns", _warn, "resources", severity="warning")],
    )
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor"])
    # Spec: warnings do not flip the exit code.
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


# --------------------------------------------------------------------- #
# --fix invokes registered fixers (mock side-effects)
# --------------------------------------------------------------------- #


def test_fix_runs_registered_fixers(monkeypatch: pytest.MonkeyPatch) -> None:
    invoked = {"count": 0}

    def _fixer() -> tuple[bool, str]:
        invoked["count"] += 1
        return (True, "fixed")

    def _check() -> doctor.CheckResult:
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
    # Warning-only check → exit 0 even before/after fix.
    assert result.exit_code == 0
    assert invoked["count"] == 1
    assert "fixed" in result.stdout
