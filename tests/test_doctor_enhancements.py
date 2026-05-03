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
    """Create a synthetic state DB with the schema bits the checks read.

    Installs the ``sessions`` table (domain table still owned by
    :class:`StateStore`) plus bootstraps the unified ``messages``
    schema via :class:`SQLAlchemyStore` so ``_record_event`` can write
    ``type='event'`` rows the scheduler-cadence check consumes.
    """
    from pollypm.store import SQLAlchemyStore

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
                "INSERT INTO sessions (name, role, project, provider, account, cwd, window_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"sess{i}", "worker", "demo", "claude", "acct", "/tmp", f"worker-{i}"),
            )
        conn.commit()
    finally:
        conn.close()
    # Bootstrap the unified ``messages`` schema on the same DB.
    SQLAlchemyStore(f"sqlite:///{path}").close()
    return path


def _record_event(db_path: Path, event_type: str, *, age_seconds: int = 0) -> None:
    """Insert a ``type='event'`` row keyed by ``event_type``, back-dated.

    #342 moved events onto the unified ``messages`` table — the
    scheduler-cadence check queries :meth:`Store.query_messages` and
    reads ``subject`` (or ``payload.event_type``) as the handler name.
    """
    import json as _json
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import insert as _insert

    from pollypm.store import SQLAlchemyStore
    from pollypm.store.schema import messages as _messages

    created_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    msg_store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        with msg_store.transaction() as conn:
            conn.execute(
                _insert(_messages),
                {
                    "scope": "system",
                    "type": "event",
                    "tier": "immediate",
                    "recipient": "*",
                    "sender": "system",
                    "state": "open",
                    "subject": event_type,
                    "body": "ok",
                    "payload_json": _json.dumps({"event_type": event_type}),
                    "labels": "[]",
                    "created_at": created_at,
                    "updated_at": created_at,
                },
            )
    finally:
        msg_store.close()


def _doctor_role_config(tmp_path: Path):
    from pollypm.models import (
        AccountConfig,
        KnownProject,
        PollyPMConfig,
        PollyPMSettings,
        ProjectKind,
        ProjectSettings,
        ProviderKind,
    )

    project_path = tmp_path / "demo"
    project_path.mkdir()
    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm" / "logs",
            snapshots_dir=tmp_path / ".pollypm" / "snapshots",
            state_db=tmp_path / ".pollypm" / "state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=tmp_path / ".pollypm" / "homes" / "claude_main",
            ),
            "codex_main": AccountConfig(
                name="codex_main",
                provider=ProviderKind.CODEX,
                home=tmp_path / ".pollypm" / "homes" / "codex_main",
            ),
        },
        sessions={},
        projects={
            "demo": KnownProject(
                key="demo",
                path=project_path,
                kind=ProjectKind.FOLDER,
            )
        },
    )


# --------------------------------------------------------------------- #
# Roles checks
# --------------------------------------------------------------------- #


def test_role_assignment_checks_skip_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (None, None))

    report = doctor.run_checks(doctor._role_assignment_checks())

    assert len(report.results) == 1
    check, result = report.results[0]
    assert check.category == "roles"
    assert result.passed and result.skipped


def test_role_assignment_checks_enumerate_global_and_project_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pollypm.models import ModelAssignment

    config = _doctor_role_config(tmp_path)
    config.pollypm.role_assignments["architect"] = ModelAssignment(alias="opus-4.7")
    config.projects["demo"].role_assignments["worker"] = ModelAssignment(
        provider="codex",
        model="gpt-5.4",
    )
    monkeypatch.setattr(
        doctor,
        "_safe_load_config",
        lambda: (tmp_path / "pollypm.toml", config),
    )

    calls: list[tuple[str, str, tuple[str, ...]]] = []

    def _probe(project, provider, model, accounts, *, timeout_seconds=0.0):
        del project, timeout_seconds
        calls.append((provider, model, tuple(name for name, _account in accounts)))
        return doctor._RoleModelSmokeResult(ok=True, account_name=accounts[0][0])

    monkeypatch.setattr(doctor, "_probe_role_model_access", _probe)

    report = doctor.run_checks(doctor._role_assignment_checks())
    text = doctor.render_human(report)

    assert report.ok
    assert "-- Roles --" in text
    assert "global.architect" in text
    assert "project.demo.worker" in text
    assert "claude/claude-opus-4-7 via alias opus-4.7, source=global, reachable via claude_main" in text
    assert "codex/gpt-5.4, source=project, reachable via codex_main" in text
    assert calls == [
        ("claude", "claude-opus-4-7", ("claude_main",)),
        ("codex", "gpt-5.4", ("codex_main",)),
    ]


def test_role_assignment_unknown_alias_is_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pollypm.models import ModelAssignment

    config = _doctor_role_config(tmp_path)
    config.pollypm.role_assignments["architect"] = ModelAssignment(alias="missing")
    monkeypatch.setattr(
        doctor,
        "_safe_load_config",
        lambda: (tmp_path / "pollypm.toml", config),
    )

    report = doctor.run_checks(doctor._role_assignment_checks())

    check, result = report.results[0]
    assert check.name == "global.architect"
    assert not result.passed
    assert result.severity == "error"
    assert "unknown alias" in result.status
    assert "fall through" in result.why


def test_role_assignment_unknown_provider_is_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pollypm.models import ModelAssignment

    config = _doctor_role_config(tmp_path)
    config.pollypm.role_assignments["reviewer"] = ModelAssignment(
        provider="bogus",
        model="bogus-1",
    )
    monkeypatch.setattr(
        doctor,
        "_safe_load_config",
        lambda: (tmp_path / "pollypm.toml", config),
    )

    report = doctor.run_checks(doctor._role_assignment_checks())

    check, result = report.results[0]
    assert check.name == "global.reviewer"
    assert not result.passed
    assert result.severity == "error"
    assert "unknown provider" in result.status
    assert "bogus" in result.why


def test_role_assignment_probe_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pollypm.models import ModelAssignment

    config = _doctor_role_config(tmp_path)
    config.pollypm.role_assignments["worker"] = ModelAssignment(
        provider="codex",
        model="gpt-5.4",
    )
    monkeypatch.setattr(
        doctor,
        "_safe_load_config",
        lambda: (tmp_path / "pollypm.toml", config),
    )
    monkeypatch.setattr(
        doctor,
        "_probe_role_model_access",
        lambda *args, **kwargs: doctor._RoleModelSmokeResult(
            ok=False,
            account_name=None,
            attempts=("codex_main: provider rejected the model",),
        ),
    )

    report = doctor.run_checks(doctor._role_assignment_checks())

    _check, result = report.results[0]
    assert not result.passed
    assert result.severity == "error"
    assert "smoke probe failed" in result.status
    assert "Probe attempts: codex_main: provider rejected the model" in result.fix


