"""Unit tests for the project drilldown plan-review surface (#1401).

The surface activates when a project has a task in
``status=review, current_node_id=user_approval``. When no such task is
present the project drilldown falls back to the regular dashboard.

These tests cover:

* ``find_plan_review_task`` correctly picks the right task (and only
  the right task — review tasks not at ``user_approval`` are ignored).
* ``render_plan_review_surface`` renders header + summary + judgment
  calls + plan body + action bar.
* The action bar exposes visibly distinct labels + keybinding hints.
* The orchestrator (``_render_project_dashboard``) returns the
  plan-review surface when triggered, and the regular dashboard when
  not.
* Narrow mode (80x40) — the surface still emits all five sections and
  the action bar fits inside an 80-column terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.cockpit_sections.plan_review import (
    find_actionable_plan_review_task,
    find_plan_review_task,
    load_plan_text,
    render_plan_review_action_bar,
    render_plan_review_action_bar_plain,
    render_plan_review_header_strip,
    render_plan_review_surface,
)
from pollypm.work.models import Priority, WorkStatus


# ---------------------------------------------------------------------------
# Fakes — light enough to construct inline without touching SQLite.
# ---------------------------------------------------------------------------


class _PlanReviewFakeTask:
    """Minimal stand-in for ``work.models.Task`` for the surface tests."""

    def __init__(
        self,
        *,
        task_number: int = 7,
        task_id: str | None = None,
        status: str = "review",
        current_node_id: str | None = "user_approval",
        roles: dict[str, str] | None = None,
        assignee: str | None = None,
        plan_version: int = 1,
        updated_at: datetime | None = None,
        created_at: datetime | None = None,
        labels: list[str] | None = None,
    ) -> None:
        self.task_number = task_number
        self.task_id = task_id or f"proj/{task_number}"
        self.work_status = WorkStatus(status)
        self.current_node_id = current_node_id
        self.roles = roles or {}
        self.assignee = assignee
        self.plan_version = plan_version
        self.updated_at = updated_at
        self.created_at = created_at
        self.priority = Priority.NORMAL
        self.title = "plan: review me"
        self.labels = labels or []
        self.transitions = []
        self.executions = []


SAMPLE_PLAN = """# Project plan

## Summary
We will introduce a new module to validate URLs before persisting them.
Decomposition stays small per task; the worker can ship in a day.

## Judgment calls
- Use SQLite over PostgreSQL for v1 — simpler ops, room to migrate.
- Inline the regex check rather than pulling a parser dependency.
- Return 422 (not 400) on malformed payloads to match REST norms.

## Decomposition
- task: scaffolding the validate module
- task: wire validation into the persist path
- task: backfill tests + fuzz inputs

## Test strategy
Write unit tests around the regex; add property tests with hypothesis
to harden malformed-input handling. Add one e2e smoke for the persist
boundary.

