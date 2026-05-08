"""Tests for the advisor inbox integration (ad05).

Covers:

* emit_insight writes the JSON sidecar + markdown body with the right
  fields for both severity + topic.
* Silent decisions never touch the inbox.
* acknowledge / dismiss / mark_converted close the entry with the
  right outcome / reason.
* convert_to_task creates a work-service task with the expected flow,
  labels, and roles, then closes the insight and links the new task.
* record_decision (assess path) appends a history line AND writes an
  inbox entry on emit; silent decisions only touch the log.
* End-to-end: emit → user dismisses with topic_cooldown → next
  context pack's trajectory surfaces the dismissal.
* Auto-close sweep closes open entries older than 7 days; sweep
  handler honors the max_age_days + now_utc overrides.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.plugins_builtin.advisor.handlers.assess import (
    build_context_pack,
    parse_decision,
    record_decision,
)
from pollypm.plugins_builtin.advisor.handlers.autoclose import (
    advisor_autoclose_handler,
)
from pollypm.plugins_builtin.advisor.handlers.detect_changes import ChangeReport
from pollypm.plugins_builtin.advisor.handlers.history_log import read_log
from pollypm.plugins_builtin.advisor.inbox import (
    INSIGHT_KIND,
    acknowledge,
    auto_close_expired,
    convert_to_task,
    dismiss,
    emit_insight,
    get_insight,
    insights_dir,
    list_insights,
)
from pollypm.plugins_builtin.advisor.state import (
    record_dismissal,
    load_state,
)


EMIT_PAYLOAD = (
    '{"emit": true, "topic": "architecture_drift", '
    '"severity": "recommendation", '
    '"summary": "Cockpit going monolithic.", '
    '"details": "Commits abc/def added panels to cockpit.py.", '
    '"suggestion": "Extract the largest panels first."}'
)

SILENT_PAYLOAD = '{"emit": false, "rationale_if_silent": "on-plan"}'


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeTask:
    project: str
    task_number: int
    title: str
    labels: list[str]
    roles: dict[str, str]
    flow_template: str


class FakeWorkService:
    def __init__(self) -> None:
        self.created: list[FakeTask] = []
        self._counter = 0

    def create(
        self, *, title, description, type, project, flow_template,
        roles, priority, labels=None, requires_human_review=False,
        **_kw,
    ):
        self._counter += 1
        task = FakeTask(
            project=project, task_number=self._counter,
            title=title, labels=list(labels or []),
            roles=dict(roles or {}), flow_template=flow_template,
        )
        self.created.append(task)
        return task


# ---------------------------------------------------------------------------
# emit_insight
# ---------------------------------------------------------------------------


class TestEmitInsight:
    def test_emit_writes_sidecar_and_body(self, tmp_path: Path) -> None:
        d = parse_decision(EMIT_PAYLOAD)
        insight = emit_insight(
            tmp_path, project="pollypm", decision=d,
            task_id="pollypm/412", commits_reviewed=["abc", "def"],
        )
        assert insight.kind == INSIGHT_KIND
        assert insight.topic == "architecture_drift"
        assert insight.severity == "recommendation"
        assert insight.status == "open"
        assert insight.task_id == "pollypm/412"
        assert insight.commits_reviewed == ["abc", "def"]

        sidecar = insights_dir(tmp_path) / f"{insight.insight_id}.json"
        body = insights_dir(tmp_path) / f"{insight.insight_id}.md"
        assert sidecar.exists()
        assert body.exists()
        body_md = body.read_text()
        assert "Cockpit going monolithic." in body_md
        assert "Extract the largest panels first." in body_md

    def test_silent_decision_raises(self, tmp_path: Path) -> None:
        d = parse_decision(SILENT_PAYLOAD)
        with pytest.raises(ValueError):
            emit_insight(tmp_path, project="p", decision=d)

    def test_list_insights_filters_by_status_and_project(self, tmp_path: Path) -> None:
        d = parse_decision(EMIT_PAYLOAD)
        a = emit_insight(tmp_path, project="a", decision=d)
        emit_insight(tmp_path, project="b", decision=d)

        acknowledge(tmp_path, a.insight_id)
        assert [e.insight_id for e in list_insights(tmp_path, status="open")] == [
            i.insight_id for i in list_insights(tmp_path, status="open")
            if i.project == "b"
        ] != []
        assert len(list_insights(tmp_path, status="closed")) == 1
        assert len(list_insights(tmp_path, status="all")) == 2
        assert len(list_insights(tmp_path, project="a", status="all")) == 1


# ---------------------------------------------------------------------------
# User actions
# ---------------------------------------------------------------------------


class TestUserActions:
    def test_acknowledge(self, tmp_path: Path) -> None:
        d = parse_decision(EMIT_PAYLOAD)
        ins = emit_insight(tmp_path, project="p", decision=d)
        closed = acknowledge(tmp_path, ins.insight_id)
        assert closed is not None
        assert closed.status == "closed"
        assert closed.outcome == "acknowledged"
        assert closed.closed_at != ""

    def test_dismiss_with_reason(self, tmp_path: Path) -> None:
        d = parse_decision(EMIT_PAYLOAD)
        ins = emit_insight(tmp_path, project="p", decision=d)
        closed = dismiss(tmp_path, ins.insight_id, reason="topic_cooldown")
        assert closed.outcome == "dismissed"
        assert closed.reason == "topic_cooldown"

    def test_acknowledge_closed_is_noop(self, tmp_path: Path) -> None:
        d = parse_decision(EMIT_PAYLOAD)
        ins = emit_insight(tmp_path, project="p", decision=d)
        acknowledge(tmp_path, ins.insight_id)
        # Second call must return the same (closed) state; outcome unchanged.
        closed = acknowledge(tmp_path, ins.insight_id)
        assert closed.outcome == "acknowledged"

    def test_missing_id_returns_none(self, tmp_path: Path) -> None:
        assert acknowledge(tmp_path, "nope") is None
        assert dismiss(tmp_path, "nope") is None

    def test_convert_to_task_creates_work_and_closes_insight(self, tmp_path: Path) -> None:
        d = parse_decision(EMIT_PAYLOAD)
        ins = emit_insight(tmp_path, project="p", decision=d)

        ws = FakeWorkService()
        updated, task = convert_to_task(
            tmp_path, ins.insight_id, work_service=ws, flow="implement_module",
        )
        assert task is not None
        assert task.flow_template == "implement_module"
        assert "advisor" in task.labels
        assert "converted_from_insight" in task.labels
        assert updated.outcome == "converted"
        assert updated.converted_task_id == f"p/{task.task_number}"
        assert updated.status == "closed"


# ---------------------------------------------------------------------------
# record_decision — assess glue
# ---------------------------------------------------------------------------


class TestRecordDecision:
    def test_silent_only_logs(self, tmp_path: Path) -> None:
        d = parse_decision(SILENT_PAYLOAD)
        result = record_decision(
            base_dir=tmp_path, project="p", decision=d,
            task_id="p/1", commits_reviewed=["abc"],
        )
        assert result["inbox_entry"] is None
        # History got one silent entry.
        log_entries = read_log(tmp_path)
        assert len(log_entries) == 1
        assert log_entries[0].decision == "silent"
        # No inbox directory (emission would create it).
        assert list_insights(tmp_path, status="all") == []

    def test_emit_writes_log_and_inbox(self, tmp_path: Path) -> None:
        d = parse_decision(EMIT_PAYLOAD)
        result = record_decision(
            base_dir=tmp_path, project="p", decision=d,
            task_id="p/7", commits_reviewed=["abc", "def"],
        )
        assert result["inbox_entry"] is not None
        assert result["inbox_entry"].project == "p"
        assert result["inbox_entry"].task_id == "p/7"
        log_entries = read_log(tmp_path)
        assert len(log_entries) == 1
        assert log_entries[0].decision == "emit"
        assert log_entries[0].topic == "architecture_drift"

    def test_invalid_output_does_not_emit_inbox(self, tmp_path: Path) -> None:
        """Parser returned emit=false with invalid_output_reason — no inbox write."""
        d = parse_decision('{"emit": "not a bool"}')
        assert d.invalid_output_reason is not None
        assert d.emit is False
        result = record_decision(base_dir=tmp_path, project="p", decision=d)
        assert result["inbox_entry"] is None


# ---------------------------------------------------------------------------
# End-to-end: emit → dismiss w/ topic_cooldown → next context pack
# ---------------------------------------------------------------------------


class TestEndToEndDismissalVisibleInContext:
    def test_dismissal_surfaces_in_next_context(self, tmp_path: Path) -> None:
        project_path = tmp_path / "proj"
        project_path.mkdir()
        base_dir = tmp_path / "state"
        base_dir.mkdir()

        # 1. Emit an insight.
        d = parse_decision(EMIT_PAYLOAD)
        record_decision(base_dir=base_dir, project="proj", decision=d)
        insights = list_insights(base_dir, status="open")
        assert len(insights) == 1

        # 2. User dismisses with topic_cooldown; the dismissal is also
        #    recorded in advisor-state so the prompt-pack sees it.
        dismiss(base_dir, insights[0].insight_id, reason="topic_cooldown")
        record_dismissal(base_dir, "proj", "architecture_drift")

        state = load_state(base_dir)
        assert any(
            d.topic == "architecture_drift"
            for d in state.get("proj").recent_dismissals
        )

        # 3. The next context pack surfaces the dismissal.
        pack = build_context_pack(
            project_key="proj",
            project_path=project_path,
            report=ChangeReport(project_path=project_path, since=None),
            base_dir=base_dir,
            include_diffs=False,
        )
        assert "architecture_drift" in pack.trajectory_section


# ---------------------------------------------------------------------------
# Auto-close sweep
# ---------------------------------------------------------------------------


class TestAutoClose:
    def test_expired_open_insight_is_closed(self, tmp_path: Path) -> None:
        d = parse_decision(EMIT_PAYLOAD)
        ins = emit_insight(tmp_path, project="p", decision=d)
        # Back-date to 10 days ago.
        sidecar = insights_dir(tmp_path) / f"{ins.insight_id}.json"
        data = json.loads(sidecar.read_text())
        data["created_at"] = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        sidecar.write_text(json.dumps(data, indent=2, sort_keys=True))

        closed = auto_close_expired(tmp_path, max_age_days=7)
        assert len(closed) == 1
        reloaded = get_insight(tmp_path, ins.insight_id)
        assert reloaded.status == "closed"
        assert reloaded.outcome == "autoclosed"

    def test_young_insight_is_not_closed(self, tmp_path: Path) -> None:
        d = parse_decision(EMIT_PAYLOAD)
        emit_insight(tmp_path, project="p", decision=d)
        closed = auto_close_expired(tmp_path, max_age_days=7)
        assert closed == []

    def test_autoclose_handler_time_travel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Acceptance §7: 7-day auto-close verified via time-travel."""
        from dataclasses import dataclass as _dc

        base_dir = tmp_path / ".pollypm"
        base_dir.mkdir()

        d = parse_decision(EMIT_PAYLOAD)
        ins = emit_insight(base_dir, project="p", decision=d)

        @_dc
        class _FakeProject:
            base_dir: Path

        @_dc
        class _FakeConfig:
            project: "_FakeProject"

        cfg = _FakeConfig(project=_FakeProject(base_dir=base_dir))
        config_path = tmp_path / "pollypm.toml"
        config_path.write_text("")
        monkeypatch.setattr(
            "pollypm.config.load_config", lambda _p: cfg,
        )
        monkeypatch.setattr(
            "pollypm.config.resolve_config_path", lambda _p: config_path,
        )

        # 8 days later — sweep should close the entry.
        future = datetime.now(UTC) + timedelta(days=8)
        result = advisor_autoclose_handler(
            {
                "config_path": str(config_path),
                "now_utc": future.isoformat(),
                "max_age_days": 7,
            }
        )
        assert result["fired"] is True
        assert result["closed_count"] == 1
        reloaded = get_insight(base_dir, ins.insight_id)
        assert reloaded.outcome == "autoclosed"