def test_role_assignment_advisories_warn_and_smoke_is_cached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pollypm.models import ModelAssignment

    config = _doctor_role_config(tmp_path)
    config.pollypm.role_assignments["architect"] = ModelAssignment(alias="haiku-4.5")
    config.projects["demo"].role_assignments["reviewer"] = ModelAssignment(alias="haiku-4.5")
    monkeypatch.setattr(
        doctor,
        "_safe_load_config",
        lambda: (tmp_path / "pollypm.toml", config),
    )

    calls: list[tuple[str, str]] = []

    def _probe(project, provider, model, accounts, *, timeout_seconds=0.0):
        del project, accounts, timeout_seconds
        calls.append((provider, model))
        return doctor._RoleModelSmokeResult(ok=True, account_name="claude_main")

    monkeypatch.setattr(doctor, "_probe_role_model_access", _probe)

    report = doctor.run_checks(doctor._role_assignment_checks())
    warnings = [
        result
        for _check, result in report.results
        if not result.passed and result.severity == "warning"
    ]

    assert len(warnings) == 2
    assert calls == [("claude", "claude-haiku-4-5-20251001")]
    assert all("weak_planning" in result.why for result in warnings)


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


def test_task_assignment_sweeper_dbs_pluralisation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Singular and plural sweeper-dbs status must not show ``project(s)``.

    Both the warn ("X missing state.db") and ok ("X have reachable
    state.db") paths share the project pluralisation. The ok branch
    also needs subject/verb agreement (``1 project has`` /
    ``5 projects have``). Mirrors cycles 45/47/48/49 on other doctor
    messages.
    """
    one_path = tmp_path / "proj-one"
    db = one_path / ".pollypm" / "state.db"
    db.parent.mkdir(parents=True)
    db.write_text("")
    one_proj = type("P", (), {"path": one_path, "tracked": True})
    monkeypatch.setattr(
        doctor,
        "_safe_load_config",
        lambda: (Path("/tmp/x"), type("C", (), {"projects": {"proj-one": one_proj}})),
    )
    ok = doctor.check_task_assignment_sweeper_dbs()
    assert ok.passed
    assert "1 tracked project has reachable state.db" in ok.status
    assert "project(s)" not in ok.status

    # Mixed: one with state.db, two without — exercises the plural
    # warn path (``2 tracked projects missing state.db``).
    have_path = tmp_path / "have"
    have_db = have_path / ".pollypm" / "state.db"
    have_db.parent.mkdir(parents=True)
    have_db.write_text("")
    miss_a = tmp_path / "miss-a"
    miss_a.mkdir()
    miss_b = tmp_path / "miss-b"
    miss_b.mkdir()
    cfg = type(
        "C", (), {"projects": {
            "have": type("P", (), {"path": have_path, "tracked": True}),
            "miss-a": type("P", (), {"path": miss_a, "tracked": True}),
            "miss-b": type("P", (), {"path": miss_b, "tracked": True}),
        }},
    )
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), cfg))
    warn = doctor.check_task_assignment_sweeper_dbs()
    assert not warn.passed
    assert "2 tracked projects missing state.db" in warn.status
    assert "project(s)" not in warn.status


def test_sessions_table_populated_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db", sessions=3)
    # #1066: the check now reads from ``_supervisor_state_db`` (the path
    # the supervisor actually writes to). Patch both helpers so the test
    # is robust against the fallback ordering.
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    result = doctor.check_sessions_table_populated()
    assert result.passed
    assert "3 row" in result.status


def test_sessions_table_populated_warn_when_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db", sessions=0)
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    result = doctor.check_sessions_table_populated()
    assert not result.passed
    assert result.severity == "warning"
    assert result.fixable
    assert callable(result.fix_fn)


def test_sessions_table_populated_skip_without_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: None)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: None)
    result = doctor.check_sessions_table_populated()
    assert result.passed and result.skipped


def test_sessions_table_populated_reads_supervisor_db_not_workspace_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Regression: #1066. ``check_sessions_table_populated`` must read from
    the path the supervisor's ``StateStore`` writes to (typically
    ``config.project.state_db`` = ``~/.pollypm/state.db``), not the
    workspace-scope DB at ``<workspace_root>/.pollypm/state.db``.

    Pre-fix, ``_primary_state_db()`` returned the workspace DB. The
    supervisor's ``repair_sessions_table`` upserted into the
    user-scope DB, so the doctor check kept reporting "sessions table
    empty" even after ``--fix`` succeeded — the canonical "fix ran but
    check still failing" symptom of the umbrella issue.
    """
    supervisor_db = _make_state_db(tmp_path / "supervisor.db", sessions=4)
    workspace_db = _make_state_db(tmp_path / "workspace.db", sessions=0)

    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: supervisor_db)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: workspace_db)

    result = doctor.check_sessions_table_populated()
    assert result.passed
    assert "4 row" in result.status
    # The check reports which DB it consulted — must be the supervisor's,
    # never the (empty) workspace DB.
    assert result.data["db"] == str(supervisor_db)