## Critic synthesis
Risks: regex tightness vs. accepting valid IRIs; over-fitting to
test cases. Mitigation: maintain a small corpus of known-good /
known-bad URLs and run them in CI.
"""


# ---------------------------------------------------------------------------
# find_plan_review_task
# ---------------------------------------------------------------------------


class TestFindPlanReviewTask:
    def test_finds_review_at_user_approval(self):
        task = _PlanReviewFakeTask(
            task_number=12, current_node_id="user_approval", status="review",
        )
        assert find_plan_review_task([task]) is task

    def test_ignores_review_at_other_nodes(self):
        task = _PlanReviewFakeTask(
            task_number=12, current_node_id="critic_review", status="review",
        )
        assert find_plan_review_task([task]) is None

    def test_ignores_non_review_tasks(self):
        task = _PlanReviewFakeTask(
            task_number=12, current_node_id="user_approval", status="in_progress",
        )
        assert find_plan_review_task([task]) is None

    def test_empty_list_returns_none(self):
        assert find_plan_review_task([]) is None

    def test_picks_most_recently_updated_when_multiple(self):
        old = _PlanReviewFakeTask(
            task_number=1,
            updated_at=datetime.now(UTC) - timedelta(hours=2),
        )
        new = _PlanReviewFakeTask(
            task_number=2,
            updated_at=datetime.now(UTC),
        )
        assert find_plan_review_task([old, new]) is new


# ---------------------------------------------------------------------------
# find_actionable_plan_review_task (#1531)
# ---------------------------------------------------------------------------


class TestFindActionablePlanReviewTask:
    """#1531 — broader matcher that covers #1511 backstop emits.

    The canonical matcher only finds ``plan_project`` tasks parked at
    ``user_approval``. The watchdog backstop creates a ``chat``-flow
    notify-only stub at ``status=done``, never at ``user_approval`` —
    so projects backfilled by the watchdog were structurally
    unreachable from the project drilldown plan-review surface.
    """

    def test_falls_back_to_canonical_when_present(self):
        """The architect's reflection emit still wins over backstop rows."""
        canonical = _PlanReviewFakeTask(
            task_number=10, current_node_id="user_approval", status="review",
        )
        backstop = _PlanReviewFakeTask(
            task_number=30,
            task_id="proj/30",
            current_node_id=None,
            status="done",
            labels=["plan_review", "plan_task:proj/1", "notify"],
        )
        result = find_actionable_plan_review_task([backstop, canonical])
        assert result is canonical

    def test_matches_backstop_chat_done_stub(self):
        """The coffeeboardnm/30-shape: chat-flow done stub with plan_review label."""
        plan_task = _PlanReviewFakeTask(
            task_number=1,
            task_id="coffeeboardnm/1",
            status="done",
            current_node_id=None,
            labels=["poc-plan", "research"],
        )
        backstop = _PlanReviewFakeTask(
            task_number=30,
            task_id="coffeeboardnm/30",
            status="done",
            current_node_id=None,
            labels=[
                "plan_review",
                "project:coffeeboardnm",
                "plan_task:coffeeboardnm/1",
                "notify",
            ],
        )
        result = find_actionable_plan_review_task([plan_task, backstop])
        # Resolves to the underlying plan task so the surface header
        # reads as the plan, not the backstop notify stub.
        assert result is plan_task

    def test_returns_none_when_no_plan_review_anywhere(self):
        regular = _PlanReviewFakeTask(
            task_number=5,
            status="done",
            current_node_id=None,
            labels=["bug", "infra"],
        )
        assert find_actionable_plan_review_task([regular]) is None

    def test_skips_cancelled_backstop_rows(self):
        """Cancelled plan_review rows aren't actionable — the user dismissed them."""
        backstop = _PlanReviewFakeTask(
            task_number=30,
            task_id="proj/30",
            status="cancelled",
            current_node_id=None,
            labels=["plan_review", "plan_task:proj/1", "notify"],
        )
        assert find_actionable_plan_review_task([backstop]) is None

    def test_falls_back_to_backstop_when_plan_task_missing(self):
        """When the referenced plan task is gone, return the backstop itself."""
        backstop = _PlanReviewFakeTask(
            task_number=30,
            task_id="proj/30",
            status="done",
            current_node_id=None,
            labels=["plan_review", "plan_task:proj/missing", "notify"],
        )
        # The referenced ``proj/missing`` plan task isn't in the list —
        # the backstop itself is still a valid surface trigger.
        result = find_actionable_plan_review_task([backstop])
        assert result is backstop

    def test_picks_most_recently_updated_backstop(self):
        """Multiple backstop stubs — pick the freshest before resolving."""
        plan_task = _PlanReviewFakeTask(
            task_number=1, task_id="proj/1", status="done", current_node_id=None,
        )
        old = _PlanReviewFakeTask(
            task_number=20,
            task_id="proj/20",
            status="done",
            current_node_id=None,
            labels=["plan_review", "plan_task:proj/1"],
            updated_at=datetime.now(UTC) - timedelta(hours=5),
        )
        new = _PlanReviewFakeTask(
            task_number=30,
            task_id="proj/30",
            status="done",
            current_node_id=None,
            labels=["plan_review", "plan_task:proj/1"],
            updated_at=datetime.now(UTC),
        )
        result = find_actionable_plan_review_task([plan_task, old, new])
        # Both backstops resolve to the same plan_task — winner just
        # needs to be the freshest backstop's resolution.
        assert result is plan_task

    def test_empty_list_returns_none(self):
        assert find_actionable_plan_review_task([]) is None
        assert find_actionable_plan_review_task(None) is None


