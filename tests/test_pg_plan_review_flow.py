"""Pg re-coverage for backend-neutral plan_review helpers (#1824).

Slice K-tests part 6 (#1737, commit 5a1b58845) deleted
``tests/test_plan_review_flow.py`` (1928 LOC) because the Pilot-driven
inbox-app cases depended on a project-root sqlite state.db. This
module re-locks the pure-helper half: sidecar-label extraction,
round-trip detection, PM primers (approval + denial), and the plan
markdown summary / judgment-calls extractors that feed the cockpit's
Action Needed card.

What's intentionally **not** ported
-----------------------------------

* Pilot-driven ``PollyInboxApp`` / ``PollyProjectDashboardApp`` UI
  tests — those instantiate the cockpit against a project-root
  ``state.db`` and exercise the v/d/A/D keybindings end-to-end. They
  belong with the cockpit-UI pg port-back (#1794 follow-up), not
  with the pure helper layer.
* Persona lookup tests (``PollyInboxApp._project_persona_name``) —
  these read ``pollypm.toml`` and require the inbox-app constructor.
  Out of scope for the helper-only port.
* Deferred-approve / undo-window tests — instantiate the inbox app
  with a sqlite svc.approve patched and exercise the 10s timer.
"""

from __future__ import annotations

from dataclasses import dataclass

from pollypm.cockpit_ui import (
    PollyProjectDashboardApp,
    _build_plan_review_denial_primer,
    _build_plan_review_primer,
    _extract_plan_judgment_calls,
    _extract_plan_review_meta,
    _extract_plan_summary_block,
    _plan_review_has_round_trip,
)


# ---------------------------------------------------------------------------
# Sidecar label parsing.
# ---------------------------------------------------------------------------


class TestPlanReviewMeta:
    def test_extract_meta_parses_sidecar_labels(self) -> None:
        labels = [
            "plan_review",
            "project:demo",
            "plan_task:demo/7",
            "explainer:/abs/path/reports/plan-review.html",
        ]
        meta = _extract_plan_review_meta(labels)
        assert meta["plan_task_id"] == "demo/7"
        assert meta["explainer_path"] == "/abs/path/reports/plan-review.html"
        assert meta["project"] == "demo"
        assert meta["fast_track"] is False

    def test_extract_meta_fast_track_flag(self) -> None:
        meta = _extract_plan_review_meta([
            "plan_review", "project:demo", "plan_task:demo/1",
            "explainer:/x.html", "fast_track",
        ])
        assert meta["fast_track"] is True

    def test_extract_meta_handles_empty_or_none(self) -> None:
        """Empty / ``None`` label lists yield ``{"fast_track": False}``.
        Defensive against the architect emitting a card without sidecar
        labels (the early-#1408 shape)."""
        assert _extract_plan_review_meta(None) == {"fast_track": False}
        assert _extract_plan_review_meta([]) == {"fast_track": False}

    def test_extract_meta_skips_non_string_entries(self) -> None:
        """Bad data on the label list (e.g. a leftover dict) is ignored —
        the helper never blows up on a malformed row."""
        meta = _extract_plan_review_meta([
            "plan_review",
            {"junk": 1},  # type: ignore[list-item]
            "project:demo",
            42,           # type: ignore[list-item]
            "plan_task:demo/3",
        ])
        assert meta["project"] == "demo"
        assert meta["plan_task_id"] == "demo/3"


# ---------------------------------------------------------------------------
# Round-trip detection — pure.
# ---------------------------------------------------------------------------


@dataclass
class _FakeEntry:
    actor: str


class TestPlanReviewRoundTrip:
    def test_only_reviewer_does_not_unlock(self) -> None:
        assert not _plan_review_has_round_trip(
            [_FakeEntry("user")], requester="user",
        )

    def test_only_pm_side_does_not_unlock(self) -> None:
        assert not _plan_review_has_round_trip(
            [_FakeEntry("architect")], requester="user",
        )

    def test_both_voices_present_unlocks(self) -> None:
        assert _plan_review_has_round_trip(
            [_FakeEntry("user"), _FakeEntry("architect")],
            requester="user",
        )

    def test_fast_track_uses_polly_as_requester(self) -> None:
        """Fast-track items use requester=polly; round-trip needs a
        non-polly actor on the other side."""
        assert not _plan_review_has_round_trip(
            [_FakeEntry("polly"), _FakeEntry("polly")],
            requester="polly",
        )
        assert _plan_review_has_round_trip(
            [_FakeEntry("polly"), _FakeEntry("architect")],
            requester="polly",
        )

    def test_empty_replies_does_not_unlock(self) -> None:
        assert not _plan_review_has_round_trip([], requester="user")
        assert not _plan_review_has_round_trip(None, requester="user")

    def test_blank_actor_strings_ignored(self) -> None:
        """A reply with an empty actor string must not count as either
        side of the round-trip."""
        entries = [_FakeEntry(""), _FakeEntry("   "), _FakeEntry("user")]
        assert not _plan_review_has_round_trip(entries, requester="user")

    def test_requester_default_is_user(self) -> None:
        """The ``requester`` kwarg defaults to ``user``."""
        assert _plan_review_has_round_trip(
            [_FakeEntry("user"), _FakeEntry("polly")],
        )


