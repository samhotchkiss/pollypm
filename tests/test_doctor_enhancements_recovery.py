"""Re-coverage of ``pm doctor`` enhancements (refs #1824).

Ports the monkeypatch-driven half of the deleted
``tests/test_doctor_enhancements.py`` (2,305 LOC, removed by Slice
K-tests part 5 — see #1737). The original mixed two things:

* Pure check-function tests (PASS/WARN/FAIL via monkeypatch). These
  are storage-backend-independent — every helper is patched, no DB
  is opened. They re-add cleanly and that is what this module covers.
* CLI-level ``--json`` / exit-code / ``--fix`` tests that bind to the
  full registered-check roster. Those are flakier in CI under the pg
  cutover (the live roster includes the pg-connection probe), so
  they are intentionally not re-added here. The
  ``test_doctor.py`` and ``test_doctor_dual_db_health.py`` modules
  already cover the registration shape via the ``--check`` path.

The pattern mirrors the surviving sibling ``test_doctor_dual_db_health.py``:
patch ``_safe_load_config`` (or the smaller helpers like
``_logs_dir_candidates``) and call the check function directly. The
unpatched ``CheckResult`` shape is what the renderer + ``run_checks``
consume in production, so per-check coverage is the load-bearing
contract.

Surfaces covered (subset of the original):

* ``check_plan_presence_gate`` — pass / warn / skip.
* ``check_architect_profile`` + ``check_visual_explainer_skill`` —
  package-shipped present / monkeypatched-missing.
* ``check_task_assignment_sweeper_dbs`` — tracked-project state.db
  pass / warn-all-missing / pluralisation.
* ``check_state_db_size`` — pass / warn.
* ``check_logs_dir_size`` — pass / warn / skip.
* ``check_session_memory_usage`` — pass / warn / skip / pluralisation.
* ``check_inbox_open_count`` — pass / warn / pluralisation.
* ``check_scheduler_roster_handlers`` — pass / warn-missing / plural.
* ``check_project_local_guide_drift`` — skip-without-config / warn.
* ``check_persona_swap_defense_wired`` — package-shipped pass.
* ``run_checks`` + ``render_human`` — section headers, footer counts,
  pluralisation (cycles 52 / 111).
* ``render_json`` — category round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pollypm import doctor


# ---------------------------------------------------------------------------
# Pipeline checks
# ---------------------------------------------------------------------------


def test_plan_gate_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_planner = type("P", (), {"enforce_plan": True, "plan_dir": "docs/plan"})
    fake_config = type("C", (), {"planner": fake_planner})
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config),
    )
    result = doctor.check_plan_presence_gate()
    assert result.passed
    assert "enabled" in result.status


def test_plan_gate_warn_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_planner = type("P", (), {"enforce_plan": False, "plan_dir": "docs/plan"})
    fake_config = type("C", (), {"planner": fake_planner})
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config),
    )
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


def test_architect_profile_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
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


def test_visual_explainer_skill_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake_path = tmp_path / "src" / "pollypm" / "doctor.py"
    fake_path.parent.mkdir(parents=True)
    fake_path.write_text("# fake")
    monkeypatch.setattr(doctor, "__file__", str(fake_path))
    result = doctor.check_visual_explainer_skill()
    assert not result.passed
    assert "visual-explainer" in result.status


def test_task_assignment_sweeper_dbs_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
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


def test_task_assignment_sweeper_dbs_warn_all_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    project_path = tmp_path / "proj-b"
    project_path.mkdir()
    fake_project = type("P", (), {"path": project_path, "tracked": True})
    fake_config = type("C", (), {"projects": {"proj-b": fake_project}})
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config),
    )
    result = doctor.check_task_assignment_sweeper_dbs()
    assert not result.passed
    assert result.severity == "warning"
    assert "no tracked project" in result.status


def test_task_assignment_sweeper_dbs_pluralisation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Singular and plural sweeper-dbs status must not show ``project(s)``.

    Both warn ("X missing state.db") and ok ("X have reachable
    state.db") paths share the project pluralisation; the ok branch
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
        "C", (),
        {"projects": {
            "have": type("P", (), {"path": have_path, "tracked": True}),
            "miss-a": type("P", (), {"path": miss_a, "tracked": True}),
            "miss-b": type("P", (), {"path": miss_b, "tracked": True}),
        }},
    )
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (Path("/tmp/x"), cfg),
    )
    warn = doctor.check_task_assignment_sweeper_dbs()
    assert not warn.passed
    assert "2 tracked projects missing state.db" in warn.status
    assert "project(s)" not in warn.status


# ---------------------------------------------------------------------------
# Guides checks
# ---------------------------------------------------------------------------


def test_project_local_guide_drift_skip_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "_safe_load_config", lambda: (None, None))
    result = doctor.check_project_local_guide_drift()
    assert result.passed and result.skipped


def test_project_local_guide_drift_warns_when_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    project_path = tmp_path / "proj-b"
    project_path.mkdir()
    fake_project = type("P", (), {"path": project_path, "name": "Project B"})
    fake_config = type("C", (), {"projects": {"proj-b": fake_project}})
    guide_path = project_path / ".pollypm" / "project-guides" / "worker.md"
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config),
    )
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


# ---------------------------------------------------------------------------
# Scheduler checks
# ---------------------------------------------------------------------------


def test_scheduler_handlers_pass() -> None:
    # Real plugin import path — the builtin plugins ship with the package.
    result = doctor.check_scheduler_roster_handlers()
    assert result.passed
    assert "registered" in result.status


def test_scheduler_handlers_warn_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor, "_expected_handlers_from_plugins", lambda: {"db.vacuum"},
    )
    result = doctor.check_scheduler_roster_handlers()
    assert not result.passed
    assert "missing scheduled handler" in result.status


def test_scheduler_handlers_pluralisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cycle 69: missing-handler status uses ``handler`` / ``handlers`` per count.

    The original text was ``missing scheduled handler(s):`` —
    parenthetical plural. Lock both singular and plural shape so
    count=1 never reads as a copy bug.
    """
    expected = list(doctor._EXPECTED_SCHEDULED_HANDLERS)
    declared_one_missing = set(expected[1:])  # drop one
    monkeypatch.setattr(
        doctor, "_expected_handlers_from_plugins",
        lambda: declared_one_missing,
    )
    one_missing = doctor.check_scheduler_roster_handlers()
    assert not one_missing.passed
    assert "missing scheduled handler:" in one_missing.status
    assert "handler(s)" not in one_missing.status

    declared_two_missing = set(expected[2:])
    monkeypatch.setattr(
        doctor, "_expected_handlers_from_plugins",
        lambda: declared_two_missing,
    )
    two_missing = doctor.check_scheduler_roster_handlers()
    assert not two_missing.passed
    assert "missing scheduled handlers:" in two_missing.status
    assert "handler(s)" not in two_missing.status


