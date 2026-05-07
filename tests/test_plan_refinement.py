"""Tests for the chat-to-refine flow (#1404).

Coverage:
- Primer text shape (refinement framing, version + task ids quoted, body
  inlined as a markdown blockquote, signal-phrase examples surfaced).
- ``is_refinement_signal`` detects the canonical phrases case-insensitively.
- ``select_chat_primer_for_project_dashboard`` returns ``None`` when no
  plan_review is pending and a fully-formed primer when one is.
- ``apply_plan_refinement`` writes the new plan body to the canonical
  path AND bumps ``plan_version`` via the SDK (audit event fires).
- Integration: full chat-to-refine cycle with a mocked architect — primer
  is built, signal triggers the apply path, version increments, audit
  event fires, file content is replaced.
"""

from __future__ import annotations

import pytest

from pollypm.plan_refinement import (
    PlanRefinementResult,
    REFINEMENT_SIGNAL_PHRASES,
    apply_plan_refinement,
    build_plan_refinement_primer,
    is_refinement_signal,
    select_chat_primer_for_project_dashboard,
)
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _create_plan_task(svc, project="demo"):
    return svc.create(
        title="Plan demo",
        description="Initial plan body",
        type="task",
        project=project,
        flow_template="standard",
        roles={"worker": "architect", "reviewer": "reviewer"},
        priority="normal",
        created_by="architect",
    )


@pytest.fixture
def svc(tmp_path):
    return SQLiteWorkService(db_path=tmp_path / "work.db")


class _FakeDashboardData:
    """Duck-typed stand-in for ``ProjectDashboardData`` used by the
    routing helper. We only feed in the fields the helper reads so the
    test stays decoupled from cockpit_ui's heavier ProjectDashboardData
    constructor.
    """

    def __init__(
        self,
        *,
        project_key="demo",
        action_items=None,
        task_buckets=None,
        plan_text="The plan body, version one.",
        plan_path=None,
    ):
        self.project_key = project_key
        self.action_items = action_items or []
        self.task_buckets = task_buckets or {}
        self.plan_text = plan_text
        self.plan_path = plan_path


# ---------------------------------------------------------------------------
# Primer text shape
# ---------------------------------------------------------------------------