# ---------------------------------------------------------------------------
# Approval primer — pure.
# ---------------------------------------------------------------------------


class TestPlanReviewApprovalPrimer:
    def test_primer_contains_coached_conversation_brief(self) -> None:
        primer = _build_plan_review_primer(
            project_key="demo",
            plan_path="/abs/docs/plan/plan.md",
            explainer_path="/abs/reports/plan-review.html",
            plan_task_id="demo/7",
            reviewer_name="Sam",
        )
        assert not primer.startswith("re: inbox/")
        assert "plan review for project: demo" in primer
        assert "/abs/docs/plan/plan.md" in primer
        assert "/abs/reports/plan-review.html" in primer
        assert "Co-refine the plan with Sam" in primer
        assert "smallest reasonable tasks" in primer
        assert "record approval for plan task demo/7 as user" in primer
        assert "pm task approve" not in primer

    def test_primer_swaps_to_polly_when_fast_tracked(self) -> None:
        primer = _build_plan_review_primer(
            project_key="demo",
            plan_path="/abs/docs/plan/plan.md",
            explainer_path="/abs/reports/plan-review.html",
            plan_task_id="demo/7",
            reviewer_name="Polly",
        )
        assert "plan review for project: demo" in primer
        assert "Co-refine the plan with Polly" in primer
        assert "record approval for plan task demo/7 as polly" in primer
        assert "pm task approve" not in primer

    def test_primer_defaults_reviewer_name_to_sam(self) -> None:
        """An empty / whitespace reviewer name falls back to ``Sam``."""
        primer = _build_plan_review_primer(
            project_key="demo",
            plan_path="/p.md",
            explainer_path="/e.html",
            plan_task_id="demo/1",
            reviewer_name="",
        )
        assert "Co-refine the plan with Sam" in primer
        assert "record approval for plan task demo/1 as user" in primer


# ---------------------------------------------------------------------------
# Denial primer — pure (#1403).
# ---------------------------------------------------------------------------


class TestPlanReviewDenialPrimer:
    def test_primer_includes_denial_reason_and_successor(self) -> None:
        primer = _build_plan_review_denial_primer(
            project_key="demo",
            cancelled_plan_task_id="demo/3",
            successor_plan_task_id="demo/4",
            denial_reason="Backlog is too coarse — break it down further.",
            reviewer_name="Sam",
        )
        assert "denied plan task demo/3" in primer
        assert "Successor plan task: demo/4" in primer
        assert "Backlog is too coarse" in primer
        assert "Sit with Sam on the concerns" in primer
        assert "plan_review_denied" in primer

    def test_primer_swaps_to_polly_when_fast_track(self) -> None:
        primer = _build_plan_review_denial_primer(
            project_key="demo",
            cancelled_plan_task_id="demo/3",
            successor_plan_task_id="demo/4",
            denial_reason="not enough decomposition",
            reviewer_name="Polly",
        )
        assert "Polly just denied plan task demo/3" in primer
        assert "Sit with Polly on the concerns" in primer

    def test_primer_omits_trailing_newline(self) -> None:
        """Output ends WITHOUT a trailing newline so the PM CLI can
        append the user's typed follow-up directly."""
        primer = _build_plan_review_denial_primer(
            project_key="demo",
            cancelled_plan_task_id="demo/3",
            successor_plan_task_id="demo/4",
            denial_reason="too lumpy",
        )
        assert primer == primer.rstrip("\n")


# ---------------------------------------------------------------------------
# Plan markdown summary + judgment-calls extractors (#1397).
# ---------------------------------------------------------------------------