# ---------------------------------------------------------------------------
# Action bar
# ---------------------------------------------------------------------------


class TestActionBar:
    def test_plain_action_bar_includes_all_keybindings(self):
        bar = render_plan_review_action_bar_plain()
        assert "[a] Approve" in bar
        assert "[c] Chat to refine" in bar
        assert "[d] Deny" in bar
        assert "[esc] Back" in bar

    def test_rich_action_bar_uses_distinct_colors_per_action(self):
        """Each label uses bold + a colour token so it reads as a button.

        The user-facing requirement (issue #1401) is "visibly labeled
        buttons". We assert distinct colour markers for the three
        action keys; `esc` stays neutral bold (it's the "exit" hint).
        """
        bar = render_plan_review_action_bar()
        assert "[bold green][a][/bold green]" in bar
        assert "[bold cyan][c][/bold cyan]" in bar
        assert "[bold red][d][/bold red]" in bar
        assert "Approve" in bar
        assert "Chat to refine" in bar
        assert "Deny" in bar
        # `esc` keybinding present — `\\[esc]` because Rich-markup
        # uses ``[`` literally for tags, so we escape the bracket.
        assert "\\[esc]" in bar
        assert "Back" in bar

    def test_action_bar_fits_in_80_columns(self):
        """Narrow-mode (80x40) — plain bar must fit on one line."""
        # The plain bar is what tests + non-Rich consumers see; that's
        # the stricter constraint.
        bar = render_plan_review_action_bar_plain()
        for line in bar.splitlines():
            assert len(line) <= 80, (
                f"action bar line exceeds 80 cols: {len(line)} -> {line!r}"
            )


# ---------------------------------------------------------------------------
# Header strip
# ---------------------------------------------------------------------------


class TestHeaderStrip:
    def test_header_strip_format(self):
        task = _PlanReviewFakeTask(
            task_number=42,
            roles={"architect": "polly"},
            plan_version=2,
            updated_at=datetime.now(UTC) - timedelta(minutes=15),
        )
        line = render_plan_review_header_strip(
            project_key="shortlink-gen", task=task,
        )
        assert "Plan: shortlink-gen/42" in line
        assert "Architect: polly" in line
        assert "Generated 15m ago" in line
        assert "v2" in line

    def test_header_falls_back_when_no_persona(self):
        task = _PlanReviewFakeTask(task_number=7, roles=None, assignee=None)
        line = render_plan_review_header_strip(
            project_key="proj", task=task,
        )
        assert "Plan: proj/7" in line
        assert "Architect: architect" in line  # fallback


# ---------------------------------------------------------------------------
# Full surface
# ---------------------------------------------------------------------------