def test_repair_sessions_table_populates_empty_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Regression: #1066. The handler must actually upsert rows when
    given an empty sessions table and live tmux windows that match the
    configured launch plan. A row count of zero before and zero after is
    the silent-no-op shape the umbrella issue calls out.
    """
    from pollypm.tmux.client import TmuxWindow

    from tests.test_sessions_registration import _config  # reuse fixture

    config = _config(tmp_path)
    from pollypm.supervisor import Supervisor
    supervisor = Supervisor(config)

    # Precondition: empty sessions table.
    assert supervisor.store.list_sessions() == []

    storage = supervisor.storage_closet_session_name()
    live_names = ["pm-heartbeat", "pm-operator", "pm-reviewer"]
    monkeypatch.setattr(supervisor.tmux, "has_session", lambda n: n == storage)
    monkeypatch.setattr(
        supervisor.tmux,
        "list_windows",
        lambda n: [
            TmuxWindow(
                session=storage,
                index=i,
                name=w,
                active=(i == 0),
                pane_id=f"%{i + 50}",
                pane_current_command="claude",
                pane_current_path=str(tmp_path),
                pane_dead=False,
            )
            for i, w in enumerate(live_names)
        ] if n == storage else [],
    )

    upserted = supervisor.repair_sessions_table()
    assert upserted == 3, "expected three rows upserted, got " + str(upserted)
    assert len(supervisor.store.list_sessions()) == 3

    # Idempotence: rerunning on a populated table is a non-destructive
    # no-op (no duplicates, no rows lost).
    rerun = supervisor.repair_sessions_table()
    assert rerun == 3
    assert len(supervisor.store.list_sessions()) == 3


def test_project_local_guide_drift_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_path = tmp_path / "proj-a"
    project_path.mkdir()
    fake_project = type("P", (), {"path": project_path, "name": "Project A"})
    fake_config = type("C", (), {"projects": {"proj-a": fake_project}})
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    monkeypatch.setattr(doctor, "_list_drifted_project_guides", lambda _path: [])
    result = doctor.check_project_local_guide_drift()
    assert result.passed
    assert "no stale project-local guides" in result.status


def test_project_local_guide_drift_warns_when_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    project_path = tmp_path / "proj-b"
    project_path.mkdir()
    fake_project = type("P", (), {"path": project_path, "name": "Project B"})
    fake_config = type("C", (), {"projects": {"proj-b": fake_project}})
    guide_path = project_path / ".pollypm" / "project-guides" / "worker.md"
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    monkeypatch.setattr(
        doctor,
        "_list_drifted_project_guides",
        lambda _path: [
            {
                "role": "worker",
                "path": guide_path,
                "forked_from": "deadbeef",
                "current_ref": "cafebabe",
                "drifted": True,
            }
        ],
    )
    result = doctor.check_project_local_guide_drift()
    assert not result.passed
    assert result.severity == "warning"
    assert "proj-b:worker" in result.status
    assert "pm project init-guide worker --project proj-b --force" in result.fix
    assert result.data["stale"][0]["project"] == "proj-b"


def test_project_local_guide_drift_skip_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (None, None))
    result = doctor.check_project_local_guide_drift()
    assert result.passed and result.skipped


def test_project_local_guide_drift_marks_truncation_in_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Cycle 101 — when more than 4 guides are stale, the headline
    sample (first 4) was indistinguishable from the complete list:
    "across 9 projects: a, b, c, d" looks final. Readers acted on
    those four and missed the rest. The fix block already showed
    "... and N more"; mirror it in the summary so the user sees
    the truncation up-front.
    """
    project_path = tmp_path / "multi"
    project_path.mkdir()
    projects = {}
    drift_entries = []
    for idx in range(6):
        key = f"proj-{idx}"
        projects[key] = type("P", (), {"path": project_path, "name": key})
        drift_entries.append(
            {
                "role": "worker",
                "path": project_path / ".pollypm" / "project-guides" / "worker.md",
                "forked_from": "deadbeef",
                "current_ref": "cafebabe",
                "drifted": True,
                "project": key,
            }
        )
    fake_config = type("C", (), {"projects": projects})
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    # Each project surfaces one stale guide — 6 entries total, > 4.
    iter_drift = iter(drift_entries)
    monkeypatch.setattr(
        doctor,
        "_list_drifted_project_guides",
        lambda _path: [next(iter_drift)],
    )
    result = doctor.check_project_local_guide_drift()
    assert not result.passed
    # The headline must announce the overflow, not silently truncate.
    assert "(+2 more)" in result.status
    # And the fix block keeps its own overflow note.
    assert "... and 2 more" in result.fix


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


def test_scheduler_handlers_pluralisation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cycle 69: missing-handler status uses ``handler`` / ``handlers`` per count.

    The previous text was ``missing scheduled handler(s):`` —
    parenthetical plural. With exactly one handler missing, that
    reads as a copy bug in the most user-visible doctor surface
    after a partial install.
    """
    # Almost-complete declared set: drop one expected handler so
    # exactly one is missing.
    expected = list(doctor._EXPECTED_SCHEDULED_HANDLERS)
    declared_one_missing = set(expected[1:])  # drop the first
    monkeypatch.setattr(
        doctor, "_expected_handlers_from_plugins", lambda: declared_one_missing,
    )
    one_missing = doctor.check_scheduler_roster_handlers()
    assert not one_missing.passed
    assert "missing scheduled handler:" in one_missing.status
    assert "handler(s)" not in one_missing.status

    # Two missing → plural.
    declared_two_missing = set(expected[2:])
    monkeypatch.setattr(
        doctor, "_expected_handlers_from_plugins", lambda: declared_two_missing,
    )
    two_missing = doctor.check_scheduler_roster_handlers()
    assert not two_missing.passed
    assert "missing scheduled handlers:" in two_missing.status
    assert "handler(s)" not in two_missing.status


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


def test_agent_worktree_count_pluralisation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Singular and plural worktree counts must not render ``worktree(s)``.

    Same shape as the inbox-count check (cycles 45/47): the doctor
    status string is shown verbatim to the user, so the parenthetical
    plural reads as a copy bug at count=1. Lock the literal out of
    both warn and ok status strings.
    """
    one = [tmp_path / "agent-1"]
    one[0].mkdir()
    monkeypatch.setattr(doctor, "_agent_worktree_dirs", lambda: one)
    ok = doctor.check_agent_worktree_count()
    assert "1 agent worktree under" in ok.status
    assert "worktree(s)" not in ok.status

    many = [tmp_path / f"agent-{i}" for i in range(2, 65)]
    for d in many:
        d.mkdir()
    monkeypatch.setattr(doctor, "_agent_worktree_dirs", lambda: many)
    warn = doctor.check_agent_worktree_count()
    assert "agent worktrees under" in warn.status
    assert "worktree(s)" not in warn.status


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


