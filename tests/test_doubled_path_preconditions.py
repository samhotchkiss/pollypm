"""Runtime preconditions for doubled-pollypm-path (#1972).

The typed-helpers + lint gate (PR 1 of #1972) is the primary defence.
This file pins the runtime backstop: any opener that bypasses the
helpers and is handed a path containing ``.pollypm/.pollypm`` raises
``RuntimeError`` loudly rather than silently leaking into the
phantom tree (per the architectural analysis in #1972).

Two openers carry the precondition:

* ``SQLiteWorkService.__init__`` — the work state DB. The biggest
  leak vector historically (~280K files / 1.8 GB).
* ``pollypm.audit.log._append_line`` — the audit writer that #1966
  identified as the most recent regression site.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_sqlite_work_service_rejects_doubled_path(tmp_path: Path) -> None:
    """Opening a state DB at ``.pollypm/.pollypm/state.db`` raises."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    doubled = tmp_path / ".pollypm" / ".pollypm" / "state.db"
    doubled.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(RuntimeError, match=r"doubled-pollypm-path"):
        SQLiteWorkService(db_path=doubled, project_path=tmp_path)


def test_sqlite_work_service_accepts_single_path(tmp_path: Path) -> None:
    """Sanity: a normal single ``.pollypm/state.db`` still opens."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    project = tmp_path / "myproject"
    project.mkdir()
    db_path = project / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    svc = SQLiteWorkService(db_path=db_path, project_path=project)
    assert svc is not None


def test_audit_append_line_rejects_doubled_path(tmp_path: Path) -> None:
    """Writing audit to a doubled path raises."""
    from pollypm.audit.log import _append_line

    doubled = tmp_path / ".pollypm" / ".pollypm" / "audit.jsonl"

    with pytest.raises(RuntimeError, match=r"doubled-pollypm-path"):
        _append_line(doubled, '{"foo":"bar"}')


def test_audit_append_line_accepts_single_path(tmp_path: Path) -> None:
    """Sanity: a normal single ``.pollypm/audit.jsonl`` still writes."""
    from pollypm.audit.log import _append_line

    target = tmp_path / "myproject" / ".pollypm" / "audit.jsonl"
    _append_line(target, '{"foo":"bar"}')

    assert target.read_text().rstrip() == '{"foo":"bar"}'


# ---------------------------------------------------------------------------
# Helper sanity — quick smoke tests for the new typed helpers (#1972 PR 1).
# Full migration tests live in test_doubled_pollypm_path_regression.py.
# ---------------------------------------------------------------------------


def test_project_state_db_path_normal(tmp_path: Path) -> None:
    from pollypm.projects import project_state_db_path

    project = tmp_path / "myproject"
    project.mkdir()
    assert project_state_db_path(project) == project / ".pollypm" / "state.db"


def test_project_state_db_path_collapses_global_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``project_path == GLOBAL_CONFIG_DIR`` the join collapses (no doubled)."""
    import pollypm.config as config_mod
    from pollypm.projects import project_state_db_path

    fake_global = tmp_path / ".pollypm"
    fake_global.mkdir()
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_DIR", fake_global)

    db = project_state_db_path(fake_global)
    assert ".pollypm/.pollypm" not in str(db), db
    assert db == fake_global / "state.db"


def test_project_audit_log_path_collapses_global_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1966 reproducer: audit log under GLOBAL_CONFIG_DIR doesn't double."""
    import pollypm.config as config_mod
    from pollypm.projects import project_audit_log_path

    fake_global = tmp_path / ".pollypm"
    fake_global.mkdir()
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_DIR", fake_global)

    log = project_audit_log_path(fake_global)
    assert ".pollypm/.pollypm" not in str(log), log
    assert log == fake_global / "audit.jsonl"


def test_typed_helpers_chain_through_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every typed helper collapses when given GLOBAL_CONFIG_DIR."""
    import pollypm.config as config_mod
    from pollypm import projects as p

    fake_global = tmp_path / ".pollypm"
    fake_global.mkdir()
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_DIR", fake_global)

    helpers = [
        p.project_state_db_path,
        p.project_audit_log_path,
        p.project_advisor_log_path,
        p.project_plugins_dir,
        p.project_gates_dir,
        p.project_flows_dir,
        p.project_rules_dir,
        p.project_magic_dir,
        p.project_config_dir,
        p.project_docs_dir,
        p.project_content_dir,
        p.project_inbox_dir,
        p.project_worker_markers_dir,
        p.project_session_markers_dir,
        p.project_system_prompts_dir,
        p.project_project_guides_dir,
        p.project_control_prompts_dir,
    ]
    for helper in helpers:
        result = helper(fake_global)
        assert ".pollypm/.pollypm" not in str(result), (
            f"{helper.__name__}({fake_global}) -> {result} doubles!"
        )