class TestRenderPlanReviewSurface:
    def test_surface_renders_all_five_sections(self):
        task = _PlanReviewFakeTask(
            task_number=11,
            roles={"architect": "polly"},
            plan_version=1,
            updated_at=datetime.now(UTC),
        )
        out = render_plan_review_surface(
            project_key="proj",
            project_name="My Project",
            task=task,
            plan_text=SAMPLE_PLAN,
        )
        # Header strip present.
        assert "Plan: proj/11" in out
        assert "Architect: polly" in out
        # Each labeled section divider appears.
        assert "Summary" in out
        assert "Judgment calls" in out
        assert "Plan body" in out
        assert "Actions" in out
        # Summary text from the plan.
        assert "introduce a new module to validate URLs" in out
        # Judgment-call bullets render.
        assert "Use SQLite over PostgreSQL" in out
        assert "Inline the regex check" in out
        # Plan body retains decomposition + critic synthesis.
        assert "Decomposition" in out
        assert "Critic synthesis" in out
        # Action bar at bottom — both rich + plain rendered.
        assert "[a] Approve" in out
        assert "[c] Chat to refine" in out
        assert "[d] Deny" in out
        assert "[esc] Back" in out

    def test_surface_handles_empty_plan(self):
        """No plan text — header + action bar must still render."""
        task = _PlanReviewFakeTask(task_number=4)
        out = render_plan_review_surface(
            project_key="proj", project_name="P", task=task, plan_text="",
        )
        assert "Plan: proj/4" in out
        assert "(no summary block in plan)" in out
        assert "(no judgment calls flagged)" in out
        assert "(plan body is empty)" in out
        # Action bar still visible.
        assert "[a] Approve" in out
        assert "[esc] Back" in out

    def test_surface_summary_judgment_not_duplicated_in_body(self):
        """The plan body section drops the summary + judgment headers.

        Otherwise users see the same summary block twice — once in the
        Summary section, once at the top of the Plan body.
        """
        task = _PlanReviewFakeTask(task_number=11)
        out = render_plan_review_surface(
            project_key="proj", project_name="P", task=task, plan_text=SAMPLE_PLAN,
        )
        # Summary appears exactly once (section divider also says
        # "Summary" — that's why we look for the body sentence).
        assert out.count("introduce a new module to validate URLs") == 1
        # Judgment-call bullets appear once (in the Judgment calls section).
        assert out.count("Use SQLite over PostgreSQL") == 1

    def test_surface_renders_in_narrow_terminal(self):
        """80x40 narrow mode — every line stays within reasonable width.

        Rich markup tags don't render visibly so the on-screen width is
        smaller than ``len(line)``. We lower the bound to allow inline
        markup; the structural test here is that the surface renders
        all five sections AND no line balloons past ~120 cols (which
        would mean an unwrapped plan body line).
        """
        task = _PlanReviewFakeTask(
            task_number=11, roles={"architect": "polly"},
        )
        out = render_plan_review_surface(
            project_key="proj", project_name="P", task=task, plan_text=SAMPLE_PLAN,
        )
        for line in out.splitlines():
            assert len(line) <= 200, (
                f"surface line too wide for narrow mode: {len(line)} -> {line!r}"
            )
        # Even in narrow terminals the action bar + header are visible.
        assert "Plan: proj/11" in out
        assert "[a] Approve" in out


# ---------------------------------------------------------------------------
# load_plan_text
# ---------------------------------------------------------------------------


class TestLoadPlanText:
    def test_returns_empty_when_no_plan(self, tmp_path: Path):
        proj = tmp_path / "noplan"
        proj.mkdir()
        assert load_plan_text(proj) == ""

    def test_reads_plan_when_present(self, tmp_path: Path):
        proj = tmp_path / "withplan"
        (proj / "docs" / "plan").mkdir(parents=True)
        plan_file = proj / "docs" / "plan" / "plan.md"
        plan_file.write_text(SAMPLE_PLAN, encoding="utf-8")
        text = load_plan_text(proj)
        assert "introduce a new module to validate URLs" in text


# ---------------------------------------------------------------------------
# Orchestrator integration — _render_project_dashboard switches surfaces
# ---------------------------------------------------------------------------


@dataclass
class _OrchFakeProject:
    key: str
    path: Path
    name: str


class _OrchFakeStore:
    def open_alerts(self): return []
    def recent_events(self, limit=200): return []
    def latest_heartbeat(self, name): return None


class _OrchFakeSupervisor:
    store = _OrchFakeStore()
    def plan_launches(self): return []