def test_session_memory_pluralisation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Singular and plural session counts must not show ``session(s)``.

    Both the warn (``X session(s) over 1 GB RSS``) and ok (``X
    session(s), largest …``) paths share the count noun. Lock both
    out at the singular boundary, mirroring cycles 47/48/50 on other
    doctor checks.
    """
    # Single session over the warn threshold.
    monkeypatch.setattr(
        doctor, "_ps_claude_rss_kb",
        lambda: [(7, 1_500_000, "claude --headless")],
    )
    warn = doctor.check_session_memory_usage()
    assert not warn.passed
    assert "1 session over 1 GB RSS" in warn.status
    assert "session(s)" not in warn.status

    # Single session under threshold (ok path, count=1).
    monkeypatch.setattr(
        doctor, "_ps_claude_rss_kb",
        lambda: [(7, 50_000, "claude --headless")],
    )
    one_ok = doctor.check_session_memory_usage()
    assert one_ok.passed
    assert "1 session, largest" in one_ok.status
    assert "session(s)" not in one_ok.status

    # Plural ok path stays plural.
    monkeypatch.setattr(
        doctor, "_ps_claude_rss_kb",
        lambda: [(1, 50_000, "claude"), (2, 80_000, "codex")],
    )
    many_ok = doctor.check_session_memory_usage()
    assert many_ok.passed
    assert "2 sessions, largest" in many_ok.status
    assert "session(s)" not in many_ok.status


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


def test_inbox_open_count_pluralisation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Singular and plural inbox counts must not render ``item(s)``.

    ``pm doctor`` runs every install/CI/recovery sweep — the
    parenthetical-s pluralisation always reads as a copy bug at
    count=1. Cycle 45 made this fix on 5 other doctor messages; this
    locks the same shape for the inbox-count check (both pass and
    warn paths share the word).
    """
    fake_config = object()
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    import pollypm.dashboard_data as dd

    monkeypatch.setattr(dd, "_count_inbox_tasks", lambda cfg: 1)
    one = doctor.check_inbox_open_count()
    assert "1 open inbox item" in one.status
    assert "item(s)" not in one.status

    monkeypatch.setattr(dd, "_count_inbox_tasks", lambda cfg: 7)
    many = doctor.check_inbox_open_count()
    assert "7 open inbox items" in many.status
    assert "item(s)" not in many.status


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
    # #1066: drift check now reads from the supervisor's state.db.
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (0, "polly:worker-x"))
    result = doctor.check_sessions_table_vs_tmux()
    assert result.passed


def test_session_drift_warn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")  # no sessions rows
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: db)
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


def test_session_drift_pluralisation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cycle 73: drift status uses ``window`` / ``windows`` per count.

    Both the warn (``N tmux window(s) without a sessions row``) and
    ok (``... ({N} row(s))``) paths used parenthetical plurals. The
    warn path matters most: a single rogue window is the easiest
    drift to introduce by accident.
    """
    db = _make_state_db(tmp_path / "state.db")
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    monkeypatch.setattr(
        doctor, "_tool_path",
        lambda name: "/usr/bin/tmux" if name == "tmux" else None,
    )
    # One rogue window that the DB doesn't know about.
    monkeypatch.setattr(
        doctor, "_run_cmd", lambda cmd, **kw: (0, "polly:worker-rogue"),
    )
    fail = doctor.check_sessions_table_vs_tmux()
    assert not fail.passed
    assert "1 tmux window without a sessions row" in fail.status
    assert "window(s)" not in fail.status

    # Single-row aligned DB → singular ok path.
    db_one = _make_state_db(tmp_path / "state_one.db")
    conn = sqlite3.connect(db_one)
    conn.execute(
        "INSERT INTO sessions (name, role, project, provider, account, cwd, window_name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("worker-x", "worker", "demo", "claude", "acct", "/tmp", "worker-x"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: db_one)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db_one)
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (0, "polly:worker-x"))
    ok = doctor.check_sessions_table_vs_tmux()
    assert ok.passed
    assert "(1 row)" in ok.status
    assert "row(s)" not in ok.status


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
    # Compact footer: "<total> checks · <passed> passed · <warnings> warning(s) · <errors> error(s)"
    # — warning/error words pluralise per count (cycle 52).
    assert "3 checks" in text
    assert "2 passed" in text
    assert "1 warning" in text
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
    # Per cycle 52, warning/error words pluralise per count.
    assert "4 checks · 2 passed · 1 warning · 1 error" in text


def test_summary_check_word_pluralises_for_single_check() -> None:
    """Cycle 111 — when only one check ran (e.g. ``pm doctor --check
    foo``), the footer read ``1 checks · 1 passed · 0 warnings · 0
    errors``. Match the noun to the count."""
    def _pass() -> doctor.CheckResult:
        return doctor._ok("ok")

    report = doctor.run_checks([doctor.Check("only", _pass, "pipeline")])
    text = doctor.render_human(report)
    assert "1 check ·" in text
    assert "1 checks" not in text


def test_render_human_labels_guide_drift_section() -> None:
    report = doctor.run_checks([
        doctor.Check("guide-x", lambda: doctor._ok("ok"), "guides"),
    ])
    text = doctor.render_human(report)
    assert "-- Guide Drift --" in text


# --------------------------------------------------------------------- #
# JSON output validity
# --------------------------------------------------------------------- #


def test_json_output_is_valid_for_new_categories() -> None:
    def _pass() -> doctor.CheckResult:
        return doctor._ok("ok", data={"foo": 1})

    report = doctor.run_checks([
        doctor.Check("pipeline-x", _pass, "pipeline"),
        doctor.Check("guide-x", _pass, "guides"),
        doctor.Check("resource-x", _pass, "resources"),
        doctor.Check("inbox-x", _pass, "inbox"),
    ])
    payload = json.loads(doctor.render_json(report))
    assert payload["ok"] is True
    categories = {c["category"] for c in payload["checks"]}
    assert categories == {"pipeline", "guides", "resources", "inbox"}
    assert payload["summary"]["total"] == 4
    assert payload["summary"]["passed"] == 4


def test_cli_doctor_json_with_new_checks() -> None:
    """End-to-end: --json output remains parseable with new checks registered."""
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor", "--json"])
    assert result.exit_code in (0, 1)
    payload = json.loads(result.stdout)
    categories = {c["category"] for c in payload["checks"]}
    # Confirm every new category appears.
    for expected in ("roles", "pipeline", "guides", "schedulers", "resources", "inbox", "sessions"):
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
    # Issue #1063: --fix verifies via re-running the check, so the
    # fixture must actually flip fail -> pass when the fixer runs.
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
    # Warning-only check → exit 0 even before/after fix.
    assert result.exit_code == 0
    assert invoked["count"] == 1
    assert "fixed" in result.stdout


# --------------------------------------------------------------------- #
# Expanded --fix coverage (PR: doctor --fix autonomy)
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
        return {"pruned": 3, "skipped_active": 0, "warned_stale": 0, "errors": 0}

    import pollypm.plugins_builtin.core_recurring.plugin as rec_plugin
    monkeypatch.setattr(rec_plugin, "agent_worktree_prune_handler", _fake_handler)
    success, message = result.fix_fn()
    assert success
    assert called["n"] == 1
    assert "pruned 3" in message


def test_logs_dir_size_fix_invokes_rotate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """--fix on an over-threshold logs dir calls the log.rotate handler."""
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

    import pollypm.plugins_builtin.core_recurring.plugin as rec_plugin
    monkeypatch.setattr(rec_plugin, "log_rotate_handler", _fake_handler)
    success, message = result.fix_fn()
    assert success
    # The fix passes the resolved logs_dir in the payload so the handler
    # doesn't have to re-load config.
    assert called["payload"] == {"logs_dir": str(logs)}
    assert "rotated 1" in message


def test_log_rotate_handler_message_pluralisation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``pm doctor --fix`` log-rotate message must not show ``(s)``.

    The fix-function status is shown verbatim to the user. With three
    counts in one line (rotated/deleted/errors), the original
    parenthetical-s pluralisation read as ``rotated 1 log(s),
    deleted 1 old archive(s), 0 error(s)`` — every count was at the
    awkward singular boundary. Lock the literal out of both the all-
    singular and all-plural cases.
    """
    import pollypm.plugins_builtin.core_recurring.plugin as rec_plugin

    monkeypatch.setattr(
        rec_plugin,
        "log_rotate_handler",
        lambda payload: {"rotated": 1, "deleted": 1, "errors": 1},
    )
    success, message = doctor._invoke_log_rotate_handler(tmp_path)
    assert not success
    assert "rotated 1 log," in message
    assert "deleted 1 old archive," in message
    assert "1 error" in message
    assert "(s)" not in message

    monkeypatch.setattr(
        rec_plugin,
        "log_rotate_handler",
        lambda payload: {"rotated": 4, "deleted": 2, "errors": 0},
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
    """``pm doctor --fix`` worktree-prune message must not show ``(s)``.

    Same shape as the log-rotate fix message: three counts in one
    line, original prose was ``pruned 1 merged worktree(s), 0 stale
    unmerged retained, 1 error(s)`` at the singular boundary.
    """
    import pollypm.plugins_builtin.core_recurring.plugin as rec_plugin

    monkeypatch.setattr(
        rec_plugin,
        "agent_worktree_prune_handler",
        lambda payload: {"pruned": 1, "warned_stale": 0, "errors": 1},
    )
    success, message = doctor._invoke_prune_handler()
    assert not success
    assert "pruned 1 merged worktree," in message
    assert "1 error" in message
    assert "(s)" not in message

    monkeypatch.setattr(
        rec_plugin,
        "agent_worktree_prune_handler",
        lambda payload: {"pruned": 5, "warned_stale": 0, "errors": 0},
    )
    success, message = doctor._invoke_prune_handler()
    assert success
    assert "pruned 5 merged worktrees," in message
    assert "0 errors" in message
    assert "(s)" not in message


def test_plan_gate_fix_rewrites_config_with_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """--fix on disabled plan gate writes a new config + .bak backup."""
    cfg_path = tmp_path / "pollypm.toml"
    cfg_path.write_text(
        "[project]\nname = 'x'\n\n[planner]\nenforce_plan = false\nplan_dir = 'docs/plan'\n"
    )

    fake_planner = type("P", (), {"enforce_plan": False, "plan_dir": "docs/plan"})
    fake_config = type("C", (), {"planner": fake_planner})
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (cfg_path, fake_config))
    result = doctor.check_plan_presence_gate()
    assert not result.passed
    assert result.fixable
    assert callable(result.fix_fn)

    success, message = result.fix_fn()
    assert success, message
    # Backup file exists with the original contents.
    bak = cfg_path.with_suffix(cfg_path.suffix + ".bak")
    assert bak.is_file()
    assert "enforce_plan = false" in bak.read_text()
    # New config has the flipped key.
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