# ---------------------------------------------------------------------------
# Resource checks
# ---------------------------------------------------------------------------


def test_state_db_size_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 1024)  # 1 KB — comfortably under any threshold
    monkeypatch.setattr(doctor, "_state_db_candidates", lambda: [db])
    result = doctor.check_state_db_size()
    assert result.passed
    assert "MB" in result.status


def test_state_db_size_warn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = tmp_path / "big.db"
    db.write_bytes(b"\0" * (600 * 1024 * 1024))  # 600 MB → warn
    monkeypatch.setattr(doctor, "_state_db_candidates", lambda: [db])
    result = doctor.check_state_db_size()
    assert not result.passed
    assert result.severity == "warning"
    assert "warn at" in result.status


def test_agent_worktree_count_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake_dirs = [tmp_path / f"agent-{i}" for i in range(5)]
    for d in fake_dirs:
        d.mkdir()
    monkeypatch.setattr(doctor, "_agent_worktree_dirs", lambda: fake_dirs)
    result = doctor.check_agent_worktree_count()
    assert result.passed
    assert "5 agent worktree" in result.status


def test_agent_worktree_count_warn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
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


def test_logs_dir_size_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "a.log").write_bytes(b"hi")
    monkeypatch.setattr(doctor, "_logs_dir_candidates", lambda: [logs])
    result = doctor.check_logs_dir_size()
    assert result.passed


def test_logs_dir_size_warn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "big.log").write_bytes(b"\0" * (600 * 1024 * 1024))
    monkeypatch.setattr(doctor, "_logs_dir_candidates", lambda: [logs])
    result = doctor.check_logs_dir_size()
    assert not result.passed
    assert result.severity == "warning"


def test_logs_dir_size_skip_when_no_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_session_memory_skip_when_no_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "_ps_claude_rss_kb", lambda: [])
    result = doctor.check_session_memory_usage()
    assert result.passed and result.skipped