class TestPlanInlineRendering:
    def test_summary_block_extracts_paragraph_under_summary_header(self) -> None:
        text = (
            "# Project plan\n\n"
            "## Summary\n"
            "Ship the rendering pipeline in three steps; each step lands\n"
            "as its own task so we can pause between phases.\n\n"
            "## Judgment calls\n- foo\n"
        )
        summary = _extract_plan_summary_block(text)
        assert summary.startswith("Ship the rendering pipeline")
        assert "three steps" in summary
        # Stops at the next header.
        assert "foo" not in summary

    def test_summary_block_falls_back_to_first_paragraph(self) -> None:
        text = (
            "# Old-style plan\n\n"
            "This is the leading paragraph that doubles as a summary.\n\n"
            "## Some other header\nThe rest.\n"
        )
        summary = _extract_plan_summary_block(text)
        assert summary.startswith("This is the leading paragraph")

    def test_summary_block_empty_input_returns_empty(self) -> None:
        assert _extract_plan_summary_block("") == ""
        assert _extract_plan_summary_block("   \n  \n") == ""

    def test_summary_block_case_insensitive_header_match(self) -> None:
        """``## SUMMARY`` / ``## summary`` both resolve."""
        text = "# t\n\n## SUMMARY\nLine one.\n"
        assert _extract_plan_summary_block(text) == "Line one."

    def test_judgment_calls_extracts_bullet_list(self) -> None:
        text = (
            "## Summary\nA paragraph.\n\n"
            "## Judgment calls\n"
            "- Whether to ship the rename in a single PR or split it\n"
            "- The cache eviction strategy\n"
            "- Stamping plan_version on every transition or only on user_approval\n\n"
            "## Plan body\nActual plan...\n"
        )
        points = _extract_plan_judgment_calls(text)
        assert len(points) == 3
        assert points[0].startswith("Whether to ship the rename")
        assert "cache eviction" in points[1]

    def test_judgment_calls_returns_empty_when_section_missing(self) -> None:
        assert _extract_plan_judgment_calls(
            "# A plan\n\nNo judgment section.",
        ) == []

    def test_judgment_calls_respects_limit_arg(self) -> None:
        """``limit`` caps the returned bullet count so the cockpit's
        Action Needed card doesn't grow unbounded."""
        text = (
            "## Judgment calls\n"
            "- one\n- two\n- three\n- four\n- five\n- six\n"
        )
        capped = _extract_plan_judgment_calls(text, limit=3)
        assert len(capped) == 3
        assert capped == ["one", "two", "three"]

    def test_judgment_calls_accepts_judgement_spelling(self) -> None:
        """The architect occasionally emits ``Judgement`` (British spelling).
        Both should resolve."""
        text = "## Judgement calls\n- a flagged point\n"
        assert _extract_plan_judgment_calls(text) == ["a flagged point"]


# ---------------------------------------------------------------------------
# Action Needed card body — pure render helper on ``PollyProjectDashboardApp``.
# ---------------------------------------------------------------------------


class TestActionCardBody:
    def test_renders_judgment_calls_for_plan_review(self) -> None:
        """The Action Needed card surfaces the architect's flagged
        judgment-call points right under the summary so the user knows
        what to weigh in on (#1397)."""
        # The dashboard app's instance method is pure (no self state
        # consumed) — we can call it via ``__new__`` without
        # constructing the textual App.
        app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
        item = {
            "is_plan_review": True,
            "plain_prompt": "A short plan summary appears here.",
            "judgment_calls": [
                "Whether to ship in one PR or split it.",
                "The cache eviction strategy.",
            ],
            "unblock_steps": ["Open the plan review surface."],
            "decision_question": "Is this ready to become tasks?",
        }
        body = app._render_action_card_body(item, compact=False)
        assert "Flagged judgment calls" in body
        assert "Whether to ship in one PR" in body
        assert "cache eviction" in body
        assert "A short plan summary appears here" in body

    def test_caps_long_summary_at_80_chars(self) -> None:
        """#1397 — in the inbox-row preview the plan summary can be a
        full paragraph; cap it at ~80 chars so the rail row stays
        readable."""
        app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
        long_summary = "x" * 200
        item = {
            "is_plan_review": True,
            "plain_prompt": long_summary,
            "judgment_calls": [],
            "unblock_steps": [],
            "decision_question": "Ready?",
        }
        body = app._render_action_card_body(item, compact=False)
        assert "x" * 100 not in body
        assert "..." in body

    def test_omits_judgment_calls_when_not_plan_review(self) -> None:
        app = PollyProjectDashboardApp.__new__(PollyProjectDashboardApp)
        item = {
            "plain_prompt": "Generic message.",
            "unblock_steps": ["Step 1.", "Step 2."],
            "decision_question": "Choose.",
        }
        body = app._render_action_card_body(item, compact=False)
        assert "Flagged judgment calls" not in body