def test_task_assignment_sweeper_fix_initializes_state_dbs(
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
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    result = doctor.check_task_assignment_sweeper_dbs()
    assert not result.passed
    assert result.fixable
    success, message = result.fix_fn()
    assert success, message
    assert (proj_a / ".pollypm" / "state.db").is_file()
    assert (proj_b / ".pollypm" / "state.db").is_file()


def test_session_drift_fix_invokes_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """--fix on session drift calls Supervisor.repair_sessions_table()."""
    db = _make_state_db(tmp_path / "state.db")
    monkeypatch.setattr(doctor, "_supervisor_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_primary_state_db", lambda: db)
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr(doctor, "_run_cmd", lambda cmd, **kw: (0, "polly:worker-rogue"))
    result = doctor.check_sessions_table_vs_tmux()
    assert not result.passed
    assert result.fixable
    assert callable(result.fix_fn)


# --------------------------------------------------------------------- #
# --fix-dry-run
# --------------------------------------------------------------------- #


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
    # The most important invariant: dry-run never runs the fixer.
    assert invoked["count"] == 0


def test_fix_dry_run_lists_manual_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-fixable failures appear under the manual-intervention list."""
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
    # Error-severity failures flip exit to 1.
    assert result.exit_code == 1
    # Cycle 56: pluralise the manual-intervention summary per count.
    assert "1 issue requires manual intervention" in result.stdout
    assert "needs-hands" in result.stdout


def test_fix_dry_run_helpers_do_not_invoke_fixers() -> None:
    """Direct module-level helper test — the helpers never call fix_fn."""
    invoked = {"count": 0}

    def _never() -> tuple[bool, str]:
        invoked["count"] += 1
        return (True, "nope")

    def _check() -> doctor.CheckResult:
        return doctor._fail(
            "x", why="w", fix="f1\nf2",
            fixable=True, fix_fn=_never, severity="warning",
        )

    report = doctor.run_checks([doctor.Check("c", _check, "pipeline", severity="warning")])
    planned = doctor.planned_fixes(report)
    manual = doctor.manual_fixes(report)
    assert planned == [("c", "f1")]
    assert manual == []
    assert invoked["count"] == 0


# --------------------------------------------------------------------- #
# Summary footer
# --------------------------------------------------------------------- #


def test_fix_summary_footer_counts_applied_and_remaining() -> None:
    """render_fix_summary reports N applied + K manual issues."""
    manual = [("needs-hands", "edit config manually"), ("another", "restart")]
    fix_results = [
        ("worktrees", True, "pruned 3"),
        ("logs", True, "rotated 1"),
        ("plan-gate", False, "write failed: permission denied"),
    ]
    summary = doctor.render_fix_summary(fix_results, manual)
    # Cycle 56: pluralise fix/issue per count instead of fix(es)/issue(s).
    assert "Applied 2 fixes" in summary
    assert "worktrees" in summary and "logs" in summary
    assert "1 fix failed" in summary
    assert "plan-gate" in summary
    assert "2 issues remain" in summary
    assert "needs-hands" in summary
    assert "(es)" not in summary
    assert "(s)" not in summary


def test_fix_summary_footer_singular_pluralisation() -> None:
    """At count=1 the footer must read ``1 fix`` / ``1 issue remains``."""
    one_applied = doctor.render_fix_summary(
        [("worktrees", True, "pruned 3")],
        [("needs-hands", "edit config")],
    )
    assert "Applied 1 fix:" in one_applied
    assert "1 issue remains" in one_applied
    assert "(es)" not in one_applied
    assert "(s)" not in one_applied


def test_fix_dry_run_pluralisation() -> None:
    """``--fix-dry-run`` output drops fix(es)/issue(s) parentheticals."""
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


def test_fix_cli_prints_summary_footer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI emits the summary footer after --fix completes."""
    # Issue #1063: --fix reports "Applied" only after re-running the
    # check and confirming it now passes. So our fixable fixture has to
    # actually transition fail -> pass when the fix_fn runs.
    state = {"healed": False}

    def _do_fix() -> tuple[bool, str]:
        state["healed"] = True
        return (True, "done")

    def _fixable() -> doctor.CheckResult:
        if state["healed"]:
            return doctor._ok("now ok")
        return doctor._fail(
            "bad", why="w", fix="run a thing",
            fixable=True, fix_fn=_do_fix,
            severity="warning",
        )

    def _manual() -> doctor.CheckResult:
        return doctor._fail(
            "stuck", why="w", fix="do it by hand", severity="warning",
        )

    monkeypatch.setattr(
        doctor, "_registered_checks",
        lambda: [
            doctor.Check("auto", _fixable, "resources", severity="warning"),
            doctor.Check("byhand", _manual, "resources", severity="warning"),
        ],
    )
    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor", "--fix"])
    assert result.exit_code == 0
    # Summary line appears; it names the applied fix and the remaining item.
    # Cycle 56: pluralise per count instead of fix(es)/issue(s).
    assert "Applied 1 fix:" in result.stdout
    assert "auto" in result.stdout
    assert "1 issue remains" in result.stdout
    assert "byhand" in result.stdout


def test_fix_cli_does_not_lie_when_handler_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #1063: --fix must NOT report 'Applied' for fixes whose
    handler ran but whose check still fails on a follow-up run.

    Repro: a fix_fn returns (True, "done") while the underlying check
    keeps reporting failure. Pre-fix, the CLI claimed "Applied 1 fix"
    and walked away. Post-fix, the CLI must surface "ran but check
    still failing" so the user knows manual intervention is needed.
    """
    def _liar_fix() -> tuple[bool, str]:
        # Handler claims success but check stays failed (the bug).
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
    # The footer must NOT lie about applying the fix.
    assert "Applied 1 fix" not in result.stdout
    assert "Applied 0 fixes" in result.stdout
    # And it MUST surface that the handler ran but the check is unfixed.
    assert "ran but check" in result.stdout
    assert "liar" in result.stdout


def test_verify_fix_results_demotes_missing_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #1064: when the post-fix report lacks a check entry,
    ``verify_fix_results`` must NOT trust the handler's self-report.

    The pre-fix code path returned ``(name, ok, message)`` unchanged
    when the check was missing — i.e. it fell back to trusting the
    handler. That's the same trust model that caused #1063. If
    ``_registered_checks()`` ever drifts mid-run, demote to unverified
    so the user knows manual confirmation is required.
    """
    raw = [("ghost-check", True, "handler says it worked")]
    empty_report = doctor.DoctorReport(results=[])

    verified = doctor.verify_fix_results(raw, empty_report)

    assert len(verified) == 1
    name, ok, message = verified[0]
    assert name == "ghost-check"
    assert ok is False, "missing post-check must demote to unverified"
    assert "unavailable to verify" in message


def test_fix_cli_real_handlers_no_op_surfaces_unverified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Issue #1064: --fix must stay honest when the *real* builtin
    handlers run but no-op (the empirical bug in #1064).

    The #1063 regression test used a synthetic ``(True, "did nothing")``
    fix_fn. That covered the verification path, but it didn't exercise
    the actual handler invocation chain (check_fn → fix_fn → builtin
    handler module → check re-run). This test wires up the *real*
    ``check_agent_worktree_count`` + ``check_logs_dir_size`` against a
    state where the real prune / log-rotate handlers report 0 work, and
    asserts the CLI says "ran but checks still failing" rather than
    "Applied".
    """
    # Pin the worktree dir enumeration above the warn threshold so the
    # real check fails before AND after the fix — the prune handler
    # mock will report 0 pruned, mimicking the #1064 empirical case.
    fake_worktrees = [tmp_path / f"agent-{i:04x}" for i in range(80)]
    for wt in fake_worktrees:
        wt.mkdir()
    monkeypatch.setattr(doctor, "_agent_worktree_dirs", lambda: fake_worktrees)

    # Logs dir over the size threshold; the real log_rotate handler
    # will be replaced with a no-op stub so we don't depend on the
    # plugin host or real archive logic.
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    big = logs_dir / "agent.log"
    big.write_bytes(b"x" * (doctor._LOGS_WARN_BYTES + 1))
    monkeypatch.setattr(doctor, "_logs_dir_candidates", lambda: [logs_dir])

    import pollypm.plugins_builtin.core_recurring.plugin as plugin_mod

    monkeypatch.setattr(
        plugin_mod, "agent_worktree_prune_handler",
        lambda payload: {"pruned": 0, "errors": 0, "warned_stale": 0},
    )
    monkeypatch.setattr(
        plugin_mod, "log_rotate_handler",
        lambda payload: {"rotated": 0, "deleted": 0, "errors": 0},
    )

    monkeypatch.setattr(
        doctor, "_registered_checks",
        lambda: [
            doctor.Check(
                "agent-worktree-count",
                doctor.check_agent_worktree_count,
                "resources",
                severity="warning",
            ),
            doctor.Check(
                "logs-dir-size",
                doctor.check_logs_dir_size,
                "resources",
                severity="warning",
            ),
        ],
    )

    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor", "--fix"])
    assert result.exit_code == 0, result.stdout
    # Both real handlers ran and reported zero work. The CLI must
    # demote them and say so, not lie about applying.
    assert "Applied 0 fixes" in result.stdout
    assert "Applied 2 fixes" not in result.stdout
    assert "ran but checks still failing" in result.stdout
    assert "agent-worktree-count" in result.stdout
    assert "logs-dir-size" in result.stdout


def test_manual_fixes_excludes_skipped_and_passing() -> None:
    """manual_fixes only lists failures that cannot auto-run."""
    def _pass() -> doctor.CheckResult:
        return doctor._ok("fine")

    def _skip_c() -> doctor.CheckResult:
        return doctor._skip("n/a")

    def _fixable() -> doctor.CheckResult:
        return doctor._fail("x", why="w", fix="f", fixable=True, fix_fn=lambda: (True, "ok"))

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


def test_tmux_missing_exposes_brew_auto_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_tool_path", lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None)
    monkeypatch.setattr(doctor, "_current_platform", lambda: "macos")
    result = doctor.check_tmux()
    assert not result.passed
    assert result.auto_fix is not None
    assert result.auto_fix.command == ["brew", "install", "tmux"]
    assert result.auto_fix.platforms == ["macos"]


def test_tmux_missing_exposes_linux_package_manager_auto_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    def _tool_path(name: str) -> str | None:
        return "/usr/bin/apt-get" if name == "apt-get" else None

    monkeypatch.setattr(doctor, "_tool_path", _tool_path)
    monkeypatch.setattr(doctor, "_current_platform", lambda: "linux")
    result = doctor.check_tmux()
    assert not result.passed
    assert result.auto_fix is not None
    assert result.auto_fix.requires_sudo is True
    assert result.auto_fix.command == ["sudo", "apt-get", "install", "-y", "tmux"]


def test_claude_cli_missing_has_auto_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    def _tool_path(name: str) -> str | None:
        return "/usr/bin/npm" if name == "npm" else None

    monkeypatch.setattr(doctor, "_tool_path", _tool_path)
    monkeypatch.setattr(doctor, "_current_platform", lambda: "linux")
    result = doctor.check_claude_cli()
    assert not result.passed
    assert result.auto_fix is not None
    assert result.auto_fix.command == ["npm", "i", "-g", "@anthropic-ai/claude-code"]


def test_codex_cli_missing_has_auto_fix(monkeypatch: pytest.MonkeyPatch) -> None:
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
        return doctor._fail("missing tmux", why="x", fix="y", auto_fix=auto_fix)

    report = doctor.run_checks([doctor.Check("tmux", _fail, "system")])
    text = doctor.render_human(report)
    assert "[f] Fix" in text


def test_render_human_hides_fix_badge_for_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    auto_fix = doctor.AutoFixPlan(
        description="Install tmux",
        command=["brew", "install", "tmux"],
        platforms=["macos"],
    )

    monkeypatch.setattr(doctor, "_current_platform", lambda: "windows")

    def _fail() -> doctor.CheckResult:
        return doctor._fail("missing tmux", why="x", fix="y", auto_fix=auto_fix)

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
    assert check["auto_fix"]["command"] == ["npm", "i", "-g", "@anthropic-ai/claude-code"]
    assert check["auto_fix_available"] is True


def test_apply_fixes_runs_supported_auto_fix_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    auto_fix = doctor.AutoFixPlan(
        description="Install Claude Code globally",
        command=["npm", "i", "-g", "@anthropic-ai/claude-code"],
        platforms=["macos", "linux"],
    )

    def _fail() -> doctor.CheckResult:
        return doctor._fail("missing", why="x", fix="y", auto_fix=auto_fix)

    monkeypatch.setattr(doctor, "run_auto_fix", lambda plan: (True, plan.description))

    report = doctor.run_checks([doctor.Check("claude", _fail, "system")])
    assert doctor.apply_fixes(report) == [("claude", True, "Install Claude Code globally")]


def test_planned_and_manual_fix_lists_treat_supported_auto_fix_as_runnable() -> None:
    auto_fix = doctor.AutoFixPlan(
        description="Install tmux",
        command=["brew", "install", "tmux"],
        platforms=["macos", "linux"],
    )

    def _fail() -> doctor.CheckResult:
        return doctor._fail("missing tmux", why="x", fix="Install tmux", auto_fix=auto_fix)

    report = doctor.run_checks([doctor.Check("tmux", _fail, "system")])
    assert doctor.planned_fixes(report) == [("tmux", "Install tmux")]
    assert doctor.manual_fixes(report) == []


# --------------------------------------------------------------------- #
# #1034 — post-#339 storage layout
# --------------------------------------------------------------------- #


def _write_minimal_state_db(path: Path) -> Path:
    """Create a tiny SQLite file at ``path`` so ``is_file`` succeeds.

    We don't need the schema: every #1034 regression test points the
    workspace / user helpers at this path and asserts the candidates
    list resolves it. Migration / size / sessions checks are exercised
    elsewhere with proper fixtures.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE _probe (id INTEGER)")
        conn.commit()
    finally:
        conn.close()
    return path


def _patch_workspace_layout(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace_db: Path | None,
    user_db: Path | None,
    config=None,
) -> None:
    """Pin the #339 helpers so tests don't read the dev machine's real DBs."""
    monkeypatch.setattr(doctor, "_workspace_state_db_path", lambda *a, **kw: workspace_db)
    monkeypatch.setattr(doctor, "_user_state_db_path", lambda: user_db or Path("/nonexistent/user.db"))
    if config is not None:
        monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), config))


