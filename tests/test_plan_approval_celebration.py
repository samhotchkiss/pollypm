"""Tests for the plan-approval celebration helpers (#1402).

Pure functions live in :mod:`pollypm.plan_approval_celebration`. The
cockpit integration tests cover the keybinding + 10s undo flow at the
TUI layer; these tests pin the message-composition contracts.
"""

from __future__ import annotations

import pytest

from pollypm.plan_approval_celebration import (
    APPROVE_UNDO_WINDOW_SECONDS,
    SPARKLE_DURATION_SECONDS,
    compose_celebration_message,
    sparkle_frames,
    undo_hint,
)


class TestUndoWindow:
    def test_undo_window_is_ten_seconds(self) -> None:
        # The issue specifies a 10-second undo window. The constant is
        # the single source of truth so cockpit + tests agree.
        assert APPROVE_UNDO_WINDOW_SECONDS == 10.0

    def test_sparkle_duration_is_about_one_second(self) -> None:
        # Animation runs ~1s on toast appearance.
        assert 0.5 <= SPARKLE_DURATION_SECONDS <= 2.0


class TestComposeCelebrationMessage:
    def test_five_task_plan_uses_out_of_the_oven(self) -> None:
        msg = compose_celebration_message(persona_name="Sage", sub_task_count=5)
        assert msg == "Sage: Out of the oven! 5 tasks plated."

    def test_one_task_plan_uses_quick_bite(self) -> None:
        msg = compose_celebration_message(persona_name="Archie", sub_task_count=1)
        assert msg == "Archie: Quick bite — 1 task plated."

    def test_twelve_task_plan_uses_big_batch(self) -> None:
        msg = compose_celebration_message(persona_name="Sage", sub_task_count=12)
        assert msg == "Sage: Big batch — 12 tasks plated."

    def test_large_plan_still_uses_big_batch(self) -> None:
        msg = compose_celebration_message(persona_name="Sage", sub_task_count=42)
        assert msg.startswith("Sage: Big batch — 42 tasks plated")

    def test_two_tasks_uses_out_of_the_oven_plural(self) -> None:
        msg = compose_celebration_message(persona_name="Sage", sub_task_count=2)
        assert msg == "Sage: Out of the oven! 2 tasks plated."

    def test_zero_tasks_falls_into_quick_bite_singular(self) -> None:
        # Defensive — zero shouldn't normally happen but we don't want
        # the toast to crash if a plan ships with no decomposition yet.
        msg = compose_celebration_message(persona_name="Sage", sub_task_count=0)
        assert msg == "Sage: Quick bite — 0 task plated."

    def test_missing_persona_falls_back_to_polly(self) -> None:
        msg = compose_celebration_message(persona_name=None, sub_task_count=3)
        assert msg.startswith("Polly: ")
        msg2 = compose_celebration_message(persona_name="   ", sub_task_count=3)
        assert msg2.startswith("Polly: ")

    @pytest.mark.parametrize("count", [-1, -10])
    def test_negative_count_clamps_to_zero(self, count: int) -> None:
        # Defensive: ``svc.children`` should never be negative, but
        # composition shouldn't blow up on bad input.
        msg = compose_celebration_message(persona_name="Sage", sub_task_count=count)
        assert "0 task plated" in msg

    def test_no_emoji_in_output(self) -> None:
        # Hard constraint from the issue: pure text, no emoji.
        # Pictographic emoji (U+1F300+) are forbidden; the ASCII +
        # em-dash text used in the celebration line is explicitly text.
        for count in (1, 5, 12, 50):
            msg = compose_celebration_message(persona_name="Sage", sub_task_count=count)
            for ch in msg:
                cp = ord(ch)
                assert not (0x1F300 <= cp <= 0x1FAFF), (
                    f"emoji {ch!r} (U+{cp:04X}) leaked into {msg!r}"
                )

    def test_persona_name_from_project_lookup(self) -> None:
        # The project's PM persona name flows in directly. We don't
        # encode any fallback per project key here — the cockpit caller
        # is responsible for resolving via ``_project_pm_persona``.
        msg = compose_celebration_message(persona_name="Sage", sub_task_count=4)
        assert "Sage:" in msg
        msg = compose_celebration_message(persona_name="Wren", sub_task_count=4)
        assert "Wren:" in msg


class TestSparkleFrames:
    def test_sparkle_frames_are_pure_text(self) -> None:
        frames = sparkle_frames()
        assert frames, "at least one sparkle frame must be available"
        # Geometric stars U+2726 / U+2727 are intentional — they are
        # text-presentation glyphs, not emoji. We just rule out the
        # supplementary pictographic emoji block.
        for frame in frames:
            assert isinstance(frame, str)
            for ch in frame:
                cp = ord(ch)
                assert not (0x1F300 <= cp <= 0x1FAFF), (
                    f"emoji codepoint U+{cp:04X} in sparkle frame"
                )

    def test_sparkle_frames_use_geometric_stars_only(self) -> None:
        # We use U+2726 (✦) and U+2727 (✧) plus whitespace.
        allowed = {ord("✦"), ord("✧"), ord(" ")}
        for frame in sparkle_frames():
            for ch in frame:
                assert ord(ch) in allowed, (
                    f"unexpected glyph {ch!r} (U+{ord(ch):04X}) in sparkle frame"
                )


class TestUndoHint:
    def test_undo_hint_without_seconds(self) -> None:
        assert undo_hint() == "[u] undo"

    def test_undo_hint_with_seconds(self) -> None:
        assert undo_hint(10.0) == "[u] undo (10s)"
        assert undo_hint(3.4) == "[u] undo (3s)"
        assert undo_hint(0.0) == "[u] undo (0s)"
