"""Tests for the PM turn-ended detector + rail glyph override (#1633)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pollypm.cockpit_rail import CockpitItem, PollyCockpitRail
from pollypm import pm_turn_state


@pytest.fixture(autouse=True)
def _isolated_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the state file + audit log under a fresh tmpdir per test."""
    state_home = tmp_path / "pollypm_state"
    state_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("POLLYPM_PM_TURN_STATE_HOME", str(state_home))
    audit_home = tmp_path / "pollypm_audit"
    audit_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))
    return state_home


def _rail() -> PollyCockpitRail:
    """Build a rail instance without running it (no I/O, no config load)."""
    return PollyCockpitRail.__new__(PollyCockpitRail)


def _polly_item(*, state: str = "live") -> CockpitItem:
    return CockpitItem(key="polly", label="Polly", state=state)


def _project_item(
    *,
    project_state: str | None = None,
    state: str = "project-green",
    approvals_pending: int = 0,
    alert_severity: str | None = None,
) -> CockpitItem:
    return CockpitItem(
        key="project:demo",
        label="demo",
        state=state,
        project_state=project_state,
        approvals_pending=approvals_pending,
        alert_severity=alert_severity,
    )


# ── pm_turn_state primitives ──────────────────────────────────────────


def test_is_pm_session_recognizes_operator() -> None:
    assert pm_turn_state.is_pm_session("operator") is True


def test_is_pm_session_recognizes_architect() -> None:
    assert pm_turn_state.is_pm_session("architect_demo") is True
    assert pm_turn_state.is_pm_session("architect-demo") is True


def test_is_pm_session_rejects_worker_and_reviewer() -> None:
    assert pm_turn_state.is_pm_session("worker_demo") is False
    assert pm_turn_state.is_pm_session("reviewer") is False
    assert pm_turn_state.is_pm_session("heartbeat") is False
    assert pm_turn_state.is_pm_session("") is False


def test_detect_pm_turn_ended_claude_empty_prompt() -> None:
    pane = "\n".join([
        "Some earlier assistant content here.",
        "──────────────────────────",
        "❯",
        "  ⏵⏵ accept edits on  shift+tab to cycle",
    ])
    assert pm_turn_state.detect_pm_turn_ended(pane) is True


def test_detect_pm_turn_ended_codex_placeholder() -> None:
    pane = "\n".join([
        "Some earlier content",
        "› Improve documentation in @filename",
    ])
    assert pm_turn_state.detect_pm_turn_ended(pane) is True


def test_detect_pm_turn_ended_false_on_mid_turn_content() -> None:
    pane = "\n".join([
        "Working on the task right now…",
        "❯ pasted user message here",
    ])
    assert pm_turn_state.detect_pm_turn_ended(pane) is False


def test_record_turn_state_emits_audit_on_active_to_ended_transition(
    _isolated_state_home: Path,
) -> None:
    transitioned = pm_turn_state.record_turn_state("operator", turn_ended=True)
    assert transitioned is True

    # The audit file should now contain exactly one pm.turn_ended event.
    audit_path = (
        Path(_isolated_state_home).parent / "pollypm_audit" / "_workspace.jsonl"
    )
    assert audit_path.exists(), "audit emit did not create the central log"
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    matching = [json.loads(line) for line in lines if line.strip()]
    matching = [evt for evt in matching if evt.get("event") == "pm.turn_ended"]
    assert len(matching) == 1
    assert matching[0]["subject"] == "operator"


def test_record_turn_state_is_idempotent_on_steady_state(
    _isolated_state_home: Path,
) -> None:
    """Repeated ended → ended calls emit at most one audit event."""
    assert pm_turn_state.record_turn_state("operator", turn_ended=True) is True
    # Second call with same state — no transition, no new audit emit.
    assert pm_turn_state.record_turn_state("operator", turn_ended=True) is False
    assert pm_turn_state.record_turn_state("operator", turn_ended=True) is False

    audit_path = (
        Path(_isolated_state_home).parent / "pollypm_audit" / "_workspace.jsonl"
    )
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    matching = [json.loads(line) for line in lines if line.strip()]
    matching = [evt for evt in matching if evt.get("event") == "pm.turn_ended"]
    assert len(matching) == 1, "duplicate audit emits on steady-state ticks"