def test_state_db_candidates_includes_workspace_db_post_339(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Regression for #1034: workspace-scope state.db must be a candidate.

    Pre-#1034, ``_state_db_candidates`` only walked tracked-project
    paths probing ``<project>/.pollypm/state.db``. Post-#339 those
    files don't exist on healthy installs — the canonical DB lives at
    ``<workspace_root>/.pollypm/state.db``. The candidates list must
    return the workspace DB even with zero tracked projects.
    """
    workspace_db = _write_minimal_state_db(tmp_path / "ws" / ".pollypm" / "state.db")
    fake_config = type("C", (), {"projects": {}, "project": type("P", (), {"workspace_root": tmp_path / "ws"})})
    from pollypm import config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "absent.toml")
    _patch_workspace_layout(monkeypatch, workspace_db=workspace_db, user_db=None, config=fake_config)
    candidates = doctor._state_db_candidates()
    assert workspace_db in candidates


def test_state_db_candidates_includes_user_db_post_339(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``~/.pollypm/state.db`` must be a candidate too — cross-project state.

    Even with no config and no workspace DB, the user-scope DB at
    ``~/.pollypm/state.db`` should be probed when present. This is
    what was missing in the #1034 symptom output: with both DBs
    actively in use, the migration / size / drift checks all skipped.
    """
    user_db = _write_minimal_state_db(tmp_path / "home" / ".pollypm" / "state.db")
    from pollypm import config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "missing.toml")
    _patch_workspace_layout(monkeypatch, workspace_db=None, user_db=user_db)
    candidates = doctor._state_db_candidates()
    assert user_db in candidates


