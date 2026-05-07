"""Coverage for the savethenovel-followup ``pm doctor`` workspace-health checks.

Three new probes were added to :mod:`pollypm.doctor` after the
2026-05-06 dual-DB post-mortem:

* :func:`pollypm.doctor.check_dual_db_work_tasks_drift` — detects
  ``work_tasks`` tables stamped on the messages-side DB.
* :func:`pollypm.doctor.check_orphan_worker_markers` — surfaces stale
  ``worker-markers/*.fresh`` files when the cockpit has not booted
  recently.
* :func:`pollypm.doctor.check_doubled_pollypm_path` — flags the
  legacy ``~/.pollypm/.pollypm/`` artifact.

Tests follow the existing pattern in ``test_doctor_enhancements.py``:
each check gets at least one PASS and one WARN/FAIL case, and we
monkeypatch helper internals so the real filesystem / config is never
touched outside ``tmp_path``.

Run targeted (full pytest is forbidden by the task spec)::

    HOME=/tmp/pytest-agent-doctor uv run pytest \
        tests/test_doctor_dual_db_health.py -q
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm import doctor


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _make_messages_db(path: Path) -> Path:
    """Create a messages-side DB with the ``messages`` table only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY,
                body TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _make_work_db(path: Path, *, rows: int = 0) -> Path:
    """Create a workspace-side DB with the ``work_tasks`` table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS work_tasks (
                project TEXT,
                task_number INTEGER,
                work_status TEXT
            );
            """
        )
        for i in range(rows):
            conn.execute(
                "INSERT INTO work_tasks (project, task_number, work_status) "
                "VALUES (?, ?, ?)",
                ("demo", i + 1, "queued"),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _stamp_work_tables_onto(path: Path) -> Path:
    """Add ``work_tasks`` to an existing messages-side DB.

    Reproduces the savethenovel stamping bug: the work-service was
    pointed at the messages-side DB and ``CREATE TABLE IF NOT EXISTS
    work_tasks`` ran against the wrong file.
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS work_tasks (
                project TEXT,
                task_number INTEGER,
                work_status TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _patch_dual_db_paths(
    monkeypatch: pytest.MonkeyPatch,
    *,
    messages_db: Path | None,
    workspace_db: Path | None,
) -> None:
    """Pin the messages-side / workspace DB path resolvers for one test.

    A fake config is also installed so ``check_dual_db_work_tasks_drift``
    does not short-circuit on the "no config" skip branch.
    """
    fake_config = type("FakeConfig", (), {"project": object(), "projects": {}})()
    monkeypatch.setattr(
        doctor, "_safe_load_config",
        lambda: (Path("/tmp/fake-config.toml"), fake_config),
    )
    monkeypatch.setattr(
        doctor, "_messages_side_db_path", lambda config=None: messages_db,
    )
    monkeypatch.setattr(
        doctor, "_workspace_state_db_path", lambda config=None: workspace_db,
    )
    # Don't let the real audit log leak in.
    monkeypatch.setattr(
        doctor,
        "_read_recent_work_db_opened_events",
        lambda *, limit=5, project_path=None: [],
    )


# --------------------------------------------------------------------- #
# check_dual_db_work_tasks_drift
# --------------------------------------------------------------------- #


def test_dual_db_skip_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (None, None))
    result = doctor.check_dual_db_work_tasks_drift()
    assert result.skipped
    assert result.passed  # skipped checks pass


def test_dual_db_pass_when_messages_side_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    messages_db = _make_messages_db(tmp_path / "user" / "state.db")
    workspace_db = _make_work_db(tmp_path / "ws" / "state.db", rows=3)
    _patch_dual_db_paths(
        monkeypatch, messages_db=messages_db, workspace_db=workspace_db,
    )
    result = doctor.check_dual_db_work_tasks_drift()
    assert result.passed
    assert "no dual-DB drift" in result.status
    assert result.data["messages_db_work_tasks"] is None
    assert result.data["workspace_db_work_tasks"] == 3


def test_dual_db_warns_on_stamping_bug_empty_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Empty ``work_tasks`` on the messages-side DB → stamping bug warn."""
    messages_db = _make_messages_db(tmp_path / "user" / "state.db")
    _stamp_work_tables_onto(messages_db)  # zero rows, table present
    workspace_db = _make_work_db(tmp_path / "ws" / "state.db", rows=2)
    _patch_dual_db_paths(
        monkeypatch, messages_db=messages_db, workspace_db=workspace_db,
    )
    result = doctor.check_dual_db_work_tasks_drift()
    assert not result.passed
    assert result.severity == "warning"
    assert "stamped on messages-side DB" in result.status
    assert "rows=0" in result.status
    assert result.data["messages_db_work_tasks"] == 0
    assert result.data["workspace_db_work_tasks"] == 2


def test_dual_db_fails_on_real_drift_both_have_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Both DBs hold ``work_tasks`` rows → schema-drift warn."""
    messages_db = _make_messages_db(tmp_path / "user" / "state.db")
    _stamp_work_tables_onto(messages_db)
    # Add rows to the messages-side DB to simulate stranded writes.
    conn = sqlite3.connect(messages_db)
    try:
        conn.execute(
            "INSERT INTO work_tasks (project, task_number, work_status) "
            "VALUES (?, ?, ?)",
            ("demo", 99, "queued"),
        )
        conn.commit()
    finally:
        conn.close()
    workspace_db = _make_work_db(tmp_path / "ws" / "state.db", rows=4)
    _patch_dual_db_paths(
        monkeypatch, messages_db=messages_db, workspace_db=workspace_db,
    )
    result = doctor.check_dual_db_work_tasks_drift()
    assert not result.passed
    assert result.severity == "warning"
    assert "BOTH DBs" in result.status
    assert "messages-side=1" in result.status
    assert "workspace=4" in result.status
    # Fix block names the canonical workspace path.
    assert str(workspace_db) in result.fix


def test_dual_db_pass_when_paths_collapse_to_same_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Messages-side and workspace resolve to the same file → no drift possible."""
    db = _make_messages_db(tmp_path / "state.db")
    _stamp_work_tables_onto(db)
    _patch_dual_db_paths(
        monkeypatch, messages_db=db, workspace_db=db,
    )
    result = doctor.check_dual_db_work_tasks_drift()
    assert result.passed
    assert "single state.db" in result.status


def test_dual_db_surfaces_recent_audit_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When ``had_messages_table_pre_open=true`` events exist, fix block
    names them so operators can find the offending callsite."""
    messages_db = _make_messages_db(tmp_path / "user" / "state.db")
    _stamp_work_tables_onto(messages_db)
    workspace_db = _make_work_db(tmp_path / "ws" / "state.db", rows=1)
    _patch_dual_db_paths(
        monkeypatch, messages_db=messages_db, workspace_db=workspace_db,
    )
    fake_events = [
        {
            "ts": "2026-05-06T10:00:00+00:00",
            "subject": str(messages_db),
            "project_path": "/path/to/project",
            "tables_created": True,
        },
    ]
    monkeypatch.setattr(
        doctor,
        "_read_recent_work_db_opened_events",
        lambda *, limit=5, project_path=None: fake_events,
    )
    result = doctor.check_dual_db_work_tasks_drift()
    assert not result.passed
    assert "2026-05-06T10:00:00+00:00" in result.fix
    assert "/path/to/project" in result.fix


# --------------------------------------------------------------------- #
# check_orphan_worker_markers
# --------------------------------------------------------------------- #


def test_orphan_worker_markers_skip_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (None, None))
    result = doctor.check_orphan_worker_markers()
    assert result.skipped
    assert result.passed


def test_orphan_worker_markers_skip_without_projects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cfg = type("C", (), {"projects": {}})()
    monkeypatch.setattr(
        doctor, "_safe_load_config",
        lambda: (Path("/tmp/x"), cfg),
    )
    result = doctor.check_orphan_worker_markers()
    assert result.skipped


def test_orphan_worker_markers_pass_when_dir_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No ``worker-markers/`` directory → no orphans, clean pass."""
    project_path = tmp_path / "demo"
    project_path.mkdir()
    fake_project = type("P", (), {"path": project_path, "tracked": True})
    fake_config = type("C", (), {"projects": {"demo": fake_project}, "project": object()})
    monkeypatch.setattr(
        doctor, "_safe_load_config",
        lambda: (Path("/tmp/x"), fake_config),
    )
    result = doctor.check_orphan_worker_markers()
    assert result.passed
    assert "no orphan worker markers" in result.status


def test_orphan_worker_markers_warns_when_marker_has_no_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A marker file whose backing task row is missing → orphan warn."""
    project_path = tmp_path / "demo"
    marker_dir = project_path / ".pollypm" / "worker-markers"
    marker_dir.mkdir(parents=True)
    marker = marker_dir / "task-demo-7.fresh"
    marker.write_text("")

    # Stub the reaper helpers so we do not need the real status_probe
    # / parse_task_window_name infrastructure spun up here. We are
    # specifically testing the doctor wrapper's plumbing — the reaper's
    # own classification is covered by ``test_worker_marker_reaper``.
    from pollypm.work import worker_marker_reaper as wm_reaper

    def _fake_classify(*, marker, project_key, project_path, workspace_root, live_window_names):
        return wm_reaper.ReapedMarker(
            project_key=project_key,
            window_name=marker.stem,
            marker_path=marker,
            reason="orphan: no work_tasks row",
        )

    monkeypatch.setattr(wm_reaper, "_classify_marker", _fake_classify)

    fake_project = type("P", (), {"path": project_path, "tracked": True})
    fake_config = type(
        "C",
        (),
        {"projects": {"demo": fake_project}, "project": object()},
    )
    monkeypatch.setattr(
        doctor, "_safe_load_config",
        lambda: (Path("/tmp/x"), fake_config),
    )
    result = doctor.check_orphan_worker_markers()
    assert not result.passed
    assert result.severity == "warning"
    assert "1 orphan worker marker" in result.status
    assert result.data["count"] == 1
    samples = result.data["samples"]
    assert samples[0]["window"] == "task-demo-7"
    assert samples[0]["project"] == "demo"


# --------------------------------------------------------------------- #
# check_doubled_pollypm_path
# --------------------------------------------------------------------- #


def test_doubled_pollypm_path_pass_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No ``~/.pollypm/.pollypm/`` directory → clean pass."""
    fake_home = tmp_path / "home" / ".pollypm"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr("pollypm.config.GLOBAL_CONFIG_DIR", fake_home)
    result = doctor.check_doubled_pollypm_path()
    assert result.passed
    assert "no doubled" in result.status


def test_doubled_pollypm_path_warns_with_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Doubled directory with content → warn carrying size + age."""
    fake_home = tmp_path / "home" / ".pollypm"
    doubled = fake_home / ".pollypm"
    doubled.mkdir(parents=True)
    (doubled / "stray.toml").write_text("legacy = true\n" * 50)
    (doubled / "logs").mkdir()
    (doubled / "logs" / "old.log").write_text("noise\n" * 100)
    monkeypatch.setattr("pollypm.config.GLOBAL_CONFIG_DIR", fake_home)
    result = doctor.check_doubled_pollypm_path()
    assert not result.passed
    assert result.severity == "warning"
    assert "files" in result.status
    assert "KiB" in result.status
    assert result.data["file_count"] == 2
    assert result.data["size_bytes"] > 0
    # Fix block tells the user to move/remove and gives a backup
    # naming pattern (the ``mv ... .bak-$(date ...)`` line).
    assert ".bak-" in result.fix


def test_doubled_pollypm_path_warns_when_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Empty doubled directory still gets flagged (just smaller fix block)."""
    fake_home = tmp_path / "home" / ".pollypm"
    doubled = fake_home / ".pollypm"
    doubled.mkdir(parents=True)
    monkeypatch.setattr("pollypm.config.GLOBAL_CONFIG_DIR", fake_home)
    result = doctor.check_doubled_pollypm_path()
    assert not result.passed
    assert result.severity == "warning"
    assert "empty" in result.status.lower()
    assert "rmdir" in result.fix


# --------------------------------------------------------------------- #
# Registration + audit emit
# --------------------------------------------------------------------- #


def test_new_checks_registered_under_filesystem_category() -> None:
    """All three new checks must appear in ``_registered_checks`` under
    the filesystem category at warning severity."""
    registered = {check.name: check for check in doctor._registered_checks()}
    for name in (
        "dual-db-work-tasks-drift",
        "orphan-worker-markers",
        "doubled-pollypm-path",
    ):
        assert name in registered, f"{name!r} not registered"
        assert registered[name].category == "filesystem"
        assert registered[name].severity == "warning"


def test_cli_doctor_emits_pm_doctor_run_audit_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The ``pm doctor`` command must emit a ``pm.doctor_run`` audit row.

    Stub the registered checks to a single passing probe so the output
    is deterministic, redirect the audit central root with
    ``$POLLYPM_AUDIT_HOME`` to a temp dir, then assert the event landed.
    """
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(tmp_path / "audit"))

    def _pass() -> doctor.CheckResult:
        return doctor._ok("ok")

    monkeypatch.setattr(
        doctor, "_registered_checks",
        lambda: [doctor.Check("demo", _pass, "test")],
    )

    import pollypm.cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["doctor"])
    assert result.exit_code == 0

    central = tmp_path / "audit" / "_workspace.jsonl"
    assert central.exists(), f"audit log not written at {central}"
    body = central.read_text()
    assert "pm.doctor_run" in body
    assert '"actor":"user"' in body
    assert '"checks_total":1' in body
    assert '"findings_total":0' in body