class TestBuildPlanRefinementPrimer:
    """Primer text needs to (a) frame the conversation as in-place
    revision, (b) quote the plan body so the PM can refer to it, (c)
    name the canonical refinement-signal phrases so the user knows the
    handoff gesture, and (d) NOT instruct the PM to rewrite the plan
    itself — that's the architect's job.
    """

    def test_includes_project_task_and_version(self):
        primer = build_plan_refinement_primer(
            project_key="savethenovel",
            plan_task_id="savethenovel/4",
            plan_version=2,
            plan_body="Build the world.",
        )
        assert "savethenovel" in primer
        assert "savethenovel/4" in primer
        assert "v2" in primer
        # The next-version hint helps the user understand version bumping.
        assert "v3" in primer

    def test_quotes_plan_body_as_blockquote(self):
        primer = build_plan_refinement_primer(
            project_key="demo",
            plan_task_id="demo/1",
            plan_version=1,
            plan_body="Line one\nLine two\n\nLine four",
        )
        assert "> Line one" in primer
        assert "> Line two" in primer
        # Blank lines stay as bare ``>`` markers (markdown convention).
        assert "\n>\n" in primer or ">\n>" in primer
        assert "> Line four" in primer

    def test_handles_empty_plan_body(self):
        primer = build_plan_refinement_primer(
            project_key="demo",
            plan_task_id="demo/1",
            plan_version=1,
            plan_body="",
        )
        assert "> (plan body is empty)" in primer

    def test_includes_signal_phrase_examples(self):
        primer = build_plan_refinement_primer(
            project_key="demo",
            plan_task_id="demo/1",
            plan_version=1,
            plan_body="x",
        )
        # At least the canonical "go architect" gesture must be quoted
        # so the user knows the handoff phrase. Other examples come
        # from REFINEMENT_SIGNAL_PHRASES.
        assert "go architect" in primer

    def test_includes_plan_path_when_supplied(self):
        primer = build_plan_refinement_primer(
            project_key="demo",
            plan_task_id="demo/1",
            plan_version=1,
            plan_body="x",
            plan_path="/repo/docs/project-plan.md",
        )
        assert "/repo/docs/project-plan.md" in primer

    def test_omits_plan_path_line_when_missing(self):
        primer = build_plan_refinement_primer(
            project_key="demo",
            plan_task_id="demo/1",
            plan_version=1,
            plan_body="x",
            plan_path=None,
        )
        assert "Plan file:" not in primer

    def test_uses_reviewer_name_for_pronouns(self):
        primer = build_plan_refinement_primer(
            project_key="demo",
            plan_task_id="demo/1",
            plan_version=1,
            plan_body="x",
            reviewer_name="Polly",
        )
        assert "Polly is here to refine" in primer

    def test_frames_in_place_revision_explicitly(self):
        primer = build_plan_refinement_primer(
            project_key="demo",
            plan_task_id="demo/1",
            plan_version=1,
            plan_body="x",
        )
        # Anti-deny framing: the primer must say "same task id" so the
        # PM doesn't accidentally request a replan/successor.
        assert "in-place" in primer.lower()
        assert "same task id" in primer.lower()

    def test_does_not_instruct_pm_to_rewrite_plan(self):
        """The architect, not the PM, ships the rewrite; primer must
        say so or the PM will burn tokens drafting plan markdown in
        the chat instead of handing off."""
        primer = build_plan_refinement_primer(
            project_key="demo",
            plan_task_id="demo/1",
            plan_version=1,
            plan_body="x",
        )
        assert "DO NOT rewrite the plan yourself" in primer


# ---------------------------------------------------------------------------
# Refinement-signal detection
# ---------------------------------------------------------------------------


class TestIsRefinementSignal:
    @pytest.mark.parametrize(
        "phrase",
        list(REFINEMENT_SIGNAL_PHRASES),
    )
    def test_canonical_phrases_match(self, phrase):
        assert is_refinement_signal(phrase) is True

    def test_match_is_case_insensitive(self):
        assert is_refinement_signal("Go Architect") is True
        assert is_refinement_signal("REVISE THE PLAN") is True

    def test_match_is_substring_so_natural_text_works(self):
        assert is_refinement_signal(
            "ok let's go architect on it"
        ) is True
        assert is_refinement_signal(
            "yeah revise the plan and ping me when it's done"
        ) is True

    def test_unrelated_text_does_not_match(self):
        assert is_refinement_signal("looks good") is False
        assert is_refinement_signal("hmm let me think") is False

    def test_empty_input_does_not_match(self):
        assert is_refinement_signal("") is False
        assert is_refinement_signal(None) is False


# ---------------------------------------------------------------------------
# Routing helper for the project dashboard
# ---------------------------------------------------------------------------