def test_session_memory_pluralisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Singular and plural session counts must not show ``session(s)``.

    Both the warn (``X session(s) over 1 GB RSS``) and ok (``X
    session(s), largest …``) paths share the count noun. Lock both
    out at the singular boundary, mirroring cycles 47/48/50 on other
    doctor checks.
    """
    monkeypatch.setattr(
        doctor, "_ps_claude_rss_kb",
        lambda: [(7, 1_500_000, "claude --headless")],
    )
    warn = doctor.check_session_memory_usage()
    assert not warn.passed
    assert "1 session over 1 GB RSS" in warn.status
    assert "session(s)" not in warn.status

    monkeypatch.setattr(
        doctor, "_ps_claude_rss_kb",
        lambda: [(7, 50_000, "claude --headless")],
    )
    one_ok = doctor.check_session_memory_usage()
    assert one_ok.passed
    assert "1 session, largest" in one_ok.status
    assert "session(s)" not in one_ok.status

    monkeypatch.setattr(
        doctor, "_ps_claude_rss_kb",
        lambda: [(1, 50_000, "claude"), (2, 80_000, "codex")],
    )
    many_ok = doctor.check_session_memory_usage()
    assert many_ok.passed
    assert "2 sessions, largest" in many_ok.status
    assert "session(s)" not in many_ok.status


# ---------------------------------------------------------------------------
# Inbox checks
# ---------------------------------------------------------------------------


def test_inbox_open_count_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_config = object()
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config),
    )
    import pollypm.dashboard_data as dd
    monkeypatch.setattr(dd, "_count_inbox_tasks", lambda cfg: 5)
    result = doctor.check_inbox_open_count()
    assert result.passed
    assert "5 open inbox" in result.status


def test_inbox_open_count_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_config = object()
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config),
    )
    import pollypm.dashboard_data as dd
    monkeypatch.setattr(dd, "_count_inbox_tasks", lambda cfg: 99)
    result = doctor.check_inbox_open_count()
    assert not result.passed
    assert result.severity == "warning"


def test_inbox_open_count_pluralisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Singular and plural inbox counts must not render ``item(s)``.

    ``pm doctor`` runs every install/CI/recovery sweep — the
    parenthetical-s pluralisation always reads as a copy bug at
    count=1. Cycle 45 made this fix on 5 other doctor messages; this
    locks the same shape for the inbox-count check (both pass and
    warn paths share the word).
    """
    fake_config = object()
    monkeypatch.setattr(
        doctor, "_safe_load_config", lambda: (Path("/tmp/x"), fake_config),
    )
    import pollypm.dashboard_data as dd

    monkeypatch.setattr(dd, "_count_inbox_tasks", lambda cfg: 1)
    one = doctor.check_inbox_open_count()
    assert "1 open inbox item" in one.status
    assert "item(s)" not in one.status

    monkeypatch.setattr(dd, "_count_inbox_tasks", lambda cfg: 7)
    many = doctor.check_inbox_open_count()
    assert "7 open inbox items" in many.status
    assert "item(s)" not in many.status


# ---------------------------------------------------------------------------
# Persona-swap defense (real-fs assertion)
# ---------------------------------------------------------------------------


def test_persona_swap_defense_pass() -> None:
    # Real-fs assertion — supervisor.py ships with the assertion wired.
    result = doctor.check_persona_swap_defense_wired()
    assert result.passed


def test_persona_swap_defense_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake_supervisor = tmp_path / "supervisor.py"
    fake_supervisor.write_text("def boot():\n    return None\n")
    fake_doctor = tmp_path / "doctor.py"
    fake_doctor.write_text("# fake")
    monkeypatch.setattr(doctor, "__file__", str(fake_doctor))
    result = doctor.check_persona_swap_defense_wired()
    assert not result.passed
    assert "_assert_session_launch_matches" in result.status


# ---------------------------------------------------------------------------
# Renderer + summary footer
# ---------------------------------------------------------------------------


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
    # Footer: ``<total> checks · <passed> passed · <warnings> warning(s) ·
    # <errors> error(s)`` — words pluralise per count (cycle 52).
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
    """Cycle 111 — single-check footer must read ``1 check ·`` not ``1 checks``."""

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


# ---------------------------------------------------------------------------
# JSON output validity
# ---------------------------------------------------------------------------


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
