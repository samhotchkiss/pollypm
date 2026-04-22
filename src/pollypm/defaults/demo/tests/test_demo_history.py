from pathlib import Path


def test_demo_history_describes_the_seeded_git_log() -> None:
    text = Path("demo_history.md").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "replayable" in lowered
    assert "three commits" in lowered or "3 commits" in lowered