class TestSelectChatPrimerForProjectDashboard:
    def test_returns_none_when_no_plan_review_pending(self):
        data = _FakeDashboardData(action_items=[], task_buckets={})
        assert select_chat_primer_for_project_dashboard(data) is None

    def test_returns_primer_when_action_item_flags_plan_review(self):
        data = _FakeDashboardData(
            action_items=[
                {
                    "is_plan_review": True,
                    "primary_ref": "demo/4",
                    "plan_version": 2,
                }
            ],
            plan_text="Plan body for demo.",
            plan_path="/repo/docs/project-plan.md",
        )
        primer = select_chat_primer_for_project_dashboard(data)
        assert primer is not None
        assert "demo/4" in primer
        assert "v2" in primer
        assert "> Plan body for demo." in primer
        assert "/repo/docs/project-plan.md" in primer

    def test_returns_primer_when_action_item_label_is_plan_review(self):
        data = _FakeDashboardData(
            action_items=[
                {
                    "labels": ["plan_review"],
                    "primary_ref": "demo/9",
                }
            ],
            plan_text="x",
        )
        primer = select_chat_primer_for_project_dashboard(data)
        assert primer is not None
        assert "demo/9" in primer

    def test_falls_back_to_review_bucket_when_no_action_item(self):
        """Even before an inbox row is emitted, a task parked at
        ``user_approval`` should make ``[c]`` route to refine mode so
        the UX doesn't depend on the inbox-emit timing."""
        data = _FakeDashboardData(
            action_items=[],
            task_buckets={
                "review": [
                    {
                        "task_id": "demo/7",
                        "current_node_id": "user_approval",
                    }
                ]
            },
            plan_text="hello",
        )
        primer = select_chat_primer_for_project_dashboard(data)
        assert primer is not None
        assert "demo/7" in primer

    def test_returns_none_for_dashboard_without_plan_task_id(self):
        data = _FakeDashboardData(
            action_items=[
                {
                    "is_plan_review": True,
                    # Missing every id field — degrade to None instead
                    # of a malformed primer.
                }
            ],
        )
        assert select_chat_primer_for_project_dashboard(data) is None


# ---------------------------------------------------------------------------
# Architect revision pass — apply_plan_refinement
# ---------------------------------------------------------------------------


class TestApplyPlanRefinement:
    def test_bumps_plan_version_via_sdk(self, svc):
        task = _create_plan_task(svc)
        result = apply_plan_refinement(
            svc,
            task_id=task.task_id,
            new_plan_body="Revised body",
        )
        assert isinstance(result, PlanRefinementResult)
        assert result.task_id == task.task_id
        assert result.old_version == 1
        assert result.new_version == 2
        # SDK side-effect: the task on disk has the new version.
        refetched = svc.get(task.task_id)
        assert refetched.plan_version == 2

    def test_writes_plan_body_to_canonical_path(self, svc, tmp_path):
        task = _create_plan_task(svc)
        project_path = tmp_path / "proj"
        project_path.mkdir()
        result = apply_plan_refinement(
            svc,
            task_id=task.task_id,
            new_plan_body="Brand new plan body",
            project_path=project_path,
        )
        # Default fallback path for fresh projects.
        expected_path = project_path / "docs" / "project-plan.md"
        assert result.plan_path == expected_path
        assert expected_path.read_text(encoding="utf-8").startswith(
            "Brand new plan body"
        )
        assert result.bytes_written > 0

    def test_writes_to_existing_plan_dir_path(self, svc, tmp_path):
        task = _create_plan_task(svc)
        project_path = tmp_path / "proj"
        plan_dir = project_path / "docs" / "plan"
        plan_dir.mkdir(parents=True)
        # Pre-create plan.md so the resolver picks ``docs/plan/plan.md``.
        (plan_dir / "plan.md").write_text("old", encoding="utf-8")
        result = apply_plan_refinement(
            svc,
            task_id=task.task_id,
            new_plan_body="updated",
            project_path=project_path,
        )
        assert result.plan_path == plan_dir / "plan.md"
        assert (plan_dir / "plan.md").read_text(encoding="utf-8").startswith(
            "updated"
        )

    def test_emits_plan_version_incremented_audit(
        self, svc, tmp_path, monkeypatch
    ):
        from pollypm.audit import read_events
        from pollypm.audit.log import EVENT_PLAN_VERSION_INCREMENTED

        audit_home = tmp_path / "audit-home"
        monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

        task = _create_plan_task(svc, project="refproj")
        apply_plan_refinement(
            svc,
            task_id=task.task_id,
            new_plan_body="refined body",
        )
        events = read_events(
            "refproj", event=EVENT_PLAN_VERSION_INCREMENTED
        )
        assert len(events) == 1
        assert events[0].metadata.get("reason") == "chat-to-refine"
        assert events[0].metadata["old_version"] == 1
        assert events[0].metadata["new_version"] == 2

    def test_skips_file_write_when_no_project_path(self, svc):
        task = _create_plan_task(svc)
        result = apply_plan_refinement(
            svc,
            task_id=task.task_id,
            new_plan_body="x",
        )
        assert result.plan_path is None
        assert result.bytes_written == 0
        # Version still bumps even without a project_path — the SDK is
        # the canonical revision marker.
        assert result.new_version == 2

    def test_appends_trailing_newline_when_missing(self, svc, tmp_path):
        task = _create_plan_task(svc)
        project_path = tmp_path / "proj"
        project_path.mkdir()
        apply_plan_refinement(
            svc,
            task_id=task.task_id,
            new_plan_body="no trailing newline",
            project_path=project_path,
        )
        body = (project_path / "docs" / "project-plan.md").read_text(
            encoding="utf-8"
        )
        assert body.endswith("\n")