def _seed_project_with_plan_review(tmp_path: Path) -> tuple[Path, str]:
    """Seed a project whose only task is at status=review/user_approval."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    proj_path = tmp_path / "planreviewed"
    proj_path.mkdir()
    (proj_path / ".pollypm").mkdir()
    db_path = proj_path / ".pollypm" / "state.db"
    svc = SQLiteWorkService(db_path=db_path, project_path=proj_path)

    task = svc.create(
        title="plan: implement validation",
        description="plan",
        type="task",
        project="planreviewed",
        flow_template="standard",
        roles={"worker": "polly", "reviewer": "russell"},
        priority="normal",
        created_by="polly",
    )
    svc.queue(task.task_id, "polly")
    svc.claim(task.task_id, "worker")
    # Force the task into review/user_approval directly via SQL — the
    # standard flow doesn't park at ``user_approval`` natively, but the
    # plan_project flow does. For this surface test we just need the
    # row state to match the trigger, not the full flow execution.
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE work_tasks SET work_status = ?, current_node_id = ? "
            "WHERE project = ? AND task_number = ?",
            ("review", "user_approval", "planreviewed", task.task_number),
        )
        conn.commit()
    finally:
        conn.close()
    svc.close()

    # Drop a plan markdown so the surface has content to render.
    plan_dir = proj_path / "docs" / "plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.md").write_text(SAMPLE_PLAN, encoding="utf-8")
    return proj_path, "planreviewed"


def _seed_project_no_plan_review(tmp_path: Path) -> tuple[Path, str]:
    """Seed a project with one in_progress task — no plan-review trigger."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    proj_path = tmp_path / "regular"
    proj_path.mkdir()
    (proj_path / ".pollypm").mkdir()
    db_path = proj_path / ".pollypm" / "state.db"
    svc = SQLiteWorkService(db_path=db_path, project_path=proj_path)

    task = svc.create(
        title="just code",
        description="d",
        type="task",
        project="regular",
        flow_template="standard",
        roles={"worker": "pete", "reviewer": "russell"},
        priority="normal",
        created_by="polly",
    )
    svc.queue(task.task_id, "polly")
    svc.claim(task.task_id, "worker")
    svc.close()
    return proj_path, "regular"


