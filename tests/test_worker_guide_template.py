"""Worker-guide content invariants.

The worker-guide.md is read by every PollyPM worker on kickoff. We pin
specific wording so prompt-tuning regressions can't silently remove
guidance the system depends on. Currently pinned:

- "Execute, don't pause to ask permission" section — counters the
  worker default of drafting an outline then stalling for a "nod."
  Without this, tasks like coffeeboardnm/1 silently sit idle for
  20+ minutes on the first plan-style step.
"""

from __future__ import annotations

from pathlib import Path

WORKER_GUIDE = (
    Path(__file__).parent.parent
    / "src"
    / "pollypm"
    / "defaults"
    / "docs"
    / "worker-guide.md"
)


def test_worker_guide_has_execute_dont_ask_section() -> None:
    text = WORKER_GUIDE.read_text(encoding="utf-8")
    assert "Execute. Don't pause to ask permission" in text
    # The guidance must reference both the spec-as-greenlight idea and
    # the explicit "do not check in for a nod" pattern — that exact
    # phrasing is what coffeeboardnm/1 stalled on.
    assert "do **not** check\nin for a \"nod.\"" in text or "do not check in for a \"nod" in text.lower()


def test_worker_guide_keeps_blocked_path_explicit() -> None:
    """The execute-don't-ask guidance must still leave a real escape
    hatch for hard blockers — otherwise we trade silent stalls for
    silent wrong work."""
    text = WORKER_GUIDE.read_text(encoding="utf-8")
    assert "hard blocker" in text.lower() or "hard blockers" in text.lower()
    assert "context note" in text.lower()