def test_state_db_candidates_dedupes_workspace_and_legacy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When the workspace DB and a legacy per-project DB resolve to the
    same file (e.g. the project root *is* the workspace root on a
    pre-#339 layout), don't return it twice — migration / size checks
    multiply-count it otherwise."""
    workspace_root = tmp_path / "shared"
    workspace_root.mkdir()
    db = _write_minimal_state_db(workspace_root / ".pollypm" / "state.db")

    fake_project = type("P", (), {"path": workspace_root, "tracked": True})
    fake_config = type(
        "C", (),
        {
            "projects": {"shared": fake_project},
            "project": type("P", (), {"workspace_root": workspace_root}),
        },
    )
    from pollypm import config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "missing.toml")
    _patch_workspace_layout(monkeypatch, workspace_db=db, user_db=None, config=fake_config)
    candidates = doctor._state_db_candidates()
    assert candidates.count(db) == 1


def test_state_db_candidates_skips_missing_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Non-existent paths must be filtered out — `is_file()` is the gate."""
    workspace_db = tmp_path / "ws" / ".pollypm" / "state.db"  # never created
    from pollypm import config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "missing.toml")
    _patch_workspace_layout(monkeypatch, workspace_db=workspace_db, user_db=None)
    assert doctor._state_db_candidates() == []