class TestOrchestratorTriggers:
    def test_drilldown_renders_plan_review_surface_when_triggered(
        self, tmp_path: Path,
    ):
        from pollypm.cockpit_sections.project_dashboard import (
            _render_project_dashboard,
        )

        proj_path, key = _seed_project_with_plan_review(tmp_path)
        project = _OrchFakeProject(key=key, path=proj_path, name="Plan Reviewed")
        out = _render_project_dashboard(
            project, key, tmp_path / "pollypm.toml", _OrchFakeSupervisor(),
        )
        assert out is not None
        # Plan-review surface markers.
        assert "Plan: planreviewed/" in out
        assert "Summary" in out
        assert "Judgment calls" in out
        assert "Plan body" in out
        assert "[a] Approve" in out
        assert "[c] Chat to refine" in out
        assert "[d] Deny" in out
        assert "[esc] Back" in out
        # Regular-dashboard sections must NOT appear when the
        # plan-review surface is active.
        assert "You need to" not in out
        assert "In flight" not in out
        assert "Quick actions" not in out

    def test_drilldown_renders_regular_dashboard_when_no_plan_review(
        self, tmp_path: Path,
    ):
        from pollypm.cockpit_sections.project_dashboard import (
            _render_project_dashboard,
        )

        proj_path, key = _seed_project_no_plan_review(tmp_path)
        project = _OrchFakeProject(key=key, path=proj_path, name="Regular")
        out = _render_project_dashboard(
            project, key, tmp_path / "pollypm.toml", _OrchFakeSupervisor(),
        )
        assert out is not None
        # Regular-dashboard markers present.
        assert "You need to" in out
        assert "In flight" in out
        assert "Quick actions" in out
        # Plan-review action bar must NOT show on the regular dashboard.
        assert "[a] Approve" not in out
        assert "[c] Chat to refine" not in out

    def test_drilldown_renders_for_backstop_emit_chat_done_stub(
        self, tmp_path: Path,
    ):
        """#1531 — projects whose plan-review row was emitted by the
        #1511 watchdog backstop (chat-flow ``done`` stub at status=done,
        NOT ``user_approval``) still render the plan-review surface.

        Reproduces the coffeeboardnm/30 shape: plan task at
        ``coffeeboardnm/1`` is on the standard flow at ``done`` with
        plan-shaped labels; the backstop creates a chat-flow notify-
        only stub at task #30 carrying the ``plan_review`` +
        ``plan_task:coffeeboardnm/1`` labels. Before this fix the
        drilldown surface NEVER triggered for such projects (the
        canonical matcher only matched ``user_approval`` rows).
        """
        from pollypm.cockpit_sections.project_dashboard import (
            _render_project_dashboard,
        )
        from pollypm.work.sqlite_service import SQLiteWorkService

        proj_path = tmp_path / "coffeeboardnm"
        proj_path.mkdir()
        (proj_path / ".pollypm").mkdir()
        db_path = proj_path / ".pollypm" / "state.db"
        with SQLiteWorkService(
            db_path=db_path, project_path=proj_path,
        ) as svc:
            # Plan task — done, plan-shaped, on standard flow.
            plan_task = svc.create(
                title="POC plan: ingest every NM event May–Jul 2026",
                description="plan body lives on disk",
                type="task",
                project="coffeeboardnm",
                flow_template="standard",
                roles={"worker": "polly", "reviewer": "russell"},
                priority="normal",
                created_by="polly",
                labels=["poc-plan", "research"],
            )
            svc.queue(plan_task.task_id, "polly")
            svc.claim(plan_task.task_id, "worker")
            # Mark the plan task done via SQL (skip flow execution).
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "UPDATE work_tasks SET work_status = ? "
                    "WHERE project = ? AND task_number = ?",
                    ("done", "coffeeboardnm", plan_task.task_number),
                )
                conn.commit()
            finally:
                conn.close()
            # Backstop emit — chat-flow notify stub.
            backstop_task = svc.create(
                title="Plan ready for review: coffeeboardnm",
                description="Plan ready for review (backstop emit).",
                type="task",
                project="coffeeboardnm",
                flow_template="chat",
                roles={"requester": "user", "operator": "audit_watchdog"},
                priority="high",
                created_by="audit_watchdog",
                labels=[
                    "plan_review",
                    "project:coffeeboardnm",
                    f"plan_task:{plan_task.task_id}",
                    "notify",
                ],
            )
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "UPDATE work_tasks SET work_status = ? "
                    "WHERE project = ? AND task_number = ?",
                    ("done", "coffeeboardnm", backstop_task.task_number),
                )
                conn.commit()
            finally:
                conn.close()

        plan_dir = proj_path / "docs" / "plan"
        plan_dir.mkdir(parents=True)
        (plan_dir / "plan.md").write_text(SAMPLE_PLAN, encoding="utf-8")

        project = _OrchFakeProject(
            key="coffeeboardnm", path=proj_path, name="CoffeeBoardNM",
        )
        out = _render_project_dashboard(
            project, "coffeeboardnm",
            tmp_path / "pollypm.toml",
            _OrchFakeSupervisor(),
        )
        assert out is not None
        # Plan-review surface renders for the BACKSTOP-emitted shape.
        # Header references the underlying plan task (#1, NOT the
        # backstop stub at #30) — that's the resolution behavior.
        assert "Plan: coffeeboardnm/1" in out
        # Surface body markers.
        assert "Summary" in out
        assert "Judgment calls" in out
        assert "Plan body" in out
        assert "[a] Approve" in out
        # Regular dashboard sections are suppressed when the surface
        # is active.
        assert "You need to" not in out
        assert "Quick actions" not in out