def test_record_turn_state_re_emits_on_re_transition(
    _isolated_state_home: Path,
) -> None:
    """ended → active → ended fires a fresh audit event."""
    assert pm_turn_state.record_turn_state("operator", turn_ended=True) is True
    # User responded — PM is active again.
    assert pm_turn_state.record_turn_state("operator", turn_ended=False) is False
    # PM finished its next turn — fresh transition, fresh audit.
    assert pm_turn_state.record_turn_state("operator", turn_ended=True) is True


def test_record_turn_state_ignores_non_pm_sessions(
    _isolated_state_home: Path,
) -> None:
    assert (
        pm_turn_state.record_turn_state("worker_demo", turn_ended=True) is False
    )
    assert pm_turn_state.is_turn_ended("worker_demo") is False


def test_is_turn_ended_reads_persisted_state(
    _isolated_state_home: Path,
) -> None:
    pm_turn_state.record_turn_state("operator", turn_ended=True)
    assert pm_turn_state.is_turn_ended("operator") is True
    pm_turn_state.record_turn_state("operator", turn_ended=False)
    assert pm_turn_state.is_turn_ended("operator") is False


# ── Rail glyph override (the load-bearing user-visible contract) ─────


def test_rail_glyph_polly_paints_diamond_when_turn_ended(
    _isolated_state_home: Path,
) -> None:
    """Polly row paints ◆ once the audit event records turn-ended."""
    pm_turn_state.record_turn_state("operator", turn_ended=True)
    glyph, _color = _rail()._indicator(_polly_item(state="live"))
    assert glyph == "◆"


def test_rail_glyph_polly_no_override_when_turn_active(
    _isolated_state_home: Path,
) -> None:
    """Without a recorded turn-ended event, Polly keeps her normal glyph."""
    # No record at all — file doesn't exist yet.
    glyph, _color = _rail()._indicator(_polly_item(state="idle"))
    assert glyph != "◆"


def test_rail_glyph_polly_clears_when_user_replies(
    _isolated_state_home: Path,
) -> None:
    """Once the PM is mid-turn again, the rail drops the ◆ override."""
    pm_turn_state.record_turn_state("operator", turn_ended=True)
    pm_turn_state.record_turn_state("operator", turn_ended=False)
    glyph, _color = _rail()._indicator(_polly_item(state="live"))
    assert glyph != "◆"


def test_rail_glyph_project_paints_diamond_for_architect_turn_ended(
    _isolated_state_home: Path,
) -> None:
    """A project's per-project architect finishing its turn lights the row."""
    pm_turn_state.record_turn_state("architect_demo", turn_ended=True)
    glyph, _color = _rail()._indicator(_project_item(project_state="working"))
    assert glyph == "◆"


def test_rail_glyph_operational_red_still_wins_over_turn_ended(
    _isolated_state_home: Path,
) -> None:
    """Operational fault keeps its ▲ even when a PM turn just ended."""
    pm_turn_state.record_turn_state("architect_demo", turn_ended=True)
    item = _project_item(
        state="project-red",
        project_state="waiting",
        alert_severity="error",
    )
    glyph, _color = _rail()._indicator(item)
    assert glyph == "▲"


def test_rail_glyph_approvals_pending_still_wins_over_turn_ended(
    _isolated_state_home: Path,
) -> None:
    """The ▶ approvals affordance still outranks the PM turn glyph."""
    pm_turn_state.record_turn_state("architect_demo", turn_ended=True)
    item = _project_item(
        state="project-green",
        project_state="working",
        approvals_pending=2,
    )
    glyph, _color = _rail()._indicator(item)
    assert glyph == "▶"