# ---------------------------------------------------------------------------
# Integration — full chat-to-refine cycle with a mocked architect.
# ---------------------------------------------------------------------------


class TestChatToRefineFullCycle:
    """End-to-end: dashboard surfaces a plan_review → user opens chat
    with refinement primer → user types tweaks → user emits the
    refinement signal → architect (mocked) ships a rewritten plan body
    → ``apply_plan_refinement`` bumps the version and rewrites the
    file. The cockpit's inline-render path (#1397) reads ``plan_version``
    off the task directly, so we assert the post-bump task carries the
    new version.
    """

    def test_full_refinement_cycle(self, svc, tmp_path, monkeypatch):
        from pollypm.audit import read_events
        from pollypm.audit.log import EVENT_PLAN_VERSION_INCREMENTED

        audit_home = tmp_path / "audit-home"
        monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))

        # 1. A real plan task exists (post-synthesize, parked at
        # user_approval).
        task = _create_plan_task(svc, project="ref")
        project_path = tmp_path / "ref-proj"
        (project_path / "docs").mkdir(parents=True)
        (project_path / "docs" / "project-plan.md").write_text(
            "v1 plan\n", encoding="utf-8"
        )

        # 2. Dashboard would surface a plan_review action item; the
        # routing helper builds a refinement primer.
        data = _FakeDashboardData(
            project_key="ref",
            action_items=[
                {
                    "is_plan_review": True,
                    "primary_ref": task.task_id,
                    "plan_version": 1,
                }
            ],
            plan_text="v1 plan",
            plan_path=str(project_path / "docs" / "project-plan.md"),
        )
        primer = select_chat_primer_for_project_dashboard(data)
        assert primer is not None
        assert task.task_id in primer

        # 3. User chats refinements; eventually says "go architect".
        last_user_message = (
            "yeah we should split module B into B1 and B2 then go architect"
        )
        assert is_refinement_signal(last_user_message) is True

        # 4. (Mocked architect) — produces a rewritten plan body. In the
        # real flow this is the architect session running its revision
        # turn; the test stands in with a fixed string.
        revised_body = "v2 plan with B1/B2 split"

        # 5. Apply the refinement: writes file + bumps version + emits
        # the audit event.
        result = apply_plan_refinement(
            svc,
            task_id=task.task_id,
            new_plan_body=revised_body,
            project_path=project_path,
            actor="architect",
            reason="chat-to-refine",
        )

        # 6. Assertions: task version, file body, audit event.
        assert result.old_version == 1
        assert result.new_version == 2
        refetched = svc.get(task.task_id)
        assert refetched.plan_version == 2
        # Same task id — refinement is in-place; not a successor.
        assert refetched.task_id == task.task_id
        assert refetched.predecessor_task_id is None

        plan_file = project_path / "docs" / "project-plan.md"
        assert plan_file.read_text(encoding="utf-8").startswith(
            "v2 plan with B1/B2 split"
        )

        events = read_events(
            "ref", event=EVENT_PLAN_VERSION_INCREMENTED
        )
        assert len(events) == 1
        assert events[0].metadata["task_id"] == task.task_id
        assert events[0].metadata["new_version"] == 2
        assert events[0].metadata.get("reason") == "chat-to-refine"