def test_workspace_state_db_path_uses_workspace_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Helper must mirror :func:`pollypm.work.db_resolver.resolve_work_db_path`."""
    fake_config = type("C", (), {"project": type("P", (), {"workspace_root": tmp_path / "abc"})})
    resolved = doctor._workspace_state_db_path(fake_config)
    assert resolved == tmp_path / "abc" / ".pollypm" / "state.db"


def test_workspace_state_db_path_returns_none_without_workspace_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_config = type("C", (), {"project": type("P", (), {"workspace_root": None})})
    assert doctor._workspace_state_db_path(fake_config) is None


def test_task_assignment_sweeper_dbs_pass_via_workspace_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Post-#1034: a tracked project with no per-project DB but a present
    workspace DB is reachable. The pre-fix code returned ``found=0``
    here and skipped/passed with ``0 tracked projects have reachable
    state.db`` — the contradiction documented in #1034.
    """
    project_path = tmp_path / "proj-c"
    project_path.mkdir()
    workspace_root = tmp_path / "ws"
    workspace_db = _write_minimal_state_db(workspace_root / ".pollypm" / "state.db")
    fake_project = type("P", (), {"path": project_path, "tracked": True})
    fake_config = type(
        "C", (),
        {
            "projects": {"proj-c": fake_project},
            "project": type("P", (), {"workspace_root": workspace_root}),
        },
    )
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    monkeypatch.setattr(doctor, "_workspace_state_db_path", lambda *a, **kw: workspace_db)
    result = doctor.check_task_assignment_sweeper_dbs()
    assert result.passed
    assert "1 tracked project has reachable state.db" in result.status


def test_task_assignment_sweeper_dbs_zero_reachable_fails_not_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """#1034 explicit ask: a ✓ status reporting "0 reachable" is a
    contradiction. When zero tracked projects have a reachable DB
    (workspace or per-project), the check must FAIL with ✗.
    """
    project_path = tmp_path / "proj-d"
    project_path.mkdir()  # no per-project state.db
    workspace_root = tmp_path / "ws"  # workspace DB does NOT exist
    fake_project = type("P", (), {"path": project_path, "tracked": True})
    fake_config = type(
        "C", (),
        {
            "projects": {"proj-d": fake_project},
            "project": type("P", (), {"workspace_root": workspace_root}),
        },
    )
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    monkeypatch.setattr(
        doctor,
        "_workspace_state_db_path",
        lambda *a, **kw: workspace_root / ".pollypm" / "state.db",
    )
    result = doctor.check_task_assignment_sweeper_dbs()
    assert not result.passed
    assert result.severity == "warning"
    assert "no tracked project" in result.status
    assert "reachable" in result.status


def test_task_assignment_sweeper_dbs_skips_when_no_tracked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """All projects untracked → skip rather than passing with ``0 reachable``."""
    fake_project = type("P", (), {"path": tmp_path / "p", "tracked": False})
    fake_config = type(
        "C", (),
        {
            "projects": {"p": fake_project},
            "project": type("P", (), {"workspace_root": tmp_path / "ws"}),
        },
    )
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    monkeypatch.setattr(
        doctor,
        "_workspace_state_db_path",
        lambda *a, **kw: tmp_path / "ws" / ".pollypm" / "state.db",
    )
    result = doctor.check_task_assignment_sweeper_dbs()
    assert result.skipped
    assert "no tracked projects" in result.status


def test_task_assignment_sweeper_dbs_fix_initializes_workspace_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When the workspace DB is the missing piece, --fix initializes
    just that file rather than scaffolding per-project DBs."""
    project_path = tmp_path / "proj-e"
    project_path.mkdir()
    workspace_root = tmp_path / "ws"
    workspace_db = workspace_root / ".pollypm" / "state.db"
    fake_project = type("P", (), {"path": project_path, "tracked": True})
    fake_config = type(
        "C", (),
        {
            "projects": {"proj-e": fake_project},
            "project": type("P", (), {"workspace_root": workspace_root}),
        },
    )
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config))
    monkeypatch.setattr(doctor, "_workspace_state_db_path", lambda *a, **kw: workspace_db)
    result = doctor.check_task_assignment_sweeper_dbs()
    assert not result.passed
    assert result.fixable
    assert callable(result.fix_fn)
    ok, msg = result.fix_fn()
    assert ok, msg
    assert workspace_db.is_file()
    assert "workspace" in msg


def test_state_migrations_finds_workspace_db_post_339(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """End-to-end: after #1034 the migration check sees the workspace DB
    even on a config with zero tracked projects (the very symptom in
    the #1034 issue body)."""
    workspace_db = tmp_path / "ws" / ".pollypm" / "state.db"
    workspace_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(workspace_db)
    try:
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER, description TEXT, applied_at TEXT)"
        )
        conn.execute("INSERT INTO schema_version VALUES (1, 'old', '2020-01-01')")
        conn.commit()
    finally:
        conn.close()

    from pollypm import config as config_mod

    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "missing.toml")
    _patch_workspace_layout(
        monkeypatch,
        workspace_db=workspace_db,
        user_db=None,
        config=type(
            "C", (),
            {"projects": {}, "project": type("P", (), {"workspace_root": tmp_path / "ws"})},
        ),
    )
    monkeypatch.setattr(doctor, "_latest_state_migration_version", lambda: 99)
    result = doctor.check_state_migrations()
    assert not result.skipped
    assert "behind" in result.status
