"""Tests for the regular dashboard's Plan section (#1620).

The Plan section renders inline on the regular project dashboard when
a plan exists on disk (or in an architect worktree via the
:func:`pollypm.cockpit_ui._dashboard_plan_path_from_worktrees`
fallback). It is suppressed entirely when no plan is present so the
dashboard stays clean for fresh projects.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.cockpit_sections.plan import (
    _preview_plan,
    _render_plan_ready_banner,
    _section_plan,
)


SAMPLE_PLAN = """# SamBlog implementation plan

## Summary
Rebuild sam.blog into a polished personal showcase. V1 includes
photography, long-form writing, projects, and a token counter.
SamBot is explicitly excluded from V1.

## Judgment calls
- MCP access is the first real blocker.
- Substack importer route is not guaranteed on Pressable.
- Notification safety is a hard gate.

## Plan
Phase 0 — Verify Pressable and MCP readiness.
Phase 1 — Rehearse Substack migration safely.
Phase 2 — Define content model and IA.
"""


class TestPlanReadyBanner:
    def test_banner_advertises_p_for_full_plan(self):
        out = _render_plan_ready_banner()
        assert "Plan ready" in out
        assert "p" in out  # the keybinding hint
        assert "A" in out  # approve hint

    def test_banner_uses_plan_ready_glyph(self):
        out = _render_plan_ready_banner()
        # ◇ — matches the operator-dashboard vocabulary (#1572).
        assert "◇" in out


class TestPreviewPlan:
    def test_returns_empty_for_blank_text(self):
        assert _preview_plan("") == ""
        assert _preview_plan("   \n\n") == ""

    def test_renders_first_n_lines(self):
        out = _preview_plan(SAMPLE_PLAN, line_limit=4)
        assert "# SamBlog implementation plan" in out
        # Only the first 4 lines should appear before the tail hint.
        assert "Phase 0" not in out
        # And the truncation marker should be present.
        assert "more lines" in out

    def test_no_truncation_marker_when_under_limit(self):
        short = "# Title\n\nA short plan."
        out = _preview_plan(short, line_limit=10)
        assert "more lines" not in out


class TestSectionPlan:
    def test_empty_when_no_plan_present(self, tmp_path: Path):
        proj = tmp_path / "fresh"
        proj.mkdir()
        assert _section_plan(proj) == []

    def test_renders_section_when_plan_on_disk(self, tmp_path: Path):
        proj = tmp_path / "ready"
        (proj / "docs" / "plan").mkdir(parents=True)
        (proj / "docs" / "plan" / "plan.md").write_text(
            SAMPLE_PLAN, encoding="utf-8",
        )
        out = "\n".join(_section_plan(proj))
        assert "Plan" in out  # divider
        assert "Plan ready" in out  # banner
        assert "SamBlog implementation plan" in out  # body preview

    def test_renders_section_when_plan_in_architect_worktree(
        self, tmp_path: Path,
    ):
        """Sam's flow: the architect commits the plan into its
        worktree at synthesize time, BEFORE the merge back to the
        project root lands. The dashboard should still surface the
        plan so Sam can read it as soon as the architect is done."""
        proj = tmp_path / "samblog"
        proj.mkdir()
        wt = (
            proj
            / ".pollypm"
            / "worktrees"
            / "architect_samblog"
            / "samblog-architect-architect_samblog"
            / "docs"
            / "plan"
        )
        wt.mkdir(parents=True)
        (wt / "plan.md").write_text(SAMPLE_PLAN, encoding="utf-8")
        out = "\n".join(_section_plan(proj))
        assert "Plan ready" in out
        assert "SamBlog implementation plan" in out
